"""Pydantic types for the /v1/resume/parse endpoint.

Kept separate from `v1.resume_matching.public_schema` so the parse surface
can evolve independently of the match surface even though it returns the
same `PublicResume` payload.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from v1.resume_matching.public_schema import PublicResume


class ParseResultItem(BaseModel):
    resume_id: str = Field(..., description="Echoed back from the request, or the filename if no id was supplied.")
    filename: str
    resume: PublicResume


class ParseErrorItem(BaseModel):
    resume_id: str
    filename: str
    error: str


class ParseStats(BaseModel):
    files_received: int
    parsed_ok: int
    parsed_failed: int
    elapsed_ms: int
    input_tokens: int = 0
    output_tokens: int = 0


class ParseResponse(BaseModel):
    status: str = "completed"
    parsed: List[ParseResultItem]
    errors: List[ParseErrorItem]
    stats: ParseStats
