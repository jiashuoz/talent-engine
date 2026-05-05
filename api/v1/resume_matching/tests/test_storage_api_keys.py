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
    # Mark revoked directly — there's no public revoke() yet (operator
    # can do it via SQL); we just need to confirm the lookup honours it.
    from sqlalchemy import update
    async with engine.begin() as conn:
        await conn.execute(
            update(v1_resume_matching_api_keys)
            .where(v1_resume_matching_api_keys.c.id == record.id)
            .values(revoked_at=datetime.now(timezone.utc))
        )
    assert await store.lookup(plaintext) is None
