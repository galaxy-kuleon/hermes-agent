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


MAX_AUTHORITY_NUDGES = 3

_HK_RE = re.compile(r"(?:\bhong\s+kong\b|\bhk\b|香港)", re.IGNORECASE)
_LEGAL_RE = re.compile(
    r"(?:trade\s*marks?|trademarks?|商標|商标|法例|法律|條例|条例|規則|规则|"
    r"\bordinance\b|\bstatute\b|\blegal\b|\blaw\b|\bsection\s+\d|"
    r"\brule\s+\d|\bcap\.?\s*\d)",
    re.IGNORECASE,
)
_TRADE_MARK_RE = re.compile(r"(?:trade\s*marks?|trademarks?|商標|商标)", re.IGNORECASE)
_REGISTERED_RE = re.compile(r"(?:registered|registration|註冊|注册)", re.IGNORECASE)
_REGISTERED_MARK_MINIMUM = {"559": frozenset({"11", "12", "52", "53"})}
_SECTION_12_6_RE = re.compile(r"(?:section\s*)?12\s*\(\s*6\s*\)", re.IGNORECASE)
_SECTION_53_5_B_RE = re.compile(
    r"(?:section\s*)?53\s*\(\s*5\s*\)\s*\(\s*b\s*\)", re.IGNORECASE
)
_NOT_BAR_RE = re.compile(
    r"(?:does\s+not|doesn't|not)\s+(?:bar|prevent|exclude|preclude)|"
    r"(?:still|remains?)\s+(?:available|open)|"
    r"不(?:妨礙|阻止|排除|限制)|仍(?:可|然可以)|不影響",
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
) -> list[dict[str, Any]]:
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
                            "provisions": tuple(
                                str(row.get("provision") or "").strip()
                                for row in (result.get("requested_provisions") or [])
                                if isinstance(row, dict) and row.get("found") is True
                            ),
                        }
                    )
    return authorities


def _citation_date(version_date: str) -> str:
    """Return the public date used by HKeL's required answer citation."""
    return version_date.split("T", 1)[0]


def _answer_cites_authorities(answer: str, authorities: list[dict[str, Any]]) -> bool:
    return all(
        authority["official_web_url"] in answer
        and _citation_date(authority["version_date"]) in answer
        for authority in authorities
    )


def _minimum_provisions(user_message: Any) -> dict[str, frozenset[str]]:
    text = _message_text(user_message)
    if _TRADE_MARK_RE.search(text) and _REGISTERED_RE.search(text):
        return _REGISTERED_MARK_MINIMUM
    return {}


def _missing_minimum_provisions(
    user_message: Any, authorities: list[dict[str, Any]]
) -> dict[str, list[str]]:
    covered: dict[str, set[str]] = {}
    for authority in authorities:
        covered.setdefault(authority["chapter"], set()).update(authority["provisions"])
    return {
        chapter: sorted(required - covered.get(chapter, set()), key=lambda x: int(x))
        for chapter, required in _minimum_provisions(user_message).items()
        if required - covered.get(chapter, set())
    }


def _confuses_opposition_with_post_registration_invalidity(
    user_message: Any, answer: str
) -> bool:
    """Catch the recurrent s.12(6) versus s.53(5)(b) category error."""
    text = _message_text(user_message)
    if not (_TRADE_MARK_RE.search(text) and _REGISTERED_RE.search(text)):
        return False
    if not _SECTION_12_6_RE.search(answer):
        return False
    section_12_6 = _SECTION_12_6_RE.search(answer)
    assert section_12_6 is not None
    window_start = max(0, section_12_6.start() - 500)
    window_end = min(len(answer), section_12_6.end() + 700)
    window = answer[window_start:window_end]
    return not (_SECTION_53_5_B_RE.search(window) and _NOT_BAR_RE.search(window))


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

    missing = _missing_minimum_provisions(
        messages[current_turn_user_idx], authorities
    )
    if missing:
        calls = "; ".join(
            f"chapter='{chapter}', provisions={provisions}"
            for chapter, provisions in missing.items()
        )
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: The official lookup was incomplete for this registered "
                "Hong Kong trade-mark dispute. Use hk_legal_authority to read the "
                "missing minimum provisions before answering: "
                f"{calls}. Then give one complete answer grounded in all successful "
                "current-turn results, including each official URL and version date.]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港商標法結論：本回合未能完整讀取已註冊商標爭議所需的"
            "最低官方法源集合。為免誤導，本次不提供不完整的救濟建議。",
        )

    if _confuses_opposition_with_post_registration_invalidity(
        messages[current_turn_user_idx], final_response
    ):
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: Correct a material legal distinction in the complete "
                "answer. Section 12(6) governs refusal on section 12(4)/(5) "
                "grounds at the opposition stage. It must not be presented as "
                "barring the separate post-registration invalidity route that "
                "section 53(5)(b) expressly provides. Either omit the unnecessary "
                "section 12(6) discussion or explicitly state that it does not bar "
                "a section 53(5)(b) invalidity application. Preserve the official "
                "URL and version citation.]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港商標法結論：最終答案未能正確區分第12(6)條的反對階段"
            "規則與第53(5)(b)條的註冊後無效申請。為免誤導，本次不提供矛盾的救濟建議。",
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
