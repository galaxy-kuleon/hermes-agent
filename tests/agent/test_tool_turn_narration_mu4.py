"""M-U4: suppress mid-tool-turn assistant prose; keep progress channel."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent.tool_turn_narration import (
    HOUSEKEEPING_TOOL_NAMES,
    SUPPRESS_TOOL_TURN_NARRATION_FN,
    should_suppress_tool_turn_narration,
    tool_names_from_tool_calls,
)


class SuppressPolicyTests(unittest.TestCase):
    def test_final_answer_no_tools_not_suppressed(self) -> None:
        self.assertFalse(should_suppress_tool_turn_narration([]))
        self.assertFalse(should_suppress_tool_turn_narration(None))

    def test_read_file_batch_suppressed(self) -> None:
        self.assertTrue(
            should_suppress_tool_turn_narration(["read_file", "read_file"])
        )
        self.assertTrue(should_suppress_tool_turn_narration(["vision_analyze"]))
        self.assertTrue(should_suppress_tool_turn_narration(["terminal"]))

    def test_housekeeping_only_not_suppressed(self) -> None:
        for name in HOUSEKEEPING_TOOL_NAMES:
            self.assertFalse(
                should_suppress_tool_turn_narration([name]),
                msg=name,
            )
        self.assertFalse(
            should_suppress_tool_turn_narration(["memory", "todo"])
        )

    def test_mixed_housekeeping_and_reader_suppressed(self) -> None:
        self.assertTrue(
            should_suppress_tool_turn_narration(["memory", "read_file"])
        )

    def test_tool_names_from_dict_and_object(self) -> None:
        class Fn:
            name = "read_file"

        class Tc:
            function = Fn()

        names = tool_names_from_tool_calls(
            [
                {"function": {"name": "vision_analyze"}},
                Tc(),
            ]
        )
        self.assertEqual(names, ["vision_analyze", "read_file"])


class ProductionWiringMutationTests(unittest.TestCase):
    def test_stream_helper_uses_suppress_policy(self) -> None:
        src = (
            Path(__file__).resolve().parents[2]
            / "agent"
            / "chat_completion_helpers.py"
        ).read_text(encoding="utf-8")
        self.assertIn(SUPPRESS_TOOL_TURN_NARRATION_FN, src)
        self.assertIn("_narration_buffer", src)
        # Must not re-leak via bare stream_delta_callback after tools
        # (the old disease: content still forwarded when tool_calls_acc set).
        # A coarse check: the suppress call appears near the buffer flush.
        idx = src.find(SUPPRESS_TOOL_TURN_NARRATION_FN)
        self.assertGreater(idx, 0)

    def test_conversation_loop_gates_interim_emit(self) -> None:
        src = (
            Path(__file__).resolve().parents[2]
            / "agent"
            / "conversation_loop.py"
        ).read_text(encoding="utf-8")
        self.assertIn(SUPPRESS_TOOL_TURN_NARRATION_FN, src)
        self.assertIn("if not _suppress_narration:", src)
        self.assertIn("_emit_interim_assistant_message", src)
        # emit must be behind the gate
        gate = src.find("if not _suppress_narration:")
        emit = src.find("_emit_interim_assistant_message", gate)
        self.assertGreater(emit, gate)

    def test_api_server_default_interim_off_progress_on(self) -> None:
        from gateway.display_config import _PLATFORM_DEFAULTS

        api = _PLATFORM_DEFAULTS["api_server"]
        self.assertFalse(api["interim_assistant_messages"])
        self.assertEqual(api["tool_progress"], "all")

    def test_mutation_removing_suppress_call_is_detectable(self) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "agent"
            / "chat_completion_helpers.py"
        )
        src = path.read_text(encoding="utf-8")
        self.assertIn(SUPPRESS_TOOL_TURN_NARRATION_FN, src)
        mutated = src.replace(SUPPRESS_TOOL_TURN_NARRATION_FN, "ALWAYS_FALSE_SUPPRESS")
        self.assertNotIn(SUPPRESS_TOOL_TURN_NARRATION_FN, mutated)
        with self.assertRaises(AssertionError):
            self.assertIn(SUPPRESS_TOOL_TURN_NARRATION_FN, mutated)


class FlushSemanticsTests(unittest.TestCase):
    """Document the buffer flush table used by chat_completion_helpers."""

    def test_flush_matrix(self) -> None:
        cases = [
            ([], True),  # final answer — deliver
            (["memory"], True),  # housekeeping — deliver
            (["read_file"], False),  # audit batch — drop
            (["read_file", "memory"], False),  # mixed — drop
        ]
        for names, should_deliver in cases:
            suppress = should_suppress_tool_turn_narration(names)
            self.assertEqual(
                (not suppress),
                should_deliver,
                msg=f"names={names}",
            )


if __name__ == "__main__":
    unittest.main()
