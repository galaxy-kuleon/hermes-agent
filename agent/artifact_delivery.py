"""Deterministic terminal delivery for artifacts created in the current turn.

Document export is a mechanism, not a language-model judgement.  A successful
``local_document_export`` result already contains the canonical signed Markdown
links.  This module makes those links part of the terminal answer even when the
model forgets to repeat them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


EXPORT_TOOL_NAME = "local_document_export"
DOWNLOADS_HEADING = "下載檔案："


def _function_call_payload(tool_call: Any) -> tuple[str, Any]:
    if not isinstance(tool_call, dict):
        return "", None
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return "", None
    return str(function.get("name") or ""), function.get("arguments")


def latest_successful_export_content(
    messages: list[Any], *, current_turn_user_idx: int
) -> str:
    """Return the exact model input behind the latest successful export."""
    if current_turn_user_idx < 0:
        return ""

    calls: dict[str, str] = {}
    for message in messages[current_turn_user_idx + 1 :]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or "")
            name, raw_arguments = _function_call_payload(tool_call)
            if not call_id or name != EXPORT_TOOL_NAME:
                continue
            try:
                arguments = (
                    raw_arguments
                    if isinstance(raw_arguments, dict)
                    else json.loads(str(raw_arguments or "{}"))
                )
            except (TypeError, ValueError):
                continue
            if not isinstance(arguments, dict):
                continue
            content = arguments.get("content_markdown")
            if isinstance(content, str) and content.strip():
                calls[call_id] = content

    latest = ""
    for message in messages[current_turn_user_idx + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool" or message.get("name") != EXPORT_TOOL_NAME:
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("success") is not True:
            continue
        content = calls.get(str(message.get("tool_call_id") or ""), "")
        if not content:
            continue
        expected_sha = str(payload.get("source_content_sha256") or "").strip()
        actual_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if expected_sha and expected_sha != actual_sha:
            continue
        latest = content
    return latest


def latest_successful_export_markdown(
    messages: list[Any], *, current_turn_user_idx: int
) -> str:
    """Return the latest successful export links after the current user row."""
    if current_turn_user_idx < 0:
        return ""

    latest = ""
    for message in messages[current_turn_user_idx + 1 :]:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "tool" or message.get("name") != EXPORT_TOOL_NAME:
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("success") is not True:
            continue
        markdown = payload.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            latest = markdown.strip()
    return latest


def ensure_export_links_in_terminal_answer(
    final_response: str,
    messages: list[Any],
    *,
    current_turn_user_idx: int,
) -> tuple[str, str]:
    """Return ``(answer, appended_suffix)`` for the current turn's export.

    Only missing canonical Markdown lines are appended.  The suffix is returned
    separately so streaming gateways can project exactly the newly added bytes
    without repeating the model's answer.
    """
    response = str(final_response or "")
    markdown = latest_successful_export_markdown(
        messages, current_turn_user_idx=current_turn_user_idx
    )
    if not markdown:
        return response, ""

    missing_links = [
        line.strip()
        for line in markdown.splitlines()
        if line.strip() and line.strip() not in response
    ]
    if not missing_links:
        return response, ""

    links = "\n".join(missing_links)
    separator = "\n\n" if response.rstrip() else ""
    suffix = f"{separator}{DOWNLOADS_HEADING}\n{links}"
    return f"{response.rstrip()}{suffix}", suffix
