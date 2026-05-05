"""PDF → text extraction.

Used by `/v1/resume/parse` to convert uploaded PDFs into plain text
before handing them to BAML. We extract in Python rather than relying
on a multimodal LLM so the same code path works across all four BAML
providers (Gemini, Qwen, Hunyuan, DeepSeek) — the latter two have
inconsistent or non-existent PDF support via their OpenAI-compatible
endpoints.

Tradeoff: we lose Gemini's vision/layout-aware PDF understanding.
For digital PDFs (Word→PDF, the majority of modern Chinese resumes)
`pypdf` extracts ~95%+ fidelity. For scanned/image-only PDFs the
extracted text will be empty or near-empty — those callers need OCR
(Tencent OCR or PaddleOCR), not handled here. We surface the empty
extract as a `PdfExtractionError` so the caller can return a clean
per-file error rather than running a useless LLM call on no input.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# How few non-whitespace chars do we treat as "this PDF is image-only"?
# Real digital resumes always exceed this in practice (a one-page resume
# is ~1500–4000 characters of meaningful text). Set well below that to
# avoid false positives on terse single-page PDFs.
_MIN_USEFUL_CHARS = 50


class PdfExtractionError(Exception):
    """Raised when a PDF is structurally broken or contains no extractable text."""


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Return the concatenated text of every page in `pdf_bytes`.

    Pages are joined by `\\n\\n` so paragraph boundaries survive into
    the prompt. Raises `PdfExtractionError` when the PDF can't be parsed
    at all, or when extraction returns less than `_MIN_USEFUL_CHARS`
    of non-whitespace content (image-only / scanned PDF — needs OCR).
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "pypdf is required for PDF parsing — pip install -r requirements.txt"
        ) from e

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise PdfExtractionError(f"Failed to open PDF: {type(e).__name__}: {e}") from e

    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:
            logger.warning("pypdf failed on page %d: %s", i, e)
            pages.append("")

    text = "\n\n".join(pages).strip()
    if len(text.replace(" ", "").replace("\n", "")) < _MIN_USEFUL_CHARS:
        raise PdfExtractionError(
            "PDF contained no extractable text. "
            "This is likely a scanned/image-only PDF — OCR is required."
        )
    return text
