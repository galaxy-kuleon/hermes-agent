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

import json
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

# A-channel (Kimi delivery-channels §4): VISIBLE markdown in assistant body.
# Do NOT wrap in HTML comments — comments do not render in OWUI.
COVERAGE_FOOTER_TITLE = "## Attachment coverage (authoritative)"
COVERAGE_FOOTER_BEGIN = COVERAGE_FOOTER_TITLE
COVERAGE_FOOTER_END = ""
COVERAGE_UNAVAILABLE_TITLE = "## Attachment coverage unavailable"
COVERAGE_UNAVAILABLE_BEGIN = COVERAGE_UNAVAILABLE_TITLE
COVERAGE_UNAVAILABLE_END = ""
COVERAGE_UNAVAILABLE_TEXT = (
    f"{COVERAGE_UNAVAILABLE_TITLE}\n\n"
    "Coverage could not be verified for this turn. "
    "Do not treat the answer as complete coverage of attachments.\n"
)
# Shown when the turn was interrupted but ledger still has facts to leave.
COVERAGE_INTERRUPTED_NOTE = (
    "- note: turn **interrupted** — coverage reflects progress at stop time, not a full audit.\n"
)

# Mutation-sensitive tokens — production adapters must call these by name.
FINALIZE_COVERAGE_FN = "finalize_attachment_coverage"
DELIVER_COVERAGE_FN = "deliver_coverage_to_persistent_body"


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


# Gap markers that describe HOW MUCH was read rather than what the format cost
# us. The covered ranges already answer them, so once those cover the total the
# marker is resolved and must not keep the attachment partial forever.
#
# `oversize_truncated` used to be classified as a format gap. Observed
# 2026-08-05: a 443-line judgment was read in full across two calls (1-334 then
# 335-443) and the coverage report still said "not fully included ... extent:
# ranges=[[1, 443]] total=443 -- gaps: oversize_truncated". The ledger knew the
# whole document had been read and told the reader otherwise, which is the
# honesty defect inverted -- and it lands exactly when the model did the right
# thing and continued.
_EXTENT_GAP_PREFIXES = ("uncovered_lines=",)
_EXTENT_GAP_MARKERS = frozenset({"unknown_total", "oversize_truncated"})


def is_extent_gap(gap: object) -> bool:
    """True for gaps the covered ranges can resolve on their own."""
    s = str(gap)
    return s in _EXTENT_GAP_MARKERS or s.startswith(_EXTENT_GAP_PREFIXES)


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
        merged_gaps = list(dict.fromkeys([*(prev.get("gaps") or []), *(gaps or [])]))
        if total is not None and ranges_cover_total(ranges, total):
            # This block answers one question only: did we end up reading it
            # all? If so, extent gaps are settled. Whether any FORMAT gap still
            # makes it partial is the next block's job -- deciding it twice is
            # how the two ended up disagreeing.
            gaps = [g for g in merged_gaps if not is_extent_gap(g)]
        else:
            status = OUTCOME_PARTIAL
            gaps = merged_gaps

    # Format gaps always keep partial even if lines cover.
    if gaps or (prev.get("gaps") and status == OUTCOME_READ):
        # only force partial when format gaps are still present
        fmt_gaps = list(dict.fromkeys([*(prev.get("gaps") or []), *(gaps or [])]))
        # strip pure extent gap notes when fully covered and no format gaps remain
        format_only = [g for g in fmt_gaps if g and not is_extent_gap(g)]
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


def seed_from_tool_history(
    messages: list[dict[str, Any]], *, task_id: str
) -> dict[str, int]:
    """Rebuild current-request coverage from durable prior read_file rows.

    File grants are request-scoped, but a conversation may deliberately read a
    large attachment over many user turns.  SessionDB already durably stores
    both each read_file call (including its stable F01 handle) and its result.
    Rehydrate that evidence into the fresh ledger instead of asking the model
    to remember offsets across turns or context compression.
    """
    from tools.file_grants import list_file_handles

    handle_paths = dict(list_file_handles(task_id) or [])
    call_handles: dict[str, str] = {}
    for message in messages or []:
        calls = message.get("tool_calls") or []
        if isinstance(calls, str):
            try:
                calls = json.loads(calls)
            except (TypeError, ValueError, json.JSONDecodeError):
                calls = []
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") or {}
            if function.get("name") != "read_file":
                continue
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError, json.JSONDecodeError):
                    arguments = {}
            raw_path = str(
                (arguments or {}).get("path")
                or (arguments or {}).get("file_path")
                or (arguments or {}).get("file")
                or ""
            ).strip()
            handle_match = re.fullmatch(r"#?(F\d{1,4})", raw_path, re.IGNORECASE)
            if handle_match:
                call_handles[str(call.get("id") or "")] = handle_match.group(1).upper()

    stats = {"tool_results": 0, "seeded_extents": 0, "seeded_unreadable": 0}
    decoder = json.JSONDecoder()
    for message in messages or []:
        if message.get("role") != "tool" or message.get("tool_name") != "read_file":
            continue
        handle = call_handles.get(str(message.get("tool_call_id") or ""), "")
        path = handle_paths.get(handle)
        if not path:
            continue
        try:
            result, _ = decoder.raw_decode(str(message.get("content") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict):
            continue
        stats["tool_results"] += 1
        if result.get("report_as") == OUTCOME_UNREADABLE or result.get("extraction_failed"):
            record_outcome(
                path,
                task_id=task_id,
                status=OUTCOME_UNREADABLE,
                reason=str(result.get("error") or result.get("reason") or "prior extraction failed"),
                gaps=[str(gap) for gap in result.get("gaps") or []],
                handle=handle,
                reader="read_file_history",
            )
            stats["seeded_unreadable"] += 1
            continue
        consumed = result.get("consumed") or {}
        if consumed.get("start") is None or consumed.get("end") is None:
            continue
        format_gaps = [
            str(gap)
            for gap in result.get("gaps") or []
            if not is_extent_gap(gap)
        ]
        record_read_extent(
            path,
            task_id=task_id,
            start=int(consumed["start"]),
            end=int(consumed["end"]),
            total=(
                int(consumed["total"])
                if consumed.get("total") is not None
                else None
            ),
            unit=str(consumed.get("unit") or "lines"),
            format_gaps=format_gaps,
            reason="rehydrated from durable read_file history",
            handle=handle,
            reader="read_file_history",
        )
        stats["seeded_extents"] += 1
    return stats


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
    interrupted: bool = False,
) -> str:
    """Return a deterministic visible markdown footer when incomplete; else empty."""
    snap = coverage_snapshot(handles, task_id=task_id)
    if snap["complete"] or snap["total"] == 0:
        return ""

    lines = [
        "",
        COVERAGE_FOOTER_TITLE,
        "",
        "The following attachments were **not fully included** in this answer.",
        "Do not treat this report as complete coverage of those materials.",
        "",
    ]
    if interrupted:
        lines.append(COVERAGE_INTERRUPTED_NOTE.rstrip())
        lines.append("")
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

    **Interrupted turns still get coverage** (U1D batch-1): interrupt is when
    the user most needs to know what was / was not read — not a reason to omit.

    Returns ``(response_text, meta)`` with footer / snapshot / status.
    """
    meta: dict[str, Any] = {
        "footer": "",
        "snapshot": None,
        "status": "skipped",
        "interrupted": bool(interrupted),
    }
    if not is_active(task_id):
        return final_response, meta

    text = final_response if final_response is not None else ""
    try:
        from tools.file_grants import list_file_handles

        handles = list(list_file_handles(task_id) or [])
    except Exception:
        if fail_closed:
            footer = COVERAGE_UNAVAILABLE_TEXT
            if interrupted:
                footer = footer.rstrip() + "\n" + COVERAGE_INTERRUPTED_NOTE
            # This is the one authoritative finalizer call. Model prose that
            # happens to contain a coverage heading is not proof that this
            # mechanism-produced footer was appended.
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
        footer = build_coverage_footer(
            handles, task_id=task_id, interrupted=bool(interrupted)
        )
    except Exception:
        if fail_closed:
            footer = COVERAGE_UNAVAILABLE_TEXT
            if interrupted:
                footer = footer.rstrip() + "\n" + COVERAGE_INTERRUPTED_NOTE
            # This is the one authoritative finalizer call. Model prose that
            # happens to contain a coverage heading is not proof that this
            # mechanism-produced footer was appended.
            text = (text.rstrip() + "\n" + footer) if text else footer
            meta.update(footer=footer, status="unavailable")
            return text, meta
        return final_response, meta

    if not footer:
        meta["status"] = "complete"
        return final_response, meta

    text = (text.rstrip() + "\n" + footer) if text else footer
    meta.update(footer=footer, status="ok")
    return text, meta


def terminal_coverage_suffix(
    result: dict[str, Any] | None,
    *,
    emitted_coverage_footer: str | None = None,
) -> str:
    """Return the structured footer unless this exact footer was emitted.

    ``emitted_coverage_footer`` is provenance supplied by the writer after a
    successful mechanism-owned write. Arbitrary model text is never inspected.
    """
    if not isinstance(result, dict):
        return ""
    footer = result.get("coverage_footer") or ""
    if not isinstance(footer, str) or not footer.strip():
        return ""
    if emitted_coverage_footer == footer:
        return ""
    return footer if footer.startswith("\n") else "\n" + footer.lstrip("\n")


def deliver_coverage_to_persistent_body(
    *,
    final_response: str | None,
    task_id: str,
    streamed_text: str = "",
    stream: bool = True,
    interrupted: bool = False,
    failed: bool = False,
    fail_closed: bool = True,
) -> dict[str, Any]:
    """Single production adapter: finalize coverage → persistent assistant body.

    Used by:
      * turn_finalizer (always, including interrupted)
      * chat-completions streaming terminal (before stop/[DONE])
      * Responses streaming terminal
      * non-stream JSON responses (body = final_response)

    Returns dict with final_response, coverage_footer, coverage_status,
    stream_suffix, persistent_assistant_body (A-channel truth for DB/reload).

    Mutation target: production must call this by name (DELIVER_COVERAGE_FN);
    replacing it with identity that omits footer must red boundary tests.
    """
    text, meta = finalize_attachment_coverage(
        final_response,
        task_id=task_id,
        interrupted=bool(interrupted),
        fail_closed=fail_closed,
    )
    footer = (meta or {}).get("footer") or ""
    status = (meta or {}).get("status") or "skipped"
    result = {
        "final_response": text,
        "coverage_footer": footer,
        "attachment_coverage": (meta or {}).get("snapshot"),
        "coverage_status": status,
        "interrupted": bool(interrupted),
        "failed": bool(failed),
    }
    if stream:
        suffix = terminal_coverage_suffix(result)
        result["stream_suffix"] = suffix
        # Persistent body: prefer full final_response (includes footer);
        # if model streamed partial text without footer, body is still final_response.
        persistent = text if text is not None else ""
        if not persistent and suffix:
            persistent = (streamed_text or "") + suffix
        result["persistent_assistant_body"] = persistent
    else:
        result["stream_suffix"] = ""
        result["persistent_assistant_body"] = text if text is not None else ""
    return result


__all__ = [
    "COVERAGE_FOOTER_BEGIN",
    "COVERAGE_FOOTER_END",
    "COVERAGE_FOOTER_TITLE",
    "COVERAGE_INTERRUPTED_NOTE",
    "COVERAGE_UNAVAILABLE_BEGIN",
    "COVERAGE_UNAVAILABLE_END",
    "COVERAGE_UNAVAILABLE_TEXT",
    "DELIVER_COVERAGE_FN",
    "FINALIZE_COVERAGE_FN",
    "OUTCOME_PARTIAL",
    "OUTCOME_PENDING",
    "OUTCOME_READ",
    "OUTCOME_UNREADABLE",
    "append_coverage_footer",
    "attachment_ledger_scope",
    "build_coverage_footer",
    "coverage_snapshot",
    "deliver_coverage_to_persistent_body",
    "finalize_attachment_coverage",
    "get_outcome",
    "is_active",
    "merge_ranges",
    "ranges_cover_total",
    "record_outcome",
    "record_read_extent",
    "seed_from_tool_history",
    "seed_extension_unreadable",
    "terminal_coverage_suffix",
    "uncovered_summary",
]
