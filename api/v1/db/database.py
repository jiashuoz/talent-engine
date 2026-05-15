"""
Database connection management using SQLAlchemy Core (async).

MySQL-only. Async path uses aiomysql; sync path (table creation, scripts)
uses pymysql. Both honour a single `DATABASE_URL` env var; if absent we
fall back to the docker-compose default pointing at the `db` service.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

_async_engine: Optional[AsyncEngine] = None
_sync_engine: Optional[Engine] = None


def _normalize(url: str, driver: str) -> str:
    """Rewrite a bare `mysql://` URL to use the requested SQLAlchemy driver.

    Accepts inputs already qualified with a driver (e.g. `mysql+aiomysql://`)
    and swaps the driver portion so the same env value can serve both
    async and sync engines.
    """
    if url.startswith("mysql+"):
        # already has a driver — replace it
        scheme_end = url.index("://")
        return f"mysql+{driver}{url[scheme_end:]}"
    if url.startswith("mysql://"):
        return f"mysql+{driver}://" + url[len("mysql://") :]
    raise ValueError(f"DATABASE_URL must start with mysql:// or mysql+driver://; got {url!r}")


def _default_url() -> str:
    return "mysql://talent_engine:talent_engine@db:3306/talent_engine"


def get_database_url() -> str:
    return _normalize(os.getenv("DATABASE_URL") or _default_url(), "aiomysql")


def get_sync_database_url() -> str:
    return _normalize(os.getenv("DATABASE_URL") or _default_url(), "pymysql")


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(
            get_database_url(),
            echo=False,
            pool_pre_ping=True,
            pool_recycle=3600,  # MySQL drops idle conns at wait_timeout (default 8h); recycle well under that
            pool_size=10,
            max_overflow=20,
        )
    return _async_engine


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(
            get_sync_database_url(),
            echo=False,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _sync_engine


async def init_db() -> None:
    """Create resume-matching tables on the sync engine."""
    from v1.resume_matching.storage import init_tables as init_resume_matching_tables

    sync_engine = get_sync_engine()
    init_resume_matching_tables(sync_engine)
    logger.info("Database tables created")


async def close_db() -> None:
    global _async_engine, _sync_engine
    if _async_engine:
        await _async_engine.dispose()
        _async_engine = None
    if _sync_engine:
        _sync_engine.dispose()
        _sync_engine = None
