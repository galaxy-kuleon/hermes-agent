"""U1D batch-1: live stream suffix wire path (behavior, not source tokens).

Kills production mutants that no-op the *call sites* inside:

  * ``APIServerAdapter._write_sse_chat_completion``
    → ``emit_chat_completion_coverage_suffix(...)``
  * ``APIServerAdapter._write_sse_responses``
    → ``emit_responses_coverage_suffix(...)``

These are the only paths that attach coverage to the bytes a streaming
8083 client actually receives. Helper-level green is not enough: if either
call is emptied (even while the function name survives as a dead comment),
the corresponding test must fail because the captured SSE payload no longer
contains the coverage footer.

Independence: each test exercises one wire path only. Emptying only the
chat-completions call must red the chat test; emptying only the Responses
call must red the Responses test.

Run:
  cd hermes-agent && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \\
    python -m pytest tests/gateway/test_u1d_stream_suffix_wire.py -q
"""

from __future__ import annotations

import asyncio
import json
import queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools.attachment_ledger import COVERAGE_FOOTER_TITLE


# Canary that only appears via coverage_footer on the agent result — never in
# the model stream deltas. Proves the suffix call site ran.
_CANARY_HANDLE = "F99-U1D-WIRE"
_COVERAGE_FOOTER = (
    f"\n{COVERAGE_FOOTER_TITLE}\n\n"
    "The following attachments were **not fully included** in this answer.\n"
    f"- `{_CANARY_HANDLE}` (wire.zip): **unreadable** — no direct reader\n"
    "Summary: read=0 partial=0 unreadable=1 unread=0 total=1.\n"
)
_MODEL_PROSE = "MODEL-PROSE-ONLY-no-coverage-here"


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, token="test-key"))


def _make_request() -> MagicMock:
    req = MagicMock()
    req.headers = {}
    return req


def _agent_result_with_coverage() -> tuple[dict, dict]:
    return (
        {
            # final_response may include footer for non-stream consumers, but
            # model stream deltas intentionally do NOT (see stream_q contents).
            "final_response": _MODEL_PROSE + _COVERAGE_FOOTER,
            "coverage_footer": _COVERAGE_FOOTER,
            "coverage_status": "ok",
        },
        {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
    )


def _decode_writes(written: list[bytes]) -> str:
    return b"".join(written).decode("utf-8", errors="replace")


def _chat_content_deltas(blob: str) -> list[str]:
    """Extract delta.content strings from chat.completion.chunk SSE data lines."""
    out: list[str] = []
    for line in blob.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("object") != "chat.completion.chunk":
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            out.append(content)
    return out


def _responses_text_deltas(blob: str) -> list[str]:
    out: list[str] = []
    for line in blob.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :].strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "response.output_text.delta":
            continue
        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            out.append(delta)
    return out


def _responses_done_text(blob: str) -> str:
    """Terminal output_text.done text (assembled assistant body on wire)."""
    for line in blob.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :].strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "response.output_text.done":
            text = obj.get("text")
            if isinstance(text, str):
                return text
    return ""


@pytest.fixture
def adapter() -> APIServerAdapter:
    return _make_adapter()


class TestChatCompletionsCoverageSuffixWire:
    """Behavior: coverage must appear in chat-completions SSE bytes."""

    def test_coverage_footer_reaches_chat_completion_sse_payload(self, adapter):
        stream_q: queue.Queue = queue.Queue()
        # Model prose only — coverage must NOT be pre-baked into stream deltas.
        stream_q.put(_MODEL_PROSE)
        stream_q.put(None)

        async def fake_agent():
            return _agent_result_with_coverage()

        async def run() -> str:
            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)
            written: list[bytes] = []

            async def write(data):
                written.append(
                    data if isinstance(data, (bytes, bytearray)) else bytes(data)
                )

            mock_response = AsyncMock()
            mock_response.write = AsyncMock(side_effect=write)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(),
                    "cmpl-u1d-wire",
                    "gpt-test",
                    1_700_000_000,
                    stream_q,
                    agent_task,
                )
            return _decode_writes(written)

        blob = asyncio.run(run())
        deltas = _chat_content_deltas(blob)
        joined = "".join(deltas)

        # Model prose was streamed.
        assert _MODEL_PROSE in joined
        # Coverage arrived via the live suffix call (not model deltas alone).
        # COUNT, not presence. The Responses writer appended the footer to
        # final_text_parts twice -- once inside _emit_text_delta and once
        # beside it -- so the wire delta was right while output_text.done and
        # the stored text carried it twice. `in` could not see that; this can.
        # Proved by review 2026-08-12.
        assert joined.count(COVERAGE_FOOTER_TITLE) == 1, (
            f"coverage footer appears {joined.count(COVERAGE_FOOTER_TITLE)}x, expected once"
        )
        assert _CANARY_HANDLE in joined
        # Suffix is a distinct content chunk (not only inside model prose chunk).
        assert any(
            COVERAGE_FOOTER_TITLE in d and _MODEL_PROSE not in d for d in deltas
        ), (
            "coverage must be a separate chat.completion.chunk content delta "
            f"(proves emit_chat_completion_coverage_suffix call site); deltas={deltas!r}"
        )
        # Stream ends cleanly after coverage.
        assert "data: [DONE]" in blob


class TestResponsesCoverageSuffixWire:
    """Behavior: coverage must appear in Responses API SSE bytes."""

    def test_coverage_footer_reaches_responses_sse_payload(self, adapter):
        stream_q: queue.Queue = queue.Queue()
        stream_q.put(_MODEL_PROSE)
        stream_q.put(None)

        async def fake_agent():
            return _agent_result_with_coverage()

        async def run() -> str:
            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)
            written: list[bytes] = []

            async def write(data):
                written.append(
                    data if isinstance(data, (bytes, bytearray)) else bytes(data)
                )

            mock_response = AsyncMock()
            mock_response.write = AsyncMock(side_effect=write)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_responses(
                    _make_request(),
                    "resp-u1d-wire",
                    "gpt-test",
                    1_700_000_000,
                    stream_q,
                    agent_task,
                    agent_ref=None,
                    conversation_history=[],
                    user_message="audit please",
                    instructions=None,
                    conversation=None,
                    store=False,
                    session_id="sess-u1d-wire",
                )
            return _decode_writes(written)

        blob = asyncio.run(run())
        deltas = _responses_text_deltas(blob)
        joined = "".join(deltas)
        done_text = _responses_done_text(blob)

        assert _MODEL_PROSE in joined
        # COUNT, not presence. The Responses writer appended the footer to
        # final_text_parts twice -- once inside _emit_text_delta and once
        # beside it -- so the wire delta was right while output_text.done and
        # the stored text carried it twice. `in` could not see that; this can.
        # Proved by review 2026-08-12.
        assert joined.count(COVERAGE_FOOTER_TITLE) == 1, (
            f"coverage footer appears {joined.count(COVERAGE_FOOTER_TITLE)}x, expected once"
        )
        assert _CANARY_HANDLE in joined
        # Distinct delta from the suffix call site (not only model prose).
        assert any(
            COVERAGE_FOOTER_TITLE in d and _MODEL_PROSE not in d for d in deltas
        ), (
            "coverage must be a separate response.output_text.delta "
            f"(proves emit_responses_coverage_suffix call site); deltas={deltas!r}"
        )
        # Terminal done text (what clients assemble) also carries coverage.
        assert COVERAGE_FOOTER_TITLE in done_text
        assert _CANARY_HANDLE in done_text
        assert _MODEL_PROSE in done_text
