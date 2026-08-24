"""Opt-in, allowlisted terminal replies from trusted tool results."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable


VERBATIM_END_TURN = "verbatim_end_turn"
VERBATIM_REPLY_TOOLS_ENV = "HERMES_VERBATIM_REPLY_TOOLS"
MAX_VERBATIM_REPLY_CHARS = 100_000
_UNTRUSTED_CLOSE = "\n</untrusted_tool_result>"


def _configured_tools() -> set[str]:
    return {
        item.strip()
        for item in os.environ.get(VERBATIM_REPLY_TOOLS_ENV, "").split(",")
        if item.strip()
    }


def _objects(value: Any, *, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 3:
        return
    if isinstance(value, dict):
        yield value
        for key in ("structuredContent", "structured_content", "result"):
            if key in value:
                yield from _objects(value[key], depth=depth + 1)
        return
    if isinstance(value, str):
        # MCP results are framed as untrusted data before they are appended to
        # the conversation. Only peel the exact outer frame that Hermes itself
        # creates; the payload's delimiter token was already neutralized by
        # the framing layer.
        if value.startswith('<untrusted_tool_result source="') and value.endswith(
            _UNTRUSTED_CLOSE
        ):
            _, separator, framed_payload = value.partition("\n\n")
            if separator:
                value = framed_payload[: -len(_UNTRUSTED_CLOSE)]
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return
        yield from _objects(decoded, depth=depth + 1)


def resolve_verbatim_tool_reply(
    messages: list[dict[str, Any]],
    *,
    allowed_tools: set[str] | None = None,
) -> str | None:
    """Return an exact terminal reply only for an explicitly trusted tool.

    Both gates are mandatory: the operator allowlists the tool name, and the
    tool opts this particular result into ``verbatim_end_turn``. Arbitrary MCP
    content cannot turn itself into a direct user response.
    """
    allowed = _configured_tools() if allowed_tools is None else allowed_tools
    if not allowed:
        return None

    current_turn: list[dict[str, Any]] = []
    for message in reversed(messages):
        if message.get("role") == "user":
            break
        current_turn.append(message)

    for message in current_turn:
        if message.get("role") != "tool":
            continue
        if str(message.get("tool_name") or "") not in allowed:
            continue
        for payload in _objects(message.get("content")):
            if payload.get("reply_mode") != VERBATIM_END_TURN:
                continue
            reply = payload.get("reply_markdown")
            if isinstance(reply, str) and 0 < len(reply) <= MAX_VERBATIM_REPLY_CHARS:
                return reply
    return None
