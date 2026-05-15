"""Operator CLI to manage public-API keys.

Run from inside the API container (DATABASE_URL must point at the right
MySQL). Four subcommands:

    create  <partner-name>          Mint a new key. Plaintext shown ONCE.
    list                            Show every key (active + revoked).
    revoke  <id-or-name>            Revoke by numeric id OR by partner name.
                                    Name revokes every active key with that name.
    rotate  <partner-name>          Revoke every active key for the partner
                                    AND mint a new one in a single transaction-ish
                                    flow. Use for "partner leaked their key".

Examples:

    python -m v1.resume_matching.scripts.manage_api_keys create xiaohongshu-prod
    python -m v1.resume_matching.scripts.manage_api_keys list
    python -m v1.resume_matching.scripts.manage_api_keys revoke 5
    python -m v1.resume_matching.scripts.manage_api_keys revoke xiaohongshu-prod
    python -m v1.resume_matching.scripts.manage_api_keys rotate xiaohongshu-prod

The plaintext key is never persisted — only the SHA-256 hash. Lose the
plaintext at creation time and you have to rotate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List

from v1.db.database import get_async_engine, get_sync_engine
from v1.resume_matching.storage import ApiKeyRecord, ApiKeyStore, init_tables


def _fmt_dt(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _print_table(rows: List[ApiKeyRecord]) -> None:
    if not rows:
        print("(no keys)")
        return
    # Column widths sized to the largest visible value, with sane minimums.
    id_w = max(2, max(len(str(r.id)) for r in rows))
    name_w = max(4, max(len(r.name) for r in rows))
    prefix_w = max(6, max(len(r.key_prefix) for r in rows))
    status_w = 8
    created_w = 23
    header = (
        f"{'ID':>{id_w}}  {'Name':<{name_w}}  {'Prefix':<{prefix_w}}  "
        f"{'Status':<{status_w}}  {'Created':<{created_w}}  Revoked"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        status = "active" if r.is_active else "revoked"
        print(
            f"{r.id:>{id_w}}  {r.name:<{name_w}}  {r.key_prefix:<{prefix_w}}  "
            f"{status:<{status_w}}  {_fmt_dt(r.created_at):<{created_w}}  "
            f"{_fmt_dt(r.revoked_at)}"
        )


def _print_minted(plaintext: str, record: ApiKeyRecord) -> None:
    print()
    print(f"  Name:       {record.name}")
    print(f"  Key ID:     {record.id}")
    print(f"  Prefix:     {record.key_prefix}")
    print(f"  Created at: {record.created_at.isoformat()}")
    print()
    print("  API key (shown ONCE — copy it now):")
    print(f"    {plaintext}")
    print()
    print("  Send via header:")
    print(f"    X-API-Key: {plaintext}")
    print()


async def _cmd_create(name: str) -> int:
    store = ApiKeyStore(get_async_engine())
    plaintext, record = await store.create(name=name)
    _print_minted(plaintext, record)
    return 0


async def _cmd_list() -> int:
    store = ApiKeyStore(get_async_engine())
    rows = await store.list_all()
    _print_table(rows)
    return 0


async def _cmd_revoke(target: str) -> int:
    """Revoke by ID if `target` parses as int, else by name."""
    store = ApiKeyStore(get_async_engine())
    try:
        key_id = int(target)
    except ValueError:
        n = await store.revoke_by_name(target)
        if n == 0:
            print(f"No active keys found for name {target!r}.")
            return 1
        print(f"Revoked {n} key(s) with name {target!r}.")
        return 0
    ok = await store.revoke_by_id(key_id)
    if not ok:
        print(f"Key id {key_id} not found or already revoked.")
        return 1
    print(f"Revoked key id {key_id}.")
    return 0


async def _cmd_rotate(name: str) -> int:
    """Revoke all active keys for `name`, then mint a fresh one."""
    store = ApiKeyStore(get_async_engine())
    n_revoked = await store.revoke_by_name(name)
    print(f"Revoked {n_revoked} prior key(s) for {name!r}.")
    plaintext, record = await store.create(name=name)
    print("Minted replacement:")
    _print_minted(plaintext, record)
    return 0


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage public-API keys for the resume-matching service.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_create = sub.add_parser("create", help="Mint a new key.")
    sp_create.add_argument("name", help="Partner label (e.g. 'xiaohongshu-prod').")

    sub.add_parser("list", help="List every key (active + revoked).")

    sp_revoke = sub.add_parser("revoke", help="Revoke a key by id or name.")
    sp_revoke.add_argument(
        "target",
        help="Numeric key id, or partner name (revokes all active keys for the name).",
    )

    sp_rotate = sub.add_parser(
        "rotate", help="Revoke all active keys for a partner and mint a fresh one."
    )
    sp_rotate.add_argument("name", help="Partner label.")

    args = parser.parse_args()

    # Ensure the tables exist before any operation — running this on a
    # fresh DB shouldn't blow up with "table does not exist".
    init_tables(get_sync_engine())

    if args.cmd == "create":
        return await _cmd_create(args.name)
    if args.cmd == "list":
        return await _cmd_list()
    if args.cmd == "revoke":
        return await _cmd_revoke(args.target)
    if args.cmd == "rotate":
        return await _cmd_rotate(args.name)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
