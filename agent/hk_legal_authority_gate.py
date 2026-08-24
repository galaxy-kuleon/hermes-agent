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
REGISTERED_MARK_REWRITE_NUDGES = 1

_HK_RE = re.compile(r"(?:\bhong\s+kong\b|\bhk\b|香港)", re.IGNORECASE)
_LEGAL_RE = re.compile(
    r"(?:trade\s*marks?|trademarks?|商標|商标|法例|法律|條例|条例|規則|规则|"
    r"\bordinance\b|\bstatute\b|\blegal\b|\blaw\b|\bsection\s+\d|"
    r"\brule\s+\d|\bcap\.?\s*\d)",
    re.IGNORECASE,
)
_LEGAL_REQUEST_RE = re.compile(
    r"(?:\b(?:advise|advice|analyse|analyze|explain|interpret|apply|challenge|"
    r"draft|prepare|review|revise|amend|"
    r"oppose|invalidate)\b|\bwhat\s+can\s+i\s+do\b|\bunder\s+(?:the\s+)?law\b|"
    r"\b(?:what|which|how)\b.{0,80}\b(?:law|legal|ordinance|statute|section|"
    r"rule|trade\s*marks?|trademarks?)\b|法律意見|法律分析|如何|怎樣|怎样|怎麼|"
    r"怎么|甚麼|什么|是否|能否|可否|應否|应否|解釋|解释|分析|查核|救濟|救济|"
    r"侵權|侵权|無效|无效|反對|反对|草擬|草拟|擬備|拟备|起草|審閱|审阅)",
    re.IGNORECASE | re.DOTALL,
)
_TRADE_MARK_RE = re.compile(r"(?:trade\s*marks?|trademarks?|商標|商标)", re.IGNORECASE)
_REGISTERED_RE = re.compile(r"(?:registered|registration|註冊|注册)", re.IGNORECASE)
_REGISTERED_MARK_MINIMUM = {"559": frozenset({"4", "11", "12", "44", "45", "52", "53"})}
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
_FAMOUS_MARK_RE = re.compile(
    r"(?:famous|well[- ]known|reputation|馳名|驰名|知名)", re.IGNORECASE
)
_INVALIDITY_RE = re.compile(
    r"(?:invalid(?:ity)?|declar(?:e|ation).{0,30}invalid|無效|无效|宣告無效|宣告无效)",
    re.IGNORECASE,
)
_REVOCATION_RE = re.compile(
    r"(?:revocation|revoke|non[- ]use|撤銷|撤销|不使用)", re.IGNORECASE
)
_BAD_FAITH_RE = re.compile(r"(?:bad\s+faith|惡意|恶意|不真誠|不真诚)", re.IGNORECASE)
_OPPOSITION_RE = re.compile(r"(?:opposition|oppose|反對|反对|異議|异议)", re.IGNORECASE)
_RECTIFICATION_RE = re.compile(
    r"(?:rectification|rectify|更正|改正|糾正|纠正)", re.IGNORECASE
)
_THREE_YEAR_RE = re.compile(
    r"(?:\b3\s*years?\b|\bthree[- ]year|3\s*年|三\s*年)", re.IGNORECASE
)
_FIVE_YEAR_RE = re.compile(
    r"(?:\b5\s*years?\b|\bfive[- ]year|5\s*年|五\s*年)", re.IGNORECASE
)
_HONG_KONG_WELL_KNOWN_RE = re.compile(
    r"(?:well[- ]known.{0,120}Hong\s+Kong|Hong\s+Kong.{0,120}well[- ]known|"
    r"在香港.{0,80}(?:馳名|驰名)|(?:馳名|驰名).{0,80}香港)",
    re.IGNORECASE | re.DOTALL,
)
_OPEN_OPPOSITION_RE = re.compile(
    r"(?:opposition.{0,100}(?:window|period).{0,80}(?:still|may|might|could).{0,30}open|"
    r"(?:if|whether).{0,80}(?:opposition|window|period).{0,80}(?:open|available)|"
    r"(?:if|when).{0,40}(?:it|the\s+(?:window|period)).{0,30}(?:is|remains).{0,20}open.{0,80}"
    r"(?:file|give).{0,30}(?:notice\s+of\s+)?opposition)",
    re.IGNORECASE | re.DOTALL,
)
_FOREIGN_FAME_INSUFFICIENT_RE = re.compile(
    r"(?=[\s\S]{0,700}(?:Korea|Korean))"
    r"(?=[\s\S]{0,700}(?:Hong\s+Kong|香港))"
    r"(?=[\s\S]{0,700}(?:well[- ]known|馳名|驰名))"
    r"[\s\S]{0,700}(?:not\s+(?:enough|sufficient)|insufficient|does\s+not\s+"
    r"(?:itself\s+)?(?:establish|prove)|must\s+(?:separately\s+)?(?:establish|prove)|"
    r"不足以|並不足夠|并不足够|仍須證明|仍须证明)",
    re.IGNORECASE,
)
_FOREIGN_FAME_OVERSTATEMENT_RE = re.compile(
    r"(?:strong(?:est)?\s+(?:case|ground|route)|decisive|sufficient).{0,260}"
    r"(?:famous|well[- ]known).{0,160}(?:Korea|Korean)|"
    r"(?:Korea|Korean).{0,260}(?:famous|well[- ]known).{0,160}"
    r"(?:strong(?:est)?\s+(?:case|ground|route)|decisive|sufficient)",
    re.IGNORECASE | re.DOTALL,
)
_PRACTICE_GUIDANCE_RE = re.compile(
    r"(?:working\s+manual|work\s+manual|practice\s+manual|registry\s+manual|"
    r"extension\s+of\s+time|time\s+limit|deadline|\blate\b|forgot|rule\s*13|"
    r"工作手冊|實務手冊|实务手册|延期|期限|逾期)",
    re.IGNORECASE,
)
_RULE_13_TIME_RE = re.compile(
    r"(?:extension\s+of\s+time|time\s+limit|deadline|\blate\b|forgot|rule\s*13|"
    r"rule\s*9[56]|延期|期限|逾期)",
    re.IGNORECASE,
)
_FALSE_MANUAL_DELIVERY_RE = re.compile(
    r"(?:manual|ipd|working\s+manual|工作手冊|實務手冊|实务手册).{0,160}"
    r"(?:truncat|unavailable|not\s+found|not\s+read|could\s+not\s+read|"
    r"can(?:not|'t)\s+(?:confirm|verify|read)|未讀|未读|無法讀|无法读|截斷|截断)|"
    r"(?:truncat|unavailable|not\s+found|not\s+read|未讀|未读|無法讀|"
    r"无法读|截斷|截断).{0,160}(?:manual|ipd|工作手冊|實務手冊|实务手册)",
    re.IGNORECASE | re.DOTALL,
)
_SIX_MONTH_RE = re.compile(
    r"(?:\b6\s*months?\b|\bsix[- ]month|6\s*個月|六\s*個月)", re.IGNORECASE
)
_THREE_MONTH_RE = re.compile(
    r"(?:\b3\s*months?\b|\bthree[- ]month|3\s*個月|三\s*個月)", re.IGNORECASE
)
_RULE_13_2_RE = re.compile(r"(?:rule\s*)?13\s*\(\s*2\s*\)", re.IGNORECASE)
_RULE_13_3_RE = re.compile(r"(?:rule\s*)?13\s*\(\s*3\s*\)", re.IGNORECASE)
_SKILL_EDIT_RE = re.compile(
    r"(?:add|update|change|modify|edit|write|patch).{0,100}\bskill\b|"
    r"\bskill\b.{0,100}(?:add|update|change|modify|edit|write|patch)|"
    r"(?:新增|更新|修改|編輯|编辑).{0,100}技能|"
    r"技能.{0,100}(?:新增|更新|修改|編輯|编辑)",
    re.IGNORECASE | re.DOTALL,
)
_ESTATE_DUTY_RE = re.compile(r"(?:estate\s+duty|遺產[稅税]|遗产[税稅])", re.IGNORECASE)
_ESTATE_DUTY_ABOLITION_RE = re.compile(
    r"(?:abolish(?:ed|ment)?|no\s+estate\s+duty|not\s+subject\s+to\s+estate\s+duty|"
    r"廢除|废除|取消|不(?:再)?徵收|不(?:再)?征收)",
    re.IGNORECASE,
)
_ESTATE_DUTY_MINIMUM = {"111": frozenset({"2"})}
_WILL_DOCUMENT_RE = re.compile(
    r"(?:\blast\s+will\b|\bwills?\s+ordinance\b|\bwill\s+template\b|"
    r"\btestament(?:ary)?\b|遺囑|遗嘱)",
    re.IGNORECASE,
)
_DIVORCE_RE = re.compile(
    r"(?:\bdivorc(?:e|ed)\b|\bdissolution\s+of\s+marriage\b|"
    r"\bannul(?:ment|led)\b|\bformer\s+spouse\b|離婚|离婚|婚姻撤銷|婚姻撤销)",
    re.IGNORECASE,
)
_MARRIAGE_RE = re.compile(
    r"(?:\b(?:re)?marri(?:age|ed|es|y)\b|結婚|结婚|再婚)", re.IGNORECASE
)
_WILL_EXECUTION_RE = re.compile(
    r"(?:\bsign(?:ed|ing|ature)?\b|\bexecut(?:e|ed|ion)\b|\bwitness(?:ed|es|ing)?\b|"
    r"簽署|签署|見證|见证)",
    re.IGNORECASE,
)
_FORMER_SPOUSE_PREDECEASED_RE = re.compile(
    r"(?:\bformer\s+spouse\b.{0,180}(?:\bpredeceas(?:e|ed)\b|"
    r"\btreated\s+as\s+(?:having\s+)?died\b)|"
    r"(?:\bpredeceas(?:e|ed)\b|\btreated\s+as\s+(?:having\s+)?died\b)"
    r".{0,180}\bformer\s+spouse\b|前配偶.{0,100}(?:視為先死|视为先死|"
    r"視為已死亡|视为已死亡))",
    re.IGNORECASE | re.DOTALL,
)
_APPOINTMENT_OMITTED_RE = re.compile(
    r"(?:\bappointment\b.{0,180}\bformer\s+spouse\b.{0,180}\bomitt?ed\b|"
    r"\bformer\s+spouse\b.{0,180}\bappointment\b.{0,180}\bomitt?ed\b|"
    r"前配偶.{0,100}(?:遺囑執行人|遗嘱执行人|受託人|受托人).{0,100}(?:略去|刪除|删除))",
    re.IGNORECASE | re.DOTALL,
)
_DEVISE_BEQUEST_LAPSE_RE = re.compile(
    r"(?:\bformer\s+spouse\b.{0,240}\b(?:devise|bequest)\b.{0,140}\blapse[ds]?\b|"
    r"\b(?:devise|bequest)\b.{0,240}\bformer\s+spouse\b.{0,140}\blapse[ds]?\b|"
    r"前配偶.{0,160}(?:遺贈|遗赠).{0,100}(?:失效|無效|无效))",
    re.IGNORECASE | re.DOTALL,
)
_CONTRARY_INTENTION_RE = re.compile(
    r"(?:\bcontrary\s+intention\b|\bunless\b.{0,120}\bwill\b|"
    r"遺囑.{0,80}相反意圖|遗嘱.{0,80}相反意图)",
    re.IGNORECASE | re.DOTALL,
)
_STATUTORY_EXCEPTION_RE = re.compile(
    r"(?:\bsubject\s+to\b.{0,100}\bexceptions?\b|\bstatutory\s+exceptions?\b|"
    r"\bunless\b.{0,160}\b(?:marriage|will)\b|法定例外|除外情況|例外情形)",
    re.IGNORECASE | re.DOTALL,
)
_DIVORCE_EFFECT_CLAIM_RE = re.compile(
    r"(?:(?:\bsection\s*15\b|\bs\.?\s*15\b).{0,240}"
    r"(?:\bdivorc(?:e|ed)\b|\bdissolution\b|\bannul(?:ment|led)\b|"
    r"\bformer\s+spouse\b)|"
    r"(?:\bdivorc(?:e|ed)\b|\bdissolution\b|\bannul(?:ment|led)\b)"
    r".{0,240}(?:\brevoke|\blapse|\bomitt?ed\b|\btake\s+effect\b|"
    r"\bsection\s*15\b|\bs\.?\s*15\b)|"
    r"離婚.{0,160}(?:第\s*15\s*條|撤銷|撤销|失效|略去))",
    re.IGNORECASE | re.DOTALL,
)
_MARRIAGE_EFFECT_CLAIM_RE = re.compile(
    r"(?:(?:\bsection\s*14\b|\bs\.?\s*14\b).{0,240}"
    r"\b(?:re)?marri(?:age|ed|es|y)\b|"
    r"\b(?:re)?marri(?:age|ed|es|y)\b.{0,240}"
    r"(?:\brevoke|\bsection\s*14\b|\bs\.?\s*14\b)|"
    r"(?:結婚|结婚|再婚).{0,160}(?:第\s*14\s*條|撤銷|撤销))",
    re.IGNORECASE | re.DOTALL,
)
_SECTION_4_RE = re.compile(r"(?:(?:section|s\.?)\s*4\b|第\s*4\s*條)", re.IGNORECASE)
_SECTION_5_RE = re.compile(r"(?:(?:section|s\.?)\s*5\b|第\s*5\s*條)", re.IGNORECASE)
_SECTION_14_RE = re.compile(r"(?:(?:section|s\.?)\s*14\b|第\s*14\s*條)", re.IGNORECASE)
_SECTION_15_RE = re.compile(r"(?:(?:section|s\.?)\s*15\b|第\s*15\s*條)", re.IGNORECASE)


@dataclass(frozen=True)
class GateDecision:
    action: str
    message: str = ""
    diagnostics: tuple[str, ...] = ()


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
    if not (_HK_RE.search(text) and _LEGAL_RE.search(text)):
        return False
    return bool(_LEGAL_REQUEST_RE.search(text) or "?" in text or "？" in text)


def is_hk_statutory_turn(messages: list[Any], current_turn_user_idx: int) -> bool:
    """Recognize a same-Matter follow-up even when it omits “Hong Kong”."""
    if not (0 <= current_turn_user_idx < len(messages)):
        return False
    current = messages[current_turn_user_idx]
    current_text = _message_text(current)
    if _SKILL_EDIT_RE.search(current_text):
        return False
    if is_hk_statutory_query(current):
        return True
    if not (
        _LEGAL_RE.search(current_text) or _PRACTICE_GUIDANCE_RE.search(current_text)
    ):
        return False
    prior_text = "\n".join(
        _message_text(message)
        for message in messages[
            max(0, current_turn_user_idx - 8) : current_turn_user_idx
        ]
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
    )
    return bool(_HK_RE.search(prior_text) and _TRADE_MARK_RE.search(prior_text))


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
    authorities: list[dict[str, Any]] = []
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
                authorities.append({
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
                    "provision_texts": tuple(
                        (
                            str(row.get("provision") or "").strip(),
                            str(row.get("text") or "").strip(),
                        )
                        for row in (result.get("requested_provisions") or [])
                        if isinstance(row, dict)
                        and row.get("found") is True
                        and str(row.get("provision") or "").strip()
                    ),
                    "practice_guidance": tuple(
                        {
                            "title": str(guidance.get("title") or "").strip(),
                            "official_url": str(
                                guidance.get("official_url") or ""
                            ).strip(),
                            "pdf_sha256": str(guidance.get("pdf_sha256") or "").strip(),
                            "verified_extracts": tuple(
                                str(extract.get("text") or "").strip()
                                for extract in (guidance.get("verified_extracts") or [])
                                if isinstance(extract, dict)
                                and str(extract.get("text") or "").strip()
                            ),
                        }
                        for guidance in (result.get("official_practice_guidance") or [])
                        if isinstance(guidance, dict)
                        and guidance.get("success") is True
                        and guidance.get("cannot_confirm") is False
                        and guidance.get("matched_page_text_complete") is True
                        and str(guidance.get("official_url") or "").startswith(
                            "https://www.ipd.gov.hk/"
                        )
                        and guidance.get("pdf_sha256")
                        and guidance.get("verified_extracts")
                    ),
                })
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


def _requires_rule_13_practice(user_message: Any) -> bool:
    text = _message_text(user_message)
    return bool(
        _TRADE_MARK_RE.search(text) or _PRACTICE_GUIDANCE_RE.search(text)
    ) and bool(_RULE_13_TIME_RE.search(text))


def _verified_practice_guidance(
    authorities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for authority in authorities:
        for guidance in authority.get("practice_guidance") or ():
            key = (guidance["official_url"], guidance["pdf_sha256"])
            if key not in seen:
                seen.add(key)
                rows.append(guidance)
    return rows


def _answer_uses_rule_13_time_evidence(answer: str) -> bool:
    return all(
        pattern.search(answer)
        for pattern in (_RULE_13_2_RE, _RULE_13_3_RE, _SIX_MONTH_RE, _THREE_MONTH_RE)
    )


def _near(
    answer: str, left: re.Pattern[str], right: re.Pattern[str], distance: int = 220
) -> bool:
    return bool(
        re.search(
            f"(?:{left.pattern}).{{0,{distance}}}(?:{right.pattern})",
            answer,
            re.IGNORECASE | re.DOTALL,
        )
        or re.search(
            f"(?:{right.pattern}).{{0,{distance}}}(?:{left.pattern})",
            answer,
            re.IGNORECASE | re.DOTALL,
        )
    )


def _mislabels_as_rectification(answer: str, section: re.Pattern[str]) -> bool:
    for match in section.finditer(answer):
        window = answer[
            max(0, match.start() - 220) : min(len(answer), match.end() + 220)
        ]
        if not _RECTIFICATION_RE.search(window):
            continue
        if re.search(
            r"(?:not|isn't|is\s+not|does\s+not)\s+(?:a\s+)?(?:rectification|rectify)|"
            r"(?:並非|不是|不屬於|不属于).{0,20}(?:更正|改正|糾正|纠正)",
            window,
            re.IGNORECASE | re.DOTALL,
        ):
            continue
        return True
    return False


def _registered_famous_mark_remedy_errors(user_message: Any, answer: str) -> list[str]:
    text = _message_text(user_message)
    if not (
        _TRADE_MARK_RE.search(text)
        and _REGISTERED_RE.search(text)
        and _FAMOUS_MARK_RE.search(text)
    ):
        return []

    section_53 = re.compile(r"(?:section|s\.?|第)?\s*53\b", re.IGNORECASE)
    section_52 = re.compile(r"(?:section|s\.?|第)?\s*52\b", re.IGNORECASE)
    section_44 = re.compile(r"(?:section|s\.?|第)?\s*44\b", re.IGNORECASE)
    section_45 = re.compile(r"(?:section|s\.?|第)?\s*45\b", re.IGNORECASE)
    section_21 = re.compile(r"(?:section|s\.?|第)?\s*21\b", re.IGNORECASE)
    section_11_5_b = re.compile(r"11\s*\(\s*5\s*\)\s*\(\s*b\s*\)", re.IGNORECASE)

    errors = []
    if not _near(answer, _INVALIDITY_RE, section_53):
        errors.append("Section 53 must be identified as declaration of invalidity")
    if not _near(answer, _REVOCATION_RE, section_52):
        errors.append("Section 52 must be identified as revocation/non-use")
    if not _near(answer, _BAD_FAITH_RE, section_11_5_b):
        errors.append("bad faith must be tied to section 11(5)(b)")
    if not _HONG_KONG_WELL_KNOWN_RE.search(answer):
        errors.append("well-known-mark protection must address Hong Kong")
    if not _FOREIGN_FAME_INSUFFICIENT_RE.search(answer):
        errors.append(
            "Korean fame alone is insufficient; Hong Kong well-known status must be proved"
        )
    if _FOREIGN_FAME_OVERSTATEMENT_RE.search(answer):
        errors.append("Korean fame must not be presented as sufficient or decisive")
    if _OPEN_OPPOSITION_RE.search(answer):
        errors.append(
            "once the mark is registered, opposition is no longer a current remedy"
        )
    if _near(answer, _OPPOSITION_RE, section_21):
        errors.append("section 21 is not the opposition provision")
    if _mislabels_as_rectification(answer, section_45):
        errors.append("section 45 is withdrawal by the applicant, not rectification")
    if _mislabels_as_rectification(answer, section_52):
        errors.append("section 52 is revocation, not rectification")
    if _OPPOSITION_RE.search(answer) and not section_44.search(answer):
        errors.append("opposition must be tied to section 44")
    if _REVOCATION_RE.search(answer) and (
        _FIVE_YEAR_RE.search(answer) or not _THREE_YEAR_RE.search(answer)
    ):
        errors.append("section 52 non-use period is 3 years, not 5 years")
    return errors


def _minimum_provisions(
    user_message: Any, final_response: str
) -> dict[str, frozenset[str]]:
    text = _message_text(user_message)
    combined = text + "\n" + (final_response or "")
    minimum: dict[str, set[str]] = {}
    if _TRADE_MARK_RE.search(text) and _REGISTERED_RE.search(text):
        for chapter, provisions in _REGISTERED_MARK_MINIMUM.items():
            minimum.setdefault(chapter, set()).update(provisions)
    # Estate-duty applicability is determined by the deceased's date of death,
    # not by Cap. 111's current-version date. Any HK legal answer that introduces
    # estate duty must therefore read the Ordinance's application provision.
    if _ESTATE_DUTY_RE.search(combined):
        for chapter, provisions in _ESTATE_DUTY_MINIMUM.items():
            minimum.setdefault(chapter, set()).update(provisions)
    # A generated/reviewed will must be checked against the execution provision.
    # Marital-status effects are separate rules and must never be inferred from
    # the execution provision or from a model's general legal memory.
    if _WILL_DOCUMENT_RE.search(text):
        minimum.setdefault("30", set()).add("5")
        if _DIVORCE_RE.search(combined):
            minimum["30"].add("15")
        if _MARRIAGE_RE.search(combined):
            minimum["30"].add("14")
    return {chapter: frozenset(provisions) for chapter, provisions in minimum.items()}


def _deterministic_registered_mark_answer(
    authorities: list[dict[str, Any]],
) -> str:
    """Render the safe minimum answer after repeated model rewrite failure.

    This is deliberately narrower than a model-authored opinion. Every legal
    route stated here is part of the verified minimum provision bundle above;
    facts or remedies requiring other authority are left as questions for
    counsel instead of being guessed.
    """
    citations: list[str] = []
    for authority in authorities:
        if (
            authority["chapter"] != "559"
            or not set(authority["provisions"]) & _REGISTERED_MARK_MINIMUM["559"]
        ):
            continue
        citation = authority["required_answer_citation"] or (
            f"Hong Kong e-Legislation, Cap. {authority['chapter']}, current "
            f"version {_citation_date(authority['version_date'])}: "
            f"{authority['official_web_url']}"
        )
        if citation not in citations:
            citations.append(citation)
    source_lines = "\n".join(f"- {citation}" for citation in citations)
    return (
        "The mark is already registered, so opposition is no longer the current "
        "route. Section 44 concerns opposition while an application is pending; "
        "section 45 is withdrawal by the applicant, not a remedy you can file.\n\n"
        "Your present statutory route is an application for a declaration of "
        "invalidity under section 53, to the Registrar or the court. The two grounds "
        "that need evidence are:\n"
        "1. Bad faith: section 11(5)(b), applied to an existing registration through "
        "section 53(3). Gather evidence that the Hong Kong applicant knew of your "
        "earlier mark and deliberately copied it.\n"
        "2. Earlier right / well-known mark: sections 12(4) and 12(5), available "
        "post-registration through section 53(5)(b). Under section 4, foreign-market "
        "fame, including Korean-market fame, alone does not prove that the mark was "
        "well known in Hong Kong. You need "
        "Hong Kong evidence such as local sales, advertising, press, customers, or "
        "recognition at the relevant date.\n\n"
        "Section 52 revocation for non-use is only a later fallback: it requires a "
        "continuous period of at least 3 years without genuine use in Hong Kong. A "
        "registration dated 2 July 2026 is therefore far too recent for that ground "
        "on 23 August 2026.\n\n"
        "Next, obtain the official registration record and goods/services, preserve "
        "the side-by-side mark comparison, collect Hong Kong reputation evidence, "
        "and collect proof of the applicant's knowledge and actual Hong Kong use. "
        "Those facts decide which invalidity ground is strongest.\n\n"
        "Verified official source for this answer:\n"
        f"{source_lines}\n\n"
        "This is a bounded statutory route analysis, not a substitute for advice on "
        "pleadings or evidence from Hong Kong trade mark counsel."
    )


def _missing_minimum_provisions(
    user_message: Any,
    final_response: str,
    authorities: list[dict[str, Any]],
) -> dict[str, list[str]]:
    covered: dict[str, set[str]] = {}
    for authority in authorities:
        covered.setdefault(authority["chapter"], set()).update(authority["provisions"])
    return {
        chapter: sorted(required - covered.get(chapter, set()), key=lambda x: int(x))
        for chapter, required in _minimum_provisions(
            user_message, final_response
        ).items()
        if required - covered.get(chapter, set())
    }


def _estate_duty_application_text(
    authorities: list[dict[str, Any]],
) -> str:
    for authority in authorities:
        if authority.get("chapter") != "111":
            continue
        for provision, text in authority.get("provision_texts") or ():
            if provision == "2" and text:
                return text
    return ""


def _estate_duty_application_error(
    answer: str, authorities: list[dict[str, Any]]
) -> str:
    if not (
        _ESTATE_DUTY_RE.search(answer or "")
        and _ESTATE_DUTY_ABOLITION_RE.search(answer or "")
    ):
        return ""
    section_text = _estate_duty_application_text(authorities)
    if not section_text:
        return ""
    cutoff = re.search(
        r"\bbefore\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\b",
        section_text,
        re.IGNORECASE,
    )
    if cutoff and cutoff.group(1).casefold() not in answer.casefold():
        return (
            "estate-duty abolition/application statement does not preserve "
            f"Cap. 111 section 2's cutoff date {cutoff.group(1)}"
        )
    return ""


def _wills_semantic_errors(user_message: Any, answer: str) -> list[str]:
    """Reject recurrent Cap. 30 category errors in will drafting/review."""
    if not _WILL_DOCUMENT_RE.search(_message_text(user_message)):
        return []

    errors: list[str] = []
    if _WILL_EXECUTION_RE.search(answer) and not _SECTION_5_RE.search(answer):
        errors.append(
            "signing and witnessing requirements must be tied to Cap. 30 section 5"
        )
    if _near(answer, _WILL_EXECUTION_RE, _SECTION_4_RE, distance=240):
        errors.append(
            "Cap. 30 section 4 must not be used for signing or witnessing requirements"
        )

    discusses_divorce_effect = bool(_DIVORCE_EFFECT_CLAIM_RE.search(answer))
    if discusses_divorce_effect:
        if not _SECTION_15_RE.search(answer):
            errors.append(
                "the effect of divorce, dissolution, or annulment on a will must be "
                "tied to Cap. 30 section 15"
            )
        if _FORMER_SPOUSE_PREDECEASED_RE.search(answer):
            errors.append(
                "Cap. 30 section 15 does not deem the former spouse to have "
                "predeceased the testator"
            )
        if not (
            _APPOINTMENT_OMITTED_RE.search(answer)
            and _DEVISE_BEQUEST_LAPSE_RE.search(answer)
            and _CONTRARY_INTENTION_RE.search(answer)
        ):
            errors.append(
                "section 15 must state its actual mechanism: a former spouse's "
                "executor/trustee appointment is omitted, and a devise or bequest "
                "to that spouse lapses except where the will shows a contrary intention"
            )

    discusses_marriage_effect = bool(_MARRIAGE_EFFECT_CLAIM_RE.search(answer))
    if discusses_marriage_effect:
        if not _SECTION_14_RE.search(answer):
            errors.append(
                "the effect of marriage or remarriage on a will must be tied to "
                "Cap. 30 section 14"
            )
        if not _STATUTORY_EXCEPTION_RE.search(answer):
            errors.append(
                "section 14 marriage revocation must preserve its statutory exceptions"
            )
    return errors


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
    if not is_hk_statutory_turn(messages, current_turn_user_idx):
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
        messages[current_turn_user_idx], final_response, authorities
    )
    if missing:
        calls = "; ".join(
            f"chapter='{chapter}', provisions={provisions}"
            for chapter, provisions in missing.items()
        )
        meanings = []
        for chapter, provisions in missing.items():
            if chapter == "111" and "2" in provisions:
                meanings.append(
                    "Cap. 111 is the Estate Duty Ordinance; section 2 controls "
                    "its date-of-death application cutoff. If the candidate "
                    "answer or a generated artifact mentions estate duty, "
                    "remove any inconsistent date and regenerate the affected "
                    "artifact after reading section 2"
                )
            elif chapter == "559":
                meanings.append(
                    "Cap. 559 is the Trade Marks Ordinance; read the listed "
                    "provisions for the registered-mark route"
                )
            elif chapter == "30":
                meanings.append(
                    "Cap. 30 section 5 controls signing and witnessing; section "
                    "14 controls the effect of marriage; section 15 controls the "
                    "effect of dissolution or annulment. Do not substitute section "
                    "4, which concerns wills made by persons not of full age. "
                    "Regenerate any affected artifact after correcting the law"
                )
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: The official lookup was incomplete for this Hong Kong "
                "statutory answer. Use hk_legal_authority to read the missing "
                f"minimum provisions before answering: {calls}. "
                + ("Meaning: " + "; ".join(meanings) + ". " if meanings else "")
                + "Then give one complete answer grounded in all successful "
                "current-turn results, including each official URL and version date.]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港法例結論：本回合未能完整讀取本題所需的最低官方法源"
            "集合。為免誤導，本次不提供未經完整法源支持的建議。",
        )

    estate_duty_error = _estate_duty_application_error(final_response, authorities)
    if estate_duty_error:
        section_text = _estate_duty_application_text(authorities)
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: Reject and rewrite the complete answer. The candidate "
                "made an estate-duty abolition/application statement that conflicts "
                "with or omits the controlling cutoff in the current-turn official "
                "Cap. 111 section 2 evidence. Use the deceased's date-of-death "
                "cutoff from the provision, not the legislation version date. Remove "
                "every inconsistent date from the answer and any generated artifact; "
                "regenerate an artifact if it contains the defect. Preserve the "
                "official URL and version citation.\n"
                f"Detected defect: {estate_duty_error}\n"
                f"Verified section 2 text: {section_text}]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港遺產稅結論：最終答案未能正確保留《遺產稅條例》"
            "第2條所載的適用截止日期。為免誤導，本次不提供日期錯誤的法律結論。",
        )

    wills_errors = _wills_semantic_errors(
        messages[current_turn_user_idx], final_response
    )
    if wills_errors:
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: Reject and rewrite the complete will answer. The current-turn "
                "official Cap. 30 provisions were read, but the candidate confused "
                "distinct statutory rules. Section 5 governs signing and witnessing; "
                "section 14 governs the effect of marriage; section 15 governs the "
                "effect of dissolution or annulment. Section 4 concerns wills made "
                "by persons not of full age. Correct every affected statement in the "
                "answer and generated document, regenerate the artifact, and preserve "
                "the official URL and version citation.\nDetected defects:\n- "
                + "\n- ".join(wills_errors)
                + "]",
                tuple(wills_errors),
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港遺囑結論：最終答案仍混淆《遺囑條例》的簽署、婚姻或"
            "離婚條文。為免誤導，本次不交付含錯誤法條的答案或文件。",
            tuple(wills_errors),
        )

    if _requires_rule_13_practice(messages[current_turn_user_idx]):
        practice_guidance = _verified_practice_guidance(authorities)
        if not practice_guidance:
            if attempts < max_attempts:
                return GateDecision(
                    "nudge",
                    "[System: This Hong Kong trade-mark time-limit answer requires "
                    "current IPD practice evidence in addition to legislation. Call "
                    "hk_legal_authority with chapter='559A', provisions=['13'] now. "
                    "Do not answer until the result contains successful official "
                    "practice guidance, a PDF SHA-256, and verified extracts.]",
                )
            return GateDecision(
                "fail",
                "無法提供可依賴的香港商標期限結論：本回合未能完整讀取知識產權署的"
                "現行實務手冊及可驗證摘錄。為免誤導，本次不提供只靠法例推斷的答案。",
            )

        practice_urls = [row["official_url"] for row in practice_guidance]
        contradicts_delivery = bool(_FALSE_MANUAL_DELIVERY_RE.search(final_response))
        cites_practice = all(url in final_response for url in practice_urls)
        uses_time_evidence = _answer_uses_rule_13_time_evidence(final_response)
        if contradicts_delivery or not cites_practice or not uses_time_evidence:
            evidence_lines = []
            for guidance in practice_guidance:
                evidence_lines.append(
                    f"Official IPD manual: {guidance['official_url']} "
                    f"(PDF SHA-256 {guidance['pdf_sha256']})"
                )
                evidence_lines.extend(
                    f"Exact verified extract: {extract}"
                    for extract in guidance["verified_extracts"]
                )
            if attempts < max_attempts:
                return GateDecision(
                    "nudge",
                    "[System: Reject and rewrite the complete answer. The current-turn "
                    "tool result delivered the official IPD manual evidence completely. "
                    "Do not claim the manual was unread, unavailable, or truncated. "
                    "Visibly cite its official URL and state the governing Rule 13(2) "
                    "six-month period and Rule 13(3) one-further-period-of-three-months "
                    "mechanism accurately, distinct from Rules 95 and 96.\n"
                    + "\n".join(evidence_lines)
                    + "]",
                )
            return GateDecision(
                "fail",
                "無法提供可依賴的香港商標期限結論：最終答案仍未正確使用已讀取的"
                "知識產權署實務手冊、Rule 13(2) 六個月期限及 Rule 13(3) 三個月機制。",
            )

    remedy_errors = _registered_famous_mark_remedy_errors(
        messages[current_turn_user_idx], final_response
    )
    registered_mark_rewrite_limit = min(max_attempts, REGISTERED_MARK_REWRITE_NUDGES)
    if remedy_errors and attempts >= registered_mark_rewrite_limit:
        return GateDecision(
            "replace",
            _deterministic_registered_mark_answer(authorities),
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
                "a section 53(5)(b) invalidity application. Also correct the complete "
                "registered-mark taxonomy now: registration ends the opposition-stage "
                "route; sections 52 and 53 are revocation and invalidity respectively; "
                "bad faith is section 11(5)(b); and Korean fame alone is insufficient "
                "to prove Hong Kong well-known status. Preserve the official URL and "
                "version citation.\nDetected defects:\n- "
                + "\n- ".join(remedy_errors)
                + "]",
            )
        return GateDecision(
            "fail",
            "無法提供可依賴的香港商標法結論：最終答案未能正確區分第12(6)條的反對階段"
            "規則與第53(5)(b)條的註冊後無效申請。為免誤導，本次不提供矛盾的救濟建議。",
        )

    if remedy_errors:
        if attempts < max_attempts:
            return GateDecision(
                "nudge",
                "[System: Reject and rewrite the complete registered-famous-mark "
                "remedy answer. The current official provisions were read, but the "
                "candidate misclassified or omitted remedies. Correct every item: "
                "section 44 is opposition at the application stage; section 45 is "
                "withdrawal by the applicant, not rectification; section 52 is "
                "revocation, including continuous non-use in Hong Kong for at least "
                "3 years; section 53 is declaration of invalidity to the Registrar "
                "or court; bad faith is section 11(5)(b) and supports invalidity via "
                "section 53(3); a well-known/earlier-right route must explain the "
                "Hong Kong protection requirement and section 53(5)(b); Korean fame "
                "alone is not enough to prove Hong Kong well-known status. Once the "
                "mark is registered, opposition is no longer a current remedy. Preserve the "
                "section 12(6) distinction and all official URLs/version dates.\n"
                "Detected defects:\n- " + "\n- ".join(remedy_errors) + "]",
            )
        return GateDecision(
            "replace",
            _deterministic_registered_mark_answer(authorities),
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
