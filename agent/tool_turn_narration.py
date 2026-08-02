"""Suppress mid-tool-turn assistant *prose* (M-U4 interim narrative noise).

Evidence (jiaocha live OWUI chats, 2026-08-03): intermediate lines like
"continue reading more files in parallel batches" are **model-generated**
content co-emitted with ``tool_calls`` (read_file batches), then streamed
onto the OpenWebUI wire via ``stream_delta_callback``. They are not tool
results and not separate multi-turn messages.

Progress for long audits belongs in tool-progress / UI status channels —
not chat prose. Final answers (no tool_calls on that completion) still
surface fully.

Subtraction policy:
* When a completion includes any **non-housekeeping** tool call, do not
  deliver that completion's assistant *content* to user-facing stream /
  interim callbacks.
* Housekeeping-only tools (memory, todo, skill_manage, session_search)
  keep content visible (answer-first + side-effect pattern).
* Tool progress events remain; quiet ≠ offline.

Does **not** edit legal/file-auditor skill or USER.md.
"""

from __future__ import annotations

from typing import Iterable

# Post-response side-effect tools — content+these may still be the real answer.
HOUSEKEEPING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "memory",
        "todo",
        "skill_manage",
        "session_search",
    }
)

# Mutation-sensitive token — production stream path must consult this helper.
SUPPRESS_TOOL_TURN_NARRATION_FN = "should_suppress_tool_turn_narration"


def should_suppress_tool_turn_narration(
    tool_names: Iterable[str] | None,
) -> bool:
    """Return True when assistant content for this tool round must not reach UI.

    Empty / None tool list → False (final-answer completion; keep content).
    Any non-housekeeping tool → True (audit batch progress prose is noise).
    Only housekeeping tools → False (preserve answer-first + memory pattern).
    """
    names = [str(n or "").strip() for n in (tool_names or []) if str(n or "").strip()]
    if not names:
        return False
    return not all(n in HOUSEKEEPING_TOOL_NAMES for n in names)


def tool_names_from_tool_calls(tool_calls) -> list[str]:
    """Extract function names from OpenAI-style tool_calls objects or dicts."""
    out: list[str] = []
    for tc in tool_calls or []:
        name = ""
        if isinstance(tc, dict):
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "")
        else:
            fn = getattr(tc, "function", None)
            name = str(getattr(fn, "name", None) or getattr(tc, "name", None) or "")
        if name:
            out.append(name)
    return out


__all__ = [
    "HOUSEKEEPING_TOOL_NAMES",
    "SUPPRESS_TOOL_TURN_NARRATION_FN",
    "should_suppress_tool_turn_narration",
    "tool_names_from_tool_calls",
]
