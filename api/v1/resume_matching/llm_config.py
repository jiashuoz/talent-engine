"""Runtime LLM-provider selection.

The .baml files reference a `Default` client whose strategy is overridden
at every call site via `baml_options={"client": resolve_llm_provider()}`.
This lets the same image run against any of the four providers wired in
`baml_src/main.baml` purely via the `LLM_PROVIDER` env var.

Set `LLM_PROVIDER` to one of:
    Gemini    — original (Vertex with google-ai fallback)
    Qwen      — Aliyun DashScope, OpenAI-compatible
    Hunyuan   — Tencent, OpenAI-compatible
    DeepSeek  — OpenAI-compatible

Default if unset: `Gemini` (preserves existing behavior).
"""

from __future__ import annotations

import os
from typing import Final

_VALID_PROVIDERS: Final[set[str]] = {"Gemini", "Qwen", "Hunyuan", "DeepSeek"}


def resolve_llm_provider() -> str:
    """Return the BAML client name to route through, validated.

    Reads `LLM_PROVIDER` lazily so tests can monkeypatch the env var
    between cases. Raises on an unknown provider rather than silently
    falling back — a typo'd env var should fail loud at boot.
    """
    name = os.getenv("LLM_PROVIDER", "Gemini")
    if name not in _VALID_PROVIDERS:
        raise ValueError(
            f"LLM_PROVIDER={name!r} is not one of {sorted(_VALID_PROVIDERS)}. "
            f"Check api/v1/resume_matching/baml_src/main.baml for the canonical list."
        )
    return name
