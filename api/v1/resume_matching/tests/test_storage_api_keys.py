"""Tests for ApiKeyStore — create, lookup, revocation, hashing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from v1.resume_matching.storage import ApiKeyStore
from v1.resume_matching.storage.api_keys import KEY_PREFIX, hash_key
from v1.resume_matching.storage.schema import metadata, v1_resume_matching_api_keys


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_create_returns_plaintext_with_known_prefix(engine) -> None:
    store = ApiKeyStore(engine)
    plaintext, record = await store.create(name="wechat-prod")
    # Plaintext starts with the well-known "mnk_" prefix so logs and
    # support contexts can identify keys at a glance.
    assert plaintext.startswith(KEY_PREFIX)
    assert record.name == "wechat-prod"
    assert record.id > 0
    assert record.is_active is True
    # Stored prefix is the first 12 chars of plaintext, never the secret body.
    assert record.key_prefix == plaintext[:12]


@pytest.mark.asyncio
async def test_create_persists_only_the_hash(engine) -> None:
    store = ApiKeyStore(engine)
    plaintext, record = await store.create(name="t")

    # Verify directly: the row holds the hash, not the plaintext.
    from sqlalchemy import select
    async with engine.connect() as conn:
        result = await conn.execute(
            select(v1_resume_matching_api_keys).where(
                v1_resume_matching_api_keys.c.id == record.id
            )
        )
        row = result.one()
    assert row.key_hash == hash_key(plaintext)
    assert plaintext not in row.key_hash  # sanity — hash isn't the secret


@pytest.mark.asyncio
async def test_lookup_returns_record_for_valid_key(engine) -> None:
    store = ApiKeyStore(engine)
    plaintext, created = await store.create(name="t")
    found = await store.lookup(plaintext)
    assert found is not None
    assert found.id == created.id
    assert found.name == "t"


@pytest.mark.asyncio
async def test_lookup_returns_none_for_unknown_key(engine) -> None:
    store = ApiKeyStore(engine)
    assert await store.lookup("mnk_does-not-exist") is None


@pytest.mark.asyncio
async def test_lookup_returns_none_for_malformed_input(engine) -> None:
    store = ApiKeyStore(engine)
    # No prefix → reject early without touching the DB.
    assert await store.lookup("totally-bogus") is None
    assert await store.lookup("") is None


@pytest.mark.asyncio
async def test_lookup_returns_none_for_revoked_key(engine) -> None:
    store = ApiKeyStore(engine)
    plaintext, record = await store.create(name="to-revoke")
    # Mark revoked via the public method; lookup must honour it.
    revoked = await store.revoke_by_id(record.id)
    assert revoked is True
    assert await store.lookup(plaintext) is None


@pytest.mark.asyncio
async def test_list_all_returns_every_key_ordered_by_id(engine) -> None:
    store = ApiKeyStore(engine)
    _, a = await store.create(name="alpha")
    _, b = await store.create(name="bravo")
    _, c = await store.create(name="charlie")
    rows = await store.list_all()
    assert [r.name for r in rows] == ["alpha", "bravo", "charlie"]
    assert [r.id for r in rows] == [a.id, b.id, c.id]


@pytest.mark.asyncio
async def test_list_all_includes_revoked_rows(engine) -> None:
    store = ApiKeyStore(engine)
    _, active = await store.create(name="still-active")
    _, doomed = await store.create(name="will-revoke")
    await store.revoke_by_id(doomed.id)
    rows = await store.list_all()
    assert len(rows) == 2
    # Revoked row still listed — operator needs to see history.
    revoked_row = next(r for r in rows if r.id == doomed.id)
    assert revoked_row.is_active is False
    assert revoked_row.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_by_id_returns_false_for_unknown_id(engine) -> None:
    store = ApiKeyStore(engine)
    assert await store.revoke_by_id(99999) is False


@pytest.mark.asyncio
async def test_revoke_by_id_is_idempotent(engine) -> None:
    store = ApiKeyStore(engine)
    _, record = await store.create(name="rev-twice")
    assert await store.revoke_by_id(record.id) is True
    # Second call: row exists but is already revoked → no update happens.
    assert await store.revoke_by_id(record.id) is False


@pytest.mark.asyncio
async def test_revoke_by_name_marks_all_active_keys_with_that_name(engine) -> None:
    store = ApiKeyStore(engine)
    # Two active keys under the same partner name (operator rotated without revoking the old one).
    _, k1 = await store.create(name="partner-A")
    _, k2 = await store.create(name="partner-A")
    _, other = await store.create(name="partner-B")
    count = await store.revoke_by_name("partner-A")
    assert count == 2
    rows = {r.id: r for r in await store.list_all()}
    assert rows[k1.id].is_active is False
    assert rows[k2.id].is_active is False
    assert rows[other.id].is_active is True


@pytest.mark.asyncio
async def test_revoke_by_name_returns_zero_when_no_match(engine) -> None:
    store = ApiKeyStore(engine)
    await store.create(name="exists")
    assert await store.revoke_by_name("never-existed") == 0
