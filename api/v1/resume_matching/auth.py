"""FastAPI dependency that authenticates a public-API request via X-API-Key.

The dependency resolves the header to an `ApiKeyRecord` and stashes it on
`request.state` so the route handler (and the usage-logger) can read the
api_key_id without re-querying. On missing or invalid key it raises a
401 — the public API is closed by default.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from v1.routers.deps import get_engine
from v1.resume_matching.storage import ApiKeyStore, ApiKeyRecord


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    engine: AsyncEngine = Depends(get_engine),
) -> ApiKeyRecord:
    """Dependency: resolve X-API-Key to an ApiKeyRecord or raise 401.

    Returns the record so handlers can log it; also stashes it on
    `request.state.api_key` so the usage-logger code path can read it
    without redundant DB lookups.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    store = ApiKeyStore(engine)
    record = await store.lookup(x_api_key)
    if record is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.api_key = record
    return record
