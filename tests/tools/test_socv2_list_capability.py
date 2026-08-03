"""soc_v2 list_conversions on the headerless (Hermes) channel.

Origin Agent reaches soc_v2 through Hermes, which forwards no OpenWebUI headers.
Listing therefore has no user context and used to be refused outright, so a user
who did not keep the job_id from the submit reply could not find their own
conversion again. Listing now accepts the same proof the mutations accept -- a
capability-signed /handoff/user/<uid>/ path.

These tests observe the handler at the ACL-guard seam, which sees the argument
dict after the grant block has decided. That covers what path (if any) is
forwarded; the capability is minted from exactly that path one branch later, so
"no path forwarded" is also "no capability minted". The signing itself lives
behind a live MCP session and is not exercised here.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import mcp_tool  # noqa: E402


@pytest.fixture
def seen(monkeypatch):
    """Capture the args that survive the grant block, then stop the call."""
    box = {}

    def _guard(server_name, args):
        box["args"] = args
        return None  # not an ACL denial; the call proceeds to transport

    monkeypatch.setattr(
        "tools.file_tools._acl_guard_code_exec_mcp_call", _guard, raising=False
    )
    # No MCP server is registered in a unit test, so the handler stops at the
    # transport with a known message. Reaching it proves the grant block let the
    # call through rather than short-circuiting with a denial.
    monkeypatch.setattr(mcp_tool, "_servers", {}, raising=False)
    return box


def _handler(tool_name):
    return mcp_tool._make_tool_handler("soc_v2", tool_name, 30.0)


def _reached_transport(out: str) -> bool:
    return "is not connected" in out


def test_list_without_path_forwards_no_path_and_is_not_denied(seen, monkeypatch):
    """No proof offered -> the file-grant check is not consulted at all.

    A capability over an empty path would authorize a path naming no user, which
    is the one thing the signature exists to prevent. The server answers an
    unproven listing with instructions, so the handler must not pre-empt that
    with a file-grant denial about a path the model was never told it needed.
    """
    consulted = {"n": 0}

    def _never(path, *, task_id, operation):
        consulted["n"] += 1
        return "", "grant denied"

    monkeypatch.setattr("tools.file_grants.resolve_file_grant", _never, raising=False)

    out = _handler("list_conversions")({}, task_id="t1")

    assert consulted["n"] == 0, "empty path must not go through the file-grant check"
    assert _reached_transport(out), f"call was short-circuited instead: {out[:200]}"
    assert "path" not in seen["args"], "no path may be forwarded without proof"


def test_list_with_path_forwards_the_canonical_path(seen, monkeypatch):
    """A real handoff path is proof, and it must be signed for THIS operation."""
    canonical = "/handoff/user/u42/report.pdf"
    ops = []

    def _grant(path, *, task_id, operation):
        ops.append(operation)
        return canonical, None

    monkeypatch.setattr("tools.file_grants.resolve_file_grant", _grant, raising=False)

    out = _handler("list_conversions")({"path": "report.pdf"}, task_id="t1")

    assert ops == ["soc_v2.list_conversions"]
    assert _reached_transport(out), f"call was short-circuited instead: {out[:200]}"
    assert seen["args"]["path"] == canonical


def test_submit_still_denies_a_missing_path(seen, monkeypatch):
    """The relaxation is scoped to listing; the mutations keep failing closed."""

    def _grant(path, *, task_id, operation):
        return "", "no grant for that path"

    monkeypatch.setattr("tools.file_grants.resolve_file_grant", _grant, raising=False)

    out = json.loads(_handler("submit_conversion")({}, task_id="t1"))

    assert out["success"] is False
    assert "no grant" in out["error"]
    assert "args" not in seen, "a denied submit must never reach the transport"
