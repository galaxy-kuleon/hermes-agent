"""Behavioural guards for authenticated API-agent owner observation."""

import asyncio
import threading
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server as subject
from gateway.platforms.api_server import APIServerAdapter
from tests.gateway.api_server_test_client import ScopedTestClient as TestClient


async def _until(predicate, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


def _require_owner_boundary(adapter):
    assert hasattr(adapter, "_api_agent_owners"), (
        "production adapter has no API-agent owner registry"
    )
    assert hasattr(adapter, "_run_owned_in_executor"), (
        "production executor has no worker-owned lifecycle"
    )


class _BlockingAgent:
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0

    def run_conversation(self, **_kwargs):
        self.entered.set()
        self.release.wait(timeout=5)
        return {"final_response": "synthetic"}


@pytest.mark.asyncio
async def test_real_run_agent_counts_the_worker_not_only_a_stream_body():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    _require_owner_boundary(adapter)
    entered, release = threading.Event(), threading.Event()
    agent = _BlockingAgent(entered, release)
    with patch.object(adapter, "_create_agent", return_value=agent):
        task = asyncio.create_task(adapter._run_agent(
            user_message="synthetic", conversation_history=[],
            session_id="owner-test",
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert len(subject._LIVE_STREAM_BODIES) == 0
            assert adapter._api_agent_owners.observe() == {
                "schema": 1,
                "status": "observed",
                "active": 1,
                "reason": None,
                "scope": "api_agent_executions",
            }
        finally:
            release.set()
        await task
    assert adapter._api_agent_owners.observe()["active"] == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_hide_a_still_running_executor_owner():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    _require_owner_boundary(adapter)
    entered, release = threading.Event(), threading.Event()
    agent = _BlockingAgent(entered, release)
    with patch.object(adapter, "_create_agent", return_value=agent):
        task = asyncio.create_task(adapter._run_agent(
            user_message="synthetic", conversation_history=[],
            session_id="owner-cancel-test",
        ))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # asyncio cancellation cannot stop the executor thread. The worker,
        # not its dead waiter, still owns the record.
        assert adapter._api_agent_owners.observe()["active"] == 1
        release.set()
        await _until(
            lambda: adapter._api_agent_owners.observe().get("active") == 0
        )


@pytest.mark.asyncio
async def test_failed_admission_is_unknown_null_and_does_not_break_the_agent():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    _require_owner_boundary(adapter)

    class RefusesAdmission(set):
        def add(self, _item):
            raise OSError("synthetic registry failure")

    adapter._api_agent_owners._owners = RefusesAdmission()

    class ImmediateAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def run_conversation(self, **_kwargs):
            return {"final_response": "synthetic"}

    with patch.object(adapter, "_create_agent", return_value=ImmediateAgent()):
        result, _usage = await adapter._run_agent(
            user_message="synthetic", conversation_history=[]
        )
    assert result["final_response"] == "synthetic"
    assert adapter._api_agent_owners.observe() == {
        "schema": 1,
        "status": "unknown",
        "active": None,
        "reason": "admission_unobserved",
        "scope": "api_agent_executions",
    }


def test_a_lost_owner_record_poison_is_unknown_not_zero():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    _require_owner_boundary(adapter)
    token = adapter._api_agent_owners.admit()
    assert token is not None
    # Hostile assignment: the population record disappears while its owner
    # still exists. Completion must refuse a numeric observation thereafter.
    adapter._api_agent_owners._owners.clear()
    adapter._api_agent_owners.finish(token)
    assert adapter._api_agent_owners.observe() == {
        "schema": 1,
        "status": "unknown",
        "active": None,
        "reason": "owner_record_lost",
        "scope": "api_agent_executions",
    }


@pytest.mark.asyncio
async def test_v1_runs_uses_the_same_actual_worker_population():
    adapter = APIServerAdapter(PlatformConfig(
        enabled=True, extra={"key": "sk-owner-secret"}
    ))
    _require_owner_boundary(adapter)
    assert hasattr(adapter, "_handle_owner_observation"), (
        "production adapter has no authenticated observation handler"
    )
    entered, release = threading.Event(), threading.Event()
    agent = _BlockingAgent(entered, release)
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get(
        "/v1/owner-observation", adapter._handle_owner_observation
    )
    async with TestClient(TestServer(app)) as client:
        with patch.object(adapter, "_create_agent", return_value=agent):
            response = await client.post(
                "/v1/runs",
                json={"input": "synthetic"},
                headers={"Authorization": "Bearer sk-owner-secret"},
            )
            assert response.status == 202
            assert await asyncio.to_thread(entered.wait, 2)
            observed = await client.get(
                "/v1/owner-observation",
                headers={"Authorization": "Bearer sk-owner-secret"},
            )
            assert observed.status == 200
            assert (await observed.json())["active"] == 1
            release.set()
            await _until(
                lambda: adapter._api_agent_owners.observe().get("active") == 0
            )


@pytest.mark.asyncio
async def test_wire_keeps_zero_unknown_and_public_health_disjoint():
    adapter = APIServerAdapter(PlatformConfig(
        enabled=True, extra={"key": "sk-owner-secret"}
    ))
    _require_owner_boundary(adapter)
    assert hasattr(adapter, "_handle_owner_observation"), (
        "production adapter has no authenticated observation handler"
    )
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_get(
        "/v1/owner-observation", adapter._handle_owner_observation
    )
    async with TestClient(TestServer(app)) as client:
        refused = await client.get("/v1/owner-observation")
        assert refused.status == 401

        headers = {"Authorization": "Bearer sk-owner-secret"}
        zero = await client.get("/v1/owner-observation", headers=headers)
        assert await zero.json() == {
            "schema": 1,
            "status": "observed",
            "active": 0,
            "reason": None,
            "scope": "api_agent_executions",
        }

        adapter._api_agent_owners.invalidate("free form must not cross wire")
        unknown = await client.get("/v1/owner-observation", headers=headers)
        assert await unknown.json() == {
            "schema": 1,
            "status": "unknown",
            "active": None,
            "reason": "instrumentation_failure",
            "scope": "api_agent_executions",
        }

        health = await client.get("/health")
        health_payload = await health.json()
        assert {"active", "reason", "scope"}.isdisjoint(health_payload)


@pytest.mark.asyncio
async def test_real_connect_registers_the_authenticated_route(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(
        enabled=True,
        extra={
            "key": "sk-owner-observation-secret-20260820",
            "host": "127.0.0.1",
            "port": 8642,
        },
    ))

    class FakeRunner:
        def __init__(self, app):
            self.app = app

        async def setup(self):
            return None

        async def cleanup(self):
            return None

    class FakeSite:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr(subject.web, "AppRunner", FakeRunner)
    monkeypatch.setattr(subject.web, "TCPSite", FakeSite)
    monkeypatch.setattr(adapter, "_load_session_activity", lambda: None)
    monkeypatch.setattr(adapter, "_mark_connected", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)
    assert await adapter.connect() is True
    try:
        paths = {
            route.resource.canonical for route in adapter._app.router.routes()
        }
        assert "/v1/owner-observation" in paths
    finally:
        await adapter.disconnect()
