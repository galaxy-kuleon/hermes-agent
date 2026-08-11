"""Tests for the end-of-turn completion explainer (#34452).

When a turn ends abnormally after tools (empty content after retries, a
partial/truncated stream, exhausted retries, or an iteration/budget limit)
the user should get a single user-visible explanation of why the reply
stopped instead of a blank or fragmentary response box.  Normal short
replies (e.g. ``Done.``) must stay quiet.

These tests exercise:
  1. ``_format_turn_completion_explanation`` — the pure reason→message map.
  2. ``_turn_completion_explainer_enabled`` — the env/config seam.
  3. An end-to-end ``run_conversation`` turn that exhausts empty-response
     retries and verifies the explanation reaches ``final_response``.

All assertions work under the mocked OpenAI SDK used elsewhere in this
suite (we patch ``run_agent.OpenAI`` and drive ``agent.client``), so they
pass identically in CI and locally.
"""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


# --------------------------------------------------------------------------
# Fixtures (mirrors tests/run_agent/test_tool_call_guardrail_runtime.py)
# --------------------------------------------------------------------------
def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent(max_iterations: int = 10, config: dict | None = None) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    # No fallback chain so empty responses exhaust deterministically.
    agent._fallback_chain = []
    return agent


# --------------------------------------------------------------------------
# 1. Pure formatter
# --------------------------------------------------------------------------
def test_explanation_quiet_for_normal_text_response():
    """A healthy text_response exit must NOT produce any explanation."""
    out = AIAgent._format_turn_completion_explanation(
        "text_response(finish_reason=stop)"
    )
    assert out == ""


def test_silence_is_an_explicit_list():
    """Only these stay quiet. Everything else speaks.

    This assertion used to include "unknown", on the reasoning "don't
    second-guess". The formatter is only consulted when `final_response` is
    empty or a truncated fragment, so silence there IS the blank box #34452
    exists to fix, and "unknown" is the INITIAL value -- every exit path nobody
    anticipated lands on it. A sentence that names no cause is not a guess.
    Reversed deliberately 2026-08-12; see the comment at the formatter's tail.
    """
    assert AIAgent._format_turn_completion_explanation("") == ""
    # A terse healthy answer must not sprout a warning.
    assert AIAgent._format_turn_completion_explanation(
        "text_response(finish_reason=stop)") == ""
    # guardrail_halt streams its own halt message; a second one talks over it.
    assert AIAgent._format_turn_completion_explanation("guardrail_halt") == ""
    # the user stopped it on purpose.
    assert AIAgent._format_turn_completion_explanation("interrupted_by_user") == ""


def test_unknown_reason_still_tells_the_user_something():
    out = AIAgent._format_turn_completion_explanation("unknown")
    assert out, "an unrecorded reason must not leave the user with a blank box"
    assert "not recorded" in out
    assert "continue" in out.lower()


def test_pending_tool_result_message_is_reachable():
    """The most specific message we have for the exact symptom users report.

    `turn_finalizer` computes "the agent was mid-work and just stopped" to emit
    an operator warning, and the formatter has a written-out message for it --
    but `pending_tool_result` is never a `_turn_exit_reason`, so nothing could
    reach it. The finalizer now passes it when the reason is otherwise
    uninformative and the last message was a tool result.
    """
    out = AIAgent._format_turn_completion_explanation("pending_tool_result")
    assert out, "the pending-tool message is unreachable again"
    assert "tool result" in out
    assert "continue" in out.lower()


def test_a_reason_nobody_wired_up_still_speaks():
    """The failure shape this codebase keeps repeating: a name nobody listed.

    The reason is generated at run time, so it cannot be in any map here or in
    production -- exactly the case that used to fall through to silence.
    """
    import uuid
    canary = "exit_" + uuid.uuid4().hex[:12]
    out = AIAgent._format_turn_completion_explanation(canary)
    assert out, f"an unanticipated reason ({canary}) fell through to silence"
    assert canary not in out, "the internal reason must not be shown to the user"


def test_explanation_for_empty_response_exhausted():
    out = AIAgent._format_turn_completion_explanation("empty_response_exhausted")
    assert out  # non-empty
    assert "empty content" in out
    assert "continue" in out.lower()


def test_explanation_for_partial_stream_recovery():
    out = AIAgent._format_turn_completion_explanation("partial_stream_recovery")
    assert "partial" in out.lower()
    assert "continue" in out.lower()


def test_explanation_for_max_iterations_reached_prefix_match():
    """``max_iterations_reached(...)`` carries a parenthetical suffix."""
    out = AIAgent._format_turn_completion_explanation(
        "max_iterations_reached(10/10)"
    )
    assert "iteration" in out.lower()


def test_explanation_for_all_retries_exhausted():
    out = AIAgent._format_turn_completion_explanation(
        "all_retries_exhausted_no_response"
    )
    assert "retries" in out.lower()


# --------------------------------------------------------------------------
# 2. Enable/disable seam
# --------------------------------------------------------------------------
def test_explainer_enabled_by_default():
    agent = _make_agent()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_TURN_COMPLETION_EXPLAINER", None)
        with patch("hermes_cli.config.load_config", return_value={}):
            assert agent._turn_completion_explainer_enabled() is True


def test_explainer_disabled_via_env():
    agent = _make_agent()
    with patch.dict(
        os.environ, {"HERMES_TURN_COMPLETION_EXPLAINER": "0"}, clear=False
    ):
        assert agent._turn_completion_explainer_enabled() is False


def test_explainer_disabled_via_config():
    agent = _make_agent()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_TURN_COMPLETION_EXPLAINER", None)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"display": {"turn_completion_explainer": False}},
        ):
            assert agent._turn_completion_explainer_enabled() is False


# --------------------------------------------------------------------------
# 3. End-to-end: empty-response exhaustion surfaces the explanation
# --------------------------------------------------------------------------
def test_run_conversation_empty_exhausted_surfaces_explanation():
    """Four empty responses in a row should exhaust retries and the final
    response should be the actionable explanation, not a bare '(empty)'."""
    agent = _make_agent(max_iterations=10)
    # 4 empty responses: retries 1..3 then the terminal on the 4th.
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="", finish_reason="stop") for _ in range(8)
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do something")

    assert result["turn_exit_reason"] == "empty_response_exhausted"
    # The user must NOT be left with a bare sentinel; the explanation wins.
    assert result["final_response"] != "(empty)"
    assert result["final_response"].strip() != ""
    assert "No reply:" in result["final_response"]


def test_run_conversation_normal_reply_stays_quiet():
    """A normal short reply like 'Done.' must NOT get an explainer footer."""
    agent = _make_agent(max_iterations=10)
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="Done.", finish_reason="stop"),
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do something")

    assert result["turn_exit_reason"].startswith("text_response")
    assert result["final_response"] == "Done."
    assert "No reply:" not in result["final_response"]


def test_finalizer_prefers_a_real_cause_over_the_stop_shape():
    """Precedence, asserted against the finalizer's own source.

    A cause like `budget_exhausted` is more actionable than "it stopped with a
    tool pending", so the substitution must apply ONLY when the reason carries
    no information. Asserted structurally because driving the finalizer needs
    the whole agent runtime.
    """
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "agent" / "turn_finalizer.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    subs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "_explain_reason" for t in n.targets)
        and isinstance(n.value, ast.Constant)
        and n.value.value == "pending_tool_result"
    ]
    assert subs, "the pending_tool_result substitution is gone"
    guard = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and any(
            isinstance(c, ast.Assign)
            and any(getattr(t, "id", "") == "_explain_reason" for t in c.targets)
            and isinstance(c.value, ast.Constant)
            and c.value.value == "pending_tool_result"
            for c in node.body
        ):
            guard = ast.unparse(node.test)
    assert guard, "the substitution is unguarded"
    assert "unknown" in guard, f"a real cause would be overridden: {guard}"
    assert "tool" in guard, f"the substitution is not tied to a pending tool: {guard}"
