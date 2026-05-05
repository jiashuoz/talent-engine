"""Storage package for the resume-matching public API.

Owns its own SQLAlchemy MetaData so schema changes here don't ripple into
the rest of the project. Tables are created via `init_tables(engine)` from
`v1.db.database.init_db()`.
"""

from v1.resume_matching.storage.schema import (
    metadata,
    v1_resume_matching_api_keys,
    v1_resume_matching_usage,
)
from v1.resume_matching.storage.api_keys import ApiKeyStore, ApiKeyRecord
from v1.resume_matching.storage.usage import UsageStore, UsageRecord

__all__ = [
    "metadata",
    "v1_resume_matching_api_keys",
    "v1_resume_matching_usage",
    "ApiKeyStore",
    "ApiKeyRecord",
    "UsageStore",
    "UsageRecord",
    "init_tables",
]


def init_tables(engine) -> None:
    """Create resume-matching tables on the given sync engine."""
    metadata.create_all(engine)
