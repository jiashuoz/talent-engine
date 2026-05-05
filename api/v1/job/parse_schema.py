"""Pydantic types for the /v1/job/parse endpoint.

A single uploaded file may contain multiple JDs glued together (the bundle
format the WeChat client ships uses `招聘单位` as the header for each).
The parser splits before LLM extraction, so the response is a flat list
of parsed jobs with provenance back to the source file + chunk index.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from v1.resume_matching.public_schema import PublicJob


class ParseJobResultItem(BaseModel):
    job_id: str = Field(
        ...,
        description=(
            "Synthesized id of the form `{filename}#{chunk_index}` for bundles, "
            "or `{filename}#0` for single-JD files. Stable for a given input."
        ),
    )
    filename: str
    chunk_index: int
    job: PublicJob


class ParseJobErrorItem(BaseModel):
    filename: str
    chunk_index: int
    error: str


class ParseJobStats(BaseModel):
    files_received: int
    chunks_detected: int
    jobs_parsed: int
    jobs_failed: int
    elapsed_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


class ParseJobResponse(BaseModel):
    status: str = "completed"
    parsed: List[ParseJobResultItem]
    errors: List[ParseJobErrorItem]
    stats: ParseJobStats
