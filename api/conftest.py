"""Shared pytest fixtures and global test config.

Disables the API-key rate limiter for the duration of the test session.
The rate limiter is a per-process token bucket — desirable in production
but it would flake the test suite as soon as one file fires >60 requests
against a single key.

Tests that specifically want to exercise the rate-limit path can monkey-
patch `RATE_LIMIT_DISABLE` back off and reset `_registry` themselves.
"""

from __future__ import annotations

import os


# Set before any application module imports, so the registry's first read
# of the env var sees the disable flag.
os.environ.setdefault("RATE_LIMIT_DISABLE", "1")
