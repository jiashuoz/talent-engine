"""End-to-end tests for /v1/resume/parse.

The BAML client is stubbed so tests don't need a real LLM. The DB engine
is in-memory SQLite.
"""

from __future__ import annotations

from typing import Dict, List

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from v1.resume.parse_router import router as parse_router
from v1.resume import parse_router as parse_router_mod
from v1.resume_matching.baml_client.types import Resume as BamlResume
from v1.resume_matching.storage import ApiKeyStore
from v1.resume_matching.storage.schema import metadata, v1_resume_matching_usage
from v1.routers.deps import get_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def api_key(engine) -> str:
    plaintext, _ = await ApiKeyStore(engine).create(name="test-key")
    return plaintext


@pytest_asyncio.fixture
async def app(engine):
    a = FastAPI()
    a.include_router(parse_router)
    a.dependency_overrides[get_engine] = lambda: engine
    yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# BAML stub
# ---------------------------------------------------------------------------


def _resume_obj(name: str = "张三") -> BamlResume:
    return BamlResume(
        name=name,
        education=[],
        experience=[],
        certifications=[],
        skills=[],
        languages=[],
        raw_text=f"raw text for {name}",
    )


class BamlStub:
    """Stubs `b.ParseResume` for parse-endpoint tests."""

    def __init__(self) -> None:
        self.parse_calls = 0
        self.fail_filenames: set[str] = set()
        self.name_for_call: List[str] = []
        self.last_client: str | None = None

    async def ParseResume(self, *, text, baml_options=None) -> BamlResume:  # noqa: N802
        self.parse_calls += 1
        # Capture the requested provider so tests can assert it was forwarded.
        if baml_options and "client" in baml_options:
            self.last_client = baml_options["client"]
        if self.parse_calls in self.fail_call_indices:
            raise RuntimeError(f"simulated parse failure on call {self.parse_calls}")
        name = self.name_for_call[self.parse_calls - 1] if (
            self.parse_calls - 1 < len(self.name_for_call)
        ) else f"候选人{self.parse_calls}"
        return _resume_obj(name=name)

    @property
    def fail_call_indices(self) -> set[int]:
        return getattr(self, "_fail_call_indices", set())

    @fail_call_indices.setter
    def fail_call_indices(self, v: set[int]) -> None:
        self._fail_call_indices = v


@pytest.fixture
def baml(monkeypatch) -> BamlStub:
    stub = BamlStub()
    stub.fail_call_indices = set()
    monkeypatch.setattr(parse_router_mod, "b", stub)
    # Stub PDF text extraction — fake PDF bytes can't be parsed by pypdf.
    monkeypatch.setattr(parse_router_mod, "extract_pdf_text", lambda pdf_bytes: "fake resume text")
    return stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _files(n: int) -> List[tuple]:
    """Build N fake PDF uploads — bytes are arbitrary; BAML is stubbed."""
    return [
        ("resumes", (f"resume_{i}.pdf", b"%PDF-1.4 fake bytes " + bytes([i]) * 32, "application/pdf"))
        for i in range(n)
    ]


async def _usage_rows(engine) -> List[Dict]:
    async with engine.connect() as conn:
        result = await conn.execute(select(v1_resume_matching_usage))
        return [dict(r._mapping) for r in result.fetchall()]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_parse_returns_one_resume(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(1),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert len(body["parsed"]) == 1
    assert body["errors"] == []
    item = body["parsed"][0]
    assert item["resume_id"] == "resume_0.pdf"   # filename fallback
    assert item["filename"] == "resume_0.pdf"
    assert item["resume"]["name"] == "候选人1"
    assert body["stats"]["files_received"] == 1
    assert body["stats"]["parsed_ok"] == 1
    assert body["stats"]["parsed_failed"] == 0


async def test_parse_returns_multiple_resumes(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(3),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["parsed"]) == 3
    assert body["stats"]["parsed_ok"] == 3


async def test_parse_uses_supplied_resume_ids(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(2),
        data={"resume_ids": ["r_001", "r_002"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = sorted(p["resume_id"] for p in body["parsed"])
    assert ids == ["r_001", "r_002"]


# ---------------------------------------------------------------------------
# Error surfacing — per-file failures don't sink the batch
# ---------------------------------------------------------------------------


async def test_parse_surfaces_per_file_errors_alongside_successes(client, api_key, baml):
    baml.fail_call_indices = {2}                 # fail the second PDF
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(3),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["parsed"]) == 2
    assert len(body["errors"]) == 1
    assert "RuntimeError" in body["errors"][0]["error"]
    assert body["stats"]["parsed_ok"] == 2
    assert body["stats"]["parsed_failed"] == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_parse_rejects_no_files(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=[],
        headers={"X-API-Key": api_key},
    )
    # FastAPI returns 422 when a required form field is missing
    assert resp.status_code in (400, 422)


async def test_parse_rejects_too_many_files(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(21),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 413


async def test_parse_rejects_oversized_pdf(client, api_key, baml):
    big = b"%PDF-1.4 " + b"x" * (5 * 1024 * 1024 + 1)
    resp = await client.post(
        "/v1/resume/parse",
        files=[("resumes", ("big.pdf", big, "application/pdf"))],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 413


async def test_parse_rejects_mismatched_resume_ids_length(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(2),
        data={"resume_ids": ["r_001"]},          # only 1 id for 2 files
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 400


async def test_parse_rejects_duplicate_resume_ids(client, api_key, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(2),
        data={"resume_ids": ["dup", "dup"]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_parse_rejects_missing_api_key(client, baml):
    resp = await client.post("/v1/resume/parse", files=_files(1))
    assert resp.status_code == 401


async def test_parse_rejects_invalid_api_key(client, baml):
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(1),
        headers={"X-API-Key": "mnk_obviously_wrong"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


async def test_parse_logs_usage_row(client, api_key, baml, engine):
    await client.post(
        "/v1/resume/parse",
        files=_files(2),
        headers={"X-API-Key": api_key},
    )
    rows = await _usage_rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row["endpoint"] == "parse"
    assert row["resume_count"] == 2
    assert row["job_count"] == 0
    assert row["pair_count"] == 2          # files-attempted (overloaded field)
    assert row["pairs_failed"] == 0
    assert row["status"] == "ok"


async def test_parse_forwards_llm_provider_env(client, api_key, baml, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "Qwen")
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(1),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    assert baml.last_client == "Qwen"


async def test_invalid_llm_provider_fails_fast(monkeypatch):
    """Unknown LLM_PROVIDER raises at config-resolution time, before any
    LLM call happens — so a typo'd deploy fails loud rather than silently."""
    from v1.resume_matching.llm_config import resolve_llm_provider

    monkeypatch.setenv("LLM_PROVIDER", "InventedModel")
    with pytest.raises(ValueError, match="InventedModel"):
        resolve_llm_provider()


async def test_parse_surfaces_pdf_extraction_error(client, api_key, baml, monkeypatch):
    """An empty/scanned-only PDF should land in errors[] without an LLM call."""
    from v1.resume import pdf_text
    from v1.resume.parse_router import _parse_one  # noqa: F401 — module access

    def raise_pdf_err(pdf_bytes):
        raise pdf_text.PdfExtractionError("simulated scanned PDF")

    # Override the test fixture's stub for this case
    import v1.resume.parse_router as parse_router_mod_local
    monkeypatch.setattr(parse_router_mod_local, "extract_pdf_text", raise_pdf_err)
    resp = await client.post(
        "/v1/resume/parse",
        files=_files(1),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parsed"] == []
    assert len(body["errors"]) == 1
    assert "PdfExtractionError" in body["errors"][0]["error"]
    assert baml.parse_calls == 0   # never reached the LLM


async def test_parse_logs_pairs_failed_on_partial_failure(client, api_key, baml, engine):
    baml.fail_call_indices = {1}
    await client.post(
        "/v1/resume/parse",
        files=_files(3),
        headers={"X-API-Key": api_key},
    )
    rows = await _usage_rows(engine)
    assert len(rows) == 1
    assert rows[0]["pairs_failed"] == 1
