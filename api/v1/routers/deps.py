"""Shared FastAPI dependencies for v1 routers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from v1.db.database import get_async_engine


def get_engine() -> AsyncEngine:
    return get_async_engine()
