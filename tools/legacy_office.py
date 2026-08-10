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

import logging
import os
import time
import tempfile
from pathlib import Path

# Same service and default OpenWebUI's skip-rag pipeline uses.
SOFFICE_URL_ENV = "HERMES_SOFFICE_URL"
DEFAULT_SOFFICE_URL = "http://soffice:2004"
SOFFICE_TIMEOUT_ENV = "HERMES_SOFFICE_TIMEOUT_SECONDS"
DEFAULT_SOFFICE_TIMEOUT_SECONDS = 120
# A legacy document large enough to exceed this is not something a chat turn can
# usefully consume, and the conversion would dominate the turn's latency.
_log = logging.getLogger(__name__)
MAX_LEGACY_BYTES_ENV = "HERMES_SOFFICE_MAX_BYTES"
# A busy sidecar is not a broken document. 503 is the shed-load signal the
# soffice sidecar emits when every conversion slot is taken; 502/504 are the
# same class from anything in front of it.
RETRYABLE_STATUS = frozenset({502, 503, 504})
RETRY_ATTEMPTS = int(os.environ.get("HERMES_SOFFICE_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_SECONDS = float(os.environ.get("HERMES_SOFFICE_RETRY_BACKOFF_SECONDS", "2"))
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

    # A 503 from the sidecar means "busy", not "this document is broken", and
    # the two must not read the same to the caller: on 2026-08-05 a real user
    # attached an eleven-document legal bundle, the sidecar shed nine of them
    # instantly, and the model reported nine case authorities as UNREADABLE.
    # The sidecar now queues, so this retry is the second line of defence for a
    # genuine overload rather than the primary fix.
    payload = None
    last_detail = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with source.open("rb") as handle:
                response = requests.post(
                    f"{_soffice_url()}/convert",
                    params={"to": target},
                    files={"file": (source.name, handle)},
                    timeout=_timeout_seconds(),
                )
            # getattr, not attribute access: a response without a status_code
            # simply has no retry signal and falls through to raise_for_status.
            # Requiring the attribute made this crash on every caller whose
            # response object did not carry one.
            status_code = getattr(response, "status_code", None)
            if status_code in RETRYABLE_STATUS and attempt < RETRY_ATTEMPTS:
                last_detail = f"HTTP {status_code} (busy)"
                # M3 failure-at-source. On 2026-08-08 nine of a user's eleven
                # case authorities were shed with HTTP 503 in 0.0s and the only
                # trace was "unreadable" in the answer; the incident had to be
                # reproduced by hand to be seen at all. Name is a filename, so
                # only its extension is logged.
                _log.warning(
                    "sidecar_busy service=soffice status=%s attempt=%d/%d ext=%s",
                    status_code, attempt, RETRY_ATTEMPTS, source.suffix.lower())
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            response.raise_for_status()
            payload = response.content
            break
        except Exception as exc:
            # Keep the status code. "HTTPError" alone cannot tell a busy
            # sidecar from a crashed one, which is exactly the ambiguity that
            # made the 2026-08-05 failures unreadable in the transcript.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            last_detail = f"{type(exc).__name__}{f' HTTP {status}' if status else ''}"
            if status in RETRYABLE_STATUS and attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise ExtractionError(
                f"soffice conversion failed for {source.name}: {last_detail}"
            ) from exc
    if payload is None:
        raise ExtractionError(
            f"soffice conversion failed for {source.name}: {last_detail} "
            f"after {RETRY_ATTEMPTS} attempts"
        )

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
