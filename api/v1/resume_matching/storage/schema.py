"""SQLAlchemy tables for resume-matching API keys + usage logs.

Two tables, both prefixed `v1_resume_matching_` so they don't collide with
the rest of the v1 schema. Owns its own MetaData instance — see package
docstring.

Design: minimum-viable, operator-only. There is no UI for creating or
revoking keys; that's done via `scripts/create_api_key.py`. Usage rows
are append-only, queried by hand when we want to look at adoption.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)


metadata = MetaData()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Stores hashed API keys. The plaintext key is shown ONCE at create time
# and never persisted. `key_prefix` (first 8 chars of the plaintext) is
# stored alongside so the operator can identify which key a usage row
# corresponds to without keeping the secret.
v1_resume_matching_api_keys = Table(
    "v1_resume_matching_api_keys",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False),                  # operator-supplied label
    Column("key_prefix", String(16), nullable=False),        # first 8 chars of plaintext
    Column("key_hash", String(64), nullable=False),          # sha256 hex
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),
    Column("revoked_at", DateTime(timezone=True), nullable=True),

    Index("idx_v1_rm_api_keys_hash", "key_hash", unique=True),
)


# Append-only log of every public-API request. Captures both successes
# and failures so the operator can spot abuse patterns. Pair counts let
# you derive LLM-cost estimates per key without re-running.
v1_resume_matching_usage = Table(
    "v1_resume_matching_usage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("api_key_id", Integer, ForeignKey("v1_resume_matching_api_keys.id", ondelete="SET NULL"), nullable=True),
    Column("endpoint", String, nullable=False),              # "match" | "match_async" | "match_poll"
    Column("resume_count", Integer, nullable=False, default=0),
    Column("job_count", Integer, nullable=False, default=0),
    Column("pair_count", Integer, nullable=False, default=0),
    Column("pairs_failed", Integer, nullable=False, default=0),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("elapsed_ms", Integer, nullable=False, default=0),
    Column("status", String, nullable=False),                # "ok" | "error"
    Column("error", Text, nullable=True),
    Column("client_ip", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, default=_utcnow),

    Index("idx_v1_rm_usage_key_created", "api_key_id", "created_at"),
    Index("idx_v1_rm_usage_created", "created_at"),
)
