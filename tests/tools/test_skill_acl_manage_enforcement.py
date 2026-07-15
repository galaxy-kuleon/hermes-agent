"""Runtime platform immutability and ACL schema minimization tests.

Covers Increment 2's action-level platform gate (all api_server roles denied;
denied mutations never touch the filesystem) and the existing schema-level
toolset minimization in ``_apply_skill_acl_toolset_minimization``.
"""

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
import tools.skill_acl as skill_acl
import tools.skill_manager_tool as smt
from gateway.platforms.api_server import _apply_skill_acl_toolset_minimization

G_READERS = "grp-readers"
G_EDITORS = "grp-editors"  # read, create, update — NO delete
G_DELETERS = "grp-deleters"  # read, create, update, delete

ENABLED_ACL = {
    "enabled": True,
    "roles": {"admin": {"read", "create", "update", "delete"}, "user": set()},
    "groups": {
        G_READERS: {"read"},
        G_EDITORS: {"read", "create", "update"},
        G_DELETERS: {"read", "create", "update", "delete"},
    },
    "protect_paths": [],
    "error": None,
}


@pytest.fixture
def acl_enabled(monkeypatch):
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ENABLED_ACL)
    yield


@pytest.fixture(autouse=True)
def _clean_session():
    tokens = set_session_vars()
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.fixture
def recorder(monkeypatch):
    """Replace every skill_manage dispatch target with a call recorder so we can
    prove denied actions never reach a filesystem mutation."""
    calls = []

    def _mk(tag):
        def _fn(*a, **k):
            calls.append(tag)
            return {"success": True}
        return _fn

    for tag, attr in [
        ("create", "_create_skill"),
        ("edit", "_edit_skill"),
        ("patch", "_patch_skill"),
        ("delete", "_delete_skill"),
        ("write_file", "_write_file"),
        ("remove_file", "_remove_file"),
    ]:
        monkeypatch.setattr(smt, attr, _mk(tag))
    return calls


def _scope(role="", groups="", platform="api_server"):
    return set_session_vars(platform=platform, user_role=role, user_groups=groups)


def _denied(result: str) -> bool:
    obj = json.loads(result)
    return obj.get("success") is False and "skills acl" in obj.get("error", "").lower()


def _immutable(result: str) -> bool:
    obj = json.loads(result)
    return obj.get("success") is False and obj.get("error_code") == "immutable_platform"


def _do(action):
    """Call skill_manage with the minimal required args for *action*."""
    kw = {
        "create": dict(content="x"),
        "edit": dict(content="x"),
        "patch": dict(old_string="a", new_string="b"),
        "delete": dict(),
        "write_file": dict(file_path="references/x.md", file_content="y"),
        "remove_file": dict(file_path="references/x.md"),
    }[action]
    return smt.skill_manage(action, "some-skill", **kw)


# ── Runtime gate matrix ──────────────────────────────────────────────────────

def test_no_perm_user_gets_immutable_platform_and_no_mutation(acl_enabled, recorder):
    tokens = _scope(role="user", groups="")  # missing group header => no perms
    try:
        for action in ["create", "edit", "patch", "delete", "write_file", "remove_file"]:
            assert _immutable(_do(action)), action
        assert recorder == []  # nothing dispatched => no filesystem mutation
    finally:
        clear_session_vars(tokens)


def test_editor_cannot_mutate_platform(acl_enabled, recorder):
    tokens = _scope(role="user", groups=G_EDITORS)
    try:
        for action in ["create", "edit", "patch", "delete", "write_file", "remove_file"]:
            assert _immutable(_do(action)), action
        assert recorder == []
    finally:
        clear_session_vars(tokens)


def test_delete_granted_group_cannot_delete_platform(acl_enabled, recorder):
    tokens = _scope(role="user", groups=G_DELETERS)
    try:
        assert _immutable(_do("delete"))
        assert recorder == []
    finally:
        clear_session_vars(tokens)


def test_admin_cannot_mutate_platform(acl_enabled, recorder):
    tokens = _scope(role="admin", groups="")
    try:
        for action in ["create", "edit", "patch", "delete", "write_file", "remove_file"]:
            assert _immutable(_do(action)), action
        assert recorder == []
    finally:
        clear_session_vars(tokens)


def test_denied_delete_leaves_dispatch_untouched(acl_enabled, recorder):
    # Explicit: a reader (read only) denied delete/edit/patch never mutates.
    tokens = _scope(role="user", groups=G_READERS)
    try:
        assert _immutable(_do("delete"))
        assert _immutable(_do("edit"))
        assert _immutable(_do("patch"))
        assert recorder == []
    finally:
        clear_session_vars(tokens)


def test_non_api_server_platform_exempt(acl_enabled, recorder):
    tokens = _scope(role="user", groups="", platform="cli")  # owner CLI, not gated
    try:
        assert not _denied(_do("delete"))
        assert "delete" in recorder
    finally:
        clear_session_vars(tokens)


def test_acl_disabled_does_not_disable_platform_immutability(recorder):
    tokens = _scope(role="user", groups="")
    try:
        assert _immutable(_do("delete"))
        assert _immutable(_do("create"))
        assert recorder == []
    finally:
        clear_session_vars(tokens)


def test_platform_immutability_precedes_acl_resolution(monkeypatch, recorder):
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ENABLED_ACL)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "require_skill_permission", _raise)
    tokens = _scope(role="user", groups=G_DELETERS)
    try:
        assert _immutable(_do("delete"))
        assert recorder == []
    finally:
        clear_session_vars(tokens)


# ── Schema-level minimization ────────────────────────────────────────────────

def test_schema_min_reader_gets_own_namespace_manager(acl_enabled):
    # Increment 1 gives every read-authorized api_server caller native CRUD on
    # their own functional user namespace. Runtime target resolution still
    # applies the platform ACL to platform/external skill mutations.
    out = _apply_skill_acl_toolset_minimization(["web", "skills"], "user", G_READERS)
    assert "skills_read" in out
    assert "skills" not in out and "skills_manage" in out
    assert "web" in out  # unrelated toolset preserved


def test_schema_min_editor_gets_read_and_manage(acl_enabled):
    out = _apply_skill_acl_toolset_minimization(["skills"], "user", G_EDITORS)
    assert set(out) == {"skills_read", "skills_manage"}


def test_schema_min_admin_gets_read_and_manage(acl_enabled):
    out = _apply_skill_acl_toolset_minimization(["skills"], "admin", "")
    assert set(out) == {"skills_read", "skills_manage"}


def test_schema_min_no_perm_drops_skills(acl_enabled):
    out = _apply_skill_acl_toolset_minimization(["web", "skills"], "user", "")
    assert out == ["web"]  # no read, no manage => no skill toolsets


def test_schema_min_disabled_unchanged():
    base = ["web", "skills", "terminal"]
    assert _apply_skill_acl_toolset_minimization(base, "user", "") == base


def test_schema_min_failsafe_on_error(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    out = _apply_skill_acl_toolset_minimization(["web", "skills", "skills_manage"], "admin", "")
    assert out == ["web"]  # fail safe: no skill toolsets exposed
