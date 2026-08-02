"""Request-scoped attachment coverage ledger (authoritative, not advisory).

Model-visible instructions are not a control (#50). This ledger records the
outcome of each attached file for the current request:

* ``pending`` — not yet settled by a reader attempt
* ``read`` — fully covered by successful extraction/text/vision (extent covers total)
* ``partial`` — content returned but known gaps remain (format gaps or incomplete extent)
* ``unreadable`` — no reader, extraction failed, or magic/type mismatch

``attachments()`` and the turn finalizer both read this store. Extraction
failures write here so a later ledger call cannot re-label the same file as
still ``readable/unread``.

Consumed extent (M-U1-D round 2 BLOCKING-2): line/page ranges are merged;
status is ``read`` only when the union covers ``1..total``. Truncated /
paginated reads stay ``partial``.
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
COVERAGE_UNAVAILABLE_BEGIN = "<!-- hermes-attachment-coverage-unavailable -->"
COVERAGE_UNAVAILABLE_END = "<!-- /hermes-attachment-coverage-unavailable -->"
COVERAGE_UNAVAILABLE_TEXT = (
    f"{COVERAGE_UNAVAILABLE_BEGIN}\n"
    "## Attachment coverage unavailable\n\n"
    "Coverage could not be verified for this turn. "
    "Do not treat the answer as complete coverage of attachments.\n"
    f"{COVERAGE_UNAVAILABLE_END}\n"
)

# Mutation-sensitive token — production finalizer must call this by name.
FINALIZE_COVERAGE_FN = "finalize_attachment_coverage"


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


def _normalize_range(start: int, end: int) -> tuple[int, int] | None:
    try:
        s = int(start)
        e = int(end)
    except (TypeError, ValueError):
        return None
    if e < s:
        s, e = e, s
    if s < 1:
        s = 1
    if e < s:
        return None
    return s, e


def merge_ranges(ranges: list[list[int] | tuple[int, int]]) -> list[list[int]]:
    """Merge inclusive 1-based ranges into a sorted disjoint list."""
    norm: list[tuple[int, int]] = []
    for r in ranges or []:
        if not r or len(r) < 2:
            continue
        pair = _normalize_range(r[0], r[1])
        if pair:
            norm.append(pair)
    if not norm:
        return []
    norm.sort()
    merged: list[list[int]] = [[norm[0][0], norm[0][1]]]
    for s, e in norm[1:]:
        last = merged[-1]
        if s <= last[1] + 1:
            last[1] = max(last[1], e)
        else:
            merged.append([s, e])
    return merged


def ranges_cover_total(ranges: list[list[int]], total: int | None) -> bool:
    """True iff merged ranges cover every unit in 1..total inclusive."""
    if total is None or total <= 0:
        return False
    merged = merge_ranges(ranges)
    if not merged:
        return False
    if merged[0][0] > 1:
        return False
    cursor = 0
    for s, e in merged:
        if s > cursor + 1:
            return False
        cursor = max(cursor, e)
        if cursor >= total:
            return True
    return cursor >= total


def uncovered_summary(ranges: list[list[int]], total: int | None) -> str:
    if total is None or total <= 0:
        return "unknown_total"
    merged = merge_ranges(ranges)
    gaps: list[str] = []
    cursor = 1
    for s, e in merged:
        if s > cursor:
            gaps.append(f"{cursor}-{s - 1}")
        cursor = max(cursor, e + 1)
    if cursor <= total:
        gaps.append(f"{cursor}-{total}")
    if not gaps:
        return ""
    return "uncovered_lines=" + ",".join(gaps)


def record_outcome(
    path: str,
    *,
    task_id: str,
    status: str,
    reason: str = "",
    gaps: list[str] | None = None,
    display_name: str = "",
    handle: str = "",
    extent: dict[str, Any] | None = None,
    reader: str = "",
) -> None:
    """Write/overwrite the authoritative outcome for *path*.

    *extent* (optional)::
        {"unit": "lines"|"pages", "total": int|None,
         "start": int, "end": int}  or  "ranges": [[s,e], ...]
    When *status* is ``read`` but extent does not cover total, status is
    forced to ``partial`` (BLOCKING-2).
    """
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

    unit = "lines"
    total: int | None = prev.get("extent_total")
    ranges = list(prev.get("ranges") or [])
    if isinstance(extent, dict):
        unit = str(extent.get("unit") or unit)
        if extent.get("total") is not None:
            try:
                total = int(extent["total"])
            except (TypeError, ValueError):
                total = None
        if extent.get("ranges"):
            ranges.extend(extent["ranges"])  # type: ignore[arg-type]
        elif extent.get("start") is not None and extent.get("end") is not None:
            ranges.append([extent["start"], extent["end"]])
    ranges = merge_ranges(ranges)

    # Force partial when claimed complete but extent incomplete.
    if status == OUTCOME_READ:
        if total is None and ranges:
            # Have ranges but unknown total → cannot claim complete.
            status = OUTCOME_PARTIAL
            reason = reason or "extent_incomplete_unknown_total"
        elif total is not None and not ranges_cover_total(ranges, total):
            status = OUTCOME_PARTIAL
            reason = reason or "extent_incomplete"
            gap_note = uncovered_summary(ranges, total)
            gaps = list(dict.fromkeys([*(gaps or []), gap_note] if gap_note else (gaps or [])))

    # partial wins over a bare success read if gaps remain.
    if prev.get("status") == OUTCOME_PARTIAL and status == OUTCOME_READ:
        if total is not None and ranges_cover_total(ranges, total) and not (
            gaps or prev.get("gaps")
        ):
            pass  # allow upgrade when ranges now cover and no format gaps
        else:
            status = OUTCOME_PARTIAL
            gaps = list(dict.fromkeys([*(prev.get("gaps") or []), *(gaps or [])]))

    # Format gaps always keep partial even if lines cover.
    if gaps or (prev.get("gaps") and status == OUTCOME_READ):
        # only force partial when format gaps are still present
        fmt_gaps = list(dict.fromkeys([*(prev.get("gaps") or []), *(gaps or [])]))
        # strip pure extent gap notes when fully covered and no format gaps remain
        format_only = [
            g
            for g in fmt_gaps
            if g
            and not str(g).startswith("uncovered_lines=")
            and g != "unknown_total"
        ]
        if format_only and status == OUTCOME_READ:
            status = OUTCOME_PARTIAL
            gaps = format_only

    store[key] = {
        "status": status,
        "reason": (reason or "")[:300],
        "gaps": list(gaps if gaps is not None else prev.get("gaps") or []),
        "display_name": display_name or prev.get("display_name") or _display_name(path),
        "handle": handle or prev.get("handle") or "",
        "path_key": key,
        "reader": reader or prev.get("reader") or "",
        "extent_unit": unit,
        "extent_total": total,
        "ranges": ranges,
        "extent_summary": (
            f"{unit} ranges={ranges} total={total}" if ranges or total is not None else ""
        ),
    }


def record_read_extent(
    path: str,
    *,
    task_id: str,
    start: int,
    end: int,
    total: int | None,
    unit: str = "lines",
    format_gaps: list[str] | None = None,
    reason: str = "",
    display_name: str = "",
    handle: str = "",
    reader: str = "read_file",
) -> str:
    """Record a successful reader page; return settled status (read|partial)."""
    status = OUTCOME_READ
    gaps = list(format_gaps or [])
    if gaps:
        status = OUTCOME_PARTIAL
        reason = reason or "extraction incomplete for this format"
    extent = {
        "unit": unit,
        "total": total,
        "start": start,
        "end": end,
    }
    record_outcome(
        path,
        task_id=task_id,
        status=status,
        reason=reason or ("extracted" if not gaps else "partial"),
        gaps=gaps,
        display_name=display_name,
        handle=handle,
        extent=extent,
        reader=reader,
    )
    out = get_outcome(path, task_id=task_id) or {}
    return str(out.get("status") or status)


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
    pending: list[dict[str, Any]] = []
    read: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []

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
            "reader": (outcome or {}).get("reader") or "",
            "extent": (outcome or {}).get("extent_summary") or "",
            "ranges": list((outcome or {}).get("ranges") or []),
            "extent_total": (outcome or {}).get("extent_total"),
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
        if row.get("extent"):
            bits.append(f"— extent: {row['extent']}")
        if row.get("gaps"):
            bits.append(f"— gaps: {', '.join(str(g) for g in row['gaps'] if g)}")
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
    fail_closed: bool = True,
) -> str:
    """Append coverage footer when grants+ledger are active and incomplete.

    When the ledger is active but coverage cannot be computed, append a
    non-identifying unavailable notice (fail closed) instead of silent pass.
    """
    text, _meta = finalize_attachment_coverage(
        final_response,
        task_id=task_id,
        interrupted=False,
        fail_closed=fail_closed,
    )
    return text


def finalize_attachment_coverage(
    final_response: str | None,
    *,
    task_id: str,
    interrupted: bool = False,
    fail_closed: bool = True,
) -> tuple[str | None, dict[str, Any]]:
    """Last-step coverage gate (must run after plugins / explainers).

    Returns ``(response_text, meta)`` where meta includes::
        footer: str  (coverage or unavailable block actually appended)
        snapshot: dict | None
        status: ok|complete|skipped|unavailable
    """
    meta: dict[str, Any] = {
        "footer": "",
        "snapshot": None,
        "status": "skipped",
    }
    if interrupted:
        return final_response, meta
    if not is_active(task_id):
        return final_response, meta

    text = final_response if final_response is not None else ""
    try:
        from tools.file_grants import list_file_handles

        handles = list(list_file_handles(task_id) or [])
    except Exception:
        if fail_closed:
            footer = COVERAGE_UNAVAILABLE_TEXT
            if COVERAGE_UNAVAILABLE_BEGIN not in text and COVERAGE_FOOTER_BEGIN not in text:
                text = (text.rstrip() + "\n" + footer) if text else footer
            meta.update(footer=footer, status="unavailable")
            return text, meta
        return final_response, meta

    if not handles:
        meta["status"] = "complete"
        return final_response, meta

    try:
        snap = coverage_snapshot(handles, task_id=task_id)
        meta["snapshot"] = snap
        footer = build_coverage_footer(handles, task_id=task_id)
    except Exception:
        if fail_closed:
            footer = COVERAGE_UNAVAILABLE_TEXT
            if COVERAGE_UNAVAILABLE_BEGIN not in text and COVERAGE_FOOTER_BEGIN not in text:
                text = (text.rstrip() + "\n" + footer) if text else footer
            meta.update(footer=footer, status="unavailable")
            return text, meta
        return final_response, meta

    if not footer:
        meta["status"] = "complete"
        return final_response, meta

    if COVERAGE_FOOTER_BEGIN in text:
        meta.update(footer=footer, status="ok")
        return text, meta
    text = (text.rstrip() + "\n" + footer) if text else footer
    meta.update(footer=footer, status="ok")
    return text, meta


def terminal_coverage_suffix(
    streamed_text: str,
    result: dict[str, Any] | None,
) -> str:
    """Coverage-only suffix that streaming adapters emit before stop/[DONE].

    Prefers structured ``coverage_footer`` on the agent result (survives
    plugins that rewrote ``final_response`` prose). Never re-emits the full
    model answer — only the mechanism-produced coverage / unavailable block.
    """
    if not isinstance(result, dict):
        return ""
    streamed = streamed_text or ""
    footer = result.get("coverage_footer") or ""
    if isinstance(footer, str) and footer.strip():
        # Already on the wire (e.g. non-stream path doubled) — skip.
        if COVERAGE_FOOTER_BEGIN in streamed or COVERAGE_UNAVAILABLE_BEGIN in streamed:
            if footer.strip() in streamed or COVERAGE_FOOTER_BEGIN in streamed:
                return ""
        return footer if footer.startswith("\n") else "\n" + footer.lstrip("\n")

    # Fallback: scrape markers out of final_response if structured field empty.
    final = result.get("final_response") or ""
    if not isinstance(final, str) or not final:
        return ""
    for marker in (COVERAGE_FOOTER_BEGIN, COVERAGE_UNAVAILABLE_BEGIN):
        idx = final.find(marker)
        if idx >= 0 and marker not in streamed:
            return ("\n" if not final[idx:].startswith("\n") else "") + final[idx:]
    return ""


__all__ = [
    "COVERAGE_FOOTER_BEGIN",
    "COVERAGE_FOOTER_END",
    "COVERAGE_FOOTER_TITLE",
    "COVERAGE_UNAVAILABLE_BEGIN",
    "COVERAGE_UNAVAILABLE_END",
    "COVERAGE_UNAVAILABLE_TEXT",
    "FINALIZE_COVERAGE_FN",
    "OUTCOME_PARTIAL",
    "OUTCOME_PENDING",
    "OUTCOME_READ",
    "OUTCOME_UNREADABLE",
    "append_coverage_footer",
    "attachment_ledger_scope",
    "build_coverage_footer",
    "coverage_snapshot",
    "finalize_attachment_coverage",
    "get_outcome",
    "is_active",
    "merge_ranges",
    "ranges_cover_total",
    "record_outcome",
    "record_read_extent",
    "seed_extension_unreadable",
    "terminal_coverage_suffix",
    "uncovered_summary",
]
