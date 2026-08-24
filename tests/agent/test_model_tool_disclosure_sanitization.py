from __future__ import annotations

from types import SimpleNamespace

from agent.agent_runtime_helpers import sanitize_api_messages
from agent.chat_completion_helpers import build_assistant_message
from agent.conversation_loop import _is_action_only_after_tool_disclosure
from agent.message_sanitization import (
    ToolDisclosureStreamScrubber,
    strip_internal_deliberation_tail,
    strip_model_tool_disclosures,
)


class _Agent:
    stream_delta_callback = None
    _stream_callback = None
    reasoning_callback = None
    verbose_logging = False

    def _extract_reasoning(self, _message):
        return None

    def _strip_think_blocks(self, text):
        return text

    def _needs_thinking_reasoning_pad(self):
        return False


def _message(content: str):
    return SimpleNamespace(
        content=content,
        tool_calls=None,
        function_call=None,
        reasoning_content=None,
        reasoning_details=None,
        model_extra=None,
    )


def test_complete_and_unclosed_typed_details_are_removed():
    complete = (
        "Checking.\n<details type=\"tool_calls\" name=\"read_file\">"
        "<summary>Tool Executed</summary>fake</details>\nActual answer."
    )
    result = strip_model_tool_disclosures(complete)
    assert result.text == "Checking.\n\nActual answer."
    assert result.removed_blocks == 1
    assert result.had_unclosed_block is False

    unclosed = strip_model_tool_disclosures(
        "Let me patch it. <details type='skill_manage'><parameter=x>fake"
    )
    assert unclosed.text == "Let me patch it. "
    assert unclosed.removed_blocks == 1
    assert unclosed.had_unclosed_block is True


def test_ordinary_details_without_transport_type_are_preserved():
    original = "<details><summary>Notes</summary>ordinary prose</details>"
    assert strip_model_tool_disclosures(original).text == original


def test_stream_scrubber_handles_every_opener_chunk_boundary():
    source = (
        "Before <details type=\"tool_calls\" name=\"search_files\">"
        "fake result</details> After"
    )
    for split in range(1, len(source)):
        scrubber = ToolDisclosureStreamScrubber()
        got = scrubber.feed(source[:split])
        got += scrubber.feed(source[split:])
        got += scrubber.flush()
        assert got == "Before  After"
        assert scrubber.removed_blocks == 1


def test_internal_deliberation_tail_is_removed_at_final_boundary():
    source = (
        "Class 14: item list complete.\n\n"
        "Let's analyze the situation. The user is asking for a retry."
    )
    assert strip_internal_deliberation_tail(source) == "Class 14: item list complete."
    assert strip_internal_deliberation_tail("Let's analyze this public issue.") == (
        "Let's analyze this public issue."
    )


def test_stream_scrubber_blocks_internal_tail_across_every_chunk_boundary():
    visible = "Class 14: item list complete."
    private = "\nLet's analyze the situation. The user needs a response."
    source = visible + private
    for split in range(1, len(source)):
        scrubber = ToolDisclosureStreamScrubber()
        got = scrubber.feed(source[:split])
        got += scrubber.feed(source[split:])
        got += scrubber.flush()
        assert got == visible


def test_history_and_storage_boundaries_strip_but_keep_raw_input_unchanged():
    fake = (
        "Let me update it. <details type=\"skill_manage\" name=\"x\">"
        "claimed success</details>"
    )
    history = [
        {"role": "assistant", "content": fake},
        {"role": "user", "content": "Did it work?"},
    ]
    cleaned = sanitize_api_messages(history)
    assert cleaned[0]["content"] == "Let me update it. "
    assert history[0]["content"] == fake

    stored = build_assistant_message(_Agent(), _message(fake), "stop")
    assert stored["content"] == "Let me update it."
    assert stored["_model_tool_disclosure_removed"]["blocks"] == 1


def test_action_preamble_is_not_accepted_as_a_substantive_result():
    assert _is_action_only_after_tool_disclosure("Let me update it now.")
    assert _is_action_only_after_tool_disclosure("")
    assert not _is_action_only_after_tool_disclosure(
        "## Result\n1. Class 14: jewelry\n2. Class 35: retail services"
    )
