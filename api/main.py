"""FastAPI app for talent-engine.

Mounts the resume-matching internal + public routers. The internal router
(`/v1/resume-matching/match`) is the streaming UI endpoint; the public
router (`/v1/public/resume-matching/*`) is the API-key-gated JSON API.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from v1.db.database import close_db, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(title="talent-engine", lifespan=lifespan)

origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


from v1.resume_matching.public_router import router as resume_matching_public_router
from v1.resume.parse_router import router as resume_parse_router
from v1.job.parse_router import router as job_parse_router

app.include_router(resume_matching_public_router)
app.include_router(resume_parse_router)
app.include_router(job_parse_router)
