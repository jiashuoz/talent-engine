"""Mint a new public-API key for the resume-matching service.

Operator-only — no UI exposes this. Run from inside the API container or
any environment where DATABASE_URL points at the right MySQL.

  $ python -m v1.resume_matching.scripts.create_api_key "wechat-mini-prod"

Prints the plaintext key once. Persist it somewhere safe — only the hash
is stored in the database, so a forgotten key has to be revoked and
re-created.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from v1.db.database import get_async_engine
from v1.resume_matching.storage import ApiKeyStore, init_tables
from v1.db.database import get_sync_engine


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Create a new resume-matching API key.")
    parser.add_argument("name", help="Human label for this key (e.g. 'wechat-mini-prod')")
    args = parser.parse_args()

    # Make sure the tables exist — running this on a fresh DB before the
    # API has booted shouldn't fail with "table does not exist".
    init_tables(get_sync_engine())

    store = ApiKeyStore(get_async_engine())
    plaintext, record = await store.create(name=args.name)

    print()
    print(f"  Name:       {record.name}")
    print(f"  Key ID:     {record.id}")
    print(f"  Prefix:     {record.key_prefix}")
    print(f"  Created at: {record.created_at.isoformat()}")
    print()
    print(f"  API key (shown ONCE — copy it now):")
    print(f"    {plaintext}")
    print()
    print("  Send via header:")
    print(f"    X-API-Key: {plaintext}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
