"""Persistent, content-addressed cache for document text extraction.

``request_file_cache`` deliberately forgets response memos at the end of a
request because those memos point at text in that request's model context.
The expensive extraction itself has no such constraint: extracted text is a
pure function of the source bytes, document suffix, and extractor contract.

This cache stores that derived value under the active ``HERMES_HOME``. It
never consults ``HOME`` directly, so container-user and Hermes-profile home
semantics cannot silently choose different stores. A new request still gets
the requested page of text; only Docling/AnyDoc/OOXML conversion is reused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

# Bump this named contract revision whenever extraction semantics change in a
# way that makes earlier text unsafe to reuse. Deployments may override it when
# upgrading an out-of-process extractor such as Docling independently.
DEFAULT_EXTRACT_CACHE_REVISION = "2026-08-23-v1"
EXTRACT_CACHE_REVISION = os.environ.get(
    "HERMES_DOCUMENT_EXTRACT_CACHE_REVISION",
    DEFAULT_EXTRACT_CACHE_REVISION,
).strip() or DEFAULT_EXTRACT_CACHE_REVISION
MAX_PERSISTED_DOCUMENT_CHARS = int(
    os.environ.get("HERMES_DOCUMENT_EXTRACT_CACHE_MAX_CHARS", "16000000")
)
_CACHE_DIRECTORY_NAME = "document-extractions"
_CACHE_FILE_SUFFIX = ".json"
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _normalise_suffix(suffix: str) -> str:
    value = str(suffix or "").strip().lower()
    if value and not value.startswith("."):
        value = "." + value
    return value


def _cache_key(source_sha256: str, suffix: str) -> str:
    identity = "\0".join(
        (EXTRACT_CACHE_REVISION, _normalise_suffix(suffix), source_sha256.lower())
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _cache_dir() -> Path:
    return get_hermes_home() / "cache" / _CACHE_DIRECTORY_NAME / EXTRACT_CACHE_REVISION


def _cache_path(source_sha256: str, suffix: str) -> Path:
    return _cache_dir() / (_cache_key(source_sha256, suffix) + _CACHE_FILE_SUFFIX)


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@contextmanager
def extraction_lock(source_sha256: str, suffix: str) -> Iterator[None]:
    """Serialise same-process extraction for one immutable source identity."""
    with _lock_for(_cache_path(source_sha256, suffix)):
        yield


def lookup(source_sha256: str, suffix: str) -> dict[str, object] | None:
    """Return a validated cached extraction, or ``None`` on miss/corruption."""
    if not source_sha256:
        return None
    path = _cache_path(source_sha256, suffix)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "document_extract_cache_invalid path=%s reason=%s", path, str(exc)
        )
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("revision") != EXTRACT_CACHE_REVISION:
        return None
    if payload.get("source_sha256") != source_sha256.lower():
        return None
    if payload.get("suffix") != _normalise_suffix(suffix):
        return None
    if not isinstance(payload.get("text"), str):
        return None
    gaps = payload.get("gaps")
    if not isinstance(gaps, list) or not all(isinstance(item, str) for item in gaps):
        return None
    try:
        payload["file_size"] = int(payload.get("file_size") or 0)
    except (TypeError, ValueError):
        return None
    logger.info(
        "document_extract_cache_hit source_sha256=%s suffix=%s chars=%s",
        source_sha256,
        _normalise_suffix(suffix),
        len(payload["text"]),
    )
    return payload


def remember(
    source_sha256: str,
    suffix: str,
    *,
    text: str,
    file_size: int,
    gaps: list[str] | None = None,
) -> bool:
    """Atomically persist one immutable extraction; cache failure is harmless."""
    if not source_sha256 or len(text) > MAX_PERSISTED_DOCUMENT_CHARS:
        return False
    path = _cache_path(source_sha256, suffix)
    payload = {
        "revision": EXTRACT_CACHE_REVISION,
        "source_sha256": source_sha256.lower(),
        "suffix": _normalise_suffix(suffix),
        "file_size": int(file_size),
        "gaps": list(gaps or []),
        "text": text,
    }
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
        temp_path = None
        logger.info(
            "document_extract_cache_store source_sha256=%s suffix=%s chars=%s",
            source_sha256,
            _normalise_suffix(suffix),
            len(text),
        )
        return True
    except OSError as exc:
        logger.warning(
            "document_extract_cache_store_failed path=%s reason=%s", path, str(exc)
        )
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "DEFAULT_EXTRACT_CACHE_REVISION",
    "EXTRACT_CACHE_REVISION",
    "MAX_PERSISTED_DOCUMENT_CHARS",
    "extraction_lock",
    "lookup",
    "remember",
]
