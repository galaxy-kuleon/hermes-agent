"""Runtime shared-library ACL and schema minimization tests.

Covers the action-level platform/shared ACL matrix (Reader denied; Editor may
create/update but not delete; Admin may create/update/delete), denied-mutation
short-circuiting, and schema-level toolset minimization.
"""

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
import tools.skill_acl as skill_acl
import tools.skill_manager_tool as smt
import tools.shared_skill_writer as shared_writer
from gateway.platforms.api_server import _apply_skill_acl_toolset_minimization

G_READERS = "grp-readers"
G_EDITORS = "grp-editors"  # read, create, update — NO delete
G_ADMINS = "grp-admins"  # read, create, update, delete

ENABLED_ACL = {
    "enabled": True,
    "authority_mode": "groups_only",
    "roles": {"admin": {"read", "create", "update", "delete"}, "user": set()},
    "groups": {
        G_READERS: {"read"},
        G_EDITORS: {"read", "create", "update"},
        G_ADMINS: {"read", "create", "update", "delete"},
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

    def _writer(action, name, *, arguments=None):
        calls.append(action)
        from gateway.session_context import get_session_env

        required = {
            "create": "create",
            "edit": "update",
            "patch": "update",
            "write_file": "update",
            "remove_file": "update",
            "delete": "delete",
        }[action]
        permissions = skill_acl.resolve_skill_permissions(
            get_session_env("HERMES_SESSION_USER_ROLE", ""),
            get_session_env("HERMES_SESSION_USER_GROUPS", ""),
            ENABLED_ACL,
        )
        if required not in permissions:
            return {
                "success": False,
                "error": "Hermes shared skills ACL denied this native mutation.",
                "error_code": "acl_denied",
            }
        return {"success": True, "namespace": "platform", "qualified_name": f"platform:{name}"}

    monkeypatch.setattr(shared_writer, "request_shared_skill_mutation", _writer)
    return calls


def _scope(role="", groups="", platform="api_server"):
    return set_session_vars(
        platform=platform, user_id="test-user", user_role=role, user_groups=groups
    )


def _denied(result: str) -> bool:
    obj = json.loads(result)
    return obj.get("success") is False and "skills acl" in obj.get("error", "").lower()


def _allowed(result: str) -> bool:
    return json.loads(result).get("success") is True


def _do(action, *, name="some-skill", namespace="platform"):
    """Call skill_manage with the minimal required args for *action*."""
    kw = {
        "create": dict(content="x"),
        "edit": dict(content="x"),
        "patch": dict(old_string="a", new_string="b"),
        "delete": dict(),
        "write_file": dict(file_path="references/x.md", file_content="y"),
        "remove_file": dict(file_path="references/x.md"),
    }[action]
    return smt.skill_manage(action, name, namespace=namespace, **kw)


# ── Runtime gate matrix ──────────────────────────────────────────────────────

@pytest.mark.parametrize("groups", ["", G_READERS])
def test_reader_or_ungrouped_cannot_mutate_shared_library(
    acl_enabled, recorder, groups
):
    tokens = _scope(role="user", groups=groups)
    try:
        for action in ["create", "edit", "patch", "delete", "write_file", "remove_file"]:
            assert _denied(_do(action)), action
        # Every denied attempt reaches the non-filesystem broker boundary so it
        # can be independently authorized and audited.
        assert recorder == ["create", "edit", "patch", "delete", "write_file", "remove_file"]
    finally:
        clear_session_vars(tokens)


def test_editor_can_create_and_update_shared_but_not_delete(acl_enabled, recorder):
    tokens = _scope(role="user", groups=G_EDITORS)
    try:
        update_actions = ["create", "edit", "patch", "write_file", "remove_file"]
        for action in update_actions:
            assert _allowed(_do(action)), action
        assert recorder == update_actions

        assert _denied(_do("delete"))
        assert recorder == update_actions + ["delete"]
    finally:
        clear_session_vars(tokens)


def test_shared_admin_group_can_create_update_and_delete(acl_enabled, recorder):
    tokens = _scope(role="user", groups=G_ADMINS)
    try:
        for action in ["create", "edit", "patch", "write_file", "remove_file", "delete"]:
            assert _allowed(_do(action)), action
        assert recorder == ["create", "edit", "patch", "write_file", "remove_file", "delete"]
    finally:
        clear_session_vars(tokens)


def test_openwebui_admin_role_alone_cannot_mutate_group_governed_shared_library(
    acl_enabled, recorder
):
    tokens = _scope(role="admin", groups="")
    try:
        for action in ["create", "edit", "patch", "delete", "write_file", "remove_file"]:
            assert _denied(_do(action)), action
        assert recorder == ["create", "edit", "patch", "delete", "write_file", "remove_file"]
    finally:
        clear_session_vars(tokens)


def test_denied_delete_leaves_dispatch_untouched(acl_enabled, recorder):
    # Explicit: a reader (read only) denied delete/edit/patch never mutates.
    tokens = _scope(role="user", groups=G_READERS)
    try:
        assert _denied(_do("delete"))
        assert _denied(_do("edit"))
        assert _denied(_do("patch"))
        assert recorder == ["delete", "edit", "patch"]
    finally:
        clear_session_vars(tokens)


def test_non_api_server_platform_exempt(acl_enabled, recorder):
    tokens = _scope(role="user", groups="", platform="cli")  # owner CLI, not gated
    try:
        assert not _denied(_do("delete"))
        assert "delete" in recorder
    finally:
        clear_session_vars(tokens)


def test_acl_disabled_keeps_api_server_shared_writes_closed(recorder):
    tokens = _scope(role="user", groups="")
    try:
        assert _denied(_do("delete"))
        assert _denied(_do("create"))
        assert recorder == []
    finally:
        clear_session_vars(tokens)


def test_shared_acl_resolution_failure_fails_closed(monkeypatch, recorder):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    tokens = _scope(role="user", groups=G_ADMINS)
    try:
        assert _denied(_do("delete"))
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


def test_schema_min_shared_admin_group_gets_read_and_manage(acl_enabled):
    out = _apply_skill_acl_toolset_minimization(["skills"], "user", G_ADMINS)
    assert set(out) == {"skills_read", "skills_manage"}


def test_schema_min_shared_admin_does_not_gain_terminal(acl_enabled):
    out = _apply_skill_acl_toolset_minimization(
        ["skills", "terminal"], "user", G_ADMINS
    )
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
