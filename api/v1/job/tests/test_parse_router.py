"""End-to-end tests for /v1/job/parse.

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

from v1.job import parse_router as parse_router_mod
from v1.job.parse_router import router as parse_router
from v1.resume_matching.baml_client.types import Job as BamlJob
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


def _job(company: str = "测试公司", position: str = "工程师") -> BamlJob:
    return BamlJob(
        company=company,
        position=position,
        majors_preferred=[],
        certifications_required=[],
        duties=[],
        location="北京",
        benefits=[],
        raw_text=f"raw {company} / {position}",
    )


class BamlStub:
    """Stubs `b.ParseSingleJob` and `b.ParseJobDescriptions`.

    Per-call failures triggered via `fail_single_call_indices` (1-indexed
    by ParseSingleJob call order) and `fail_list_call`.
    """

    def __init__(self) -> None:
        self.single_calls = 0
        self.list_calls = 0
        self.fail_single_call_indices: set[int] = set()
        self.fail_list_call: bool = False
        self.list_jobs_per_call: List[List[BamlJob]] = []

    async def ParseSingleJob(self, *, text: str, baml_options=None) -> BamlJob:  # noqa: N802
        self.single_calls += 1
        if self.single_calls in self.fail_single_call_indices:
            raise RuntimeError(f"simulated single-parse failure on call {self.single_calls}")
        # Tag with the call index so tests can assert ordering.
        return _job(company=f"公司{self.single_calls}")

    async def ParseJobDescriptions(self, *, text: str, baml_options=None) -> List[BamlJob]:  # noqa: N802
        self.list_calls += 1
        if self.fail_list_call:
            raise RuntimeError("simulated list-parse failure")
        if self.list_jobs_per_call:
            return self.list_jobs_per_call.pop(0)
        # Default: return one job tagged with the call index.
        return [_job(company=f"List公司{self.list_calls}_0"), _job(company=f"List公司{self.list_calls}_1")]


@pytest.fixture
def baml(monkeypatch) -> BamlStub:
    stub = BamlStub()
    monkeypatch.setattr(parse_router_mod, "b", stub)
    return stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Single-JD body — no `招聘单位` header → list-fallback path.
SINGLE_JD_NO_HEADER = """\
岗位：测试工程师
工作地点：北京
"""


# Bundled body — three jobs separated by `招聘单位` headers → split path.
BUNDLED_JDS = """\
preamble line that should be discarded

招聘单位：A公司
岗位：A岗位
工作地点：北京

招聘单位：B公司
岗位：B岗位
工作地点：上海

招聘单位：C公司
岗位：C岗位
工作地点：深圳
"""


def _file(name: str, body: str):
    return ("jds", (name, body.encode("utf-8"), "text/plain"))


async def _usage_rows(engine) -> List[Dict]:
    async with engine.connect() as conn:
        result = await conn.execute(select(v1_resume_matching_usage))
        return [dict(r._mapping) for r in result.fetchall()]


# ---------------------------------------------------------------------------
# Happy path — header-bundle splits into per-chunk parses
# ---------------------------------------------------------------------------


async def test_parse_bundled_file_splits_into_chunks(client, api_key, baml):
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("bundle.txt", BUNDLED_JDS)],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert len(body["parsed"]) == 3                       # three 招聘单位 chunks
    assert body["errors"] == []
    # Provenance: each result tags filename + chunk_index
    assert all(p["filename"] == "bundle.txt" for p in body["parsed"])
    assert sorted(p["chunk_index"] for p in body["parsed"]) == [0, 1, 2]
    assert all(p["job_id"].startswith("bundle.txt#") for p in body["parsed"])
    # Confirms ParseSingleJob path was used, not the list fallback
    assert baml.single_calls == 3
    assert baml.list_calls == 0


async def test_parse_multiple_files_aggregated(client, api_key, baml):
    resp = await client.post(
        "/v1/job/parse",
        files=[
            _file("a.txt", BUNDLED_JDS),                  # 3 chunks
            _file("b.txt", BUNDLED_JDS),                  # 3 chunks
        ],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["parsed"]) == 6
    assert body["stats"]["files_received"] == 2
    assert body["stats"]["chunks_detected"] == 6
    filenames = sorted({p["filename"] for p in body["parsed"]})
    assert filenames == ["a.txt", "b.txt"]


# ---------------------------------------------------------------------------
# Fallback — file with no `招聘单位` header → list-parse
# ---------------------------------------------------------------------------


async def test_parse_unheaded_file_uses_list_fallback(client, api_key, baml):
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("plain.txt", SINGLE_JD_NO_HEADER)],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Default stub returns 2 jobs from ParseJobDescriptions
    assert len(body["parsed"]) == 2
    assert baml.single_calls == 0
    assert baml.list_calls == 1


# ---------------------------------------------------------------------------
# Per-chunk error surfacing
# ---------------------------------------------------------------------------


async def test_parse_surfaces_per_chunk_errors_alongside_successes(client, api_key, baml):
    baml.fail_single_call_indices = {2}                   # fail second chunk
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("bundle.txt", BUNDLED_JDS)],         # 3 chunks
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["parsed"]) == 2
    assert len(body["errors"]) == 1
    err = body["errors"][0]
    assert "RuntimeError" in err["error"]
    assert err["filename"] == "bundle.txt"
    assert err["chunk_index"] == 1                        # 1-indexed call → 0-indexed chunk
    assert body["stats"]["jobs_parsed"] == 2
    assert body["stats"]["jobs_failed"] == 1


async def test_parse_list_fallback_failure_yields_file_level_error(client, api_key, baml):
    baml.fail_list_call = True
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("plain.txt", SINGLE_JD_NO_HEADER)],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parsed"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["filename"] == "plain.txt"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_parse_rejects_no_files(client, api_key, baml):
    resp = await client.post("/v1/job/parse", files=[], headers={"X-API-Key": api_key})
    assert resp.status_code in (400, 422)


async def test_parse_rejects_too_many_files(client, api_key, baml):
    files = [_file(f"f{i}.txt", BUNDLED_JDS) for i in range(11)]
    resp = await client.post("/v1/job/parse", files=files, headers={"X-API-Key": api_key})
    assert resp.status_code == 413


async def test_parse_rejects_oversized_file(client, api_key, baml):
    big = "x" * (200 * 1024 + 1)
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("big.txt", big)],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 413


async def test_parse_rejects_too_many_chunks(client, api_key, baml):
    # 101 chunks across one file should trip MAX_CHUNKS_PER_REQUEST=100
    body = "\n".join([f"招聘单位：公司{i}\n岗位：岗位{i}\n工作地点：北京\n" for i in range(101)])
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("big_bundle.txt", body)],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 413


async def test_parse_rejects_non_utf8(client, api_key, baml):
    bad = b"\xff\xfe garbage bytes"                        # invalid UTF-8
    resp = await client.post(
        "/v1/job/parse",
        files=[("jds", ("bad.txt", bad, "text/plain"))],
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_parse_rejects_missing_api_key(client, baml):
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("a.txt", BUNDLED_JDS)],
    )
    assert resp.status_code == 401


async def test_parse_rejects_invalid_api_key(client, baml):
    resp = await client.post(
        "/v1/job/parse",
        files=[_file("a.txt", BUNDLED_JDS)],
        headers={"X-API-Key": "mnk_obviously_wrong"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


async def test_parse_logs_usage_row(client, api_key, baml, engine):
    await client.post(
        "/v1/job/parse",
        files=[_file("a.txt", BUNDLED_JDS)],              # 3 chunks
        headers={"X-API-Key": api_key},
    )
    rows = await _usage_rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row["endpoint"] == "parse_job"
    assert row["resume_count"] == 0
    assert row["job_count"] == 1                          # files
    assert row["pair_count"] == 3                         # chunks attempted
    assert row["pairs_failed"] == 0
    assert row["status"] == "ok"


async def test_parse_logs_pairs_failed_on_partial_failure(client, api_key, baml, engine):
    baml.fail_single_call_indices = {1}                   # fail first chunk
    await client.post(
        "/v1/job/parse",
        files=[_file("a.txt", BUNDLED_JDS)],              # 3 chunks
        headers={"X-API-Key": api_key},
    )
    rows = await _usage_rows(engine)
    assert len(rows) == 1
    assert rows[0]["pairs_failed"] == 1
