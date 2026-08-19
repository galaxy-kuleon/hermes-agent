"""`attachments` — what was attached to this request, and what has been read.

Why this exists
---------------
A 46-file audit on 8083 read 18 files, re-read five of them 10-14 times, never
touched the other 28, and told the user nothing was wrong. The model had no way
to answer "which files do I have?" or "which have I already read?" except by
scanning its own transcript, which is exactly the bookkeeping a language model
is worst at and a program is best at.

The skill in use tried to compensate by instructing the model to store pending
file paths in its memory tool and retrieve them later. That cannot work across
a turn boundary: a new attachment set changes the session hash, so the next
turn runs in a fresh session with none of that memory.

This tool answers both questions from request state that already exists — the
grant alias table and the read memo — so coverage becomes a fact the model can
look up rather than something it has to remember.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools import request_file_cache
from tools.file_grants import list_file_handles
from tools.file_reader_routing import reader_guidance as _reader_guidance


# Path B basenames are "<3-digit ordinal>-<8-hex nonce>-<sanitised name>".
_HANDOFF_NAME_PREFIX = re.compile(r"^\d{3}-[0-9a-f]{8}-")

def _display_name(path: str) -> str:
    return _HANDOFF_NAME_PREFIX.sub("", Path(path).name, count=1) or Path(path).name


def attachments_tool(task_id: str = "default") -> str:
    """Return the attachment ledger for the current request."""
    handles = list_file_handles(task_id)
    if not handles:
        return json.dumps(
            {
                "success": True,
                "total": 0,
                "note": (
                    "No files are attached to this request. Do not guess file "
                    "names or paths — ask the user to attach them."
                ),
                "files": [],
            },
            ensure_ascii=False,
        )

    from tools.file_reader_routing import (
        READ_WITH_UNSUPPORTED,
        UNREADABLE_REPORT_INSTRUCTION,
    )
    from tools.attachment_ledger import (
        OUTCOME_PARTIAL,
        OUTCOME_PENDING,
        OUTCOME_READ,
        OUTCOME_UNREADABLE,
        coverage_snapshot,
        get_outcome,
    )

    files = []
    for handle, path in handles:
        memo = None
        if request_file_cache.is_active(task_id):
            # Whole-file reads are the default shape; a paged read still counts
            # as "started" but is reported by its own range, not as complete.
            memo = request_file_cache.lookup_latest(path, task_id=task_id)
        entry = {
            "id": handle,
            "name": _display_name(path),
        }
        entry["read_with"], entry["read_instruction"] = _reader_guidance(
            handle,
            path,
        )
        # reader_available = route exists; outcome = authoritative execution result.
        entry["reader_available"] = entry["read_with"] != READ_WITH_UNSUPPORTED
        outcome = get_outcome(path, task_id=task_id) or {}
        status = outcome.get("status") or OUTCOME_PENDING
        entry["status"] = status
        entry["report_as"] = status
        # Backward-compatible fields (honest): readable means FULL coverage only.
        entry["readable"] = status == OUTCOME_READ
        entry["read"] = status in {OUTCOME_READ, OUTCOME_PARTIAL}
        if outcome.get("reason"):
            entry["reason"] = outcome["reason"]
        if outcome.get("gaps"):
            entry["gaps"] = outcome["gaps"]
        if memo is not None and status in {OUTCOME_READ, OUTCOME_PARTIAL}:
            entry["chars"] = memo.get("chars")
            entry["lines"] = memo.get("lines")
        files.append(entry)

    snap = coverage_snapshot(handles, task_id=task_id)
    unread_ids = [row["id"] for row in snap["pending"]]
    unreadable_ids = [row["id"] for row in snap["unreadable"]]
    partial_ids = [row["id"] for row in snap["partial"]]
    payload = {
        "success": True,
        "total": len(files),
        "read": len(snap["read"]),
        "partial": len(snap["partial"]),
        "partial_ids": partial_ids,
        "unread": len(unread_ids),
        "unread_ids": unread_ids,
        "unreadable": len(unreadable_ids),
        "unreadable_ids": unreadable_ids,
        "files": files,
        "complete": snap["complete"],
    }
    note_parts: list[str] = []
    if unreadable_ids:
        names = ", ".join(
            f'{e["id"]}({e["name"]})'
            for e in files
            if e["id"] in unreadable_ids
        )[:500]
        note_parts.append(
            f"{len(unreadable_ids)} of {len(files)} attached file(s) are "
            f"unreadable: {names}. {UNREADABLE_REPORT_INSTRUCTION}"
        )
    if partial_ids:
        note_parts.append(
            f"{len(partial_ids)} file(s) are only partially extracted "
            f"({', '.join(partial_ids[:12])}). Name them as partial in any "
            "final report; do not claim full coverage."
        )
    if unread_ids:
        note_parts.append(
            f"{len(unread_ids)} of {len(files)} attached files have not been "
            f"read yet, starting with {unread_ids[0]}. Follow each file's "
            "read_with and read_instruction fields; do not send vision_analyze "
            "or unsupported attachments to read_file first. Do not report on a "
            "file you have not read, and do not claim full coverage while this "
            "list is non-empty."
        )
    elif snap["complete"]:
        note_parts.append(
            "Every attached file has full coverage in this request. Repeating "
            "a read returns a short memo instead of the text."
        )
    elif not unread_ids:
        note_parts.append(
            "No pending unread files remain, but partial/unreadable entries "
            "must still appear in the final report."
        )
    payload["note"] = " ".join(note_parts)
    return json.dumps(payload, ensure_ascii=False)


ATTACHMENTS_SCHEMA = {
    "name": "attachments",
    "description": (
        "List the files attached to the current request and which of them you "
        "have already read. Each file says whether to use read_file, "
        "vision_analyze, or no direct reader (readable=false / unreadable), "
        "including an exact instruction. Unreadable attachments must be named "
        "as unreadable in any final report — never invent their content. "
        "Returns each file's short id and name, plus unread_ids and "
        "unreadable_ids. Call this before claiming an audit or summary is "
        "complete, and whenever you are unsure which files you were given — "
        "it is authoritative, unlike your recollection of earlier turns. "
        "Takes no arguments."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _handle_attachments(args, **kw):
    return attachments_tool(task_id=kw.get("task_id") or "default")


__all__ = [
    "ATTACHMENTS_SCHEMA",
    "_handle_attachments",
    "attachments_tool",
]
