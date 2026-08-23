"""Fail-closed turn-end gate for Hong Kong statutory legal answers.

Prompt policy is advisory: a small model can follow a stale user skill and
silently skip official authority.  This module keeps the mechanism separate
from that policy.  It inspects only the active user turn and only accepts an
answer after a successful ``hk_legal_authority`` result from that same turn is
cited with its official URL and version date.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


MAX_AUTHORITY_NUDGES = 2

_HK_RE = re.compile(r"(?:\bhong\s+kong\b|\bhk\b|香港)", re.IGNORECASE)
_LEGAL_RE = re.compile(
    r"(?:trade\s*marks?|trademarks?|商標|商标|法例|法律|條例|条例|規則|规则|"
    r"\bordinance\b|\bstatute\b|\blegal\b|\blaw\b|\bsection\s+\d|"
    r"\brule\s+\d|\bcap\.?\s*\d)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateDecision:
    action: str
    message: str = ""


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
        )
    return ""


def is_hk_statutory_query(user_message: Any) -> bool:
    """Conservatively identify requests that can create HK-law reliance."""
    text = _message_text(user_message)
    return bool(_HK_RE.search(text) and _LEGAL_RE.search(text))


def _json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _json_objects(item)
        return
    if not isinstance(value, str):
        return
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return
    yield from _json_objects(decoded)


def successful_authorities(
    messages: list[Any], *, current_turn_user_idx: int
) -> list[dict[str, str]]:
    """Return verified official authority results from this user turn only."""
    if current_turn_user_idx < 0:
        return []
    authorities: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages[current_turn_user_idx + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool" or message.get("name") != "hk_legal_authority":
            continue
        for result in _json_objects(message.get("content")):
            url = str(result.get("official_web_url") or "").strip()
            version = str(result.get("version_date") or "").strip()
            chapter = str(result.get("chapter") or "").strip()
            if (
                result.get("success") is True
                and result.get("cannot_confirm") is False
                and url.startswith("https://www.elegislation.gov.hk/")
                and version
                and chapter
            ):
                key = (chapter, version, url)
                if key not in seen:
                    seen.add(key)
                    authorities.append(
                        {
                            "chapter": chapter,
                            "version_date": version,
                            "official_web_url": url,
                            "required_answer_citation": str(
                                result.get("required_answer_citation") or ""
                            ).strip(),
                        }
                    )
    return authorities


def _answer_cites_authorities(answer: str, authorities: list[dict[str, str]]) -> bool:
    return all(
        authority["official_web_url"] in answer
        and authority["version_date"] in answer
        for authority in authorities
    )


def evaluate_hk_legal_answer(
    *,
    messages: list[Any],
    current_turn_user_idx: int,
    final_response: str,
    attempts: int,
    max_attempts: int = MAX_AUTHORITY_NUDGES,
) -> GateDecision:
    """Return pass, nudge, or fail for a candidate final response."""
    if not (0 <= current_turn_user_idx < len(messages)):
        return GateDecision("pass")
    if not is_hk_statutory_query(messages[current_turn_user_idx]):
        return GateDecision("pass")

    authorities = successful_authorities(
        messages, current_turn_user_idx=current_turn_user_idx
    )
    if not authorities:
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: This Hong Kong statutory-law answer is not grounded in "
                "a successful official HKeL result from the current user turn. "
                "Call hk_legal_authority now for every provision relied on. Treat "
                "user skills and OpenViking as non-authoritative. If the official "
                "tool cannot confirm the law, return cannot-confirm and do not "
                "repeat the ungrounded answer.]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港法例結論：本回合未能成功讀取香港電子法例的官方現行文本。"
            "為免誤導，我不會重複未經官方法源確認的條文、期限、表格、費用或救濟建議。",
        )

    if not _answer_cites_authorities(final_response, authorities):
        citations = "\n".join(
            authority["required_answer_citation"]
            or (
                f"Hong Kong e-Legislation, Cap. {authority['chapter']}, current "
                f"version {authority['version_date']}: {authority['official_web_url']}"
            )
            for authority in authorities
        )
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: The current-turn official HKeL lookup succeeded, but "
                "your proposed answer omitted its official URL or version date. "
                "Rewrite the complete answer and visibly include every citation "
                "below. Do not alter the verified provision meanings.\n"
                f"{citations}]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港法例結論：雖然已讀取官方文本，但最終答案未能保留可核對的"
            "香港電子法例網址及版本日期。為免誤導，本次不提供未完整引用的法律結論。",
        )

    return GateDecision("pass")
