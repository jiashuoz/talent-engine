"""Authenticated PDF→JSON resume parsing.

  POST /v1/resume/parse — multipart upload, JSON response

Auth, key issuance, and usage logging share the same backing tables as
`v1.resume_matching.public_router`: a single API key works for both
parsing and matching, and operator scripts in
`v1.resume_matching.scripts.create_api_key` continue to work unchanged.

Sync only. A typical batch (≤ 20 PDFs) parses in ~10–30 s with the
pipeline's default concurrency cap, well under WeChat's 60 s default.
If parse latency or batch size ever grows past that, mirror the async
+ poll pattern from public_router.py.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncEngine

from v1.resume_matching.auth import require_api_key
from v1.resume_matching.baml_client.async_client import b
from v1.resume_matching.pipeline import _collector_tokens
from v1.resume_matching.public_schema import from_baml_resume
from v1.resume_matching.storage import ApiKeyRecord, UsageRecord, UsageStore
from v1.resume.parse_schema import (
    ParseErrorItem,
    ParseResponse,
    ParseResultItem,
    ParseStats,
)
from v1.routers.deps import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/resume", tags=["resume"])


# Defensive caps. Parsing one PDF via Gemini Flash is ~3–8s; 20 in
# parallel still finishes well under WeChat's default 60s timeout.
MAX_FILES = 20
MAX_PDF_BYTES = 5 * 1024 * 1024
PARSE_CONCURRENCY = 10


async def _parse_one(
    *,
    resume_id: str,
    filename: str,
    pdf_bytes: bytes,
    sem: asyncio.Semaphore,
) -> tuple[Optional[ParseResultItem], Optional[ParseErrorItem], int, int]:
    """Parse one PDF with a per-call BAML Collector for token tracking.

    Returns (success_item, error_item, input_tokens, output_tokens). Exactly
    one of the first two is non-None.
    """
    from baml_py import Collector, Pdf

    collector = Collector(name="resume-parse")
    async with sem:
        try:
            pdf = Pdf.from_base64(base64.b64encode(pdf_bytes).decode("ascii"))
            baml_resume = await b.ParseResume(
                resume_pdf=pdf,
                baml_options={"collector": collector},
            )
            in_tok, out_tok = _collector_tokens(collector)
            return (
                ParseResultItem(
                    resume_id=resume_id,
                    filename=filename,
                    resume=from_baml_resume(baml_resume),
                ),
                None,
                in_tok,
                out_tok,
            )
        except Exception as e:
            logger.exception("ParseResume failed for %s", filename)
            in_tok, out_tok = _collector_tokens(collector)
            return (
                None,
                ParseErrorItem(
                    resume_id=resume_id,
                    filename=filename,
                    error=f"{type(e).__name__}: {e}",
                ),
                in_tok,
                out_tok,
            )


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


@router.post("/parse", response_model=ParseResponse)
async def parse_endpoint(
    request: Request,
    resumes: List[UploadFile] = File(..., description="One or more resume PDFs."),
    resume_ids: Optional[List[str]] = Form(
        default=None,
        description="Optional client-supplied ids, one per file in order. Defaults to filename.",
    ),
    api_key: ApiKeyRecord = Depends(require_api_key),
    engine: AsyncEngine = Depends(get_engine),
) -> ParseResponse:
    """Parse resume PDFs into the structured `Resume` shape consumed by /match.

    Per-file failures (corrupt PDF, BAML extraction error) appear in
    `errors[]` with status 200; only request-level failures return 4xx/5xx.
    """
    if not resumes:
        raise HTTPException(400, "resumes must be non-empty")
    if len(resumes) > MAX_FILES:
        raise HTTPException(413, f"max {MAX_FILES} resumes per request")
    if resume_ids is not None and len(resume_ids) != len(resumes):
        raise HTTPException(400, "resume_ids length must match resumes length")
    if resume_ids is not None and len(set(resume_ids)) != len(resume_ids):
        raise HTTPException(400, "resume_ids values must be unique")

    started = time.perf_counter()
    files: List[tuple[str, str, bytes]] = []
    for i, upload in enumerate(resumes):
        data = await upload.read()
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(
                413, f"{upload.filename}: PDF exceeds {MAX_PDF_BYTES // (1024*1024)} MB"
            )
        rid = resume_ids[i] if resume_ids else (upload.filename or f"resume_{i}")
        files.append((rid, upload.filename or f"resume_{i}", data))

    sem = asyncio.Semaphore(PARSE_CONCURRENCY)
    results = await asyncio.gather(
        *[_parse_one(resume_id=rid, filename=fn, pdf_bytes=pb, sem=sem)
          for rid, fn, pb in files]
    )

    parsed: List[ParseResultItem] = []
    errors: List[ParseErrorItem] = []
    total_in = 0
    total_out = 0
    for ok, err, in_tok, out_tok in results:
        if ok is not None:
            parsed.append(ok)
        if err is not None:
            errors.append(err)
        total_in += in_tok
        total_out += out_tok

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Usage log — overload pair_count/pairs_failed as files-attempted/files-failed.
    # Same schema as the match endpoint so existing operator queries keep working.
    await UsageStore(engine).log(UsageRecord(
        api_key_id=api_key.id,
        endpoint="parse",
        resume_count=len(files),
        job_count=0,
        pair_count=len(files),
        pairs_failed=len(errors),
        input_tokens=total_in,
        output_tokens=total_out,
        elapsed_ms=elapsed_ms,
        status="ok",
        error=None,
        client_ip=_client_ip(request),
    ))

    return ParseResponse(
        status="completed",
        parsed=parsed,
        errors=errors,
        stats=ParseStats(
            files_received=len(files),
            parsed_ok=len(parsed),
            parsed_failed=len(errors),
            elapsed_ms=elapsed_ms,
            input_tokens=total_in,
            output_tokens=total_out,
        ),
    )
