"""Tests for issue #11 — enforce READ permission across skill list/view paths.

Covers the toolset split (skills_read / skills_manage / backward-compatible
skills) and the api_server-scoped read gate on skills_list / skill_view
(no-read, reader-group, admin, missing group header, exact-name attempt,
non-api_server exemption, and ACL-disabled backward compatibility).
"""

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
import tools.skill_acl as skill_acl
from tools import skills_tool
from tools.skills_tool import _acl_read_block
from toolsets import resolve_toolset

G_READERS = "grp-readers"
G_EDITORS = "grp-editors"

# Normalized config shape (as load_skill_acl_config returns: grant values are sets).
ENABLED_ACL = {
    "enabled": True,
    "roles": {"admin": {"read", "create", "update", "delete"}, "user": set()},
    "groups": {G_READERS: {"read"}, G_EDITORS: {"read", "create", "update"}},
    "protect_paths": [],
    "error": None,
}


@pytest.fixture
def acl_enabled(monkeypatch):
    """Force the resolver to see an enabled ACL (config.yaml is untouched)."""
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ENABLED_ACL)
    yield


@pytest.fixture(autouse=True)
def _clean_session():
    # Each test sets its own scope; ensure no leakage across tests.
    tokens = set_session_vars()
    try:
        yield
    finally:
        clear_session_vars(tokens)


def _scope(role="", groups="", platform="api_server"):
    return set_session_vars(platform=platform, user_role=role, user_groups=groups)


def _denied(result: str) -> bool:
    # Matches both the normal "Hermes skills ACL denied ..." and the fail-closed
    # "Hermes skills ACL: read access could not be verified ... denied." reasons.
    obj = json.loads(result)
    return obj.get("success") is False and "skills acl" in obj.get("error", "").lower()


def _raise(*a, **k):
    raise RuntimeError("boom")


DISABLED_ACL = {
    "enabled": False,
    "roles": {},
    "groups": {},
    "protect_paths": [],
    "error": None,
}


# ── Toolset split (backward compatible) ──────────────────────────────────────

def test_toolset_split_backward_compatible():
    assert set(resolve_toolset("skills_read")) == {"skills_list", "skill_view"}
    assert set(resolve_toolset("skills_manage")) == {"skill_manage"}
    # legacy "skills" still resolves to all three via includes
    assert set(resolve_toolset("skills")) == {"skills_list", "skill_view", "skill_manage"}


# ── _acl_read_block decisions ────────────────────────────────────────────────

def test_no_read_user_blocked(acl_enabled):
    tokens = _scope(role="user", groups="")  # non-admin, no group => missing group header
    try:
        assert _acl_read_block("skills_list") is not None
        assert _acl_read_block("skill_view") is not None
    finally:
        clear_session_vars(tokens)


def test_reader_group_allowed_to_read(acl_enabled):
    tokens = _scope(role="user", groups=G_READERS)
    try:
        assert _acl_read_block("skills_list") is None
        assert _acl_read_block("skill_view") is None
    finally:
        clear_session_vars(tokens)


def test_admin_allowed_to_read(acl_enabled):
    tokens = _scope(role="admin", groups="")
    try:
        assert _acl_read_block("skills_list") is None
    finally:
        clear_session_vars(tokens)


def test_non_api_server_platform_exempt(acl_enabled):
    # CLI/cron etc. are never blocked, even with ACL enabled and no grant.
    tokens = _scope(role="user", groups="", platform="cli")
    try:
        assert _acl_read_block("skills_list") is None
        assert _acl_read_block("skill_view") is None
    finally:
        clear_session_vars(tokens)


def test_acl_disabled_is_backward_compatible():
    # No monkeypatch => live config has no skills_acl => disabled => allow.
    tokens = _scope(role="user", groups="")
    try:
        assert _acl_read_block("skills_list") is None
        assert _acl_read_block("skill_view") is None
    finally:
        clear_session_vars(tokens)


# ── End-to-end gating on the actual tools (denial short-circuits) ─────────────

def test_skills_list_denied_for_no_read(acl_enabled):
    tokens = _scope(role="user", groups="")
    try:
        assert _denied(skills_tool.skills_list())
    finally:
        clear_session_vars(tokens)


def test_skill_view_exact_name_denied_without_leak(acl_enabled):
    tokens = _scope(role="user", groups="")
    try:
        result = skills_tool.skill_view("super-secret-skill")
        assert _denied(result)
        # Non-leaky: the denial must not reveal whether the skill exists.
        assert "super-secret-skill" not in result
    finally:
        clear_session_vars(tokens)


def test_owner_can_read_explicit_private_skill_without_shared_read_grant(
    acl_enabled, tmp_path, monkeypatch
):
    user_root = tmp_path / "user-skills" / "alice" / "private"
    user_root.mkdir(parents=True)
    (user_root / "SKILL.md").write_text(
        "---\nname: private\ndescription: Private skill.\n---\n\nPRIVATE-CANARY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = set_session_vars(
        platform="api_server", user_id="alice", user_role="user", user_groups=""
    )
    try:
        private = json.loads(skills_tool.skill_view("user:private"))
        assert private["success"] is True
        assert private["namespace"] == "user"
        assert "PRIVATE-CANARY" in private["content"]

        platform = skills_tool.skill_view("platform:private")
        assert _denied(platform)
    finally:
        clear_session_vars(tokens)


def test_reader_group_passes_gate_into_tool(acl_enabled):
    # Reader gets PAST the ACL gate; result is normal tool output, not an ACL denial.
    tokens = _scope(role="user", groups=G_READERS)
    try:
        result = skills_tool.skill_view("definitely-missing-skill")
        assert "skills acl" not in result.lower()
    finally:
        clear_session_vars(tokens)


# ── Fail-CLOSED on resolution error when ACL enabled on api_server ───────────

def test_failclosed_on_resolution_error_when_enabled(monkeypatch):
    # ACL enabled on api_server, but permission resolution raises => must DENY.
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ENABLED_ACL)
    monkeypatch.setattr(skill_acl, "require_skill_permission", _raise)
    tokens = _scope(role="user", groups=G_READERS, platform="api_server")
    try:
        blocked = _acl_read_block("skills_list")
        assert blocked is not None and "denied" in blocked.lower()
        # End-to-end: the tool returns a (non-leaky) denial, not skill data.
        assert _denied(skills_tool.skills_list())
        assert _denied(skills_tool.skill_view("whatever"))
    finally:
        clear_session_vars(tokens)


def test_failopen_when_disabled_even_if_resolution_would_raise(monkeypatch):
    # ACL disabled => allow, even if the decision call would have raised.
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: DISABLED_ACL)
    monkeypatch.setattr(skill_acl, "require_skill_permission", _raise)
    tokens = _scope(role="user", groups="", platform="api_server")
    try:
        assert _acl_read_block("skills_list") is None
        assert _acl_read_block("skill_view") is None
    finally:
        clear_session_vars(tokens)


def test_non_api_server_exempt_even_if_resolution_would_raise(monkeypatch):
    # Non-api_server platforms never reach resolution and are never blocked.
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    monkeypatch.setattr(skill_acl, "require_skill_permission", _raise)
    tokens = _scope(role="user", groups="", platform="cli")
    try:
        assert _acl_read_block("skills_list") is None
    finally:
        clear_session_vars(tokens)
