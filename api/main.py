"""FastAPI app for talent-engine.

Mounts the resume-matching internal + public routers. The internal router
(`/v1/resume-matching/match`) is the streaming UI endpoint; the public
router (`/v1/public/resume-matching/*`) is the API-key-gated JSON API.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from v1.db.database import close_db, get_async_engine, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Async-match job state + the rate-limit bucket registry both live in
    # this process. Horizontally scaling will silently break async polls
    # (50% land on the wrong replica) and dilute rate limits. Flag loudly
    # at boot so operators don't accidentally bump replica counts in the
    # 微信云托管 console. Set TALENT_ENGINE_ALLOW_MULTI_REPLICA=1 to suppress
    # once we have a shared store.
    if os.getenv("TALENT_ENGINE_ALLOW_MULTI_REPLICA", "").lower() not in {"1", "true", "yes"}:
        logger.warning(
            "talent-engine: async-match + rate-limit state is process-local. "
            "Run with exactly one replica. Set TALENT_ENGINE_ALLOW_MULTI_REPLICA=1 "
            "to silence this warning once a shared store is wired."
        )
    yield
    await close_db()


app = FastAPI(title="talent-engine", lifespan=lifespan)

# Lightweight request-id middleware: trust an incoming `X-Request-Id` if
# present (so partners can correlate calls in their own logs), else mint
# a short token. Stashed on request.state and echoed in the response so
# partner support questions like "this call at 14:32 failed" are greppable.
class _RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or "req_" + secrets.token_urlsafe(8)
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


app.add_middleware(_RequestIdMiddleware)


# API auth is via X-API-Key (a header, not cookies). We don't use credentials
# at all, so allow_credentials stays False — which lets allow_origins="*" work
# correctly. The combo `allow_origins=["*"]` + `allow_credentials=True` is
# rejected by browsers per the CORS spec and would silently break any web
# partner that ever needs to call this API cross-origin.
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    """Liveness + DB-reachability check.

    Hits the database with `SELECT 1` so the platform's liveness probe
    catches the case where the app is up but MySQL is unreachable. Returns
    503 if the DB ping fails — that turns into a restart loop on 微信云托管,
    which is the right thing to do for a stateless API container.
    """
    from fastapi import HTTPException

    try:
        async with get_async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.warning("health check failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"status": "ok"}


from v1.resume_matching.public_router import router as resume_matching_public_router
from v1.resume.parse_router import router as resume_parse_router
from v1.job.parse_router import router as job_parse_router

app.include_router(resume_matching_public_router)
app.include_router(resume_parse_router)
app.include_router(job_parse_router)
