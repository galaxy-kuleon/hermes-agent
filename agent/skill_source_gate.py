"""Deterministic final-answer gate for declared skill source contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MAX_SKILL_SOURCE_NUDGES = 3
_CLASS_LIST_RE = re.compile(r"\bclasses?\b([^\n.:;]{0,80})", re.IGNORECASE)
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


@dataclass(frozen=True)
class SkillSourceDecision:
    action: str
    message: str
    missing: tuple[tuple[str, str], ...] = ()


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


def has_explicit_skill_mutation_intent(user_message: Any) -> bool:
    """Return whether the user explicitly asked to mutate skill state."""
    return bool(_SKILL_MUTATION_INTENT_RE.search(_message_text(user_message)))


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


def evaluate_skill_source_contract(
    *,
    messages: list[Any],
    current_turn_user_idx: int,
    attempts: int,
) -> SkillSourceDecision | None:
    """Require this turn's declared linked skill sources before finalization."""
    if not (0 <= current_turn_user_idx < len(messages)):
        return None
    user_text = _message_text(messages[current_turn_user_idx])
    current_messages = messages[current_turn_user_idx + 1 :]
    required: list[tuple[str, str]] = []
    successful: set[tuple[str, str]] = set()
    for message in current_messages:
        payload = _payload(message)
        if payload is None:
            continue
        contract = payload.get("source_contract")
        if isinstance(contract, dict) and contract.get("required_before_answer") is True:
            examples = contract.get("declared_skill_view_examples") or []
            if isinstance(examples, list):
                required.extend(_required_sources(examples, user_text))
        source = _successful_source(payload)
        if source:
            successful.add(source)

    missing = tuple(source for source in dict.fromkeys(required) if source not in successful)
    if not missing:
        return None

    calls = "\n".join(
        f'- skill_view(name={name!r}, file_path={file_path!r})'
        for name, file_path in missing
    )
    if attempts < MAX_SKILL_SOURCE_NUDGES:
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
    return SkillSourceDecision(
        action="fail",
        message=(
            "Cannot confirm the requested skill result because its required "
            "declared sources were not loaded successfully in this turn."
        ),
        missing=missing,
    )
