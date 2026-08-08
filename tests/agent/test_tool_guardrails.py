"""Pure tool-call guardrail primitive tests."""

import json

from agent.tool_guardrails import (
    IDEMPOTENT_TOOL_NAMES,
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)


def test_default_config_is_soft_warning_only_with_hard_stop_disabled():
    cfg = ToolCallGuardrailConfig()

    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2


def test_success_resets_exact_signature_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, same_tool_failure_halt_after=99)
    )
    args = {"query": "same"}

    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", args, '{"ok":true}', failed=False)

    assert controller.before_call("web_search", args).action == "allow"
    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert controller.before_call("web_search", args).action == "allow"


def test_file_mutation_lint_error_result_is_not_a_tool_failure():
    write_result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })
    patch_result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert classify_tool_failure("write_file", write_result) == (False, "")
    assert classify_tool_failure("patch", patch_result) == (False, "")


def test_same_tool_varying_args_warns_by_default_without_halting():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(same_tool_failure_warn_after=2, same_tool_failure_halt_after=3)
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    fourth = controller.after_call("terminal", {"command": "cmd-4"}, '{"exit_code":1}', failed=True)

    assert first.action == "allow"
    assert [second.action, third.action, fourth.action] == ["warn", "warn", "warn"]
    assert {second.code, third.code, fourth.code} == {"same_tool_failure_warning"}
    assert "Do not switch to text-only replies" in second.message
    assert "keep using tools" in second.message
    assert "diagnose before retrying" in second.message
    assert "different tool" in second.message
    assert controller.halt_decision is None


def test_hard_stop_enabled_halts_same_tool_varying_args_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=2,
            same_tool_failure_halt_after=3,
        )
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    assert first.action == "allow"
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    assert second.action == "warn"
    assert second.code == "same_tool_failure_warning"
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    assert third.action == "halt"
    assert third.code == "same_tool_failure_halt"
    assert third.count == 3


def test_idempotent_no_progress_repeated_result_warns_without_blocking_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    for _ in range(4):
        assert controller.before_call("read_file", args).action == "allow"
        decision = controller.after_call("read_file", args, result, failed=False)

    assert decision.action == "warn"
    assert decision.code == "idempotent_no_progress_warning"
    assert controller.before_call("read_file", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_idempotent_no_progress_future_repeat():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    assert controller.before_call("read_file", args).action == "allow"
    assert controller.after_call("read_file", args, result, failed=False).action == "allow"
    assert controller.before_call("read_file", args).action == "allow"
    warn = controller.after_call("read_file", args, result, failed=False)
    assert warn.action == "warn"
    assert warn.code == "idempotent_no_progress_warning"

    blocked = controller.before_call("read_file", args)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_reset_for_turn_clears_bounded_guardrail_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, no_progress_block_after=2)
    )
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)

    assert controller.before_call("web_search", {"query": "same"}).action == "block"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "block"

    controller.reset_for_turn()

    assert controller.before_call("web_search", {"query": "same"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"


# --- 2026-07-28: platform-scoped hard stops ---------------------------------


def test_default_config_hard_stops_on_api_server_only():
    from agent.tool_guardrails import ToolCallGuardrailConfig

    config = ToolCallGuardrailConfig()
    # Warnings alone did not stop a live 46-file audit from issuing 96 read
    # calls over 18 files: 48 no-progress warnings were emitted and ignored.
    assert config.hard_stop_enabled is False
    assert "api_server" in config.hard_stop_platforms
    # A CLI/TUI loop has a human who can interrupt within seconds, so it keeps
    # the gentler behaviour.
    assert "cli" not in config.hard_stop_platforms
    assert "tui" not in config.hard_stop_platforms


def test_hard_stop_platforms_is_configurable():
    from agent.tool_guardrails import ToolCallGuardrailConfig

    config = ToolCallGuardrailConfig.from_mapping(
        {"hard_stop_platforms": ["discord", " ", "api_server"]}
    )
    assert config.hard_stop_platforms == frozenset({"discord", "api_server"})


def test_hard_stop_platforms_falls_back_to_default_on_garbage():
    from agent.tool_guardrails import ToolCallGuardrailConfig

    defaults = ToolCallGuardrailConfig()
    for garbage in ("api_server", 7, None):
        config = ToolCallGuardrailConfig.from_mapping(
            {"hard_stop_platforms": garbage}
        )
        assert config.hard_stop_platforms == defaults.hard_stop_platforms, garbage


def test_repeated_identical_read_blocks_once_hard_stops_are_on():
    from agent.tool_guardrails import (
        ToolCallGuardrailConfig,
        ToolCallGuardrailController,
    )

    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True)
    )
    args = {"path": "/handoff/user/u/chat/c/message/m/001-report.pdf"}
    result = '{"content": "same text every time"}'

    blocked_at = None
    for attempt in range(1, 12):
        decision = controller.before_call("read_file", args)
        if decision.should_halt:
            blocked_at = attempt
            break
        controller.after_call("read_file", args, result, failed=False)

    assert blocked_at is not None, "an unchanging read must eventually be blocked"
    # The live incident re-read single files 14 times; the block must land far
    # below that to be worth anything.
    assert blocked_at <= 8, blocked_at


def test_platform_resolver_is_consulted_at_decision_time_not_construction():
    """Ordering must not decide whether hard stops apply.

    One api_server entry point calls `_create_agent` before `set_session_vars`,
    so a platform read taken when the controller is built saw an empty string
    and silently left hard stops off on the surface that needs them most.
    """
    from agent.tool_guardrails import (
        ToolCallGuardrailConfig,
        ToolCallGuardrailController,
    )

    platform = {"value": ""}  # unbound at construction, as on that path
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(), platform_resolver=lambda: platform["value"]
    )
    args = {"path": "F01"}
    result = '{"content": "unchanging"}'

    def first_block():
        controller.reset_for_turn()
        for attempt in range(1, 12):
            if controller.before_call("read_file", args).should_halt:
                return attempt
            controller.after_call("read_file", args, result, failed=False)
        return None

    assert first_block() is None, "cli/unbound must stay warn-only"

    platform["value"] = "api_server"
    assert first_block() is not None, "api_server must hard stop once bound"


def test_a_broken_platform_resolver_degrades_to_warn_only():
    from agent.tool_guardrails import (
        ToolCallGuardrailConfig,
        ToolCallGuardrailController,
    )

    def boom():
        raise RuntimeError("session context unavailable")

    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(), platform_resolver=boom
    )
    args = {"path": "F01"}
    for _ in range(12):
        assert not controller.before_call("read_file", args).should_halt
        controller.after_call("read_file", args, '{"content": "x"}', failed=False)


def test_explicit_hard_stop_enabled_does_not_need_a_resolver():
    from agent.tool_guardrails import (
        ToolCallGuardrailConfig,
        ToolCallGuardrailController,
    )

    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True)
    )
    args = {"path": "F01"}
    blocked = False
    for _ in range(12):
        if controller.before_call("read_file", args).should_halt:
            blocked = True
            break
        controller.after_call("read_file", args, '{"content": "x"}', failed=False)
    assert blocked


def test_repeated_attachments_on_api_server_is_stopped():
    """The 2026-08-07 empty-reply loop (chat 6090080e).

    A brand-new user's second message produced roughly eighty-five identical
    `attachments({})` calls -- each SUCCEEDING with "no files are attached" --
    and then an empty reply. Nothing stopped it because `attachments` was not
    in IDEMPOTENT_TOOL_NAMES, so the no-progress guard never saw the tool.

    A succeeding call that returns the same answer forever is still no
    progress, and on api_server there is no human to press Stop.
    """
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(), platform_resolver=lambda: "api_server"
    )
    args = {}
    same_result = '{"success": true, "total": 0, "files": []}'

    actions = []
    for _ in range(12):
        before = controller.before_call("attachments", args)
        actions.append(before.action)
        if before.action == "block":
            break
        controller.after_call("attachments", args, same_result, failed=False)

    assert "block" in actions, (
        "an unchanging idempotent call must be stopped on api_server, "
        f"got {actions}"
    )
    assert actions.index("block") <= 8, (
        f"stopped far too late: {actions.index('block')} calls were allowed"
    )


def test_attachments_is_registered_as_idempotent():
    """Pinned separately: the guard above is only reachable via this set."""
    assert "attachments" in IDEMPOTENT_TOOL_NAMES


def test_block_message_tells_the_model_to_answer_not_to_report():
    """A control that halts a loop must not become the answer.

    Observed live 2026-08-08 (chat 9f5a792b): the guardrail correctly stopped a
    repeated skill_view, and the model then gave the user 257 characters naming
    `idempotent_no_progress_block` instead of the trademark advice they asked
    for. The halt worked; the wording invited a report.
    """
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(), platform_resolver=lambda: "api_server"
    )
    args = {"name": "tw-tmc"}
    same = '{"success": true, "content": "..."}'
    decision = None
    for _ in range(12):
        before = controller.before_call("skill_view", args)
        if before.action == "block":
            decision = before
            break
        controller.after_call("skill_view", args, same, failed=False)

    assert decision is not None, "the loop was never blocked"
    msg = decision.message.lower()
    assert "answer the user" in msg, f"must direct the model onward: {decision.message}"
    assert "do not mention it" in msg, f"must forbid surfacing it: {decision.message}"
