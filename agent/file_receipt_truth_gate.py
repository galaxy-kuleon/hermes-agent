"""Keep terminal file claims aligned with structured read receipts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MAX_FILE_RECEIPT_TRUTH_NUDGES = 2

_INCOMPLETE_CLAIM_RE = re.compile(
    r"truncat|partial(?:ly)?|incomplete|omitted|did not come through|"
    r"didn['’]t come through|only the opening|not (?:read|returned) in full|"
    r"無法完整|未完整|截斷|截断|不完整",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FileReceiptTruthDecision:
    action: str
    message: str
    diagnostics: tuple[str, ...] = ()


def _complete_read_receipts(
    messages: list[Any], current_turn_user_idx: int
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for message in messages[current_turn_user_idx + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool" or message.get("name") != "read_file":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        consumed = payload.get("consumed") or {}
        complete_extent = (
            consumed.get("total") is not None
            and consumed.get("end") == consumed.get("total")
        )
        if (
            payload.get("truncated") is False
            and payload.get("readable") is True
            and payload.get("report_as") == "read"
            and complete_extent
        ):
            receipts.append(payload)
    return receipts


def _receipt_anchors(receipt: dict[str, Any]) -> tuple[str, ...]:
    source = receipt.get("source") or {}
    values = [
        str(source.get("request_handle") or "").strip(),
        str(receipt.get("name") or "").strip(),
    ]
    anchors: list[str] = []
    for value in values:
        if not value:
            continue
        anchors.append(re.escape(value))
        stem = value.rsplit(".", 1)[0]
        if stem and stem != value:
            anchors.append(re.escape(stem))
    return tuple(dict.fromkeys(anchors))


def _false_incomplete_claim(
    final_response: str, receipt: dict[str, Any]
) -> bool:
    anchors = _receipt_anchors(receipt)
    if not anchors:
        return False
    anchor = "(?:" + "|".join(anchors) + ")"
    incomplete = _INCOMPLETE_CLAIM_RE.pattern
    return bool(
        re.search(
            rf"(?:{anchor}).{{0,240}}(?:{incomplete})|"
            rf"(?:{incomplete}).{{0,240}}(?:{anchor})",
            final_response,
            re.IGNORECASE | re.DOTALL,
        )
    )


def evaluate_file_receipt_truth(
    *,
    messages: list[Any],
    current_turn_user_idx: int,
    final_response: str,
    attempts: int,
    import_files_available: bool,
) -> FileReceiptTruthDecision | None:
    receipts = _complete_read_receipts(messages, current_turn_user_idx)
    contradicted = [
        receipt for receipt in receipts if _false_incomplete_claim(final_response, receipt)
    ]
    if not contradicted:
        return None
    labels = tuple(
        str((receipt.get("source") or {}).get("request_handle") or receipt.get("name"))
        for receipt in contradicted
    )
    diagnostics = tuple(
        f"{label}: structured receipt says complete but answer says incomplete"
        for label in labels
    )
    if attempts < MAX_FILE_RECEIPT_TRUTH_NUDGES:
        continuation = (
            " For a requested portable skill library, use one skill_manage "
            "import_files call for the complete attachment-handle list, or publish "
            "with namespace='platform' and source_paths to import and share it in "
            "one coherent workflow."
            if import_files_available
            else " Continue the requested task from the complete receipt."
        )
        return FileReceiptTruthDecision(
            "nudge",
            "[System: The structured read_file receipt for "
            + ", ".join(labels)
            + " says truncated=false, readable=true, report_as=read, and consumed "
            "end=total. Do not treat UI display compaction as document truncation. "
            "Do not ask the user to reattach the file or start a fresh session on "
            "that false premise."
            + continuation
            + "]",
            diagnostics,
        )
    return FileReceiptTruthDecision(
        "replace",
        "The latest structured file receipt confirms the requested file was read "
        "completely; the earlier claim that it was truncated was incorrect. I have "
        "not completed the requested packaging, so I will not claim that the skill "
        "is portable yet. The existing attachment remains usable in this session "
        "and does not need to be reattached merely because the UI shortened the "
        "displayed tool preview.",
        diagnostics,
    )


__all__ = [
    "FileReceiptTruthDecision",
    "MAX_FILE_RECEIPT_TRUTH_NUDGES",
    "evaluate_file_receipt_truth",
]
