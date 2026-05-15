"""Tests for `extract_pdf_text`.

Constructing a real PDF byte-perfect in test code requires reportlab or
a binary fixture — neither is in this repo. Instead we exercise the
error paths with garbage bytes and monkey-patch pypdf's `PdfReader` for
the cases that need a controllable page sequence.
"""

from __future__ import annotations

from typing import List

import pytest

from v1.resume.pdf_text import PdfExtractionError, extract_pdf_text


def test_garbage_bytes_raise_extraction_error() -> None:
    with pytest.raises(PdfExtractionError) as exc:
        extract_pdf_text(b"this is not a PDF, just random bytes")
    assert "Failed to open PDF" in str(exc.value)


def test_empty_bytes_raise_extraction_error() -> None:
    with pytest.raises(PdfExtractionError):
        extract_pdf_text(b"")


class _FakePage:
    def __init__(self, txt: str, fail: bool = False) -> None:
        self._txt = txt
        self._fail = fail

    def extract_text(self) -> str:
        if self._fail:
            raise RuntimeError("synthetic page failure")
        return self._txt


def _install_fake_reader(monkeypatch, pages: List[_FakePage]) -> None:
    """Replace pypdf.PdfReader so extract_pdf_text sees `pages`."""
    import pypdf

    class _FakeReader:
        def __init__(self, _buf) -> None:
            self.pages = pages

    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)


def test_text_below_min_chars_threshold_raises_image_only_hint(monkeypatch) -> None:
    """Sub-threshold extracted text → caller gets a clear 'needs OCR' error."""
    _install_fake_reader(monkeypatch, [_FakePage("hi")])
    with pytest.raises(PdfExtractionError) as exc:
        extract_pdf_text(b"anything; FakeReader ignores it")
    msg = str(exc.value).lower()
    assert "scanned" in msg or "image" in msg


def test_text_above_threshold_round_trips(monkeypatch) -> None:
    long_text = "candidate resume content " * 5     # > 50 chars after strip
    _install_fake_reader(monkeypatch, [_FakePage(long_text)])
    out = extract_pdf_text(b"anything")
    assert long_text.strip() in out


def test_multiple_pages_joined_by_double_newline(monkeypatch) -> None:
    pages = [_FakePage("page one content " * 3), _FakePage("page two content " * 3)]
    _install_fake_reader(monkeypatch, pages)
    out = extract_pdf_text(b"anything")
    assert "page one" in out
    assert "page two" in out
    assert "\n\n" in out


def test_per_page_failure_does_not_crash(monkeypatch) -> None:
    """One bad page logs + continues; others still contribute output."""
    pages = [
        _FakePage("first page text " * 4, fail=False),
        _FakePage("UNUSED", fail=True),
        _FakePage("third page text " * 4, fail=False),
    ]
    _install_fake_reader(monkeypatch, pages)
    out = extract_pdf_text(b"anything")
    assert "first page" in out
    assert "third page" in out
    assert "UNUSED" not in out
