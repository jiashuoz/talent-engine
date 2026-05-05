"""Tests for the per-IP sliding-window rate limiter.

The limiter is the billing-abuse guard on the unauthenticated public
endpoint. These tests pin behaviour we care about:

  - first N requests from an IP succeed
  - the (N+1)th raises HTTPException(429)
  - the 429 carries a Retry-After header computed from the window
  - separate IPs have separate buckets
  - expired entries fall out of the window (sliding, not fixed)
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

from v1.resume_matching.rate_limit import IPRateLimiter


@pytest.mark.asyncio
async def test_allows_requests_up_to_cap() -> None:
    rl = IPRateLimiter(max_requests=3, window_sec=60)
    for _ in range(3):
        await rl.check("1.2.3.4")  # must not raise


@pytest.mark.asyncio
async def test_blocks_past_cap_with_429_and_retry_after() -> None:
    rl = IPRateLimiter(max_requests=2, window_sec=60)
    await rl.check("1.2.3.4")
    await rl.check("1.2.3.4")

    with pytest.raises(HTTPException) as exc:
        await rl.check("1.2.3.4")

    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers
    retry_after = int(exc.value.headers["Retry-After"])
    # The oldest hit was just recorded, so retry_after should be ~window.
    assert 55 <= retry_after <= 61


@pytest.mark.asyncio
async def test_separate_ips_have_separate_buckets() -> None:
    rl = IPRateLimiter(max_requests=1, window_sec=60)
    await rl.check("1.1.1.1")
    await rl.check("2.2.2.2")  # different IP — independent bucket, OK

    with pytest.raises(HTTPException):
        await rl.check("1.1.1.1")  # same IP — now at cap
    with pytest.raises(HTTPException):
        await rl.check("2.2.2.2")


@pytest.mark.asyncio
async def test_expired_entries_drop_out_of_window(monkeypatch) -> None:
    rl = IPRateLimiter(max_requests=2, window_sec=60)

    fake_now = [1000.0]

    def clock() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", clock)

    await rl.check("1.2.3.4")
    await rl.check("1.2.3.4")
    # Cap hit at t=1000.
    with pytest.raises(HTTPException):
        await rl.check("1.2.3.4")

    # Advance past the window — the two earlier hits should expire.
    fake_now[0] = 1000 + 61
    await rl.check("1.2.3.4")  # must not raise — bucket is empty again
    await rl.check("1.2.3.4")


@pytest.mark.asyncio
async def test_concurrent_requests_on_same_ip_are_serialized() -> None:
    # Under asyncio.gather the lock in check() must ensure the cap is
    # enforced exactly — not off-by-one due to race.
    rl = IPRateLimiter(max_requests=3, window_sec=60)
    results = await asyncio.gather(
        *[rl.check("1.2.3.4") for _ in range(5)],
        return_exceptions=True,
    )
    ok = [r for r in results if r is None]
    err = [r for r in results if isinstance(r, HTTPException)]
    assert len(ok) == 3
    assert len(err) == 2
    assert all(e.status_code == 429 for e in err)
