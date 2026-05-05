"""Tests for router-side helpers: input validation, SSE encoding, response serialization.

These cover the deterministic slices of the endpoint logic that don't need
the real LLM pipeline — so we can catch regressions in size/type gating,
the SSE wire format, and the shape of the final `done` payload without
spinning up a streaming response.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from v1.resume_matching.baml_client.types import (
    Education,
    Experience,
    Job,
    MatchScore,
    Resume,
)
from v1.resume_matching.pipeline import (
    JobParse,
    Match,
    MatchReport,
    ResumeReport,
)
from v1.resume_matching.router import (
    MAX_JOB_FILES,
    MAX_PDF_BYTES,
    MAX_RESUMES,
    MAX_TEXT_BYTES,
    _read_jobs,
    _read_resumes,
    _serialize,
    _sse,
)


# ---------------------------------------------------------------------------
# SSE encoding
# ---------------------------------------------------------------------------


def test_sse_encodes_event_and_data_with_blank_line_terminator() -> None:
    record = _sse("progress", {"type": "resume_parsed", "name": "张三"})
    assert record.endswith("\n\n"), "SSE records must end with a blank line"
    lines = record.strip().split("\n")
    assert lines[0] == "event: progress"
    payload = json.loads(lines[1].removeprefix("data: "))
    assert payload == {"type": "resume_parsed", "name": "张三"}


def test_sse_preserves_unicode_without_escaping() -> None:
    # `ensure_ascii=False` is load-bearing: we don't want SSE payloads bloated
    # with \uXXXX escapes for Chinese content that the client has to reparse.
    record = _sse("progress", {"company": "北京银行"})
    assert "北京银行" in record
    assert "\\u" not in record


# ---------------------------------------------------------------------------
# File upload validation
# ---------------------------------------------------------------------------


def _upload(filename: str, content: bytes) -> UploadFile:
    """Build a minimal UploadFile the same way FastAPI does under the hood."""
    return UploadFile(filename=filename, file=io.BytesIO(content))


@pytest.mark.asyncio
async def test_read_resumes_accepts_pdf() -> None:
    out = await _read_resumes([_upload("r.pdf", b"%PDF-1.4 fake")])
    assert len(out) == 1
    assert out[0].filename == "r.pdf"
    assert out[0].pdf_bytes == b"%PDF-1.4 fake"


@pytest.mark.asyncio
async def test_read_resumes_rejects_non_pdf() -> None:
    with pytest.raises(HTTPException) as exc:
        await _read_resumes([_upload("r.txt", b"not a pdf")])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_read_resumes_rejects_oversize() -> None:
    with pytest.raises(HTTPException) as exc:
        await _read_resumes([_upload("r.pdf", b"x" * (MAX_PDF_BYTES + 1))])
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_read_resumes_enforces_count_cap() -> None:
    files = [_upload(f"r{i}.pdf", b"x") for i in range(MAX_RESUMES + 1)]
    with pytest.raises(HTTPException) as exc:
        await _read_resumes(files)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_read_jobs_accepts_txt_and_decodes_utf8() -> None:
    content = "招聘单位：测试\n招聘岗位：岗位".encode("utf-8")
    out = await _read_jobs([_upload("j.txt", content)])
    assert len(out) == 1
    assert "测试" in out[0].text


@pytest.mark.asyncio
async def test_read_jobs_rejects_pdf_for_now() -> None:
    # PDF JDs are explicitly unsupported. The 400 forces the client into
    # a clear error rather than silently mis-parsing PDF bytes as text.
    with pytest.raises(HTTPException) as exc:
        await _read_jobs([_upload("j.pdf", b"%PDF-1.4")])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_read_jobs_rejects_other_extensions() -> None:
    with pytest.raises(HTTPException) as exc:
        await _read_jobs([_upload("j.docx", b"garbage")])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_read_jobs_enforces_size_and_count_caps() -> None:
    with pytest.raises(HTTPException):
        await _read_jobs([_upload("j.txt", b"x" * (MAX_TEXT_BYTES + 1))])
    too_many = [_upload(f"j{i}.txt", b"x") for i in range(MAX_JOB_FILES + 1)]
    with pytest.raises(HTTPException):
        await _read_jobs(too_many)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _build_report() -> MatchReport:
    job = Job(
        company="A公司",
        position="岗位A",
        education_min="专科",
        age_min=20,
        age_max=30,
        majors_preferred=["计算机"],
        experience_years_min=1,
        gender_preference="不限",
        height_min_cm=None,
        certifications_required=[],
        image_requirements=None,
        duties=["做事"],
        salary_min=5000,
        salary_max=8000,
        work_schedule="长白班",
        location="济南",
        benefits=["五险"],
        raw_text="招聘单位：A公司 ...",
    )
    resume = Resume(
        name="张三",
        gender="男",
        birth_year=2000,
        age=24,
        phone=None,
        email=None,
        hometown=None,
        education=[Education(
            school="山东大学", degree="本科", major="CS",
            start="2018.9", end="2022.6", gpa_or_rank=None,
        )],
        experience=[Experience(
            organization="X公司", title="实习", start=None, end=None,
            description="做了点事",
        )],
        certifications=["计算机二级"],
        skills=["Python"],
        languages=["英语"],
        self_evaluation=None,
        raw_text="张三的简历全文 ...",
    )
    score = MatchScore(
        score=87, verdict="可推荐",
        hard_fails=[], strengths=["学历匹配"],
        gaps=["缺乏特定行业经验"], reasoning="整体不错",
    )
    return MatchReport(
        jobs=[JobParse(source_filename="jd.txt", job=job)],
        resumes=[ResumeReport(
            filename="r.pdf",
            resume=resume,
            top_matches=[Match(job_index=0, score=score)],
        )],
    )


def test_serialize_shape_matches_frontend_contract() -> None:
    report = _build_report()
    out = _serialize(report)

    # Top-level keys the frontend destructures into state.
    assert set(out.keys()) == {"jobs", "resumes"}

    j = out["jobs"][0]
    assert j["company"] == "A公司"
    assert j["raw_text"].startswith("招聘单位")
    assert j["salary_min"] == 5000

    r = out["resumes"][0]
    assert r["filename"] == "r.pdf"
    assert r["resume"]["name"] == "张三"
    assert r["resume"]["education"][0]["school"] == "山东大学"
    m = r["top_matches"][0]
    assert m["job_index"] == 0
    assert m["score"] == 87
    assert m["verdict"] == "可推荐"
    assert m["gaps"] == ["缺乏特定行业经验"]


def test_serialize_handles_parse_error_resume() -> None:
    # Resumes that failed to parse still need to appear in the list with
    # parse_error set and resume=None; the frontend renders a red banner.
    report = MatchReport(
        jobs=[],
        resumes=[ResumeReport(
            filename="broken.pdf",
            resume=None,
            parse_error="PdfError: corrupt",
        )],
    )
    out = _serialize(report)
    assert out["resumes"][0]["resume"] is None
    assert out["resumes"][0]["parse_error"] == "PdfError: corrupt"
    assert out["resumes"][0]["top_matches"] == []


def test_serialize_empty_report() -> None:
    out = _serialize(MatchReport(jobs=[], resumes=[]))
    assert out == {"jobs": [], "resumes": []}
