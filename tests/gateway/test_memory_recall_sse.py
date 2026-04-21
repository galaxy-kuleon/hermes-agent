"""Behavioural probe: hermes.memory.recalled SSE event emission.

Verifies two invariants:
1. When memory_recall_callback is called with non-empty prefetch text, the SSE
   stream contains ``event: hermes.memory.recalled`` with the expected JSON shape.
2. When memory_recall_callback is NOT called (empty prefetch), no such event
   appears in the stream.
"""

import json
import asyncio
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)

# ---------------------------------------------------------------------------
# Test helpers (mirrors test_api_server.py conventions)
# ---------------------------------------------------------------------------

_CANNED_PREFETCH = "user mentioned their favourite colour is blue"


def _make_adapter() -> APIServerAdapter:
    config = PlatformConfig(enabled=True, extra={})
    return APIServerAdapter(config)


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _parse_sse_events(body: str) -> list[dict]:
    """Return list of {event_type, data} for every event block in the SSE body."""
    events = []
    current_event = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            current_event = None
            continue
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            events.append({"event_type": current_event, "data": data_str})
            current_event = None
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemoryRecallSSE:
    @pytest.mark.asyncio
    async def test_memory_recalled_event_emitted_when_prefetch_nonempty(self):
        """memory_recall_callback with non-empty text → event: hermes.memory.recalled in SSE."""
        adapter = _make_adapter()
        app = _create_app(adapter)

        async def _mock_run_agent(**kwargs):
            recall_cb = kwargs.get("memory_recall_callback")
            delta_cb = kwargs.get("stream_delta_callback")
            if recall_cb:
                recall_cb(_CANNED_PREFETCH)
            if delta_cb:
                await asyncio.sleep(0.02)
                delta_cb("Hello.")
            return (
                {"final_response": "Hello.", "messages": [], "api_calls": 1},
                {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            )

        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes",
                        "messages": [{"role": "user", "content": "what is my favourite colour?"}],
                        "stream": True,
                    },
                )
                assert resp.status == 200
                body = await resp.text()

        # Primary assertion: custom event type is present
        assert "event: hermes.memory.recalled" in body, (
            f"Expected 'event: hermes.memory.recalled' in SSE body.\nBody:\n{body}"
        )

        # Validate JSON payload shape
        events = _parse_sse_events(body)
        recall_events = [
            e for e in events if e["event_type"] == "hermes.memory.recalled"
        ]
        assert len(recall_events) == 1, f"Expected exactly 1 recall event, got {len(recall_events)}"

        payload = json.loads(recall_events[0]["data"])
        assert payload["provider"] == "holographic"
        assert "context_preview" in payload
        assert payload["context_preview"] == _CANNED_PREFETCH[:200]
        assert "context_token_estimate" in payload
        assert payload["context_token_estimate"] == len(_CANNED_PREFETCH) // 4

    @pytest.mark.asyncio
    async def test_memory_recalled_event_absent_when_prefetch_empty(self):
        """memory_recall_callback NOT called → no hermes.memory.recalled event in SSE."""
        adapter = _make_adapter()
        app = _create_app(adapter)

        async def _mock_run_agent(**kwargs):
            # Intentionally do NOT call memory_recall_callback — simulates empty prefetch
            delta_cb = kwargs.get("stream_delta_callback")
            if delta_cb:
                await asyncio.sleep(0.02)
                delta_cb("No memory context here.")
            return (
                {"final_response": "No memory context here.", "messages": [], "api_calls": 1},
                {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
            )

        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                )
                assert resp.status == 200
                body = await resp.text()

        assert "event: hermes.memory.recalled" not in body, (
            f"hermes.memory.recalled must NOT appear when prefetch is empty.\nBody:\n{body}"
        )
