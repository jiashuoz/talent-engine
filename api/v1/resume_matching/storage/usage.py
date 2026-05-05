"""UsageStore — append-only log of public-API requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine

from v1.resume_matching.storage.schema import v1_resume_matching_usage


@dataclass
class UsageRecord:
    api_key_id: Optional[int]
    endpoint: str                # "match" | "match_async"  (poll requests aren't logged)
    resume_count: int
    job_count: int
    pair_count: int
    pairs_failed: int
    elapsed_ms: int
    status: str                  # "ok" | "error"
    # BAML token totals — sum across all ScoreMatch calls in this request.
    # 0 when scoring failed before any LLM call landed (e.g. validation),
    # or when the BAML collector didn't report usage. Multiply by current
    # Gemini 2.5 Flash rates for a per-request cost estimate.
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None
    client_ip: Optional[str] = None


class UsageStore:

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def log(self, record: UsageRecord) -> None:
        """Insert a usage row. Never raises into the caller — the request
        path must not fail because telemetry insertion failed.
        """
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    v1_resume_matching_usage.insert().values(
                        api_key_id=record.api_key_id,
                        endpoint=record.endpoint,
                        resume_count=record.resume_count,
                        job_count=record.job_count,
                        pair_count=record.pair_count,
                        pairs_failed=record.pairs_failed,
                        input_tokens=record.input_tokens,
                        output_tokens=record.output_tokens,
                        elapsed_ms=record.elapsed_ms,
                        status=record.status,
                        error=record.error,
                        client_ip=record.client_ip,
                    )
                )
        except Exception:
            # Telemetry only — log and swallow.
            import logging
            logging.getLogger(__name__).warning(
                "failed to log resume_matching usage row", exc_info=True,
            )
