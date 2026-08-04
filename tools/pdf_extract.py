"""PDF-to-text extraction for ``read_file`` via the docling service.

Hermes ships no in-process PDF library (``read_extract`` is deliberately pure
stdlib for the OOXML/notebook formats). PDFs are extracted by POSTing to the
docling service — the same service OpenWebUI RAG already uses
(``DOCLING_SERVER_URL``, default ``http://docling:5001``). Text-based PDFs return
their text; scanned PDFs go through docling's OCR. Any failure (service down,
timeout, oversized, no extractable text) raises :class:`ExtractionError` so
``read_file`` returns an honest unreadable error (no raw-bytes fallthrough).

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
#
# The timeout is a function of size because OCR cost is: a 40-page scan of a
# letter (82.9 MB) took 54.4s measured against the live docling on 2026-08-05,
# so a flat 60s failed it by seconds. Base + per-MB keeps small files snappy and
# still bounds the worst case.
_DOCLING_TIMEOUT_BASE_SECONDS = int(
    os.environ.get("HERMES_DOCLING_CONVERT_TIMEOUT_SECONDS", "60")
)
_DOCLING_TIMEOUT_PER_MB_SECONDS = float(
    os.environ.get("HERMES_DOCLING_CONVERT_TIMEOUT_PER_MB_SECONDS", "2")
)
_DOCLING_TIMEOUT_MAX_SECONDS = int(
    os.environ.get("HERMES_DOCLING_CONVERT_TIMEOUT_MAX_SECONDS", "600")
)
# Bytes measure the scanner's DPI, not the work: the same 40 pages are ~200 KB
# typed and 82.9 MB scanned. This is only a memory guard on reading the file in
# one piece -- the real bound is the timeout above. 25 MB rejected a real
# client's letter of claim (2026-08-04, user asked whether their defences held
# and the agent could not read the claim being defended against).
_DOCLING_MAX_PDF_BYTES = int(
    os.environ.get("HERMES_DOCLING_MAX_PDF_BYTES", str(256 * 1024 * 1024))
)
_MULTIPART_BOUNDARY = "hermesDoclingPdfExtractBoundary7f3a"
_MULTIPART_FIELD = "files"  # docling /v1/convert/file expects field name "files"
# docling defaults to image_export_mode=embedded, which inlines every page image
# as a base64 data: URI in the markdown. On that same 40-page scan the response
# was 40,522,865 characters of which 40,500,156 (99.94%) were base64 and 23,059
# were the text we asked for -- 40 MB moved to deliver 23 KB, straight into an
# agent's context window. "placeholder" leaves a <!-- image --> marker instead.
# "referenced" also avoids the base64 but emits bare filenames
# (image_000000_<sha>.png) that resolve nowhere from here, so the agent would
# get 50 dead links; read_file wants text, so placeholder is the honest mode.
_DOCLING_IMAGE_EXPORT_MODE = os.environ.get("HERMES_DOCLING_IMAGE_EXPORT_MODE", "placeholder")


def _timeout_for(size_bytes: int) -> int:
    """Seconds to allow docling for a file of this size (bounded)."""
    scaled = _DOCLING_TIMEOUT_BASE_SECONDS + int(
        (size_bytes / (1024 * 1024)) * _DOCLING_TIMEOUT_PER_MB_SECONDS
    )
    return min(scaled, _DOCLING_TIMEOUT_MAX_SECONDS)

PDF_EXTENSION = ".pdf"


def is_pdf(path) -> bool:
    return Path(str(path)).suffix.lower() == PDF_EXTENSION


def _build_multipart_body(filename: str, data: bytes) -> bytes:
    b = _MULTIPART_BOUNDARY
    field = (
        f"--{b}\r\n"
        f'Content-Disposition: form-data; name="image_export_mode"\r\n\r\n'
        f"{_DOCLING_IMAGE_EXPORT_MODE}\r\n"
    ).encode("utf-8")
    head = (
        f"--{b}\r\n"
        f'Content-Disposition: form-data; name="{_MULTIPART_FIELD}"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{b}--\r\n".encode("utf-8")
    return field + head + data + tail


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
        with urllib.request.urlopen(req, timeout=_timeout_for(size)) as resp:
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
