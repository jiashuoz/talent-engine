"""Per-API-key token-bucket rate limiter.

Process-local — keyed by `api_key_id`, holds nothing across restarts. This
is acceptable for the current single-replica 微信云托管 deployment; if we
ever scale horizontally we'll need Redis (or whatever 云托管 offers) as
the shared bucket store.

Default policy: 60 tokens / minute / key, refilled continuously, burst up
to the full bucket capacity. Each public-API request consumes 1 token at
the handler entry, before any LLM work — so a runaway client hits the
limit before draining the model quota.

Override via env:
    RATE_LIMIT_RPM        — tokens added per 60s (default 60)
    RATE_LIMIT_BURST      — max bucket capacity (default = RATE_LIMIT_RPM)
    RATE_LIMIT_DISABLE=1  — bypass entirely (tests, local debug)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict

from fastapi import Depends, HTTPException

from v1.resume_matching.auth import require_api_key
from v1.resume_matching.storage import ApiKeyRecord

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("ignoring non-integer %s=%r; falling back to %d", name, raw, default)
        return default


def _rpm() -> int:
    return _env_int("RATE_LIMIT_RPM", 60)


def _burst() -> int:
    return _env_int("RATE_LIMIT_BURST", _rpm())


def _disabled() -> bool:
    return os.getenv("RATE_LIMIT_DISABLE", "").lower() in {"1", "true", "yes"}


@dataclass
class _Bucket:
    """Continuous-refill token bucket. Tokens regenerate at `rpm/60` per second."""
    tokens: float
    capacity: int
    refill_per_sec: float
    last_refill: float = field(default_factory=time.monotonic)

    def take(self, cost: int = 1) -> tuple[bool, float]:
        """Try to consume `cost` tokens.

        Returns (allowed, retry_after_seconds). When allowed is True, retry_after is 0.
        When False, retry_after is the wall-clock wait until the bucket holds `cost`.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        missing = cost - self.tokens
        retry_after = missing / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
        return False, retry_after


class _Registry:
    """Process-local map of api_key_id → bucket. Lazy-creates on first use."""

    def __init__(self) -> None:
        self._buckets: Dict[int, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def consume(self, api_key_id: int, cost: int = 1) -> tuple[bool, float]:
        async with self._lock:
            bucket = self._buckets.get(api_key_id)
            if bucket is None:
                cap = _burst()
                bucket = _Bucket(
                    tokens=float(cap),
                    capacity=cap,
                    refill_per_sec=_rpm() / 60.0,
                )
                self._buckets[api_key_id] = bucket
            return bucket.take(cost)


_registry = _Registry()


async def enforce_rate_limit(
    api_key: ApiKeyRecord = Depends(require_api_key),
) -> ApiKeyRecord:
    """FastAPI dependency: consume 1 token from this key's bucket or raise 429.

    Wraps `require_api_key` so handlers can replace `Depends(require_api_key)`
    with `Depends(enforce_rate_limit)` and get auth + rate limiting in one
    line. The returned record is the same shape, so call-sites that read
    `api_key.id` need no other changes.
    """
    if _disabled():
        return api_key
    allowed, retry_after = await _registry.consume(api_key.id, cost=1)
    if allowed:
        return api_key
    raise HTTPException(
        status_code=429,
        detail=(
            f"Rate limit exceeded ({_rpm()} req/min). "
            f"Retry after ~{int(retry_after) + 1}s."
        ),
        headers={"Retry-After": str(int(retry_after) + 1)},
    )
