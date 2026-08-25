"""Truthful terminal answers for retained-evidence memory deletion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MAX_MEMORY_DELETION_NUDGES = 1

_ACTIVE_PROJECTION_RE = re.compile(
    r"(?:active\s+(?:memory|projection|view)|memory\s+projection).{0,100}"
    r"(?:remov|delet|hidden|no\s+longer\s+used)|"
    r"(?:remov|delet|hidden).{0,100}(?:active\s+(?:memory|projection|view)|"
    r"memory\s+projection)",
    re.IGNORECASE | re.DOTALL,
)
_RETAINED_ORIGINAL_RE = re.compile(
    r"(?:original|raw|source|content|evidence).{0,120}"
    r"(?:retain|preserv|kept|remain)|"
    r"(?:retain|preserv|kept|remain).{0,120}"
    r"(?:original|raw|source|content|evidence)",
    re.IGNORECASE | re.DOTALL,
)
_FALSE_PHYSICAL_DELETION_RE = re.compile(
    r"(?:permanent(?:ly)?|irreversibl(?:e|y)|forever).{0,80}"
    r"(?:delet|remov|eras|gone)|"
    r"(?:delet|remov|eras|gone).{0,80}"
    r"(?:permanent(?:ly)?|irreversibl(?:e|y)|forever)|"
    r"cannot\s+be\s+(?:recovered|retrieved)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class MemoryDeletionDecision:
    action: str
    message: str
    diagnostics: tuple[str, ...] = ()


def _retained_deletion_receipts(
    messages: list[Any], current_turn_user_idx: int
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for message in messages[current_turn_user_idx + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool" or message.get("name") != "viking_forget":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("status") == "deleted"
            and payload.get("active_projection_removed") is True
            and str(payload.get("evidence_uri") or "").strip()
        ):
            receipts.append(payload)
    return receipts


def evaluate_memory_deletion_answer(
    *,
    messages: list[Any],
    current_turn_user_idx: int,
    final_response: str,
    attempts: int,
) -> MemoryDeletionDecision | None:
    receipts = _retained_deletion_receipts(messages, current_turn_user_idx)
    if not receipts:
        return None
    diagnostics: list[str] = []
    if not _ACTIVE_PROJECTION_RE.search(final_response):
        diagnostics.append("active projection removal is not stated")
    if not _RETAINED_ORIGINAL_RE.search(final_response):
        diagnostics.append("retained original evidence is not stated")
    if _FALSE_PHYSICAL_DELETION_RE.search(final_response):
        diagnostics.append("answer falsely claims permanent physical deletion")
    if not diagnostics:
        return None
    if attempts < MAX_MEMORY_DELETION_NUDGES:
        return MemoryDeletionDecision(
            "nudge",
            "[System: The memory deletion tool removed only the active memory "
            "projection and retained the complete original in isolated deletion "
            "evidence. State both facts plainly. Do not say it was permanently, "
            "irreversibly, or physically erased. Preserve any other policy boundary "
            "already required by the user's request.]",
            tuple(diagnostics),
        )
    return MemoryDeletionDecision(
        "replace",
        "The requested memory has been removed from the active memory projection, "
        "so it will no longer be used as active memory. Its complete original "
        "content remains retained in isolated deletion evidence for authorized "
        "audit and replay. Any skill-first preference is retained only in a "
        "compatible form: it does not disable `hk_legal_authority`, which remains "
        "mandatory for Hong Kong statutory legal conclusions.",
        tuple(diagnostics),
    )
