"""Deterministic final-answer gate for declared skill source contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MAX_SKILL_SOURCE_NUDGES = 3
MAX_SKILL_CORRECTION_SOURCE_CHARS = 24_000
_CLASS_LIST_RE = re.compile(
    r"\bclass(?:es)?\s*([1-9]\d?(?:\s*(?:,|and|&|/)\s*[1-9]\d?)*)",
    re.IGNORECASE,
)
_CLASS_NUMBER_RE = re.compile(r"\b([1-9]|[1-9][0-9])\b")
_CLASS_PLACEHOLDER_RE = re.compile(r"(?<=class-)N(?=\.)")
_SKILL_MUTATION_INTENT_RE = re.compile(
    r"(?:\b(?:create|edit|update|modify|patch|change|write|delete|remove|rename|"
    r"publish|install|sync|save|remember)\b.{0,120}\bskills?\b|"
    r"\bskills?\b.{0,120}\b(?:create|edit|update|modify|patch|change|write|"
    r"delete|remove|rename|publish|install|sync|save)\b|"
    r"(?:建立|新增|編輯|编辑|修改|更新|修補|删除|刪除|移除|重新命名|發佈|发布|"
    r"安裝|安装|同步|儲存|保存|記住).{0,80}(?:技能|skill)|"
    r"(?:技能|skill).{0,80}(?:建立|新增|編輯|编辑|修改|更新|修補|删除|刪除|"
    r"移除|重新命名|發佈|发布|安裝|安装|同步|儲存|保存))",
    re.IGNORECASE | re.DOTALL,
)
_REFERENTIAL_SKILL_MUTATION_RE = re.compile(
    r"(?:^|\b)(?:yes|yep|correct|please\s+do|do\s+it|go\s+ahead|proceed)\b|"
    r"\bmake\s+(?:it|this|that)\s+(?:the\s+)?governing\s+rule\b|"
    r"\bmake\s+(?:it|this|that)\s+permanent(?:\s+across\s+sessions)?\b|"
    r"(?:好|是|對|对|可以|請做|请做|照做|繼續|继续|設為|设为).{0,60}"
    r"(?:永久|規則|规则|技能|skill)",
    re.IGNORECASE | re.DOTALL,
)
_SKILL_MUTATION_CONTEXT_RE = re.compile(
    r"\bskills?\b.{0,180}\b(?:create|edit|update|modify|patch|change|write|"
    r"delete|remove|rename|publish|save|governing\s+rule|permanent)\b|"
    r"\b(?:create|edit|update|modify|patch|change|write|delete|remove|rename|"
    r"publish|save|governing\s+rule|permanent)\b.{0,180}\bskills?\b|"
    r"(?:技能|skill).{0,120}(?:建立|新增|編輯|编辑|修改|更新|修補|刪除|"
    r"删除|移除|規則|规则|永久)|"
    r"(?:建立|新增|編輯|编辑|修改|更新|修補|刪除|删除|移除|規則|规则|"
    r"永久).{0,120}(?:技能|skill)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SkillSourceDecision:
    action: str
    message: str
    missing: tuple[tuple[str, str], ...] = ()
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


def has_explicit_skill_mutation_intent(
    user_message: Any,
    conversation_history: list[Any] | None = None,
) -> bool:
    """Resolve direct edits and narrow referential confirmations.

    A bare ``yes`` never grants mutation authority by itself. It does count
    when the recent conversation explicitly proposed or described a skill
    mutation, so users do not have to repeat the exact edit command that the
    assistant just asked them to confirm.
    """
    current = _message_text(user_message)
    if _SKILL_MUTATION_INTENT_RE.search(current):
        return True
    if not _REFERENTIAL_SKILL_MUTATION_RE.search(current):
        return False
    recent = "\n".join(
        _message_text(message)
        for message in (conversation_history or [])[-6:]
        if isinstance(message, dict)
        and message.get("role") in {"user", "assistant"}
    )
    return bool(_SKILL_MUTATION_CONTEXT_RE.search(recent))


def _payload(message: Any) -> dict | None:
    if not isinstance(message, dict) or message.get("role") != "tool":
        return None
    try:
        value = json.loads(_message_text(message))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def requested_class_numbers(text: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for match in _CLASS_LIST_RE.finditer(text or ""):
        numbers.extend(int(value) for value in _CLASS_NUMBER_RE.findall(match.group(1)))
    return tuple(dict.fromkeys(numbers))


def _required_sources(
    examples: list[dict[str, Any]], user_text: str
) -> tuple[tuple[str, str], ...]:
    classes = requested_class_numbers(user_text)
    required: list[tuple[str, str]] = []
    for example in examples:
        name = str(example.get("name") or "").strip()
        file_path = str(example.get("file_path") or "").strip()
        if not name or not file_path:
            continue
        if _CLASS_PLACEHOLDER_RE.search(file_path) and classes:
            required.extend(
                (name, _CLASS_PLACEHOLDER_RE.sub(str(number), file_path))
                for number in classes
            )
        else:
            required.append((name, file_path))
    return tuple(dict.fromkeys(required))


def _successful_source(payload: dict) -> tuple[str, str] | None:
    if payload.get("success") is not True:
        return None
    name = str(payload.get("name") or "").strip()
    file_path = str(
        payload.get("file")
        or (payload.get("routing") or {}).get("file_path")
        or ""
    ).strip()
    if not name or not file_path or file_path == "SKILL.md":
        return None
    return name, file_path


_CLASS_SECTION_RE = re.compile(
    r"(?im)^\s{0,3}(?:#{1,6}\s*)?(?:class\s*|第\s*)([1-9]\d?)"
    r"(?=\s|[-—:：類类]|$)"
)
_NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*(?:[-*]\s*)?\d+[.)、]\s+\S.*$")
_NUMBERED_ITEM_CAPTURE_RE = re.compile(
    r"(?m)^\s*(?:[-*]\s*)?\d+[.)、]\s+(?P<item>\S.*)$"
)
_EXPLICIT_ITEM_COUNT_RE = re.compile(
    r"(?:\b(?:only|exactly|at\s+most|maximum|max)\s*(\d+)\s*items?\b|"
    r"(?:只要|僅要|仅要|最多|不超過|不超过)\s*(\d+)\s*(?:項|项))",
    re.IGNORECASE,
)


def _class_sections(text: str) -> dict[int, str]:
    matches = list(_CLASS_SECTION_RE.finditer(text or ""))
    return {
        int(match.group(1)): (text or "")[
            match.start() : matches[index + 1].start() if index + 1 < len(matches) else None
        ]
        for index, match in enumerate(matches)
    }


def _explicit_item_count(user_text: str) -> int | None:
    match = _EXPLICIT_ITEM_COUNT_RE.search(user_text or "")
    if not match:
        return None
    value = next((group for group in match.groups() if group is not None), None)
    return int(value) if value is not None else None


def _numbered_item_count(text: str) -> int:
    return len(_NUMBERED_ITEM_RE.findall(text or ""))


def _numbered_items(text: str) -> tuple[str, ...]:
    """Return item text while removing Markdown wrappers, not legal wording."""
    items: list[str] = []
    for match in _NUMBERED_ITEM_CAPTURE_RE.finditer(text or ""):
        item = match.group("item").strip()
        item = re.sub(r"^(?:\*\*|__)(.*?)(?:\*\*|__)$", r"\1", item).strip()
        items.append(item)
    return tuple(items)


def _source_content_for_class(
    source_contents: dict[tuple[str, str], str], class_number: int
) -> str:
    expected = re.compile(rf"(?:^|/)class-{class_number}\.md$", re.IGNORECASE)
    return next(
        (
            content
            for (_, file_path), content in source_contents.items()
            if expected.search(file_path)
        ),
        "",
    )


def _without_layout_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _label_pattern(label: str) -> str:
    return rf"(?im)^\s*(?:#{{1,6}}\s*)?(?:\*\*|__)?{label}(?:\*\*|__)?\s*$"


def _corrective_source_packet(
    required: list[tuple[str, str]],
    source_contents: dict[tuple[str, str], str],
) -> str:
    ordered = list(dict.fromkeys(required))
    if not ordered or any(source not in source_contents for source in ordered):
        return ""
    parts = [
        f"--- authoritative source: {name} / {file_path} ---\n{source_contents[(name, file_path)]}"
        for name, file_path in ordered
    ]
    packet = "\n\n".join(parts)
    if len(packet) > MAX_SKILL_CORRECTION_SOURCE_CHARS:
        return ""
    return packet


def _answer_contract_diagnostics(
    *,
    contract: dict[str, Any],
    user_text: str,
    final_response: str,
    source_contents: dict[tuple[str, str], str],
) -> tuple[str, ...]:
    classes = requested_class_numbers(user_text)
    if not classes or not final_response.strip():
        return ("final answer is empty",) if classes else ()

    sections = _class_sections(final_response)
    explicit_count = _explicit_item_count(user_text)
    diagnostics: list[str] = []
    for class_number in classes:
        section = sections.get(class_number)
        if section is None:
            diagnostics.append(f"Class {class_number} section is missing")
            continue
        item_count = _numbered_item_count(section)
        if contract.get("list_every_chosen_item") is True and item_count < 1:
            diagnostics.append(f"Class {class_number} does not list each chosen item")

        if contract.get("require_authoritative_item_wording") is True:
            authoritative_source = _without_layout_whitespace(
                _source_content_for_class(source_contents, class_number)
            )
            seen_items: set[str] = set()
            for item in _numbered_items(section):
                compact_item = _without_layout_whitespace(item)
                if compact_item in seen_items:
                    diagnostics.append(
                        f"Class {class_number} repeats item wording {item!r}"
                    )
                seen_items.add(compact_item)
                if not authoritative_source or compact_item not in authoritative_source:
                    diagnostics.append(
                        f"Class {class_number} item wording is not present in its authoritative source: {item!r}"
                    )

        total_match = re.search(
            r"(?im)^\s*(?:\*\*|__)?(?:Total|總計|总计|合計|合计|小計|小计|"
            r"總數|总数|共)\s*[:：]?\s*(\d+)\s*(?:items?|項|项)?"
            r"(?:\*\*|__)?\s*$",
            section,
        )
        if contract.get("require_total") is True and total_match is None:
            diagnostics.append(f"Class {class_number} is missing the required Total line")
        elif total_match and int(total_match.group(1)) != item_count:
            diagnostics.append(
                f"Class {class_number} Total says {total_match.group(1)} but lists {item_count} items"
            )

        if explicit_count is not None and item_count > explicit_count:
            diagnostics.append(
                f"Class {class_number} lists {item_count} items, above the user's explicit {explicit_count}-item limit"
            )

        if 1 <= class_number <= 34:
            maximum = int(contract.get("class_1_34_max_items") or 0)
            if maximum and item_count > maximum:
                diagnostics.append(
                    f"Class {class_number} lists {item_count} items, above the {maximum}-item cap"
                )
            if explicit_count is not None:
                continue
            relevant_heading = re.search(
                _label_pattern(
                    r"(?:Relevant\s+items|(?:直接)?相關(?:項目|商品)|"
                    r"(?:直接)?相关(?:项目|商品))(?:\s*[（(]\s*\d+\s*[）)])?\s*[:：]?"
                ),
                section,
            )
            coverage_heading = re.search(
                _label_pattern(
                    r"(?:Coverage\s+items|(?:補充|补充|跨組|跨组|延伸)?"
                    r"(?:覆蓋|覆盖|涵蓋|涵盖)(?:項目|项目)|"
                    r"(?:補充|补充|延伸)(?:項目|项目))"
                    r"(?:\s*[（(]\s*\d+\s*[）)])?\s*[:：]?"
                ),
                section,
            )
            if contract.get("require_relevant_and_coverage_sections") is True and (
                relevant_heading is None or coverage_heading is None
            ):
                diagnostics.append(
                    f"Class {class_number} must use separate Relevant items and Coverage items sections"
                )
                continue
            if (
                relevant_heading
                and coverage_heading
                and relevant_heading.start() < coverage_heading.start()
            ):
                relevant_text = section[relevant_heading.end() : coverage_heading.start()]
                coverage_text = section[coverage_heading.end() :]
                relevant_count = _numbered_item_count(relevant_text)
                coverage_count = _numbered_item_count(coverage_text)
                relevant_max = int(contract.get("class_1_34_relevant_max_items") or 0)
                coverage_required = int(contract.get("class_1_34_coverage_items") or 0)
                subgroup_required = int(
                    contract.get("class_1_34_coverage_distinct_subgroups") or 0
                )
                if relevant_max and relevant_count > relevant_max:
                    diagnostics.append(
                        f"Class {class_number} has {relevant_count} relevant items, above the {relevant_max}-item relevant cap"
                    )
                if relevant_count < 1:
                    diagnostics.append(
                        f"Class {class_number} must include at least one relevant item"
                    )
                if coverage_required and coverage_count != coverage_required:
                    diagnostics.append(
                        f"Class {class_number} must list exactly {coverage_required} coverage items; found {coverage_count}"
                    )
                subgroup_codes = set(
                    re.findall(
                        r"(?m)^\s*[-*]\s*(?:\*\*|__)?(\d{4})\b",
                        coverage_text,
                    )
                )
                if subgroup_required and len(subgroup_codes) < subgroup_required:
                    diagnostics.append(
                        f"Class {class_number} coverage must span {subgroup_required} subgroup codes; found {len(subgroup_codes)}"
                    )
                if contract.get("coverage_must_add_new_subgroups") is True:
                    relevant_subgroup_codes = set(
                        re.findall(
                            r"(?m)^\s*[-*]\s*(?:\*\*|__)?(\d{4})\b",
                            relevant_text,
                        )
                    )
                    overlap = sorted(relevant_subgroup_codes & subgroup_codes)
                    if overlap:
                        diagnostics.append(
                            f"Class {class_number} coverage reuses relevant subgroup codes instead of adding new subgroup coverage: {', '.join(overlap)}"
                        )

        if class_number == 35 and re.search(
            r"wholesal|retail|direct\s+sales|批發|批发|零售|直銷|直销",
            user_text,
            re.IGNORECASE,
        ):
            maximum = int(contract.get("class_35_wholesale_retail_max_items") or 0)
            if maximum and item_count > maximum:
                diagnostics.append(
                    f"Class 35 lists {item_count} wholesale/retail items, above the {maximum}-item cap"
                )

    return tuple(diagnostics)


def evaluate_skill_source_contract(
    *,
    messages: list[Any],
    current_turn_user_idx: int,
    attempts: int,
    final_response: str = "",
) -> SkillSourceDecision | None:
    """Require this turn's declared linked skill sources before finalization."""
    if not (0 <= current_turn_user_idx < len(messages)):
        return None
    user_text = _message_text(messages[current_turn_user_idx])
    current_messages = messages[current_turn_user_idx + 1 :]
    required: list[tuple[str, str]] = []
    successful: set[tuple[str, str]] = set()
    source_contents: dict[tuple[str, str], str] = {}
    answer_contract: dict[str, Any] = {}
    for message in current_messages:
        payload = _payload(message)
        if payload is None:
            continue
        contract = payload.get("source_contract")
        if isinstance(contract, dict) and contract.get("required_before_answer") is True:
            examples = contract.get("declared_skill_view_examples") or []
            if isinstance(examples, list):
                required.extend(_required_sources(examples, user_text))
            declared_answer_contract = contract.get("answer_contract")
            if isinstance(declared_answer_contract, dict):
                answer_contract.update(declared_answer_contract)
        source = _successful_source(payload)
        if source:
            successful.add(source)
            content = payload.get("content")
            if payload.get("content_complete") is True and isinstance(content, str):
                source_contents[source] = content

    missing = tuple(source for source in dict.fromkeys(required) if source not in successful)
    diagnostics = ()
    if not missing and answer_contract:
        diagnostics = _answer_contract_diagnostics(
            contract=answer_contract,
            user_text=user_text,
            final_response=final_response,
            source_contents=source_contents,
        )
    if not missing and not diagnostics:
        return None

    calls = "\n".join(
        f'- skill_view(name={name!r}, file_path={file_path!r})'
        for name, file_path in missing
    )
    if attempts < MAX_SKILL_SOURCE_NUDGES:
        if diagnostics:
            problems = "\n".join(f"- {item}" for item in diagnostics)
            source_packet = _corrective_source_packet(required, source_contents)
            contract_packet = json.dumps(
                answer_contract,
                ensure_ascii=False,
                sort_keys=True,
            )
            grounding = (
                "\n\nThe following are full-fidelity copies of every required "
                "authoritative source already loaded in this turn. Use them "
                "directly; they are not summaries or truncated views:\n"
                f"{source_packet}"
                if source_packet
                else "\n\nThe required sources are too large to duplicate here. "
                "Use targeted native source search only for the exact missing "
                "items; do not guess or reread an undeclared path."
            )
            return SkillSourceDecision(
                action="nudge",
                message=(
                    "The final answer violates the named skill's declared answer "
                    "contract. Rewrite the answer only; do not ask the user to "
                    "choose from a candidate dump and do not use more tools unless "
                    "a loaded authoritative source is genuinely insufficient. "
                    "Follow the skill's exact section labels, list every selected "
                    "item, subgroup code, coverage allocation, cap, and Total line. "
                    "The machine-readable contract is:\n"
                    f"{contract_packet}\n"
                    "Return only this literal outer shape (repeat once per "
                    "requested class and replace every bracketed value):\n"
                    "Class [N] — [name]\n\n"
                    "Relevant items:\n"
                    "- [4-digit subgroup code] [subgroup name]:\n"
                    "  1. [authoritative item]\n\n"
                    "Coverage items:\n"
                    "- [4-digit subgroup code] [subgroup name]:\n"
                    "  1. [authoritative item]\n\n"
                    "Total: [exact listed-item count] items\n"
                    "For a class where Coverage items are not required, omit "
                    "that heading but keep Relevant items and Total. Do not "
                    "use a table, prose preamble, or any other heading.\n"
                    "Fix these mechanically observed problems:\n"
                    f"{problems}{grounding}"
                ),
                diagnostics=diagnostics,
            )
        return SkillSourceDecision(
            action="nudge",
            message=(
                "Required skill sources were not loaded successfully in this turn. "
                "Do not answer from memory, a prior run, attachments, local-path "
                "guesses, or terminal access. Issue these exact native calls now, "
                "then answer only from their authoritative results. Reapply every "
                "count, coverage, wording, and response-format rule from the named "
                "skill; list every item it requires and do not claim a successful "
                "source was truncated when content_complete=true:\n"
                f"{calls}"
            ),
            missing=missing,
        )
    if diagnostics:
        return SkillSourceDecision(
            action="fail",
            message=(
                "Cannot confirm the requested skill result because the final "
                "answer repeatedly violated its declared count, coverage, or "
                "response-format contract."
            ),
            diagnostics=diagnostics,
        )
    return SkillSourceDecision(
        action="fail",
        message=(
            "Cannot confirm the requested skill result because its required "
            "declared sources were not loaded successfully in this turn."
        ),
        missing=missing,
    )
