"""Authorization contracts for request-scoped local file grants."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from tools.file_tools import read_file_tool, search_tool


def _file_grant_scope(task_id, paths):
    from tools.file_grants import file_grant_scope

    return file_grant_scope(task_id, paths)


def test_granted_file_is_readable_for_same_task(tmp_path):
    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "report.txt"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("authorized content\n", encoding="utf-8")

    with _file_grant_scope("task-1", [str(allowed)]):
        result = json.loads(read_file_tool(str(allowed), task_id="task-1"))

    assert "error" not in result
    assert "authorized content" in result["content"]


def test_no_active_grant_scope_preserves_local_file_access(tmp_path):
    local_file = tmp_path / "normal-cli-file.txt"
    local_file.write_text("ordinary local content\n", encoding="utf-8")

    result = json.loads(read_file_tool(str(local_file), task_id="cli-task"))

    assert "error" not in result
    assert "ordinary local content" in result["content"]


def test_cross_chat_file_is_denied_before_file_io(tmp_path):
    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.txt"
    denied = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-2" / "private.txt"

    with _file_grant_scope("task-1", [str(allowed)]), patch(
        "tools.file_tools._get_file_ops",
        side_effect=AssertionError("authorization must run before file I/O"),
    ):
        result = json.loads(read_file_tool(str(denied), task_id="task-1"))

    assert result["success"] is False
    assert "not granted" in result["error"].lower()


def test_shared_workspace_search_is_denied_before_search_io(tmp_path):
    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.txt"
    shared_workspace = tmp_path / "workspace"

    with _file_grant_scope("task-1", [str(allowed)]), patch(
        "tools.file_tools._get_file_ops",
        side_effect=AssertionError("authorization must run before search I/O"),
    ):
        result = json.loads(
            search_tool("invoice", path=str(shared_workspace), task_id="task-1")
        )

    assert result["success"] is False
    assert "not granted" in result["error"].lower()


def test_grants_do_not_leak_to_another_task(tmp_path):
    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.txt"

    with _file_grant_scope("task-1", [str(allowed)]), patch(
        "tools.file_tools._get_file_ops",
        side_effect=AssertionError("authorization must run before file I/O"),
    ):
        result = json.loads(read_file_tool(str(allowed), task_id="task-2"))

    assert result["success"] is False
    assert "not granted" in result["error"].lower()


def test_grants_propagate_to_concurrent_tool_worker(tmp_path):
    from tools.file_grants import file_grant_error
    from tools.thread_context import propagate_context_to_thread

    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.txt"
    denied = allowed.with_name("denied.txt")

    def check_worker_scope():
        return (
            file_grant_error(allowed, task_id="task-1", operation="read"),
            file_grant_error(denied, task_id="task-1", operation="read"),
        )

    with _file_grant_scope("task-1", [allowed]):
        worker = propagate_context_to_thread(check_worker_scope)
        with ThreadPoolExecutor(max_workers=1) as executor:
            allowed_error, denied_error = executor.submit(worker).result()

    assert allowed_error is None
    assert "not granted" in denied_error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_vision_handler_forwards_task_id_to_both_local_path_consumers(
    monkeypatch,
    native,
):
    from tools import vision_tools

    monkeypatch.setattr(
        vision_tools,
        "_should_use_native_vision_fast_path",
        lambda: native,
    )
    target_name = "_vision_analyze_native" if native else "vision_analyze_tool"
    target = AsyncMock(return_value=json.dumps({"success": False}))
    monkeypatch.setattr(vision_tools, target_name, target)

    await vision_tools._handle_vision_analyze(
        {"image_url": "/handoff/denied.png", "question": "inspect"},
        task_id="task-1",
    )

    assert target.await_args.kwargs["task_id"] == "task-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["native", "fallback"])
async def test_ungranted_vision_path_is_denied_before_image_io(
    monkeypatch,
    tmp_path,
    entrypoint,
):
    from tools.vision_tools import _vision_analyze_native, vision_analyze_tool

    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.png"
    denied = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-2" / "private.png"
    with _file_grant_scope("task-1", [str(allowed)]), patch.object(
        Path,
        "is_file",
        side_effect=AssertionError("authorization must run before image I/O"),
    ):
        if entrypoint == "native":
            result = await _vision_analyze_native(
                str(denied),
                "inspect",
                task_id="task-1",
            )
        else:
            result = await vision_analyze_tool(
                str(denied),
                "inspect",
                task_id="task-1",
            )

    assert "not granted" in json.loads(result)["error"].lower()


@pytest.mark.parametrize("tool_name", ["submit_conversion", "resubmit_conversion"])
def test_ungranted_socv2_path_is_denied_before_mcp_io(tmp_path, tool_name):
    from tools.mcp_tool import _make_tool_handler, _servers

    allowed = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.pdf"
    denied = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-2" / "private.pdf"
    handler = _make_tool_handler("soc_v2", tool_name, 30)
    fake_server = SimpleNamespace(session=object())

    with _file_grant_scope("task-1", [str(allowed)]), patch.dict(
        _servers,
        {"soc_v2": fake_server},
    ), patch(
        "tools.mcp_tool._run_on_mcp_loop",
        side_effect=AssertionError("authorization must run before MCP I/O"),
    ):
        result = json.loads(
            handler(
                {"path": str(denied), "job_id": "job-1"},
                task_id="task-1",
            )
        )

    assert "not granted" in result["error"].lower()


def test_granted_socv2_path_uses_hidden_trusted_mcp_metadata(
    monkeypatch,
    tmp_path,
):
    from tools.file_grants import _CAPABILITY_META_KEY
    from tools.mcp_tool import _make_tool_handler, _servers

    class AsyncLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    granted = tmp_path / "handoff" / "user" / "user-1" / "chat" / "chat-1" / "allowed.pdf"
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(text='{"ok":true}')],
                isError=False,
            )
        )
    )
    fake_server = SimpleNamespace(
        session=session,
        _rpc_lock=AsyncLock(),
        _pending_call_context=None,
    )
    handler = _make_tool_handler("soc_v2", "submit_conversion", 30)
    monkeypatch.setenv("HERMES_FILE_CAPABILITY_KEY", "test-capability-secret")

    def run_locally(coro_or_factory, timeout=30):
        del timeout
        coroutine = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        return asyncio.run(coroutine)

    with _file_grant_scope("task-1", [str(granted)]), patch.dict(
        _servers,
        {"soc_v2": fake_server},
    ), patch("tools.mcp_tool._run_on_mcp_loop", side_effect=run_locally):
        result = handler(
            {
                "path": str(granted),
                _CAPABILITY_META_KEY: "model-forged-token",
                "_meta": {"model": "forged"},
            },
            task_id="task-1",
        )

    assert "test-capability-secret" not in result
    call = session.call_tool.await_args
    assert call.kwargs["arguments"] == {"path": str(granted)}
    capability = call.kwargs["meta"][_CAPABILITY_META_KEY]
    assert capability != "model-forged-token"
    assert capability not in result
