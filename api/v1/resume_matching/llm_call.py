"""Wrap any BAML call with a timeout + one retry on transient failure.

BAML clients (Qwen via DashScope, DeepSeek, Hunyuan, Gemini) all see
occasional 5xx / connection-reset / DNS-flake failures in mainland CN
production. Without a wrapper, those become permanent per-pair errors
even when a second attempt would have succeeded.

Policy:
  - Hard timeout: configurable, default 30s. Long enough for DeepSeek's
    p95 (~10s), short enough that a hung connection doesn't pin a slot.
  - One retry on `asyncio.TimeoutError`, `ConnectionError`, or anything
    that looks like a transient HTTP 5xx (best-effort substring check).
  - Validation errors (Pydantic, BAML schema) are not retried — a
    second call will produce the same output shape.

Environment overrides:
    LLM_CALL_TIMEOUT_SEC   — per-attempt timeout (default 30)
    LLM_CALL_MAX_RETRIES   — extra attempts after the first (default 1)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _timeout_sec() -> float:
    try:
        return max(1.0, float(os.getenv("LLM_CALL_TIMEOUT_SEC", "30")))
    except ValueError:
        return 30.0


def _max_retries() -> int:
    try:
        return max(0, int(os.getenv("LLM_CALL_MAX_RETRIES", "1")))
    except ValueError:
        return 1


def _looks_transient(exc: BaseException) -> bool:
    """Best-effort: should this exception be retried?

    BAML doesn't expose a stable typed-error hierarchy across providers, so
    we match a small set of known-transient classes plus a string check for
    HTTP 5xx markers in the error message.
    """
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    msg = str(exc).lower()
    # Common transient signals from OpenAI-compatible providers.
    return any(token in msg for token in (
        "503", "502", "504", "timeout", "timed out", "connection reset",
        "temporarily unavailable", "rate limit",
    ))


async def with_timeout_retry(
    make_coro: Callable[[], Awaitable[T]],
    *,
    label: str = "llm-call",
    timeout: float | None = None,
    max_retries: int | None = None,
) -> T:
    """Run `make_coro()` with a timeout and one retry on transient failure.

    `make_coro` is a *callable returning a coroutine*, not a coroutine —
    we need to be able to call it again on retry. Most call sites just
    pass `lambda: b.ScoreMatch(...)`.
    """
    t = timeout if timeout is not None else _timeout_sec()
    retries = max_retries if max_retries is not None else _max_retries()

    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(make_coro(), timeout=t)
        except BaseException as e:  # noqa: BLE001 — see _looks_transient
            last_exc = e
            if attempt < retries and _looks_transient(e):
                logger.warning(
                    "%s attempt %d/%d failed (%s); retrying",
                    label, attempt + 1, retries + 1, type(e).__name__,
                )
                continue
            raise
    # Unreachable — the loop always either returns or re-raises.
    assert last_exc is not None
    raise last_exc
