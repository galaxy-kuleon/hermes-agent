"""PDF-to-text extraction for ``read_file`` via the docling service.

Hermes ships no in-process PDF library (``read_extract`` is deliberately pure
stdlib for the OOXML/notebook formats). PDFs are extracted by POSTing to the
docling service — the same service OpenWebUI RAG already uses
(``DOCLING_SERVER_URL``, default ``http://docling:5001``). Text-based PDFs return
their text; scanned PDFs go through docling's OCR. Any failure (service down,
timeout, oversized, no extractable text) raises :class:`ExtractionError` so
``read_file`` falls back to its normal binary-file guard.

This closes the Path-B P0 (2026-07-15): a user uploads a PDF and asks a question,
the file is delivered to hermes via skip-rag handoff, but the agent had no
reliable way to read PDF text — ``read_file`` treated it as an unreadable binary,
so the agent wandered (soc_v2 convert, vision, browser) and never answered.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from tools.read_extract import ExtractionError

# docling endpoint — shared with the OpenWebUI RAG docling integration.
_DOCLING_URL = os.environ.get("DOCLING_SERVER_URL", "http://docling:5001").rstrip("/")
_DOCLING_CONVERT_PATH = "/v1/convert/file"
# Named tunables (constants, not magic values). Env overrides for deployment.
_DOCLING_TIMEOUT_SECONDS = int(os.environ.get("HERMES_DOCLING_CONVERT_TIMEOUT_SECONDS", "60"))
_DOCLING_MAX_PDF_BYTES = int(os.environ.get("HERMES_DOCLING_MAX_PDF_BYTES", str(25 * 1024 * 1024)))
_MULTIPART_BOUNDARY = "hermesDoclingPdfExtractBoundary7f3a"
_MULTIPART_FIELD = "files"  # docling /v1/convert/file expects field name "files"

PDF_EXTENSION = ".pdf"


def is_pdf(path) -> bool:
    return Path(str(path)).suffix.lower() == PDF_EXTENSION


def _build_multipart_body(filename: str, data: bytes) -> bytes:
    b = _MULTIPART_BOUNDARY
    head = (
        f"--{b}\r\n"
        f'Content-Disposition: form-data; name="{_MULTIPART_FIELD}"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{b}--\r\n".encode("utf-8")
    return head + data + tail


def extract_pdf_text(path) -> str:
    """Return extracted text for a PDF via docling, or raise ExtractionError.

    Prefers docling's markdown rendering (``document.md_content``), falling back
    to ``text_content``. Raises when docling is unreachable, times out, the file
    is oversized, or no text could be extracted.
    """
    p = str(path)
    try:
        size = os.path.getsize(p)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if size > _DOCLING_MAX_PDF_BYTES:
        raise ExtractionError(
            f"PDF too large for text extraction ({size} bytes > {_DOCLING_MAX_PDF_BYTES})"
        )
    try:
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    body = _build_multipart_body(Path(p).name, data)
    req = urllib.request.Request(
        f"{_DOCLING_URL}{_DOCLING_CONVERT_PATH}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={_MULTIPART_BOUNDARY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_DOCLING_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # URLError, timeout, JSON error — all fail closed to fallback
        raise ExtractionError(f"docling PDF extraction failed: {exc}") from exc

    if payload.get("status") != "success":
        raise ExtractionError(f"docling did not succeed: {payload.get('errors')!r}")
    doc = payload.get("document") or {}
    text = doc.get("md_content") or doc.get("text_content") or ""
    if not text.strip():
        raise ExtractionError(
            "PDF produced no extractable text (image-only scan without OCR text?)"
        )
    return text
