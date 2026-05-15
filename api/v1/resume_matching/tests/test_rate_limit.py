"""Exercises the token-bucket dependency end-to-end.

Tests are explicit about overriding the test-wide RATE_LIMIT_DISABLE=1
that conftest.py sets — the rest of the suite stays unthrottled.
"""

from __future__ import annotations

import pytest

from v1.resume_matching import rate_limit as rl
from v1.resume_matching.storage import ApiKeyRecord


def _record(key_id: int) -> ApiKeyRecord:
    from datetime import datetime, timezone
    return ApiKeyRecord(
        id=key_id, name=f"key-{key_id}", key_prefix="mnk_test",
        created_at=datetime.now(timezone.utc), revoked_at=None,
    )


@pytest.fixture(autouse=True)
def _enable_rate_limit(monkeypatch):
    """Flip the kill-switch off for this module only, plus fresh registry."""
    monkeypatch.delenv("RATE_LIMIT_DISABLE", raising=False)
    monkeypatch.setenv("RATE_LIMIT_RPM", "5")     # 5/min, capacity 5 → fast saturation
    monkeypatch.setenv("RATE_LIMIT_BURST", "5")
    # Replace the module-level registry so each test starts with empty buckets.
    monkeypatch.setattr(rl, "_registry", rl._Registry())


@pytest.mark.asyncio
async def test_first_n_requests_allowed_up_to_burst() -> None:
    record = _record(1)
    for _ in range(5):
        out = await rl.enforce_rate_limit(api_key=record)
        assert out is record


@pytest.mark.asyncio
async def test_burst_plus_one_raises_429() -> None:
    from fastapi import HTTPException

    record = _record(2)
    for _ in range(5):
        await rl.enforce_rate_limit(api_key=record)
    with pytest.raises(HTTPException) as exc:
        await rl.enforce_rate_limit(api_key=record)
    assert exc.value.status_code == 429
    # Retry-After header is set so partners' clients can back off cleanly.
    assert "Retry-After" in exc.value.headers


@pytest.mark.asyncio
async def test_buckets_are_per_key_independent() -> None:
    r1, r2 = _record(10), _record(11)
    for _ in range(5):
        await rl.enforce_rate_limit(api_key=r1)
    # r1 saturated; r2 has its own full bucket.
    out = await rl.enforce_rate_limit(api_key=r2)
    assert out is r2


@pytest.mark.asyncio
async def test_disable_flag_skips_enforcement(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_DISABLE", "1")
    record = _record(99)
    # 100 calls; would saturate the bucket if enforcement were active.
    for _ in range(100):
        out = await rl.enforce_rate_limit(api_key=record)
        assert out is record
