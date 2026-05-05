"""Per-IP sliding-window rate limiter for the public match endpoint.

In-memory only — adequate for the single-process uvicorn instance we run.
Horizontally-scaled deployment would need Redis or similar. The limiter
protects against accidental misuse + slow-drip billing abuse on the
unauthenticated public page; it is not a security control.

Defaults (request count + window) chosen to allow a handful of legitimate
uses per IP per hour — enough for 陈总's team to demo without pestering,
not enough for a scraper to rack up an LLM bill unnoticed.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request

# Allow enough headroom for a real user to iterate several times —
# uploading a new xlsx, tweaking the sender's instructions, reviewing
# drafts, and retrying a few times all fit in a single session. Drive-by
# scrapers still bounce after the cap. Can be tightened again if abuse
# shows up in the usage email stream.
DEFAULT_MAX_REQUESTS = 30
DEFAULT_WINDOW_SEC = 60 * 60  # 1 hour


class IPRateLimiter:
    """Sliding-window counter per client IP.

    Each request records its timestamp in a deque keyed by IP. Expired
    entries are pruned on the next check for that IP (lazy cleanup — good
    enough for a small user base; switch to a background sweep if the IP
    set grows unbounded).
    """

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        window_sec: int = DEFAULT_WINDOW_SEC,
    ) -> None:
        self.max_requests = max_requests
        self.window_sec = window_sec
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, ip: str) -> None:
        """Record an attempt by `ip`; raise HTTPException(429) if over cap.

        The method both checks AND records — callers should invoke it once
        per accepted request. Holding the lock across prune + append is cheap
        (deques, short critical section) and keeps the window calc atomic.
        """
        now = time.monotonic()
        cutoff = now - self.window_sec
        async with self._lock:
            q = self._hits[ip]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                oldest = q[0]
                retry_after = int(self.window_sec - (now - oldest)) + 1
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Rate limit exceeded: max {self.max_requests} requests "
                        f"per {self.window_sec // 60} minutes. Retry in ~{retry_after}s."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )
            q.append(now)


_limiter = IPRateLimiter()


async def rate_limit(request: Request) -> None:
    """FastAPI dependency — throws 429 when the caller's IP is over cap."""
    # `request.client.host` is None when behind some proxies without
    # forwarding; fall back to X-Forwarded-For if set, else "unknown" so all
    # such callers share a bucket (safe under-limit for us).
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    await _limiter.check(ip)
