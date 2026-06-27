"""Tests for the centralized Hermes skill ACL resolver (issue #10).

Resolver-only: covers config loading/normalization, permission resolution
(role grants, group grants, union, admin, fail-safe deny, delete independence),
malformed-config fail-safe, ACL-disabled legacy behavior, the action->permission
mapping, and session-driven ``require_skill_permission``.
"""

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools.skill_acl import (
    ALL_PERMISSIONS,
    load_skill_acl_config,
    require_skill_permission,
    resolve_skill_permissions,
)

# Stable OpenWebUI group IDs used across the cases.
G_READERS = "grp-readers-001"
G_EDITORS = "grp-editors-002"
G_ADMINS = "grp-admins-003"


def _cfg(**skills_acl):
    """Wrap a skills_acl block in a full-config dict (as load_config returns)."""
    return {"skills_acl": skills_acl}


def _enabled_cfg():
    return _cfg(
        enabled=True,
        roles={"admin": ["read", "create", "update", "delete"], "user": []},
        groups={
            G_READERS: ["read"],
            G_EDITORS: ["read", "create", "update"],
            G_ADMINS: ["read", "create", "update", "delete"],
        },
        protect_paths=["/home/hermes/skills"],
    )


# ---------------------------------------------------------------------------
# load_skill_acl_config
# ---------------------------------------------------------------------------

def test_absent_section_is_disabled_legacy():
    cfg = load_skill_acl_config({})  # no skills_acl key
    assert cfg["enabled"] is False
    assert cfg["error"] is None


def test_explicitly_disabled_is_legacy_full_access():
    cfg = load_skill_acl_config(_cfg(enabled=False, roles={"user": ["read"]}))
    assert cfg["enabled"] is False
    assert resolve_skill_permissions("user", "", cfg) == set(ALL_PERMISSIONS)


def test_load_normalizes_and_filters_unknown_permissions():
    cfg = load_skill_acl_config(
        _cfg(enabled=True, roles={"user": ["read", "bogus", "DELETE"]})
    )
    assert cfg["enabled"] is True
    assert cfg["roles"]["user"] == {"read", "delete"}  # unknown dropped, case-folded
    assert cfg["error"] is None


def test_protect_paths_normalized_to_list():
    cfg = load_skill_acl_config(_cfg(enabled=True, protect_paths="/home/hermes/skills"))
    assert cfg["protect_paths"] == ["/home/hermes/skills"]


# ---------------------------------------------------------------------------
# resolve_skill_permissions
# ---------------------------------------------------------------------------

def test_admin_role_gets_all_permissions():
    cfg = load_skill_acl_config(_enabled_cfg())
    assert resolve_skill_permissions("admin", "", cfg) == set(ALL_PERMISSIONS)
    assert resolve_skill_permissions("Admin", "", cfg) == set(ALL_PERMISSIONS)  # case-insensitive


def test_role_grant():
    cfg = load_skill_acl_config(_cfg(enabled=True, roles={"editor": ["read", "create"]}))
    assert resolve_skill_permissions("editor", "", cfg) == {"read", "create"}


def test_group_grant_string_and_list():
    cfg = load_skill_acl_config(_enabled_cfg())
    assert resolve_skill_permissions("user", G_READERS, cfg) == {"read"}
    assert resolve_skill_permissions("user", [G_EDITORS], cfg) == {"read", "create", "update"}


def test_union_of_role_and_group_grants():
    cfg = load_skill_acl_config(
        _cfg(enabled=True, roles={"user": ["read"]}, groups={G_EDITORS: ["create", "update"]})
    )
    assert resolve_skill_permissions("user", G_EDITORS, cfg) == {"read", "create", "update"}


def test_union_across_multiple_groups():
    cfg = load_skill_acl_config(_enabled_cfg())
    perms = resolve_skill_permissions("user", f"{G_READERS},{G_ADMINS}", cfg)
    assert perms == set(ALL_PERMISSIONS)  # admins group grants everything


def test_failsafe_deny_for_non_admin_no_grant():
    cfg = load_skill_acl_config(_enabled_cfg())
    assert resolve_skill_permissions("user", "", cfg) == set()
    assert resolve_skill_permissions("user", "grp-unknown", cfg) == set()


def test_delete_independent_from_update():
    cfg = load_skill_acl_config(_enabled_cfg())
    perms = resolve_skill_permissions("user", G_EDITORS, cfg)  # read/create/update, NO delete
    assert "update" in perms
    assert "delete" not in perms


def test_malformed_config_failsafe_enable_and_deny():
    # roles is a string, not a mapping -> structural error -> fail safe enable+deny
    cfg = load_skill_acl_config(_cfg(enabled=False, roles="oops"))
    assert cfg["enabled"] is True
    assert cfg["error"]
    assert resolve_skill_permissions("user", G_EDITORS, cfg) == set()
    # admin still works under fail-safe
    assert resolve_skill_permissions("admin", "", cfg) == set(ALL_PERMISSIONS)


def test_malformed_top_level_section():
    cfg = load_skill_acl_config({"skills_acl": "not-a-mapping"})
    assert cfg["enabled"] is True
    assert cfg["error"]
    assert resolve_skill_permissions("user", "", cfg) == set()


def test_malformed_roles_with_valid_group_still_denies_nonadmin():
    # Structural error in `roles` (value not a list) MUST NOT be partially
    # honored even when a VALID `groups` grant also exists. (pane %1 finding)
    raw = _cfg(enabled=True, roles={"user": "not-a-list"}, groups={G_EDITORS: ["read"]})
    cfg = load_skill_acl_config(raw)
    assert cfg["enabled"] is True
    assert cfg["error"]  # structural error recorded
    # non-admin fully denied despite the valid group grant
    assert resolve_skill_permissions("user", G_EDITORS, cfg) == set()
    # admin still allowed under malformed config
    assert resolve_skill_permissions("admin", "", cfg) == set(ALL_PERMISSIONS)
    # require_skill_permission denies the non-admin with a config-error reason
    tokens = set_session_vars(user_role="user", user_groups=G_EDITORS)
    try:
        ok, reason = require_skill_permission("skill_view", config=raw)
    finally:
        clear_session_vars(tokens)
    assert ok is False
    assert "config error" in reason.lower()


# ---------------------------------------------------------------------------
# require_skill_permission (action mapping + session-driven)
# ---------------------------------------------------------------------------

def test_require_allows_everything_when_disabled():
    ok, reason = require_skill_permission("delete", config={})  # no skills_acl
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "action,perm",
    [
        ("skills_list", "read"),
        ("skill_view", "read"),
        ("create", "create"),
        ("edit", "update"),
        ("patch", "update"),
        ("write_file", "update"),
        ("remove_file", "update"),
        ("delete", "delete"),
    ],
)
def test_require_action_mapping_against_editor_group(action, perm):
    cfg = _enabled_cfg()
    tokens = set_session_vars(user_role="user", user_groups=G_EDITORS)
    try:
        ok, reason = require_skill_permission(action, config=cfg)
    finally:
        clear_session_vars(tokens)
    # editor group grants read/create/update but NOT delete
    if perm == "delete":
        assert ok is False and "denied" in reason.lower()
    else:
        assert ok is True


def test_require_reads_session_role_admin():
    cfg = _enabled_cfg()
    tokens = set_session_vars(user_role="admin", user_groups="")
    try:
        assert require_skill_permission("delete", config=cfg)[0] is True
    finally:
        clear_session_vars(tokens)


def test_require_denies_non_admin_no_group():
    cfg = _enabled_cfg()
    tokens = set_session_vars(user_role="user", user_groups="")
    try:
        ok, reason = require_skill_permission("skills_list", config=cfg)
    finally:
        clear_session_vars(tokens)
    assert ok is False
    assert "denied" in reason.lower()


def test_require_unknown_action_denied_when_enabled():
    ok, reason = require_skill_permission("frobnicate", config=_enabled_cfg())
    assert ok is False
    assert "unknown action" in reason.lower()


def test_require_reason_notes_config_error_on_malformed():
    cfg = _cfg(enabled=True, groups="bad")
    tokens = set_session_vars(user_role="user", user_groups=G_EDITORS)
    try:
        ok, reason = require_skill_permission("skill_view", config=cfg)
    finally:
        clear_session_vars(tokens)
    assert ok is False
    assert "config error" in reason.lower()
