"""End-to-end test of the public JSON API against real desktop data.

What it does:
  1. Loads resume PDFs + JD .txt files from Desktop/岗位说明以及简历/.
  2. Parses them via BAML (the same parsing the WeChat mini program would do
     client-side, except here we run it server-side for convenience — the
     point is to produce realistic structured PublicResume/PublicJob input).
  3. Spins up the FastAPI app in-process and POSTs the parsed payload to
     /v1/resume-matching/match with an X-API-Key header. This exercises
     auth, validation, routing, dependency injection, and the score_pairs
     pipeline — same code paths as a deployed server.
  4. Writes a markdown report under scripts/out/.

In-process via httpx.ASGITransport rather than hitting localhost:8000 so we
test the worktree's modified code, not whatever image docker compose is
running. Pass --base-url to point at an external server instead.

Cost: ~25 parse + N×M score calls against Gemini/Vertex. Default is
limited to 3 resumes × 1 JD file (~10 score calls) — pass --full for the
whole dataset (~150 calls).

Usage (from api/):
    .venv/bin/python -m v1.resume_matching.scripts.test_public_api
    .venv/bin/python -m v1.resume_matching.scripts.test_public_api --full
    .venv/bin/python -m v1.resume_matching.scripts.test_public_api \
        --resume-limit 5 --job-limit 2
    .venv/bin/python -m v1.resume_matching.scripts.test_public_api \
        --base-url http://localhost:8001 --api-key mnk_...
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

# Make `python -m v1.resume_matching.scripts.test_public_api` work when
# the script is invoked from api/ as cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import httpx
from baml_py import Pdf
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from v1.resume_matching import pipeline as pipeline_mod
from v1.resume_matching.public_router import router
from v1.resume_matching.public_schema import (
    JobItem,
    MatchRequest,
    PublicJob,
    PublicResume,
    ResumeItem,
    from_baml_job,
    from_baml_resume,
)
from v1.resume_matching.storage import ApiKeyStore
from v1.resume_matching.storage.schema import metadata
from v1.routers.deps import get_engine


DATA_DIR = Path.home() / "Desktop" / "岗位说明以及简历"
OUT_DIR = Path(__file__).resolve().parent / "out"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_pdfs(limit: Optional[int]) -> List[Tuple[str, bytes]]:
    """Return (filename, pdf_bytes) for each resume PDF, optionally capped."""
    resume_dir = DATA_DIR / "简历"
    if not resume_dir.exists():
        raise SystemExit(f"Resume dir not found: {resume_dir}")
    paths = sorted(resume_dir.glob("*.pdf"))
    if limit is not None:
        paths = paths[:limit]
    return [(p.name, p.read_bytes()) for p in paths]


def _load_jds(limit: Optional[int]) -> List[Tuple[str, str]]:
    """Return (filename, text) for each JD .txt file, optionally capped."""
    paths = sorted(DATA_DIR.glob("岗位说明-*.txt"))
    if limit is not None:
        paths = paths[:limit]
    return [(p.name, p.read_text(encoding="utf-8")) for p in paths]


# ---------------------------------------------------------------------------
# Parsing — simulates the mini program's client-side parsing step.
# ---------------------------------------------------------------------------


async def _parse_resume(filename: str, pdf_bytes: bytes) -> Optional[PublicResume]:
    pdf = Pdf.from_base64(base64.b64encode(pdf_bytes).decode("ascii"))
    try:
        baml_resume = await pipeline_mod.b.ParseResume(resume_pdf=pdf)
        return from_baml_resume(baml_resume)
    except Exception as e:
        print(f"  ! parse failed for {filename}: {type(e).__name__}: {e}")
        return None


async def _parse_jds(filename: str, text: str) -> List[PublicJob]:
    """Split a multi-job JD file on 招聘单位 headers and parse each chunk."""
    chunks = pipeline_mod._split_jd_text(text)
    if not chunks:
        # Fall back to the list-parse path for files without standard headers.
        try:
            baml_jobs = await pipeline_mod.b.ParseJobDescriptions(text=text)
            return [from_baml_job(j) for j in baml_jobs]
        except Exception as e:
            print(f"  ! list parse failed for {filename}: {type(e).__name__}: {e}")
            return []

    out: List[PublicJob] = []
    for i, chunk in enumerate(chunks):
        try:
            baml_job = await pipeline_mod.b.ParseSingleJob(text=chunk)
            out.append(from_baml_job(baml_job))
        except Exception as e:
            print(f"  ! chunk {i} parse failed for {filename}: {type(e).__name__}: {e}")
    return out


async def _build_public_payload(
    resume_pdfs: List[Tuple[str, bytes]],
    jd_files: List[Tuple[str, str]],
) -> Tuple[List[ResumeItem], List[JobItem], dict]:
    """Parse all inputs and assemble the MatchRequest payload.

    Also returns a side dict mapping resume_id/job_id back to filenames so
    the markdown report can show file provenance.
    """
    print(f"Parsing {len(resume_pdfs)} resumes + {len(jd_files)} JD files...")

    resume_tasks = [_parse_resume(name, data) for name, data in resume_pdfs]
    jd_tasks = [_parse_jds(name, text) for name, text in jd_files]

    parsed_resumes, parsed_jds = await asyncio.gather(
        asyncio.gather(*resume_tasks),
        asyncio.gather(*jd_tasks),
    )

    resume_items: List[ResumeItem] = []
    id_to_filename: dict = {"resumes": {}, "jobs": {}}
    for (filename, _), public in zip(resume_pdfs, parsed_resumes):
        if public is None:
            continue
        rid = f"r_{len(resume_items):03d}"
        resume_items.append(ResumeItem(resume_id=rid, resume=public))
        id_to_filename["resumes"][rid] = (filename, public.name)

    job_items: List[JobItem] = []
    for (filename, _), jobs in zip(jd_files, parsed_jds):
        for job in jobs:
            jid = f"j_{len(job_items):03d}"
            job_items.append(JobItem(job_id=jid, job=job))
            id_to_filename["jobs"][jid] = (filename, job.company, job.position)

    print(f"Parsed -> {len(resume_items)} resumes, {len(job_items)} jobs")
    return resume_items, job_items, id_to_filename


# ---------------------------------------------------------------------------
# In-process FastAPI app + API key
# ---------------------------------------------------------------------------


async def _build_in_process_client() -> Tuple[httpx.AsyncClient, str]:
    """Return (client, api_key) wired against an in-process app + sqlite."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    plaintext, _ = await ApiKeyStore(engine).create(name="local-test-script")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_engine] = lambda: engine

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=httpx.Timeout(120.0),  # LLM scoring on ~150 pairs takes a while
    )
    return client, plaintext


# ---------------------------------------------------------------------------
# Markdown reporter
# ---------------------------------------------------------------------------


def _render_markdown(*, response: dict, id_to_filename: dict) -> str:
    """Render the API response into a per-resume top-3 markdown report.

    Mirrors the legacy demo.py format so a human can sanity-check the
    new API output side-by-side with the old report.
    """
    matches = response.get("matches", [])
    errors = response.get("errors", [])
    stats = response.get("stats") or {}

    # Group matches by resume_id, sort each group desc by score.
    by_resume: dict = {}
    for m in matches:
        by_resume.setdefault(m["resume_id"], []).append(m)
    for rid in by_resume:
        by_resume[rid].sort(key=lambda m: m["score"], reverse=True)

    lines: List[str] = []
    lines.append("# 简历匹配报告 (public API)")
    lines.append("")
    lines.append(f"- Pairs scored: {stats.get('pairs_scored', '?')}")
    lines.append(f"- Pairs failed: {stats.get('pairs_failed', '?')}")
    lines.append(f"- Elapsed: {stats.get('elapsed_ms', '?')} ms")
    lines.append("")

    lines.append("## 岗位库")
    for jid, (src, company, position) in id_to_filename["jobs"].items():
        lines.append(f"- **[{jid}] {company} — {position}** (源: {src})")
    lines.append("")

    lines.append("## 按简历看 Top-3 推荐")
    for rid, (filename, name) in id_to_filename["resumes"].items():
        lines.append(f"### {filename} — {name or '?'}")
        top = by_resume.get(rid, [])[:3]
        if not top:
            lines.append("_无匹配_")
            lines.append("")
            continue
        for rank, m in enumerate(top, 1):
            jid = m["job_id"]
            _, company, position = id_to_filename["jobs"].get(jid, ("?", "?", "?"))
            lines.append(
                f"**#{rank}  {company} — {position}  "
                f"(score: {m['score']}, {m['verdict']})**"
            )
            lines.append(f"- 推理: {m['reasoning']}")
            if m["hard_fails"]:
                lines.append(f"- ⚠️ 硬不合: {'; '.join(m['hard_fails'])}")
            if m["strengths"]:
                lines.append(f"- ✅ 优势: {'; '.join(m['strengths'])}")
            if m["gaps"]:
                lines.append(f"- 📋 差距: {'; '.join(m['gaps'])}")
            lines.append("")

    if errors:
        lines.append("## Errors")
        for e in errors:
            lines.append(f"- r={e['resume_id']} j={e.get('job_id') or '-'}: {e['error']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="Run on all resumes + all JD files (~150 LLM scoring calls).")
    ap.add_argument("--resume-limit", type=int, default=3,
                    help="Cap resumes (default 3 for cheap smoke test). Ignored with --full.")
    ap.add_argument("--job-limit", type=int, default=1,
                    help="Cap JD files (default 1 for cheap smoke test). Ignored with --full.")
    ap.add_argument("--base-url", default=None,
                    help="Hit an external running server instead of in-process. "
                         "When set, --api-key is required.")
    ap.add_argument("--api-key", default=None,
                    help="API key for the external server (only with --base-url).")
    ap.add_argument("--mode", choices=["sync", "async"], default="sync",
                    help="Which endpoint to exercise (sync or async+poll).")
    args = ap.parse_args()

    resume_limit = None if args.full else args.resume_limit
    job_limit = None if args.full else args.job_limit

    resume_pdfs = _load_pdfs(resume_limit)
    jd_files = _load_jds(job_limit)
    print(f"Loaded {len(resume_pdfs)} resume PDFs + {len(jd_files)} JD files from {DATA_DIR}")

    # Phase 1: parse client-side. Real BAML calls.
    t0 = time.perf_counter()
    resume_items, job_items, id_to_filename = await _build_public_payload(
        resume_pdfs, jd_files,
    )
    print(f"Parsing done in {time.perf_counter() - t0:.1f}s")

    if not resume_items or not job_items:
        print("Nothing to score — aborting.")
        return 1

    request = MatchRequest(resumes=resume_items, jobs=job_items)

    # Phase 2: HTTP — in-process by default, external server if --base-url.
    if args.base_url:
        if not args.api_key:
            raise SystemExit("--api-key is required when --base-url is set")
        client = httpx.AsyncClient(
            base_url=args.base_url, timeout=httpx.Timeout(120.0),
        )
        api_key = args.api_key
    else:
        client, api_key = await _build_in_process_client()

    headers = {"X-API-Key": api_key}

    print(f"\nPOST {args.mode} -> {args.base_url or '<in-process>'}/v1/resume-matching/match"
          f"  ({len(resume_items)}×{len(job_items)} = {len(resume_items)*len(job_items)} pairs)")

    t0 = time.perf_counter()
    async with client:
        if args.mode == "sync":
            resp = await client.post(
                "/v1/resume-matching/match",
                json=request.model_dump(), headers=headers,
            )
            resp.raise_for_status()
            response_data = resp.json()
        else:
            resp = await client.post(
                "/v1/resume-matching/match/async",
                json=request.model_dump(), headers=headers,
            )
            resp.raise_for_status()
            job_id = resp.json()["job_id"]
            print(f"Async job: {job_id} — polling...")
            while True:
                await asyncio.sleep(2.0)
                poll = await client.get(
                    f"/v1/resume-matching/match/{job_id}", headers=headers,
                )
                poll.raise_for_status()
                response_data = poll.json()
                p = response_data.get("progress") or {}
                print(f"  status={response_data['status']} "
                      f"progress={p.get('pairs_done', '?')}/{p.get('pairs_total', '?')}")
                if response_data["status"] in ("completed", "failed"):
                    break

    elapsed = time.perf_counter() - t0
    print(f"\nMatch done in {elapsed:.1f}s")
    print(f"  pairs_scored={response_data.get('stats', {}).get('pairs_scored')} "
          f"pairs_failed={response_data.get('stats', {}).get('pairs_failed')}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = OUT_DIR / "public_api_report.md"
    md_path.write_text(
        _render_markdown(response=response_data, id_to_filename=id_to_filename),
        encoding="utf-8",
    )
    print(f"Report: {md_path}")

    json_path = OUT_DIR / "public_api_response.json"
    json_path.write_text(
        json.dumps(response_data, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"Raw:    {json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
