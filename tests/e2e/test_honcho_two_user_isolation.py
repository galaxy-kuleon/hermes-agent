"""E2E smoke test: two-user memory isolation via honcho provider.

Tests that facts stored for user alice are NOT visible when querying as user bob
and vice versa, exercising the full hermes-agent → honcho path through
POST /v1/memory/tool.

Skip conditions (test must never fail due to infrastructure absence):
  - HONCHO_BASE_URL env var is not set
  - The honcho server at HONCHO_BASE_URL is unreachable
  - The hermes-agent API server is not running (HERMES_API_URL not set)

When both servers are up this test is fully self-contained and idempotent.
It uses per-run unique facts so repeated runs cannot leak state from prior runs.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

# ---------------------------------------------------------------------------
# Infrastructure availability guards
# ---------------------------------------------------------------------------

HONCHO_BASE_URL = os.environ.get("HONCHO_BASE_URL", "").strip()
HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642").strip()
HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "").strip()

_SKIP_REASON_HONCHO = (
    "HONCHO_BASE_URL is not set — skipping honcho e2e isolation test. "
    "Set HONCHO_BASE_URL=http://127.0.0.1:8000 when the honcho server is running."
)
_SKIP_REASON_HERMES = (
    "Hermes-agent API server is unreachable — skipping honcho e2e isolation test. "
    "Set HERMES_API_URL to the running hermes-agent API base URL."
)


def _honcho_reachable() -> bool:
    """Return True if the honcho server responds to a healthcheck."""
    if not HONCHO_BASE_URL:
        return False
    try:
        import urllib.request
        url = HONCHO_BASE_URL.rstrip("/") + "/health"
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310
            return resp.status == 200
    except Exception:
        return False


def _hermes_api_reachable() -> bool:
    """Return True if the hermes-agent API server is responding."""
    try:
        import urllib.request
        url = HERMES_API_URL.rstrip("/") + "/v1/models"
        req = urllib.request.Request(url)
        if HERMES_API_KEY:
            req.add_header("Authorization", f"Bearer {HERMES_API_KEY}")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            return resp.status in (200, 401)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _memory_tool(
    tool_name: str,
    args: dict,
    *,
    user_id: str,
    tenant_id: str = "test-tenant",
    api_url: str = HERMES_API_URL,
) -> dict:
    """POST /v1/memory/tool and return the parsed response body."""
    import json
    import urllib.request

    body = json.dumps(
        {"tool_name": tool_name, "args": args, "user_id": user_id, "tenant_id": tenant_id}
    ).encode()
    headers = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/memory/tool",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHonchoTwoUserIsolation:
    """Per-user memory isolation through the hermes-agent memory tool endpoint.

    Each test method is independently skip-guarded so a partial infrastructure
    setup (honcho up but hermes not yet running) gives clear skip messages
    rather than hard failures.
    """

    @pytest.fixture(autouse=True)
    def _require_infrastructure(self):
        if not HONCHO_BASE_URL:
            pytest.skip(_SKIP_REASON_HONCHO)
        if not _honcho_reachable():
            pytest.skip(
                f"Honcho server at {HONCHO_BASE_URL} is unreachable — "
                "start the honcho docker-compose stack first."
            )
        if not _hermes_api_reachable():
            pytest.skip(
                f"Hermes-agent API at {HERMES_API_URL} is unreachable — "
                f"{_SKIP_REASON_HERMES}"
            )

    def test_alice_fact_not_visible_to_bob(self):
        """Fact stored for alice MUST NOT appear when querying as bob."""
        run_id = uuid.uuid4().hex[:8]
        alice_fact = f"alice-unique-fact-{run_id}"
        bob_fact = f"bob-unique-fact-{run_id}"

        # Store a unique fact for alice as a conclusion
        _memory_tool(
            "honcho_conclude",
            {"conclusion": alice_fact, "peer": "user"},
            user_id="alice",
        )

        # Store a different unique fact for bob
        _memory_tool(
            "honcho_conclude",
            {"conclusion": bob_fact, "peer": "user"},
            user_id="bob",
        )

        # Brief pause — honcho background write threads may be async
        time.sleep(1.0)

        # Search bob's memory for alice's fact — must NOT appear
        bob_view = _memory_tool(
            "honcho_search",
            {"query": alice_fact, "max_tokens": 2000},
            user_id="bob",
        )
        bob_result_text = str(bob_view)
        assert alice_fact not in bob_result_text, (
            f"alice's fact '{alice_fact}' leaked into bob's search results: {bob_view}"
        )

        # Verify alice's fact IS visible to alice
        alice_view = _memory_tool(
            "honcho_search",
            {"query": alice_fact, "max_tokens": 2000},
            user_id="alice",
        )
        alice_result_text = str(alice_view)
        assert alice_fact in alice_result_text, (
            f"alice's own fact '{alice_fact}' not found in alice's search results: {alice_view}"
        )

    def test_bob_fact_not_visible_to_alice(self):
        """Fact stored for bob MUST NOT appear when querying as alice."""
        run_id = uuid.uuid4().hex[:8]
        alice_fact = f"alice-unique-fact-{run_id}"
        bob_fact = f"bob-unique-fact-{run_id}"

        _memory_tool(
            "honcho_conclude",
            {"conclusion": alice_fact, "peer": "user"},
            user_id="alice",
        )
        _memory_tool(
            "honcho_conclude",
            {"conclusion": bob_fact, "peer": "user"},
            user_id="bob",
        )

        time.sleep(1.0)

        # Search alice's memory for bob's fact — must NOT appear
        alice_view = _memory_tool(
            "honcho_search",
            {"query": bob_fact, "max_tokens": 2000},
            user_id="alice",
        )
        alice_result_text = str(alice_view)
        assert bob_fact not in alice_result_text, (
            f"bob's fact '{bob_fact}' leaked into alice's search results: {alice_view}"
        )

        # Verify bob's fact IS visible to bob
        bob_view = _memory_tool(
            "honcho_search",
            {"query": bob_fact, "max_tokens": 2000},
            user_id="bob",
        )
        bob_result_text = str(bob_view)
        assert bob_fact in bob_result_text, (
            f"bob's own fact '{bob_fact}' not found in bob's search results: {bob_view}"
        )

    def test_profile_peer_cards_are_isolated(self):
        """honcho_profile peer cards are scoped per user_id — cross-reads return no bleed."""
        run_id = uuid.uuid4().hex[:8]
        alice_card_fact = f"alice-card-{run_id}"
        bob_card_fact = f"bob-card-{run_id}"

        # Write distinct peer cards for alice and bob
        _memory_tool(
            "honcho_profile",
            {"card": [alice_card_fact], "peer": "user"},
            user_id="alice",
        )
        _memory_tool(
            "honcho_profile",
            {"card": [bob_card_fact], "peer": "user"},
            user_id="bob",
        )

        time.sleep(0.5)

        # Read back alice's card — must NOT contain bob's fact
        alice_profile = _memory_tool(
            "honcho_profile",
            {"peer": "user"},
            user_id="alice",
        )
        alice_profile_text = str(alice_profile)
        assert bob_card_fact not in alice_profile_text, (
            f"bob's card fact '{bob_card_fact}' leaked into alice's profile: {alice_profile}"
        )
        assert alice_card_fact in alice_profile_text, (
            f"alice's own card fact '{alice_card_fact}' not found in alice's profile: {alice_profile}"
        )

        # Read back bob's card — must NOT contain alice's fact
        bob_profile = _memory_tool(
            "honcho_profile",
            {"peer": "user"},
            user_id="bob",
        )
        bob_profile_text = str(bob_profile)
        assert alice_card_fact not in bob_profile_text, (
            f"alice's card fact '{alice_card_fact}' leaked into bob's profile: {bob_profile}"
        )
        assert bob_card_fact in bob_profile_text, (
            f"bob's own card fact '{bob_card_fact}' not found in bob's profile: {bob_profile}"
        )
