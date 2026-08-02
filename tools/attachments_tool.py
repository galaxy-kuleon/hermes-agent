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

    files = []
    read_count = 0
    for handle, path in handles:
        memo = None
        if request_file_cache.is_active(task_id):
            # Whole-file reads are the default shape; a paged read still counts
            # as "started" but is reported by its own range, not as complete.
            memo = request_file_cache.lookup(path, 1, 500, task_id=task_id)
        entry = {
            "id": handle,
            "name": _display_name(path),
            "read": memo is not None,
        }
        entry["read_with"], entry["read_instruction"] = _reader_guidance(
            handle,
            path,
        )
        if memo is not None:
            read_count += 1
            entry["chars"] = memo.get("chars")
            entry["lines"] = memo.get("lines")
        files.append(entry)

    unread = [entry["id"] for entry in files if not entry["read"]]
    payload = {
        "success": True,
        "total": len(files),
        "read": read_count,
        "unread": len(unread),
        "unread_ids": unread,
        "files": files,
    }
    if unread:
        payload["note"] = (
            f"{len(unread)} of {len(files)} attached files have not been read "
            f"yet, starting with {unread[0]}. Follow each file's read_with and "
            "read_instruction fields; do not send vision_analyze or unsupported "
            "attachments to read_file first. Do not report on a file you have "
            "not read, and do not claim full coverage while this list is "
            "non-empty."
        )
    else:
        payload["note"] = (
            "Every attached file has been read in this request. Repeating a "
            "read returns a short memo instead of the text."
        )
    return json.dumps(payload, ensure_ascii=False)


ATTACHMENTS_SCHEMA = {
    "name": "attachments",
    "description": (
        "List the files attached to the current request and which of them you "
        "have already read. Each file says whether to use read_file, "
        "vision_analyze, or no direct reader, including an exact instruction. "
        "Returns each file's short id and name, plus the ids still unread. Call "
        "this before claiming an audit or summary is complete, and whenever "
        "you are unsure which files you were given — it is authoritative, "
        "unlike your recollection of earlier turns. Takes no arguments."
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
