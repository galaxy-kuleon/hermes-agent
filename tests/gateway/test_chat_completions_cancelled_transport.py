"""HTTP-boundary regression for a cancelled prepared Chat Completions stream.

This deliberately drives aiohttp's real TestServer/TestClient socket.  A fake
``StreamResponse`` can prove which methods production called, but it cannot
prove that the peer receives a valid chunked terminal or that aiohttp's parser
does not raise ``ClientPayloadError(TransferEncodingError)``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server


_ANSWER_DELTA = "prefix-before-cancel"
_REQUEST_HEADERS = {
    "Authorization": "Bearer test-api-key",
    "X-OpenWebUI-User-Id": "test-user",
    "X-OpenWebUI-User-Name": "Cancellation Transport Test",
    "X-OpenWebUI-User-Role": "admin",
    "X-OpenWebUI-User-Groups": "test-group",
    "X-OpenWebUI-Chat-Id": "test-chat",
}


async def _cancel_after_answer_crosses_socket(subject: Any) -> dict[str, Any]:
    """Return transport observations after cancelling a prepared response.

    ``subject`` is a module-shaped object.  The production test passes the
    imported current module; accepting it explicitly also lets the cold-review
    harness execute the same boundary against historical source loaded in
    memory, without a checkout or a test-only production hook.
    """
    adapter = subject.APIServerAdapter(
        subject.PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )
    loop = asyncio.get_running_loop()
    handler_seen: asyncio.Future[asyncio.Task[Any]] = loop.create_future()
    release_agent = asyncio.Event()

    class _ControlledAgent:
        def interrupt(self, _reason: str) -> None:
            # Production cancellation cooperatively interrupts the agent before
            # draining its task.  Make that ownership transition observable and
            # bounded rather than waiting for the five-second timeout.
            release_agent.set()

    async def _controlled_run_agent(**kwargs):
        agent_ref = kwargs.get("agent_ref")
        if agent_ref is not None:
            agent_ref[0] = _ControlledAgent()
        kwargs["stream_delta_callback"](_ANSWER_DELTA)
        await release_agent.wait()
        return (
            {"final_response": "unreachable normal result", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def _capture_real_handler(request):
        if not handler_seen.done():
            handler_seen.set_result(asyncio.current_task())
        return await adapter._handle_chat_completions(request)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", _capture_real_handler)
    prefix_lines: list[bytes] = []
    tail = b""
    read_exception: BaseException | None = None
    stream_exception: BaseException | None = None
    at_eof = False
    handler_task = None

    try:
        async with TestClient(TestServer(app)) as client:
            with patch.object(
                adapter, "_run_agent", side_effect=_controlled_run_agent
            ):
                response = await client.post(
                    "/v1/chat/completions",
                    headers=_REQUEST_HEADERS,
                    json={
                        "model": "test",
                        "messages": [{"role": "user", "content": "boundary probe"}],
                        "stream": True,
                    },
                )
                assert response.status == 200

                # Do not infer prepare/write from server state.  The answer byte
                # must cross the client read boundary before cancellation.
                while True:
                    line = await asyncio.wait_for(response.content.readline(), timeout=2)
                    assert line, "response reached EOF before the controlled answer delta"
                    prefix_lines.append(line)
                    if _ANSWER_DELTA.encode() in line:
                        break

                handler_task = await asyncio.wait_for(handler_seen, timeout=2)
                assert handler_task is not None and not handler_task.done()
                handler_task.cancel()

                try:
                    tail = await asyncio.wait_for(response.content.read(), timeout=3)
                except Exception as exc:  # the historical control lands here
                    read_exception = exc
                at_eof = response.content.at_eof()
                stream_exception = response.content.exception()
    finally:
        release_agent.set()
        if handler_task is not None:
            with contextlib.suppress(BaseException):
                await handler_task
        # Let the controlled agent task and its done callback unwind before the
        # test loop closes; pending-task noise would obscure the wire verdict.
        await asyncio.sleep(0)

    body = b"".join(prefix_lines) + tail
    return {
        "body": body,
        "read_exception": read_exception,
        "stream_exception": stream_exception,
        "at_eof": at_eof,
    }


@pytest.mark.asyncio
async def test_cancelling_a_prepared_stream_sends_a_complete_http_and_sse_terminal():
    observed = await _cancel_after_answer_crosses_socket(api_server)
    exc = observed["read_exception"]
    cause = getattr(exc, "__cause__", None)

    assert exc is None, (
        "prepared response was truncated: "
        f"{type(exc).__name__} wrapping {type(cause).__name__}"
    )
    assert observed["stream_exception"] is None
    assert observed["at_eof"] is True

    body = observed["body"]
    assert _ANSWER_DELTA.encode() in body
    assert body.count(b"data: [DONE]") == 1
    assert body.count(b'"finish_reason": "stop"') == 1
