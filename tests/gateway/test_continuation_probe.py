"""Behavioural tests for the capability-gated continuation probe (W4 T1).

The probe lives in agent/_continuation_probe.py and is invoked from
AIAgent.__init__ after memory_manager.initialize_all. It fires a
callback only when:
  - the active memory manager has at least one provider whose
    tool schema advertises a *_reasoning tool
  - that tool responds with non-empty text not equal to the
    sentinel [NONE]

Providers without reasoning capability (e.g. holographic's fact_store)
MUST be silently skipped — no event, no false positives.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent._continuation_probe import (
    find_reasoning_tool,
    maybe_emit_continuation,
    _parse_result,
)


class _SpyMemoryManager:
    """Minimal MemoryManager stand-in that exposes get_tool_schemas via
    providers and records handle_tool_call invocations.
    """

    def __init__(self, tool_schemas: List[Dict[str, Any]], reasoning_result: Any):
        self._schemas = tool_schemas
        self._reasoning_result = reasoning_result
        self.providers = [self]
        self.handle_tool_call_calls: List[Dict[str, Any]] = []

    def get_tool_schemas(self):
        return self._schemas

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.handle_tool_call_calls.append(
            {"tool_name": tool_name, "args": args, "kwargs": kwargs}
        )
        return self._reasoning_result


def test_find_reasoning_tool_returns_honcho_reasoning():
    """Tool name honcho_reasoning is recognised as reasoning-capable."""
    mgr = _SpyMemoryManager(
        [{"name": "honcho_reasoning"}, {"name": "fact_store"}],
        reasoning_result='{"result": "user was porting module X"}',
    )
    assert find_reasoning_tool(mgr) == "honcho_reasoning"


def test_find_reasoning_tool_recognises_any_reasoning_suffix():
    """Any tool name ending in _reasoning qualifies (e.g. mem0_reasoning)."""
    mgr = _SpyMemoryManager(
        [{"name": "fact_store"}, {"name": "mem0_reasoning"}],
        reasoning_result="[NONE]",
    )
    assert find_reasoning_tool(mgr) == "mem0_reasoning"


def test_find_reasoning_tool_returns_none_for_fact_store_only():
    """Holographic-like providers have no reasoning tool — None."""
    mgr = _SpyMemoryManager(
        [{"name": "fact_store"}, {"name": "fact_feedback"}],
        reasoning_result="irrelevant",
    )
    assert find_reasoning_tool(mgr) is None


def test_maybe_emit_continuation_fires_callback_on_non_sentinel():
    """Reasoning tool returns real summary → callback invoked with it."""
    mgr = _SpyMemoryManager(
        [{"name": "honcho_reasoning"}],
        reasoning_result='{"result": "You were porting skip_rag.py to the new ABC."}',
    )

    received: Dict[str, Any] = {}

    def callback(task_summary: str, **extra: Any):
        received["task_summary"] = task_summary
        received["extra"] = extra

    maybe_emit_continuation(
        mgr,
        callback,
        identity_kwargs={"user_id": "alice"},
        _synchronous=True,
    )

    assert received["task_summary"] == "You were porting skip_rag.py to the new ABC."
    # Identity kwargs MUST reach handle_tool_call.
    assert mgr.handle_tool_call_calls, "handle_tool_call was not invoked"
    call = mgr.handle_tool_call_calls[0]
    assert call["tool_name"] == "honcho_reasoning"
    assert call["kwargs"]["user_id"] == "alice"


def test_maybe_emit_continuation_skips_on_sentinel():
    """Tool returns [NONE] → callback NOT invoked, no event emitted."""
    mgr = _SpyMemoryManager(
        [{"name": "honcho_reasoning"}],
        reasoning_result='{"result": "[NONE]"}',
    )
    called: List[str] = []
    maybe_emit_continuation(
        mgr,
        lambda **kw: called.append("invoked"),
        _synchronous=True,
    )
    assert called == [], "callback should not fire on [NONE] sentinel"
    # But the reasoning tool WAS called — that's expected.
    assert len(mgr.handle_tool_call_calls) == 1


def test_maybe_emit_continuation_skips_without_reasoning_capability():
    """No reasoning tool registered → callback NOT invoked, handle_tool_call
    NEVER called (capability gating)."""
    mgr = _SpyMemoryManager(
        [{"name": "fact_store"}],
        reasoning_result="would be invoked if asked",
    )
    called: List[str] = []
    maybe_emit_continuation(
        mgr,
        lambda **kw: called.append("invoked"),
        _synchronous=True,
    )
    assert called == []
    assert mgr.handle_tool_call_calls == [], (
        "handle_tool_call must not be invoked when no reasoning tool is advertised"
    )


def test_maybe_emit_continuation_handles_whitespace_sentinel():
    """[NONE] with trailing/leading whitespace still treated as no-op."""
    mgr = _SpyMemoryManager(
        [{"name": "honcho_reasoning"}],
        reasoning_result='{"result": "  [NONE]  \\n"}',
    )
    called: List[str] = []
    maybe_emit_continuation(
        mgr,
        lambda **kw: called.append("invoked"),
        _synchronous=True,
    )
    assert called == []


def test_parse_result_unwraps_json_result_key():
    assert _parse_result('{"result": "hello"}') == "hello"
    assert _parse_result({"result": "world"}) == "world"
    assert _parse_result("plain text") == "plain text"
    assert _parse_result(None) == ""
