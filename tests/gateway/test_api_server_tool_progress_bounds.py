"""Transport bounds for OpenAI-compatible tool-progress SSE events."""

import html
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


SSE_PHYSICAL_LINE_LIMIT_BYTES = 65_536
SSE_MIN_WRAPPER_HEADROOM_BYTES = 16_384
LARGE_RESULT_CHARS = 95_153
LARGE_ARGUMENT_CHARS = 100_000
QUOTE_STRESS_CHARS = 100_000
LONG_CORRELATION_METADATA_CHARS = 3_072
FINAL_TEXT = "bounded stream completed"
REQUEST_HEADERS = {
    "Authorization": "Bearer test-api-key",
    "X-OpenWebUI-User-Id": "test-user",
    "X-OpenWebUI-User-Name": "Transport Test",
    "X-OpenWebUI-User-Role": "admin",
    "X-OpenWebUI-User-Groups": "test-group",
    "X-OpenWebUI-Chat-Id": "test-chat",
}


def _make_sized_text(total_chars: int, tail_marker: str) -> str:
    pattern = '漢字🙂<&"\\\\\n'
    body_chars = total_chars - len(tail_marker)
    assert body_chars > 0
    body = (pattern * ((body_chars // len(pattern)) + 1))[:body_chars]
    value = body + tail_marker
    assert len(value) == total_chars
    return value


def _make_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _strict_json_loads(text: str) -> object:
    def _reject_non_standard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    return json.loads(text, parse_constant=_reject_non_standard_constant)


def _tool_progress_events(body: str) -> list[dict]:
    events: list[dict] = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line != "event: hermes.tool.progress":
            continue
        assert index + 1 < len(lines)
        data_line = lines[index + 1]
        assert data_line.startswith("data: ")
        events.append(_strict_json_loads(data_line.removeprefix("data: ")))
    return events


def _chat_content_deltas(body: str) -> list[str]:
    deltas: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = _strict_json_loads(line.removeprefix("data: "))
        if payload.get("object") != "chat.completion.chunk":
            continue
        for choice in payload.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if content:
                deltas.append(content)
    return deltas


def _assert_physical_lines_bounded(body: str) -> None:
    for line in body.splitlines():
        assert len(line.encode("utf-8")) <= SSE_PHYSICAL_LINE_LIMIT_BYTES, (
            f"SSE line is {len(line.encode('utf-8'))} bytes"
        )


def _assert_physical_lines_have_headroom(body: str) -> None:
    max_line_bytes = max(len(line.encode("utf-8")) for line in body.splitlines())
    assert max_line_bytes <= (
        SSE_PHYSICAL_LINE_LIMIT_BYTES - SSE_MIN_WRAPPER_HEADROOM_BYTES
    ), f"SSE line has insufficient wrapper headroom: {max_line_bytes} bytes"


@pytest.mark.asyncio
async def test_large_arguments_and_result_are_bounded_end_to_end():
    result_tail = "RESULT_TAIL_MUST_NOT_CROSS_WIRE"
    argument_tail = "ARGUMENT_TAIL_MUST_NOT_CROSS_WIRE"
    full_result = _make_sized_text(LARGE_RESULT_CHARS, result_tail)
    full_arguments = {
        "query": _make_sized_text(LARGE_ARGUMENT_CHARS, argument_tail),
        "mode": "exact",
    }
    original_arguments = dict(full_arguments)
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"]("call_large_1", "read_file", full_arguments)
        kwargs["tool_complete_callback"](
            "call_large_1", "read_file", full_arguments, full_result
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "read safely"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    _assert_physical_lines_have_headroom(body)
    assert body.encode("utf-8").decode("utf-8") == body
    events = _tool_progress_events(body)
    assert [(event["status"], event["toolCallId"]) for event in events] == [
        ("running", "call_large_1"),
        ("completed", "call_large_1"),
    ]

    running, completed = events
    assert running["tool"] == completed["tool"] == "read_file"
    assert running["emoji"]
    assert running["label"]
    assert running["argumentsTruncated"] is True
    assert completed["argumentsTruncated"] is True
    assert completed["resultTruncated"] is True

    serialized_arguments = json.dumps(
        full_arguments,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    assert running["argumentsOriginalChars"] == len(serialized_arguments)
    assert running["argumentsOriginalUtf8Bytes"] == len(
        serialized_arguments.encode("utf-8")
    )
    assert completed["resultOriginalChars"] == len(full_result)
    assert completed["resultOriginalUtf8Bytes"] == len(full_result.encode("utf-8"))

    assert result_tail not in body
    assert argument_tail not in body
    html_deltas = [
        delta
        for delta in _chat_content_deltas(body)
        if '<details type="tool_calls"' in delta
    ]
    assert len(html_deltas) == 1
    assert 'done="true"' in html_deltas[0]
    assert "Tool Executed" in html_deltas[0]
    assert "…[truncated, full output is 95153 chars]" in html_deltas[0]
    assert result_tail not in html_deltas[0]
    assert argument_tail not in html_deltas[0]
    assert FINAL_TEXT in _chat_content_deltas(body)
    assert body.rstrip().endswith("data: [DONE]")

    # Transport previewing must not mutate the agent's full internal values.
    assert full_arguments == original_arguments
    assert len(full_result) == LARGE_RESULT_CHARS
    assert full_result.endswith(result_tail)


@pytest.mark.parametrize(
    ("case_name", "character"),
    [
        ("quotes", '"'),
        ("backslashes", "\\"),
        ("ampersands", "&"),
        ("cjk", "漢"),
        ("emoji", "🙂"),
    ],
)
@pytest.mark.asyncio
async def test_adversarial_value_expansion_keeps_custom_and_html_lines_bounded(
    case_name: str,
    character: str,
):
    stress_payload = character * QUOTE_STRESS_CHARS
    call_id = f"call_{case_name}_1"
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"](call_id, "read_file", stress_payload)
        kwargs["tool_complete_callback"](
            call_id,
            "read_file",
            stress_payload,
            stress_payload,
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "expansion stress"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    _assert_physical_lines_have_headroom(body)
    events = _tool_progress_events(body)
    assert [(event["status"], event["toolCallId"]) for event in events] == [
        ("running", call_id),
        ("completed", call_id),
    ]
    running, completed = events
    assert running["argumentsTruncated"] is True
    assert completed["argumentsTruncated"] is True
    assert completed["resultTruncated"] is True
    assert running["argumentsOriginalChars"] == QUOTE_STRESS_CHARS
    assert running["argumentsOriginalUtf8Bytes"] == len(stress_payload.encode("utf-8"))
    assert completed["resultOriginalChars"] == QUOTE_STRESS_CHARS
    assert completed["resultOriginalUtf8Bytes"] == len(stress_payload.encode("utf-8"))
    assert stress_payload not in body
    assert FINAL_TEXT in _chat_content_deltas(body)
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_completed_html_overflow_uses_same_minimal_wire_payload():
    quoted_metadata = '"' * LONG_CORRELATION_METADATA_CHARS
    quote_payload = '"' * QUOTE_STRESS_CHARS
    tool_name = quoted_metadata
    call_id = quoted_metadata
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"](call_id, tool_name, quote_payload)
        kwargs["tool_complete_callback"](
            call_id,
            tool_name,
            quote_payload,
            quote_payload,
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with (
            patch.object(adapter, "_run_agent", side_effect=_mock_run_agent),
            patch("agent.display.build_tool_preview", return_value="stable label"),
            patch("gateway.platforms.api_server.logger.warning") as warning_mock,
        ):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "metadata stress"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    _assert_physical_lines_have_headroom(body)
    running, completed = _tool_progress_events(body)
    assert running.get("transportTruncated", False) is False
    assert completed["transportTruncated"] is True
    assert completed["toolCallId"] == call_id
    assert completed["status"] == "completed"
    assert completed["toolTruncated"] is True
    assert completed["argumentsTruncated"] is True
    assert completed["resultTruncated"] is True
    assert completed["argumentsOriginalChars"] == QUOTE_STRESS_CHARS
    assert completed["argumentsOriginalUtf8Bytes"] == QUOTE_STRESS_CHARS
    assert completed["resultOriginalChars"] == QUOTE_STRESS_CHARS
    assert completed["resultOriginalUtf8Bytes"] == QUOTE_STRESS_CHARS
    assert "arguments" not in completed
    assert "result" not in completed

    html_deltas = [
        delta
        for delta in _chat_content_deltas(body)
        if '<details type="tool_calls"' in delta
    ]
    assert len(html_deltas) == 1
    html_delta = html_deltas[0]
    assert f'id="{html.escape(completed["toolCallId"])}"' in html_delta
    assert f'name="{html.escape(completed["tool"])}"' in html_delta
    assert "…[truncated, full output is 100000 chars]" in html_delta
    assert quote_payload not in body
    assert FINAL_TEXT in _chat_content_deltas(body)
    assert body.rstrip().endswith("data: [DONE]")

    fallback_warnings = [
        call
        for call in warning_mock.call_args_list
        if call.args and call.args[0].startswith("tool_progress_html_fallback")
    ]
    assert len(fallback_warnings) == 1
    warning_args = fallback_warnings[0].args
    assert warning_args[1] == "completed"
    assert len(warning_args) == 7
    assert all(isinstance(value, int) for value in warning_args[2:])
    rendered_warning_args = " ".join(str(value) for value in warning_args)
    assert quoted_metadata not in rendered_warning_args
    assert quote_payload not in rendered_warning_args


@pytest.mark.asyncio
async def test_unexpected_large_metadata_uses_correlated_minimal_fallback():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )
    tool_tail = "TOOL_TAIL_MUST_NOT_CROSS_WIRE"
    oversized_tool = _make_sized_text(
        SSE_PHYSICAL_LINE_LIMIT_BYTES * 2,
        tool_tail,
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"](
            "call_fallback_1", oversized_tool, {"command": "safe"}
        )
        kwargs["tool_complete_callback"](
            "call_fallback_1", oversized_tool, {"command": "safe"}, "ok"
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "fallback"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    running, completed = _tool_progress_events(body)
    assert running["toolCallId"] == "call_fallback_1"
    assert running["status"] == "running"
    assert running["transportTruncated"] is True
    assert running["toolTruncated"] is True
    assert running["argumentsTruncated"] is True
    assert completed["toolCallId"] == "call_fallback_1"
    assert completed["status"] == "completed"
    assert completed["transportTruncated"] is True
    assert completed["toolTruncated"] is True
    assert completed["resultTruncated"] is True
    assert tool_tail not in body
    html_deltas = [
        delta
        for delta in _chat_content_deltas(body)
        if '<details type="tool_calls"' in delta
    ]
    assert len(html_deltas) == 1
    assert tool_tail not in html_deltas[0]
    assert FINAL_TEXT in _chat_content_deltas(body)
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_internal_and_orphan_events_remain_filtered():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"](
            "call_internal_1", "_thinking", {"text": "internal"}
        )
        kwargs["tool_complete_callback"](
            "call_internal_1", "_thinking", {"text": "internal"}, "hidden"
        )
        kwargs["tool_complete_callback"](
            "call_orphan_1", "web_search", {"query": "orphan"}, "hidden"
        )
        kwargs["tool_start_callback"](
            "call_visible_1", "web_search", {"query": "visible"}
        )
        kwargs["tool_complete_callback"](
            "call_visible_1", "web_search", {"query": "visible"}, "ok"
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "filter"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    events = _tool_progress_events(body)
    assert [(event["status"], event["toolCallId"]) for event in events] == [
        ("running", "call_visible_1"),
        ("completed", "call_visible_1"),
    ]
    assert "call_internal_1" not in body
    assert "call_orphan_1" not in body
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_small_payload_preserves_legacy_fields_without_false_truncation():
    arguments = {"command": "printf safe"}
    result = "small result"
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"]("call_small_1", "terminal", arguments)
        kwargs["tool_complete_callback"]("call_small_1", "terminal", arguments, result)
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "small"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    running, completed = _tool_progress_events(body)
    assert running["arguments"] == arguments
    assert completed["arguments"] == arguments
    assert completed["result"] == result
    assert running.get("argumentsTruncated", False) is False
    assert completed.get("argumentsTruncated", False) is False
    assert completed.get("resultTruncated", False) is False
    assert "argumentsOriginalChars" not in running
    assert "resultOriginalChars" not in completed
    html_deltas = [
        delta
        for delta in _chat_content_deltas(body)
        if '<details type="tool_calls"' in delta
    ]
    assert len(html_deltas) == 1
    assert "…[truncated" not in html_deltas[0]
    assert body.rstrip().endswith("data: [DONE]")


class _NonJsonValue:
    def __str__(self) -> str:
        return "non-json-value"


class _HostileStringValue:
    def __str__(self) -> str:
        raise RuntimeError("must not escape")


@pytest.mark.asyncio
async def test_non_json_native_values_use_safe_transport_fallback():
    arguments = {
        "path": Path("/tmp/example.txt"),
        "opaque": _NonJsonValue(),
    }
    result = _NonJsonValue()
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"]("call_non_json_1", "read_file", arguments)
        kwargs["tool_complete_callback"](
            "call_non_json_1", "read_file", arguments, result
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "non-json"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    running, completed = _tool_progress_events(body)
    assert running["arguments"] == {
        "path": "/tmp/example.txt",
        "opaque": "non-json-value",
    }
    assert completed["result"] == "non-json-value"
    assert running.get("argumentsTruncated", False) is False
    assert completed.get("resultTruncated", False) is False
    assert running["argumentsJsonCoerced"] is True
    assert completed["argumentsJsonCoerced"] is True
    assert completed["resultJsonCoerced"] is True
    serialized_arguments = json.dumps(
        {
            "path": "/tmp/example.txt",
            "opaque": "non-json-value",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert running["argumentsOriginalChars"] == len(serialized_arguments)
    assert running["argumentsOriginalUtf8Bytes"] == len(
        serialized_arguments.encode("utf-8")
    )
    serialized_result = json.dumps(
        "non-json-value",
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert completed["resultOriginalChars"] == len(serialized_result)
    assert completed["resultOriginalUtf8Bytes"] == len(
        serialized_result.encode("utf-8")
    )
    assert FINAL_TEXT in _chat_content_deltas(body)
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_non_finite_and_hostile_values_emit_strict_json_fallbacks():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    async def _mock_run_agent(**kwargs):
        kwargs["tool_start_callback"](
            "call_nan_1", "read_file", {"value": float("nan")}
        )
        kwargs["tool_complete_callback"](
            "call_nan_1", "read_file", {"value": float("nan")}, float("nan")
        )
        kwargs["tool_start_callback"]("call_hostile_1", "read_file", {"path": "safe"})
        kwargs["tool_complete_callback"](
            "call_hostile_1",
            "read_file",
            {"path": "safe"},
            _HostileStringValue(),
        )
        kwargs["stream_delta_callback"](FINAL_TEXT)
        return (
            {"final_response": FINAL_TEXT, "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    app = _make_app(adapter)
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            response = await client.post(
                "/v1/chat/completions",
                headers=REQUEST_HEADERS,
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "strict json"}],
                    "stream": True,
                },
            )
            assert response.status == 200
            body = await response.text()

    _assert_physical_lines_bounded(body)
    events = _tool_progress_events(body)
    assert [(event["status"], event["toolCallId"]) for event in events] == [
        ("running", "call_nan_1"),
        ("completed", "call_nan_1"),
        ("running", "call_hostile_1"),
        ("completed", "call_hostile_1"),
    ]
    nan_running, nan_completed, _, hostile_completed = events
    assert nan_running["arguments"] == "<unserializable dict>"
    assert nan_running["argumentsJsonCoerced"] is True
    assert nan_completed["result"] == "<unserializable float>"
    assert nan_completed["resultJsonCoerced"] is True
    assert hostile_completed["result"] == "<unserializable _HostileStringValue>"
    assert hostile_completed["resultJsonCoerced"] is True
    assert "NaN" not in body
    assert "must not escape" not in body
    assert FINAL_TEXT in _chat_content_deltas(body)
    assert body.rstrip().endswith("data: [DONE]")
