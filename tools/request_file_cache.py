"""Per-request memo of files already read, so a re-read costs nothing.

Why this exists
---------------
A live 46-file audit issued 96 ``read_file`` calls that resolved to only 18
distinct files: the first five were each re-read 10-14 times inside a single
turn, and every repeat pushed the file's full text back into the prompt.

``file_tools`` already has a ``(path, offset, limit)`` dedup that returns a
stub for an unchanged re-read and escalates to a hard block after two stubs.
It did not fire once during that incident, because the structured-document
extraction branch (PDF / DOCX / XLSX / MSG / notebooks) returns at
``file_tools.py`` ~L1211 while the dedup check lives at ~L1258 — every file
type this feature exists to serve returns before the dedup is reached.

Rather than reorder that branch and its escalation state, the memo here sits
in the ``read_file_tool`` wrapper, ahead of every branch, so no future early
return can bypass it. It is request-scoped rather than task-scoped, which the
existing tracker is not.

The loop guardrail also noticed the repeats — 48 ``idempotent_no_progress``
warnings — but warnings are advisory and the model ignored all of them. Advice
a model can ignore is not a control. This module makes the *result* cheap
instead: repeats return a short acknowledgement pointing at the earlier tool
result, which is still in the model's context for that turn. The call is not
blocked, because blocking a read the model believes it needs produces a
different failure (it invents a workaround); it is answered truthfully and
without the payload.

Scope is one request, entered alongside the file-grant scope. Turn boundaries
matter: a later turn may run in a fresh agent session whose context no longer
contains the earlier tool result, so a memo that outlived its request would
point at something the model genuinely cannot see.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional


_CACHE: ContextVar[dict[str, dict[tuple[str, int, int], dict[str, object]]] | None] = (
    ContextVar("request_file_read_cache", default=None)
)
_DOCUMENT_CACHE: ContextVar[dict[str, dict[str, dict[str, object]]] | None] = (
    ContextVar("request_document_extract_cache", default=None)
)

# A memo is metadata only (never content), so this bound exists to stop a
# pathological turn from growing the dict without limit, not to save memory.
MAX_MEMOS_PER_REQUEST = 512
MAX_DOCUMENTS_PER_REQUEST = 64
MAX_DOCUMENT_CACHE_CHARS_PER_REQUEST = 16_000_000


def _task_key(task_id: str) -> str:
    return str(task_id or "default")


def _path_key(path: str) -> str:
    """Canonical form, so a handle and its literal path share one memo."""
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return str(path)


@contextmanager
def request_file_cache_scope(task_id: str) -> Iterator[None]:
    """Bind an empty read memo to *task_id* for the lifetime of one request."""
    key = _task_key(task_id)
    current = _CACHE.get() or {}
    updated = dict(current)
    updated[key] = {}
    token = _CACHE.set(updated)
    current_documents = _DOCUMENT_CACHE.get() or {}
    updated_documents = dict(current_documents)
    updated_documents[key] = {}
    document_token = _DOCUMENT_CACHE.set(updated_documents)
    try:
        yield
    finally:
        _DOCUMENT_CACHE.reset(document_token)
        _CACHE.reset(token)


def invalidate(task_id: str) -> int:
    """Forget every memo for *task_id*, returning how many were dropped.

    A memo says "the text is in the earlier tool result above". Context
    compression can delete or summarise that result mid-request, at which point
    the statement is false and the model has no way to recover the content — it
    would have to guess at different pagination arguments. Compression therefore
    invalidates the memo: the next identical read costs one real read again,
    which is the correct price for not lying about what is in context.
    """
    memos = _memos(task_id)
    documents = _documents(task_id)
    dropped = len(memos or {}) + len(documents or {})
    if memos:
        memos.clear()
    if documents:
        documents.clear()
    return dropped


def _memos(task_id: str) -> Optional[dict[tuple[str, int, int], dict[str, object]]]:
    return (_CACHE.get() or {}).get(_task_key(task_id))


def _documents(task_id: str) -> Optional[dict[str, dict[str, object]]]:
    return (_DOCUMENT_CACHE.get() or {}).get(_task_key(task_id))


def is_active(task_id: str) -> bool:
    """True when a request scope is open for *task_id*."""
    return _memos(task_id) is not None


def lookup(path: str, offset: int, limit: int, *, task_id: str) -> Optional[dict]:
    """Return the memo for an identical earlier read, or None."""
    memos = _memos(task_id)
    if memos is None:
        return None
    return memos.get((_path_key(path), int(offset), int(limit)))


def lookup_latest(path: str, *, task_id: str) -> Optional[dict]:
    """Return metadata for the most recent read of *path*, at any range.

    The attachment ledger reports progress independent of the caller's chosen
    pagination. Exact repeat suppression still uses :func:`lookup`.
    """
    memos = _memos(task_id)
    if memos is None:
        return None
    canonical = _path_key(path)
    matches = [memo for (memo_path, _offset, _limit), memo in memos.items() if memo_path == canonical]
    return max(matches, key=lambda memo: int(memo.get("sequence") or 0), default=None)


def lookup_document(path: str, *, task_id: str) -> Optional[dict[str, object]]:
    """Return a request-local full document extraction for pagination."""
    documents = _documents(task_id)
    if documents is None:
        return None
    return documents.get(_path_key(path))


def remember_document(
    path: str,
    *,
    task_id: str,
    text: str,
    file_size: int,
    gaps: list[str] | None = None,
) -> bool:
    """Cache one extraction so later offsets do not rerun Docling/anydoc."""
    documents = _documents(task_id)
    if documents is None:
        return False
    key = _path_key(path)
    if key in documents:
        return True
    if len(documents) >= MAX_DOCUMENTS_PER_REQUEST:
        return False
    used_chars = sum(len(str(row.get("text") or "")) for row in documents.values())
    if used_chars + len(text) > MAX_DOCUMENT_CACHE_CHARS_PER_REQUEST:
        return False
    documents[key] = {
        "text": text,
        "file_size": int(file_size),
        "gaps": list(gaps or []),
    }
    return True


def remember(
    path: str,
    offset: int,
    limit: int,
    *,
    task_id: str,
    content: str,
    display_name: str = "",
) -> None:
    """Record that *path* was read, keeping a digest rather than the text."""
    memos = _memos(task_id)
    if memos is None or len(memos) >= MAX_MEMOS_PER_REQUEST:
        return
    memos[(_path_key(path), int(offset), int(limit))] = {
        "sequence": len(memos) + 1,
        "chars": len(content),
        "lines": content.count("\n") + 1 if content else 0,
        "digest": hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:12],
        "display_name": display_name or str(path).rsplit("/", 1)[-1],
    }


def repeat_notice(memo: dict, *, handle: str = "") -> dict:
    """Build the payload returned for a repeated identical read."""
    label = handle or memo.get("display_name") or "this file"
    return {
        "success": True,
        "already_read": True,
        "note": (
            f"{label} was already read earlier in this turn (read #"
            f"{memo.get('sequence')}, {memo.get('chars')} chars, "
            f"{memo.get('lines')} lines). Its full text is in the earlier "
            "tool result above — scroll back and use it. Content is omitted "
            "here so the same text is not repeated in context. If you need a "
            "different part of the file, call read_file again with a different "
            "offset."
        ),
        "digest": memo.get("digest"),
        "chars": memo.get("chars"),
        "lines": memo.get("lines"),
    }


__all__ = [
    "MAX_DOCUMENTS_PER_REQUEST",
    "MAX_DOCUMENT_CACHE_CHARS_PER_REQUEST",
    "MAX_MEMOS_PER_REQUEST",
    "invalidate",
    "is_active",
    "lookup",
    "lookup_latest",
    "lookup_document",
    "remember",
    "remember_document",
    "repeat_notice",
    "request_file_cache_scope",
]
