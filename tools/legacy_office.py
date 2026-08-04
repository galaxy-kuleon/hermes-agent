"""Convert legacy binary Office documents to OOXML via the soffice sidecar.

Word 97-2003 `.doc`, Excel `.xls` and PowerPoint `.ppt` are binary formats that
the stdlib extractors cannot open. Without this they reach `read_file`'s binary
guard and come back as "Cannot read binary file … Use vision_analyze for images,
or terminal to inspect binary files" — advice that does not help, because the
file is a document and terminal is not available to the caller.

That is not hypothetical. On 2026-07-28 a live 31-file trademark audit hit it on
`legacy-power-of-attorney.doc`; powers of attorney and older correspondence in these
matters are routinely still `.doc`.

OpenWebUI's Path A already solves this by POSTing the bytes to the shared
`soffice` service and converting the modern result. Hermes reaches the same
service on the same network, so this routes through it and hands the OOXML bytes
to the extractor that already exists for that type.

Failures raise `ExtractionError` so the caller falls back to exactly the previous
behaviour — a sidecar outage degrades to today's binary guard rather than to a
new failure mode.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Same service and default OpenWebUI's skip-rag pipeline uses.
SOFFICE_URL_ENV = "HERMES_SOFFICE_URL"
DEFAULT_SOFFICE_URL = "http://soffice:2004"
SOFFICE_TIMEOUT_ENV = "HERMES_SOFFICE_TIMEOUT_SECONDS"
DEFAULT_SOFFICE_TIMEOUT_SECONDS = 120
# A legacy document large enough to exceed this is not something a chat turn can
# usefully consume, and the conversion would dominate the turn's latency.
MAX_LEGACY_BYTES_ENV = "HERMES_SOFFICE_MAX_BYTES"
DEFAULT_MAX_LEGACY_BYTES = 50 * 1024 * 1024

# Only formats whose converted target already has an extractor. `.ppt` is absent
# on purpose: there is no PPTX extractor yet, so converting it would trade one
# unreadable file for another.
LEGACY_EXT_TO_TARGET = {
    ".doc": "docx",
    ".xls": "xlsx",
    # RTF was missing here, so read_file fell through to plain text and handed
    # the model raw \pard\plain\f0\fs24 control words. 2026-08-04, chat
    # ce9195e0: a user asked for a 1000-word summary of an RTF judgment, and
    # the model reported "much of that was RTF formatting code rather than
    # readable text" -- the user had to ask "have you read the judgment in
    # full?" to find out it had not. soffice converts RTF cleanly (verified
    # against the live service 2026-08-05).
    ".rtf": "docx",
}


def _soffice_url() -> str:
    return (os.environ.get(SOFFICE_URL_ENV) or DEFAULT_SOFFICE_URL).rstrip("/")


def _timeout_seconds() -> int:
    raw = os.environ.get(SOFFICE_TIMEOUT_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOFFICE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_SOFFICE_TIMEOUT_SECONDS


def _max_bytes() -> int:
    raw = os.environ.get(MAX_LEGACY_BYTES_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_LEGACY_BYTES
    return value if value > 0 else DEFAULT_MAX_LEGACY_BYTES


def legacy_target_extension(path: str) -> str:
    """Return the OOXML target for a legacy document, or '' if not one."""
    return LEGACY_EXT_TO_TARGET.get(Path(path).suffix.lower(), "")


def convert_to_ooxml(path: str) -> tuple[str, str]:
    """Convert *path* via soffice and return `(temp_path, target_extension)`.

    The caller owns the returned temporary file and must delete it.
    """
    from tools.read_extract import ExtractionError

    target = legacy_target_extension(path)
    if not target:
        raise ExtractionError(f"{Path(path).suffix} is not a convertible legacy format")

    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ExtractionError(f"cannot stat legacy document: {exc}") from exc
    limit = _max_bytes()
    if size > limit:
        raise ExtractionError(
            f"legacy document is {size} bytes, above the {limit}-byte conversion limit"
        )

    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests ships with hermes
        raise ExtractionError("requests is unavailable for soffice conversion") from exc

    try:
        with source.open("rb") as handle:
            response = requests.post(
                f"{_soffice_url()}/convert",
                params={"to": target},
                files={"file": (source.name, handle)},
                timeout=_timeout_seconds(),
            )
        response.raise_for_status()
        payload = response.content
    except Exception as exc:
        raise ExtractionError(
            f"soffice conversion failed for {source.name}: {type(exc).__name__}"
        ) from exc

    if not payload:
        raise ExtractionError(f"soffice returned no content for {source.name}")

    handle_fd, temp_path = tempfile.mkstemp(suffix=f".{target}", prefix="hermes-legacy-")
    try:
        with os.fdopen(handle_fd, "wb") as out:
            out.write(payload)
    except OSError as exc:
        os.unlink(temp_path)
        raise ExtractionError(f"cannot buffer converted document: {exc}") from exc
    return temp_path, target


__all__ = [
    "DEFAULT_MAX_LEGACY_BYTES",
    "DEFAULT_SOFFICE_TIMEOUT_SECONDS",
    "DEFAULT_SOFFICE_URL",
    "LEGACY_EXT_TO_TARGET",
    "convert_to_ooxml",
    "legacy_target_extension",
]
