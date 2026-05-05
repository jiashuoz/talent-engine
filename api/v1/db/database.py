"""
Database connection management using SQLAlchemy Core (async).

Slimmed copy of mnexa-ai's `api/v1/db/database.py`, scoped to what
resume-matching needs: an async engine for request handlers, a sync
engine for table creation, and Cloud SQL Unix-socket support so the
same code runs locally and in Cloud Run / Aliyun FC.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

_async_engine: Optional[AsyncEngine] = None
_sync_engine: Optional[Engine] = None


def is_cloud_sql() -> bool:
    return os.getenv("CLOUD_SQL_CONNECTION_NAME") is not None


def _build_cloud_sql_url() -> str:
    connection_name = os.getenv("CLOUD_SQL_CONNECTION_NAME")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "")
    db_name = os.getenv("DB_NAME", "talent_engine")
    if not connection_name:
        raise ValueError("CLOUD_SQL_CONNECTION_NAME required for Cloud SQL")
    encoded_password = quote_plus(db_password)
    return f"postgresql://{db_user}:{encoded_password}@/{db_name}?host=/cloudsql/{connection_name}"


def get_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url.replace("postgresql://", "postgresql+asyncpg://")
    if is_cloud_sql():
        return _build_cloud_sql_url().replace("postgresql://", "postgresql+asyncpg://")
    return "postgresql+asyncpg://postgres:postgres@db/talent_engine"


def get_sync_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url.replace("postgresql://", "postgresql+psycopg2://").replace(
            "+asyncpg://", "+psycopg2://"
        )
    if is_cloud_sql():
        return _build_cloud_sql_url().replace("postgresql://", "postgresql+psycopg2://")
    return "postgresql+psycopg2://postgres:postgres@db/talent_engine"


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        url = get_database_url()
        if is_cloud_sql():
            _async_engine = create_async_engine(
                url,
                echo=False,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
            )
        else:
            _async_engine = create_async_engine(
                url,
                echo=False,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
            )
    return _async_engine


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        url = get_sync_database_url()
        if is_cloud_sql():
            _sync_engine = create_engine(
                url,
                echo=False,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
            )
        else:
            _sync_engine = create_engine(url, echo=False, pool_pre_ping=True)
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
