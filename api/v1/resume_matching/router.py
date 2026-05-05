"""Public HTTP endpoint for resume-matching.

One streaming route: POST /v1/resume-matching/match — multipart form upload,
Server-Sent Events response. Events fire per milestone (JD parsed, resume
parsed, resume scored) so the frontend can render live progress. The final
`done` event carries the full report payload.

No auth — the page is intentionally public. Rate limiter + quota guards
bound the cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from v1.resume_matching.notify import send_usage_email
from v1.resume_matching.pipeline import JobInput, ResumeInput, match_all
from v1.resume_matching.rate_limit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/resume-matching", tags=["resume-matching"])

MAX_RESUMES = 20
MAX_JOB_FILES = 10
MAX_PDF_BYTES = 5 * 1024 * 1024   # 5 MB per resume PDF
MAX_TEXT_BYTES = 200 * 1024       # 200 KB per JD file


@router.post("/match", dependencies=[Depends(rate_limit)])
async def match_endpoint(
    request: Request,
    resumes: List[UploadFile] = File(...),
    jobs: List[UploadFile] = File(...),
) -> StreamingResponse:
    """Parse resumes + JDs, stream progress via SSE, end with full report.

    Wire format (one event per SSE record):
        event: progress
        data: {"type":"resume_parsed","filename":"x.pdf", ...}

        event: done
        data: {"jobs":[...], "resumes":[...]}

    Validation happens before streaming starts — a 4xx on size/count limits
    is returned as a normal JSON error, not an SSE event, so the frontend's
    fetch() promise rejects clearly.

    Every run (success or failure) fires a usage-log email to
    `USAGE_NOTIFY_TO` so the operator can track adoption + spot abuse.
    """
    resume_inputs = await _read_resumes(resumes)
    job_inputs = await _read_jobs(jobs)

    # Capture caller metadata for the telemetry email before we hand the
    # request object off to the background task. Same IP-resolution logic
    # as the rate limiter (X-Forwarded-For with fallback to client.host).
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    user_agent = request.headers.get("user-agent")

    # asyncio.Queue carries event dicts from the pipeline's callback to the
    # SSE generator. Unbounded because the queue is drained in lockstep
    # with the HTTP response; pipeline produces a small, finite number of
    # events (bounded by resume + JD count).
    queue: asyncio.Queue[Dict[str, Any] | None] = asyncio.Queue()

    async def on_progress(event: Dict[str, Any]) -> None:
        await queue.put(event)

    async def run_pipeline() -> None:
        started = time.perf_counter()
        jobs_parsed = 0
        resumes_scored = 0
        summaries: List[Dict[str, Any]] = []
        error_str: str | None = None
        try:
            report = await match_all(
                resumes=resume_inputs,
                jobs=job_inputs,
                on_progress=on_progress,
            )
            jobs_parsed = len(report.jobs)
            resumes_scored = sum(
                1 for rr in report.resumes if rr.resume is not None
            )
            summaries = _build_summaries(report)
            await queue.put({"type": "_done_sentinel", "report": _serialize(report)})
        except Exception as e:
            logger.exception("match_all failed")
            error_str = f"{type(e).__name__}: {e}"
            await queue.put({"type": "_error_sentinel", "message": error_str})
        finally:
            elapsed = time.perf_counter() - started
            # Fire-and-forget: email delivery must not block stream termination.
            asyncio.create_task(send_usage_email(
                ip=client_ip,
                resume_count=len(resume_inputs),
                jd_file_count=len(job_inputs),
                jobs_parsed=jobs_parsed,
                resumes_scored=resumes_scored,
                elapsed_sec=elapsed,
                error=error_str,
                user_agent=user_agent,
                summaries=summaries or None,
            ))
            await queue.put(None)  # end-of-stream marker

    pipeline_task = asyncio.create_task(run_pipeline())

    async def event_stream():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                event_type = event.get("type", "progress")
                if event_type == "_done_sentinel":
                    yield _sse("done", event["report"])
                elif event_type == "_error_sentinel":
                    yield _sse("error", {"message": event["message"]})
                else:
                    yield _sse("progress", event)
        finally:
            # Ensure the pipeline task is awaited to propagate cancellation
            # cleanly if the client disconnects mid-stream.
            if not pipeline_task.done():
                pipeline_task.cancel()
                try:
                    await pipeline_task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # Prevent proxies/CDNs from buffering the stream (otherwise the
            # client sees all events arrive at the end, defeating progress).
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse(event_name: str, data: Any) -> str:
    """Format one Server-Sent Event. Blank line terminates the record."""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _read_resumes(files: List[UploadFile]) -> List[ResumeInput]:
    if not files:
        raise HTTPException(400, "At least one resume required")
    if len(files) > MAX_RESUMES:
        raise HTTPException(413, f"Max {MAX_RESUMES} resumes per request")
    out: List[ResumeInput] = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(413, f"Resume '{f.filename}' exceeds {MAX_PDF_BYTES // 1024 // 1024}MB")
        if not (f.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, f"Resume '{f.filename}' must be a PDF")
        out.append(ResumeInput(filename=f.filename or "resume.pdf", pdf_bytes=data))
    return out


async def _read_jobs(files: List[UploadFile]) -> List[JobInput]:
    if not files:
        raise HTTPException(400, "At least one job description required")
    if len(files) > MAX_JOB_FILES:
        raise HTTPException(413, f"Max {MAX_JOB_FILES} JD files per request")
    out: List[JobInput] = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_TEXT_BYTES:
            raise HTTPException(413, f"JD file '{f.filename}' exceeds {MAX_TEXT_BYTES // 1024}KB")
        name = (f.filename or "").lower()
        if name.endswith(".txt") or name.endswith(".md"):
            text = data.decode("utf-8", errors="replace")
        elif name.endswith(".pdf"):
            raise HTTPException(400, f"JD file '{f.filename}' must be .txt for now (PDF support pending)")
        else:
            raise HTTPException(400, f"JD file '{f.filename}' must be .txt")
        out.append(JobInput(filename=f.filename or "jd.txt", text=text))
    return out


def _build_summaries(report) -> List[Dict[str, Any]]:
    """Extract just the per-resume top-match info the usage email needs.

    Kept minimal so the email body stays skimmable. The full report is
    still in the HTTP response to the client — this is telemetry-only.
    """
    out: List[Dict[str, Any]] = []
    for rr in report.resumes:
        entry: Dict[str, Any] = {
            "filename": rr.filename,
            "name": rr.resume.name if rr.resume else None,
            "parse_error": rr.parse_error,
            "top": None,
        }
        if rr.top_matches:
            top = rr.top_matches[0]
            jp = report.jobs[top.job_index]
            entry["top"] = {
                "company": jp.job.company,
                "position": jp.job.position,
                "score": top.score.score,
                "verdict": top.score.verdict,
            }
        out.append(entry)
    return out


def _serialize(report) -> dict:
    """Turn MatchReport dataclasses + BAML types into plain JSON."""
    return {
        "jobs": [
            {
                "source": jp.source_filename,
                "company": jp.job.company,
                "position": jp.job.position,
                "education_min": jp.job.education_min,
                "age_min": jp.job.age_min,
                "age_max": jp.job.age_max,
                "majors_preferred": jp.job.majors_preferred,
                "experience_years_min": jp.job.experience_years_min,
                "certifications_required": jp.job.certifications_required,
                "location": jp.job.location,
                "salary_min": jp.job.salary_min,
                "salary_max": jp.job.salary_max,
                "benefits": jp.job.benefits,
                "raw_text": jp.job.raw_text,
            }
            for jp in report.jobs
        ],
        "resumes": [
            {
                "filename": rr.filename,
                "parse_error": rr.parse_error,
                "resume": None if rr.resume is None else {
                    "name": rr.resume.name,
                    "gender": rr.resume.gender,
                    "age": rr.resume.age,
                    "birth_year": rr.resume.birth_year,
                    "phone": rr.resume.phone,
                    "email": rr.resume.email,
                    "hometown": rr.resume.hometown,
                    "education": [
                        {
                            "school": e.school, "degree": e.degree, "major": e.major,
                            "start": e.start, "end": e.end, "gpa_or_rank": e.gpa_or_rank,
                        }
                        for e in rr.resume.education
                    ],
                    "certifications": rr.resume.certifications,
                    "skills": rr.resume.skills,
                },
                "top_matches": [
                    {
                        "job_index": m.job_index,
                        "score": m.score.score,
                        "verdict": m.score.verdict,
                        "hard_fails": m.score.hard_fails,
                        "strengths": m.score.strengths,
                        "gaps": m.score.gaps,
                        "reasoning": m.score.reasoning,
                    }
                    for m in rr.top_matches
                ],
            }
            for rr in report.resumes
        ],
    }
