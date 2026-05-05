"""Authenticated JSON API for resume-matching.

Three endpoints:

  POST /v1/resume-matching/api/match           — sync, returns full result
  POST /v1/resume-matching/api/match/async     — kicks off background job
  GET  /v1/resume-matching/api/match/{job_id}  — poll an async job

Auth is via X-API-Key header. Unlike the public demo (`/v1/resume-matching/match`,
multipart + SSE), this surface is closed by default — keys are minted by
the operator via `scripts/create_api_key.py`. Every request — successful
or not — is logged to `v1_resume_matching_usage` for adoption tracking.

Async job storage is intentionally in-memory: the cluster is one process
today, and we documented that clients must retry on 404. If we ever need
to survive restarts, swap the `_AsyncJobStore` implementation for a
DB-backed one without touching the route handlers.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from v1.routers.deps import get_engine
from v1.resume_matching.auth import require_api_key
from v1.resume_matching.pipeline import (
    DEFAULT_CONCURRENCY,
    PairScore,
    score_pairs,
)
from v1.resume_matching.public_schema import (
    AsyncJobAccepted,
    AsyncJobProgress,
    AsyncPollResponse,
    MatchErrorItem,
    MatchRequest,
    MatchResponse,
    MatchResultItem,
    MatchStats,
    from_baml_score,
    to_baml_job,
    to_baml_resume,
)
from v1.resume_matching.storage import ApiKeyRecord, UsageRecord, UsageStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/resume-matching/api", tags=["resume-matching-api"])


# Defensive caps. The pipeline can technically handle more, but anything
# bigger than this from a mini program is almost certainly accidental
# (or malicious). Limits are well above the demo limits because parsing
# is the client's problem here, not ours — pure scoring scales further.
MAX_RESUMES = 100
MAX_JOBS = 100
MAX_PAIRS = 1000      # N × M; cheap defense against 100×100=10k accidents
ASYNC_JOB_TTL_SEC = 60 * 60   # 1 hour — clients are expected to poll within this window


# ---------------------------------------------------------------------------
# In-memory async job store
# ---------------------------------------------------------------------------


@dataclass
class _AsyncJob:
    """Snapshot of one in-flight or completed async match job.

    `task` keeps a strong reference so the worker isn't garbage-collected
    while running. We don't expose it; callers see only status, progress,
    result, error.
    """
    job_id: str
    status: str = "queued"                 # queued | running | completed | failed
    pairs_done: int = 0
    pairs_total: int = 0
    result: Optional[MatchResponse] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None


class _AsyncJobStore:
    """Process-local job registry. Loses everything on restart by design."""

    def __init__(self) -> None:
        self._jobs: Dict[str, _AsyncJob] = {}
        self._lock = asyncio.Lock()

    async def put(self, job: _AsyncJob) -> None:
        async with self._lock:
            self._sweep_locked()
            self._jobs[job.job_id] = job

    async def get(self, job_id: str) -> Optional[_AsyncJob]:
        async with self._lock:
            self._sweep_locked()
            return self._jobs.get(job_id)

    def _sweep_locked(self) -> None:
        """Drop any job older than TTL. Lazy — only runs when we touch the store."""
        cutoff = time.time() - ASYNC_JOB_TTL_SEC
        stale = [jid for jid, j in self._jobs.items() if j.created_at < cutoff]
        for jid in stale:
            self._jobs.pop(jid, None)


_jobs = _AsyncJobStore()


# ---------------------------------------------------------------------------
# Request validation + scoring
# ---------------------------------------------------------------------------


def _validate(req: MatchRequest) -> None:
    """Defensive size checks — raise HTTPException on violation.

    Done in the handler before we touch the pipeline so the mini program
    sees a clear 4xx on accidental over-sends rather than a partial result
    plus a vague 500.
    """
    if not req.resumes:
        raise HTTPException(400, "resumes must be non-empty")
    if not req.jobs:
        raise HTTPException(400, "jobs must be non-empty")
    if len(req.resumes) > MAX_RESUMES:
        raise HTTPException(413, f"max {MAX_RESUMES} resumes per request")
    if len(req.jobs) > MAX_JOBS:
        raise HTTPException(413, f"max {MAX_JOBS} jobs per request")
    if len(req.resumes) * len(req.jobs) > MAX_PAIRS:
        raise HTTPException(
            413,
            f"too many pairs: {len(req.resumes)}×{len(req.jobs)} > {MAX_PAIRS}",
        )
    # Reject duplicate ids early — silent dedup would surprise the client
    # since they'd lose results without an obvious error.
    rids = [r.resume_id for r in req.resumes]
    if len(set(rids)) != len(rids):
        raise HTTPException(400, "resume_id values must be unique")
    jids = [j.job_id for j in req.jobs]
    if len(set(jids)) != len(jids):
        raise HTTPException(400, "job_id values must be unique")


def _build_pairs(req: MatchRequest) -> List[Tuple[str, str, object, object]]:
    """Translate every (resume, job) into BAML types and pair them up."""
    baml_resumes = [(r.resume_id, to_baml_resume(r.resume)) for r in req.resumes]
    baml_jobs = [(j.job_id, to_baml_job(j.job)) for j in req.jobs]
    pairs: List[Tuple[str, str, object, object]] = []
    for rid, br in baml_resumes:
        for jid, bj in baml_jobs:
            pairs.append((rid, jid, br, bj))
    return pairs


def _format_response(
    *,
    pair_results: List[PairScore],
    elapsed_ms: int,
) -> MatchResponse:
    matches: List[MatchResultItem] = []
    errors: List[MatchErrorItem] = []
    for pr in pair_results:
        if pr.score is not None:
            matches.append(from_baml_score(
                resume_id=pr.resume_id, job_id=pr.job_id, score=pr.score,
            ))
        else:
            errors.append(MatchErrorItem(
                resume_id=pr.resume_id, job_id=pr.job_id,
                error=pr.error or "unknown",
            ))
    return MatchResponse(
        status="completed",
        matches=matches,
        errors=errors,
        stats=MatchStats(
            pairs_scored=len(matches),
            pairs_failed=len(errors),
            elapsed_ms=elapsed_ms,
        ),
    )


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


async def _resolve_concurrency(req: MatchRequest) -> int:
    requested = req.options.concurrency or DEFAULT_CONCURRENCY
    # Hard server cap so a client can't blow the LLM quota with one request.
    return max(1, min(requested, DEFAULT_CONCURRENCY * 2))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/match", response_model=MatchResponse)
async def match_sync(
    payload: MatchRequest,
    request: Request,
    api_key: ApiKeyRecord = Depends(require_api_key),
    engine: AsyncEngine = Depends(get_engine),
) -> MatchResponse:
    """Score every (resume × job) pair and return results inline.

    Use for small batches that fit comfortably in WeChat's HTTPS request
    timeout (~60s default). For larger jobs the client should use
    `/match/async` and poll.
    """
    _validate(payload)
    started = time.perf_counter()
    pairs = _build_pairs(payload)
    concurrency = await _resolve_concurrency(payload)

    pair_results: List[PairScore] = []
    error_str: Optional[str] = None
    try:
        pair_results = await score_pairs(pairs=pairs, concurrency=concurrency)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response = _format_response(pair_results=pair_results, elapsed_ms=elapsed_ms)
        return response
    except Exception as e:
        logger.exception("score_pairs failed (sync)")
        error_str = f"{type(e).__name__}: {e}"
        raise HTTPException(500, "Match scoring failed") from e
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await UsageStore(engine).log(UsageRecord(
            api_key_id=api_key.id,
            endpoint="match",
            resume_count=len(payload.resumes),
            job_count=len(payload.jobs),
            pair_count=len(pairs),
            pairs_failed=sum(1 for p in pair_results if p.score is None),
            input_tokens=sum(p.input_tokens for p in pair_results),
            output_tokens=sum(p.output_tokens for p in pair_results),
            elapsed_ms=elapsed_ms,
            status="error" if error_str else "ok",
            error=error_str,
            client_ip=_client_ip(request),
        ))


@router.post("/match/async", response_model=AsyncJobAccepted, status_code=202)
async def match_async(
    payload: MatchRequest,
    request: Request,
    api_key: ApiKeyRecord = Depends(require_api_key),
    engine: AsyncEngine = Depends(get_engine),
) -> AsyncJobAccepted:
    """Accept a match request and return a job_id to poll.

    The worker runs as an asyncio task on the same process. If the
    container restarts before the job completes, the job is lost — clients
    must retry. Acceptable for the current single-VM deployment.
    """
    _validate(payload)
    pairs = _build_pairs(payload)
    concurrency = await _resolve_concurrency(payload)

    job_id = "rmj_" + secrets.token_urlsafe(16)
    job = _AsyncJob(job_id=job_id, status="queued", pairs_total=len(pairs))
    await _jobs.put(job)

    client_ip = _client_ip(request)

    async def _worker() -> None:
        job.status = "running"
        started = time.perf_counter()
        pair_results: List[PairScore] = []
        error_str: Optional[str] = None

        async def on_progress(done: int, total: int) -> None:
            job.pairs_done = done
            job.pairs_total = total

        try:
            pair_results = await score_pairs(
                pairs=pairs,
                concurrency=concurrency,
                on_progress=on_progress,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            job.result = _format_response(
                pair_results=pair_results, elapsed_ms=elapsed_ms,
            )
            job.pairs_done = job.pairs_total
            job.status = "completed"
        except Exception as e:
            logger.exception("score_pairs failed (async job=%s)", job_id)
            error_str = f"{type(e).__name__}: {e}"
            job.error = error_str
            job.status = "failed"
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            await UsageStore(engine).log(UsageRecord(
                api_key_id=api_key.id,
                endpoint="match_async",
                resume_count=len(payload.resumes),
                job_count=len(payload.jobs),
                pair_count=len(pairs),
                pairs_failed=sum(1 for p in pair_results if p.score is None),
                input_tokens=sum(p.input_tokens for p in pair_results),
                output_tokens=sum(p.output_tokens for p in pair_results),
                elapsed_ms=elapsed_ms,
                status="error" if error_str else "ok",
                error=error_str,
                client_ip=client_ip,
            ))

    job.task = asyncio.create_task(_worker())
    return AsyncJobAccepted(job_id=job_id, status="queued")


@router.get("/match/{job_id}", response_model=AsyncPollResponse)
async def match_poll(
    job_id: str,
    api_key: ApiKeyRecord = Depends(require_api_key),
) -> AsyncPollResponse:
    """Return the current state of an async match job.

    404 covers three cases without distinguishing them: never existed,
    expired (>1h old), or lost to a server restart. Clients are expected
    to retry-from-scratch on 404.
    """
    job = await _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired")

    progress = AsyncJobProgress(
        pairs_done=job.pairs_done, pairs_total=job.pairs_total,
    ) if job.pairs_total else None

    if job.status == "completed" and job.result is not None:
        return AsyncPollResponse(
            job_id=job.job_id,
            status="completed",
            progress=progress,
            matches=job.result.matches,
            errors=job.result.errors,
            stats=job.result.stats,
        )
    if job.status == "failed":
        return AsyncPollResponse(
            job_id=job.job_id,
            status="failed",
            progress=progress,
            error=job.error or "unknown error",
        )
    return AsyncPollResponse(
        job_id=job.job_id,
        status=job.status,
        progress=progress,
    )
