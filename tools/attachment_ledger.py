"""Request-scoped attachment coverage ledger (authoritative, not advisory).

Model-visible instructions are not a control (#50). This ledger records the
outcome of each attached file for the current request:

* ``pending`` — not yet settled by a reader attempt
* ``read`` — fully covered by a successful extraction/text read
* ``partial`` — content returned but known gaps remain (e.g. MSG body-only)
* ``unreadable`` — no reader, extraction failed, or magic/type mismatch

``attachments()`` and the turn finalizer both read this store. Extraction
failures write here so a later ledger call cannot re-label the same file as
still ``readable/unread``.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Optional

OUTCOME_PENDING = "pending"
OUTCOME_READ = "read"
OUTCOME_PARTIAL = "partial"
OUTCOME_UNREADABLE = "unreadable"

_VALID = frozenset(
    {OUTCOME_PENDING, OUTCOME_READ, OUTCOME_PARTIAL, OUTCOME_UNREADABLE}
)

_OUTCOMES: ContextVar[dict[str, dict[str, dict[str, Any]]] | None] = ContextVar(
    "attachment_outcomes",
    default=None,
)

_HANDOFF_NAME_PREFIX = re.compile(r"^\d{3}-[0-9a-f]{8}-")

# Footer markers for mutation tests — must appear when coverage is incomplete.
COVERAGE_FOOTER_BEGIN = "<!-- hermes-attachment-coverage -->"
COVERAGE_FOOTER_END = "<!-- /hermes-attachment-coverage -->"
COVERAGE_FOOTER_TITLE = "## Attachment coverage (authoritative)"


def _task_key(task_id: str) -> str:
    return str(task_id or "default")


def _path_key(path: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return str(path)


def _display_name(path: str) -> str:
    return _HANDOFF_NAME_PREFIX.sub("", Path(path).name, count=1) or Path(path).name


@contextmanager
def attachment_ledger_scope(task_id: str) -> Iterator[None]:
    """Bind an empty outcome map for *task_id* for one request."""
    key = _task_key(task_id)
    current = _OUTCOMES.get() or {}
    updated = dict(current)
    updated[key] = {}
    token = _OUTCOMES.set(updated)
    try:
        yield
    finally:
        _OUTCOMES.reset(token)


def is_active(task_id: str) -> bool:
    store = _OUTCOMES.get() or {}
    return _task_key(task_id) in store


def _store(task_id: str) -> Optional[dict[str, dict[str, Any]]]:
    return (_OUTCOMES.get() or {}).get(_task_key(task_id))


def record_outcome(
    path: str,
    *,
    task_id: str,
    status: str,
    reason: str = "",
    gaps: list[str] | None = None,
    display_name: str = "",
    handle: str = "",
) -> None:
    """Write/overwrite the authoritative outcome for *path*."""
    if status not in _VALID:
        raise ValueError(f"invalid attachment outcome status: {status!r}")
    store = _store(task_id)
    if store is None:
        return
    key = _path_key(path)
    prev = store.get(key) or {}
    # Do not downgrade unreadable → pending/read via a later partial open.
    if prev.get("status") == OUTCOME_UNREADABLE and status in {
        OUTCOME_PENDING,
        OUTCOME_READ,
        OUTCOME_PARTIAL,
    }:
        return
    # partial wins over a bare success read if gaps remain.
    if prev.get("status") == OUTCOME_PARTIAL and status == OUTCOME_READ:
        status = OUTCOME_PARTIAL
        gaps = list(dict.fromkeys([*(prev.get("gaps") or []), *(gaps or [])]))
    store[key] = {
        "status": status,
        "reason": (reason or "")[:300],
        "gaps": list(gaps or prev.get("gaps") or []),
        "display_name": display_name or prev.get("display_name") or _display_name(path),
        "handle": handle or prev.get("handle") or "",
        "path_key": key,
    }


def get_outcome(path: str, *, task_id: str) -> Optional[dict[str, Any]]:
    store = _store(task_id)
    if store is None:
        return None
    return store.get(_path_key(path))


def seed_extension_unreadable(
    path: str,
    *,
    task_id: str,
    handle: str = "",
    reason: str = "no direct reader for this attachment type",
) -> None:
    record_outcome(
        path,
        task_id=task_id,
        status=OUTCOME_UNREADABLE,
        reason=reason,
        handle=handle,
        display_name=_display_name(path),
    )


def coverage_snapshot(
    handles: list[tuple[str, str]],
    *,
    task_id: str,
) -> dict[str, Any]:
    """Build coverage buckets for *handles* ``[(handle, path), ...]``."""
    pending: list[dict[str, str]] = []
    read: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    unreadable: list[dict[str, str]] = []

    for handle, path in handles:
        name = _display_name(path)
        outcome = get_outcome(path, task_id=task_id)
        status = (outcome or {}).get("status") or OUTCOME_PENDING
        row = {
            "id": handle,
            "name": name,
            "status": status,
            "reason": (outcome or {}).get("reason") or "",
            "gaps": list((outcome or {}).get("gaps") or []),
        }
        if status == OUTCOME_READ:
            read.append(row)
        elif status == OUTCOME_PARTIAL:
            partial.append(row)
        elif status == OUTCOME_UNREADABLE:
            unreadable.append(row)
        else:
            pending.append(row)

    incomplete = pending + partial + unreadable
    return {
        "total": len(handles),
        "read": read,
        "partial": partial,
        "unreadable": unreadable,
        "pending": pending,
        "incomplete": incomplete,
        "complete": not incomplete,
    }


def build_coverage_footer(
    handles: list[tuple[str, str]],
    *,
    task_id: str,
) -> str:
    """Return a deterministic footer when coverage is incomplete; else empty.

    Mutation target: removing this function call from the finalizer, or
    dropping an incomplete entry, must make tests red.
    """
    snap = coverage_snapshot(handles, task_id=task_id)
    if snap["complete"] or snap["total"] == 0:
        return ""

    lines = [
        "",
        COVERAGE_FOOTER_BEGIN,
        COVERAGE_FOOTER_TITLE,
        "",
        "The following attachments were **not fully included** in this answer.",
        "Do not treat this report as complete coverage of those materials.",
        "",
    ]
    for row in snap["incomplete"]:
        bits = [f"- `{row['id']}` ({row['name']}): **{row['status']}**"]
        if row.get("reason"):
            bits.append(f"— {row['reason']}")
        if row.get("gaps"):
            bits.append(f"— gaps: {', '.join(row['gaps'])}")
        lines.append(" ".join(bits))
    lines.extend(
        [
            "",
            f"Summary: read={len(snap['read'])} partial={len(snap['partial'])} "
            f"unreadable={len(snap['unreadable'])} unread={len(snap['pending'])} "
            f"total={snap['total']}.",
            COVERAGE_FOOTER_END,
            "",
        ]
    )
    return "\n".join(lines)


def append_coverage_footer(
    final_response: str,
    *,
    task_id: str,
) -> str:
    """Append coverage footer when grants+ledger are active and incomplete."""
    if not is_active(task_id):
        return final_response
    try:
        from tools.file_grants import list_file_handles
    except Exception:
        return final_response
    handles = list_file_handles(task_id)
    if not handles:
        return final_response
    # list_file_handles returns [(handle, path), ...] in handle order.
    footer = build_coverage_footer(list(handles), task_id=task_id)
    if not footer:
        return final_response
    text = final_response or ""
    if COVERAGE_FOOTER_BEGIN in text:
        return text
    return text.rstrip() + "\n" + footer


__all__ = [
    "COVERAGE_FOOTER_BEGIN",
    "COVERAGE_FOOTER_END",
    "COVERAGE_FOOTER_TITLE",
    "OUTCOME_PARTIAL",
    "OUTCOME_PENDING",
    "OUTCOME_READ",
    "OUTCOME_UNREADABLE",
    "append_coverage_footer",
    "attachment_ledger_scope",
    "build_coverage_footer",
    "coverage_snapshot",
    "get_outcome",
    "is_active",
    "record_outcome",
    "seed_extension_unreadable",
]
