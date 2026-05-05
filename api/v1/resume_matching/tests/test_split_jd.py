"""Tests for the regex-based JD splitter.

The splitter is the first robustness layer for multi-job JD files — it
replaces an earlier LLM boundary-detection pass that was unreliable at
counting characters. We verify:

  - single-job files pass through as one chunk
  - multi-job files produce one chunk per 招聘单位 header
  - 【招聘单位】 (bracketed) form is recognized
  - preamble before the first header is discarded
  - incidental occurrences of the phrase inside a job body do NOT split
  - empty / whitespace-only input returns an empty list (fallback triggers)
"""

from v1.resume_matching.pipeline import _split_jd_text


def test_single_job_produces_one_chunk() -> None:
    text = """招聘单位：某公司
招聘岗位：客服
岗位要求：大专"""
    assert len(_split_jd_text(text)) == 1


def test_multiple_jobs_separated_by_blank_lines() -> None:
    text = """招聘单位：A公司
招聘岗位：岗位A


招聘单位：B公司
招聘岗位：岗位B


招聘单位：C公司
招聘岗位：岗位C"""
    chunks = _split_jd_text(text)
    assert len(chunks) == 3
    assert chunks[0].startswith("招聘单位：A公司")
    assert chunks[1].startswith("招聘单位：B公司")
    assert chunks[2].startswith("招聘单位：C公司")
    # No chunk should contain another job's header — guards against merging.
    assert "B公司" not in chunks[0]
    assert "A公司" not in chunks[1]


def test_bracketed_header_form_is_recognized() -> None:
    text = """【招聘单位】重汽集团
【招聘岗位】客服

【招聘单位】神思电子
【招聘岗位】运维"""
    chunks = _split_jd_text(text)
    assert len(chunks) == 2
    assert "重汽集团" in chunks[0]
    assert "神思电子" in chunks[1]


def test_preamble_before_first_header_is_discarded() -> None:
    text = """本文档包含 2025 年秋招岗位
更新日期 2025-11-01

招聘单位：甲公司
招聘岗位：某岗位

招聘单位：乙公司
招聘岗位：另一岗位"""
    chunks = _split_jd_text(text)
    assert len(chunks) == 2
    # Preamble must not leak into the first job chunk.
    assert "更新日期" not in chunks[0]
    assert chunks[0].startswith("招聘单位：甲公司")


def test_incidental_phrase_inside_body_does_not_split() -> None:
    # "招聘单位" appears mid-sentence inside a job body; because our regex
    # requires line-start, this is NOT a split point.
    text = """招聘单位：某公司
岗位要求：熟悉招聘单位业务流程，能与 HR 协作
工作内容：协助招聘"""
    chunks = _split_jd_text(text)
    assert len(chunks) == 1


def test_empty_input_returns_empty_list() -> None:
    assert _split_jd_text("") == []
    assert _split_jd_text("   \n\n  ") == []


def test_document_without_header_returns_empty_list() -> None:
    # Triggers the router-level fallback to ParseJobDescriptions.
    text = """We are hiring a software engineer.
Requirements: 3+ years Python."""
    assert _split_jd_text(text) == []


def test_whitespace_trim_per_chunk() -> None:
    # Leading/trailing blank lines are stripped from each chunk.
    text = "\n\n\n招聘单位：公司A\n岗位要求：...\n\n\n"
    chunks = _split_jd_text(text)
    assert len(chunks) == 1
    assert chunks[0] == "招聘单位：公司A\n岗位要求：..."
