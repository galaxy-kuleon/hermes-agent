"""Tests for identity header plumbing in the API server.

Verifies that X-Hermes-User-Id and X-Hermes-Tenant-Id HTTP headers are
extracted from incoming requests and forwarded as user_id / tenant_id
kwargs to AIAgent construction — matching the kwargs that honcho reads
via kwargs.get("user_id") and kwargs.get("tenant_id").

Contract:
  - Headers present and non-empty → kwargs forwarded
  - Headers absent                → kwargs omitted (None to _create_agent; AIAgent
                                    never receives them or receives None)
  - Headers whitespace-only       → treated as absent (omitted)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


# ---------------------------------------------------------------------------
# Minimal helpers — replicate _make_adapter / _create_app pattern from
# the main test_api_server.py so this file has no cross-file dependency.
# ---------------------------------------------------------------------------


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra: dict = {}
    if api_key:
        extra["key"] = api_key
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _make_app(adapter: APIServerAdapter):
    from aiohttp import web
    from gateway.platforms.api_server import (
        cors_middleware,
        security_headers_middleware,
    )

    mws = [
        mw for mw in (cors_middleware, security_headers_middleware) if mw is not None
    ]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


_MINIMAL_BODY = {
    "model": "hermes-agent",
    "messages": [{"role": "user", "content": "hello"}],
}

_FAKE_RESULT = (
    {"final_response": "ok", "messages": [], "api_calls": 1},
    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
)


# ---------------------------------------------------------------------------
# Test 1 — Happy path: headers present → kwargs forwarded to _create_agent
# ---------------------------------------------------------------------------


class TestIdentityHeadersForwarded:
    @pytest.mark.asyncio
    async def test_headers_reach_create_agent_as_kwargs(self):
        """POST with X-Hermes-User-Id + X-Hermes-Tenant-Id → _create_agent receives
        user_id='alice' and tenant_id='acme'."""
        adapter = _make_adapter()
        app = _make_app(adapter)

        # Capture the kwargs that _create_agent is called with.
        create_agent_calls: list = []
        _real_create_agent = adapter._create_agent

        def _spy_create_agent(**kwargs):
            create_agent_calls.append(kwargs)
            # Return a minimal agent stub so _run_agent can finish.
            agent = MagicMock()
            agent.run_conversation.return_value = {
                "final_response": "ok",
                "messages": [],
                "api_calls": 1,
            }
            agent.session_prompt_tokens = 1
            agent.session_completion_tokens = 1
            agent.session_total_tokens = 2
            return agent

        with patch.object(adapter, "_create_agent", side_effect=_spy_create_agent):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json=_MINIMAL_BODY,
                    headers={
                        "X-Hermes-User-Id": "alice",
                        "X-Hermes-Tenant-Id": "acme",
                    },
                )
                assert resp.status == 200

        assert len(create_agent_calls) == 1, "Expected exactly one _create_agent call"
        kwargs = create_agent_calls[0]
        assert kwargs.get("user_id") == "alice", (
            f"Expected user_id='alice', got {kwargs.get('user_id')!r}"
        )
        assert kwargs.get("tenant_id") == "acme", (
            f"Expected tenant_id='acme', got {kwargs.get('tenant_id')!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Absence symmetry: no identity headers → kwargs absent / falsy
# ---------------------------------------------------------------------------


class TestIdentityHeadersAbsent:
    @pytest.mark.asyncio
    async def test_no_headers_no_exception_and_no_identity_kwargs(self):
        """POST without identity headers → no exception; _create_agent receives
        user_id=None and tenant_id=None (honcho's kwargs.get() returns falsy)."""
        adapter = _make_adapter()
        app = _make_app(adapter)

        create_agent_calls: list = []

        def _spy_create_agent(**kwargs):
            create_agent_calls.append(kwargs)
            agent = MagicMock()
            agent.run_conversation.return_value = {
                "final_response": "ok",
                "messages": [],
                "api_calls": 1,
            }
            agent.session_prompt_tokens = 1
            agent.session_completion_tokens = 1
            agent.session_total_tokens = 2
            return agent

        with patch.object(adapter, "_create_agent", side_effect=_spy_create_agent):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json=_MINIMAL_BODY,
                    # No identity headers
                )
                assert resp.status == 200

        assert len(create_agent_calls) == 1
        kwargs = create_agent_calls[0]
        # Both absent or None — honcho's kwargs.get("user_id") must be falsy
        assert not kwargs.get("user_id"), (
            f"Expected user_id to be falsy/absent, got {kwargs.get('user_id')!r}"
        )
        assert not kwargs.get("tenant_id"), (
            f"Expected tenant_id to be falsy/absent, got {kwargs.get('tenant_id')!r}"
        )


# ---------------------------------------------------------------------------
# Test 3 — Whitespace-only header: treated as absent (omitted)
# ---------------------------------------------------------------------------


class TestIdentityHeadersWhitespaceOnly:
    @pytest.mark.asyncio
    async def test_whitespace_only_headers_treated_as_absent(self):
        """POST with X-Hermes-User-Id containing only whitespace → same as absent.
        Empty-after-strip must never propagate as an identity value."""
        adapter = _make_adapter()
        app = _make_app(adapter)

        create_agent_calls: list = []

        def _spy_create_agent(**kwargs):
            create_agent_calls.append(kwargs)
            agent = MagicMock()
            agent.run_conversation.return_value = {
                "final_response": "ok",
                "messages": [],
                "api_calls": 1,
            }
            agent.session_prompt_tokens = 1
            agent.session_completion_tokens = 1
            agent.session_total_tokens = 2
            return agent

        with patch.object(adapter, "_create_agent", side_effect=_spy_create_agent):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json=_MINIMAL_BODY,
                    headers={
                        "X-Hermes-User-Id": "   ",  # whitespace-only
                        "X-Hermes-Tenant-Id": "\t",  # whitespace-only
                    },
                )
                assert resp.status == 200

        assert len(create_agent_calls) == 1
        kwargs = create_agent_calls[0]
        assert not kwargs.get("user_id"), (
            f"Whitespace-only user_id must be absent/falsy, got {kwargs.get('user_id')!r}"
        )
        assert not kwargs.get("tenant_id"), (
            f"Whitespace-only tenant_id must be absent/falsy, got {kwargs.get('tenant_id')!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Tier A: identity kwargs reach memory_manager.initialize_all
#
# Probes one layer deeper than Tests 1-3: rather than stopping at _create_agent,
# this test verifies that user_id="alice" + tenant_id="acme" travel the full
# chain from the HTTP header through AIAgent.__init__ into the
# MemoryManager.initialize_all(**_init_kwargs) call.
#
# Strategy:
#   1. Patch hermes_cli.config.load_config  → returns {"memory": {"provider": "test-spy"}}
#      so AIAgent.__init__ believes a memory provider is configured.
#   2. Patch plugins.memory.load_memory_provider → returns a MagicMock whose
#      .is_available() is True, so the code proceeds past the availability guard.
#   3. Patch agent.memory_manager.MemoryManager → returns a spy instance whose
#      .initialize_all() records every kwargs dict it receives.
#   4. POST to /v1/chat/completions with X-Hermes-User-Id + X-Hermes-Tenant-Id.
#   5. Assert the spy's initialize_all was called with user_id="alice" and
#      tenant_id="acme".
#
# No live hermes server required.  The upstream LLM call is absorbed by the
# _spy_create_agent stub (same pattern as Tests 1-3).
# ---------------------------------------------------------------------------


class TestTierAMemoryManagerReceivesIdentityKwargs:
    @pytest.mark.asyncio
    async def test_tier_a_memory_manager_receives_identity_kwargs(self):
        """X-Hermes-User-Id / X-Hermes-Tenant-Id headers must reach
        MemoryManager.initialize_all as user_id / tenant_id kwargs.

        This closes the Tier A end-to-end identity propagation contract:
        header → _handle_chat_completions → _run_agent → _create_agent →
        AIAgent(**identity_kwargs) → _init_kwargs["user_id"/"tenant_id"] →
        memory_manager.initialize_all(**_init_kwargs).
        """
        # Capture kwargs passed to initialize_all from within AIAgent.__init__.
        initialize_all_calls: list[dict] = []

        # MemoryManager spy: add_provider is a no-op; initialize_all records kwargs.
        mock_mm_instance = MagicMock()
        mock_mm_instance.providers = [MagicMock()]  # non-empty → code enters init block
        mock_mm_instance.initialize_all.side_effect = lambda **kw: (
            initialize_all_calls.append(kw)
        )

        MockMemoryManager = MagicMock(return_value=mock_mm_instance)

        # Memory provider mock: is_available() → True.
        mock_provider = MagicMock()
        mock_provider.is_available.return_value = True

        adapter = _make_adapter()
        app = _make_app(adapter)

        # _create_agent spy that actually constructs AIAgent (not a stub) — but
        # only patches away the LLM call so run_conversation returns immediately.
        # We need real AIAgent construction so the memory init path executes.
        # _run_agent calls self._create_agent(...) → AIAgent(...)
        # We do NOT stub _create_agent here; instead we let it run normally while
        # intercepting memory internals, then stub run_conversation on the returned
        # agent to avoid hitting a real LLM.
        _real_create_agent = adapter._create_agent

        def _create_agent_with_stubbed_llm(**kwargs):
            agent = _real_create_agent(**kwargs)
            # Stub run_conversation so we don't need a live LLM endpoint.
            agent.run_conversation = MagicMock(
                return_value={
                    "final_response": "ok",
                    "messages": [],
                    "api_calls": 1,
                }
            )
            agent.session_prompt_tokens = 1
            agent.session_completion_tokens = 1
            agent.session_total_tokens = 2
            return agent

        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={"memory": {"provider": "test-spy"}},
            ),
            patch(
                "agent.memory_manager.MemoryManager",
                MockMemoryManager,
            ),
            patch(
                "plugins.memory.load_memory_provider",
                return_value=mock_provider,
            ),
            patch.object(
                adapter, "_create_agent", side_effect=_create_agent_with_stubbed_llm
            ),
        ):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json=_MINIMAL_BODY,
                    headers={
                        "X-Hermes-User-Id": "alice",
                        "X-Hermes-Tenant-Id": "acme",
                    },
                )
                assert resp.status == 200

        assert len(initialize_all_calls) >= 1, (
            "Expected memory_manager.initialize_all to be called at least once; "
            "identity kwargs never reached the memory initialization path."
        )
        # The last call is the most complete (session_title may be added after user_id).
        all_kwargs = initialize_all_calls[-1]
        assert all_kwargs.get("user_id") == "alice", (
            f"Expected user_id='alice' in initialize_all kwargs, got: {all_kwargs!r}"
        )
        assert all_kwargs.get("tenant_id") == "acme", (
            f"Expected tenant_id='acme' in initialize_all kwargs, got: {all_kwargs!r}"
        )
