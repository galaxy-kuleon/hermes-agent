"""Behavioural test for POST /v1/memory/tool (W3 T1).

Exercises the admin / inspection endpoint that dispatches to
memory_manager.handle_tool_call with user_id / tenant_id scoping kwargs.

These tests mock plugins.memory.load_memory_provider + hermes_cli.config
so they do not require a real provider plugin to be configured. The
dispatch chain (body → MemoryManager → provider.handle_tool_call) is
exercised end-to-end on real code paths.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


class _SpyProvider:
    """Minimal MemoryProvider stub that records handle_tool_call kwargs."""

    name = "spy"
    _calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._calls.append({"op": "initialize", "session_id": session_id, "kwargs": kwargs})

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        pass

    def on_session_end(self, messages):
        pass

    def on_memory_write(self, action, target, content):
        pass

    def get_tool_schemas(self):
        return [{"name": "fact_store", "description": "spy", "parameters": {"type": "object"}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self._calls.append({
            "op": "handle_tool_call",
            "tool_name": tool_name,
            "args": args,
            "kwargs": kwargs,
        })
        return json.dumps({"recorded": True, "tool_name": tool_name, "kwargs": kwargs})

    def shutdown(self) -> None:
        pass


class _HonchoCompatProvider:
    """Minimal honcho-like provider for fact_store compatibility tests."""

    name = "honcho"

    def __init__(self):
        self.card: list[str] = []
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.calls.append({"op": "initialize", "session_id": session_id, "kwargs": kwargs})

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        pass

    def on_session_end(self, messages):
        pass

    def on_memory_write(self, action, target, content):
        pass

    def get_tool_schemas(self):
        return [
            {"name": "honcho_profile", "description": "profile", "parameters": {"type": "object"}},
            {"name": "honcho_search", "description": "search", "parameters": {"type": "object"}},
        ]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append({"op": "handle_tool_call", "tool_name": tool_name, "args": args, "kwargs": kwargs})
        if tool_name == "honcho_profile":
            card_update = args.get("card")
            if card_update is not None:
                self.card = list(card_update)
                return json.dumps({"result": f"Peer card updated ({len(self.card)} facts).", "card": self.card})
            return json.dumps({"result": list(self.card)})
        if tool_name == "honcho_search":
            query = str(args.get("query") or "").lower()
            hits = [fact for fact in self.card if query in fact.lower()]
            if not hits:
                return json.dumps({"result": "No relevant context found."})
            return json.dumps({"result": "\n".join(f"- {fact}" for fact in hits)})
        return json.dumps({"error": f"unexpected tool {tool_name}"})

    def shutdown(self) -> None:
        pass


def _make_adapter():
    """Construct an APIServerAdapter minimally for handler invocation."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._api_key = ""  # disable auth for test (local-only use semantics)
    return adapter


async def _client(adapter):
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/memory/tool", adapter._handle_memory_tool)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_memory_tool_dispatches_with_identity_kwargs():
    """POST /v1/memory/tool with user_id/tenant_id → handle_tool_call receives them."""
    _SpyProvider._calls.clear()
    spy = _SpyProvider()

    with patch("plugins.memory.load_memory_provider", return_value=spy), \
         patch("hermes_cli.config.load_config", return_value={"memory": {"provider": "spy"}}):
        adapter = _make_adapter()
        client = await _client(adapter)
        try:
            resp = await client.post(
                "/v1/memory/tool",
                json={
                    "tool_name": "fact_store",
                    "args": {"action": "list", "limit": 5},
                    "user_id": "alice",
                    "tenant_id": "acme",
                },
            )
            assert resp.status == 200
            body = await resp.json()
            # Provider returned a JSON string; adapter should parse + wrap
            inner = body["result"]
            assert inner["recorded"] is True
            assert inner["tool_name"] == "fact_store"
            # handle_tool_call MUST have seen user_id + tenant_id kwargs
            assert inner["kwargs"]["user_id"] == "alice"
            assert inner["kwargs"]["tenant_id"] == "acme"
        finally:
            await client.close()

    # Also assert initialize_all received the same kwargs (scoping discipline)
    init_calls = [c for c in _SpyProvider._calls if c["op"] == "initialize"]
    assert init_calls, "provider.initialize was not called"
    assert init_calls[-1]["kwargs"].get("user_id") == "alice"
    assert init_calls[-1]["kwargs"].get("tenant_id") == "acme"


@pytest.mark.asyncio
async def test_memory_tool_omits_falsy_identity():
    """Empty or missing user_id/tenant_id → kwargs omitted, not empty-string-forwarded."""
    _SpyProvider._calls.clear()
    spy = _SpyProvider()

    with patch("plugins.memory.load_memory_provider", return_value=spy), \
         patch("hermes_cli.config.load_config", return_value={"memory": {"provider": "spy"}}):
        adapter = _make_adapter()
        client = await _client(adapter)
        try:
            # Test 1: no identity fields at all
            resp = await client.post(
                "/v1/memory/tool",
                json={"tool_name": "fact_store", "args": {"action": "list"}},
            )
            assert resp.status == 200
            body = await resp.json()
            tool_call = [c for c in _SpyProvider._calls if c["op"] == "handle_tool_call"][-1]
            assert "user_id" not in tool_call["kwargs"]
            assert "tenant_id" not in tool_call["kwargs"]

            # Test 2: whitespace-only identity fields → same as absent
            _SpyProvider._calls.clear()
            resp = await client.post(
                "/v1/memory/tool",
                json={
                    "tool_name": "fact_store",
                    "args": {"action": "list"},
                    "user_id": "   ",
                    "tenant_id": "",
                },
            )
            assert resp.status == 200
            tool_call = [c for c in _SpyProvider._calls if c["op"] == "handle_tool_call"][-1]
            assert "user_id" not in tool_call["kwargs"]
            assert "tenant_id" not in tool_call["kwargs"]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_memory_tool_rejects_malformed_body():
    """Missing tool_name or bad args → 400."""
    spy = _SpyProvider()
    with patch("plugins.memory.load_memory_provider", return_value=spy), \
         patch("hermes_cli.config.load_config", return_value={"memory": {"provider": "spy"}}):
        adapter = _make_adapter()
        client = await _client(adapter)
        try:
            # Missing tool_name → 400
            resp = await client.post("/v1/memory/tool", json={"args": {"action": "list"}})
            assert resp.status == 400

            # Missing args → permissive (treated as empty dict, forwarded to provider)
            # Not a 400 because an empty-args call is a valid "list all" semantic.
            resp = await client.post("/v1/memory/tool", json={"tool_name": "fact_store"})
            assert resp.status == 200

            # args not a dict → 400 (type violation)
            resp = await client.post(
                "/v1/memory/tool",
                json={"tool_name": "fact_store", "args": "not-a-dict"},
            )
            assert resp.status == 400
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_memory_tool_fact_store_compat_routes_to_honcho_profile_and_search():
    """fact_store admin calls remain usable when honcho is the active provider."""
    provider = _HonchoCompatProvider()

    with patch("plugins.memory.load_memory_provider", return_value=provider), \
         patch("hermes_cli.config.load_config", return_value={"memory": {"provider": "honcho"}}):
        adapter = _make_adapter()
        client = await _client(adapter)
        try:
            add_resp = await client.post(
                "/v1/memory/tool",
                json={
                    "tool_name": "fact_store",
                    "args": {"action": "add", "content": "Alice likes ramen"},
                    "user_id": "alice",
                },
            )
            assert add_resp.status == 200
            add_body = await add_resp.json()
            assert add_body["result"] == {"fact_id": 1, "status": "added"}

            list_resp = await client.post(
                "/v1/memory/tool",
                json={
                    "tool_name": "fact_store",
                    "args": {"action": "list"},
                    "user_id": "alice",
                },
            )
            assert list_resp.status == 200
            list_body = await list_resp.json()
            assert list_body["result"]["facts"] == [{"id": 1, "content": "Alice likes ramen"}]
            assert list_body["result"]["count"] == 1

            search_resp = await client.post(
                "/v1/memory/tool",
                json={
                    "tool_name": "fact_store",
                    "args": {"action": "search", "query": "ramen", "limit": 5},
                    "user_id": "alice",
                },
            )
            assert search_resp.status == 200
            search_body = await search_resp.json()
            assert search_body["result"]["results"] == [{"id": 1, "content": "Alice likes ramen"}]

            remove_resp = await client.post(
                "/v1/memory/tool",
                json={
                    "tool_name": "fact_store",
                    "args": {"action": "remove", "fact_id": 1},
                    "user_id": "alice",
                },
            )
            assert remove_resp.status == 200
            remove_body = await remove_resp.json()
            assert remove_body["result"] == {"removed": True}

            honcho_tools = [call["tool_name"] for call in provider.calls if call["op"] == "handle_tool_call"]
            assert "fact_store" not in honcho_tools
            assert honcho_tools.count("honcho_profile") >= 3
        finally:
            await client.close()
