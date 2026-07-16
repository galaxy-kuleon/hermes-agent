"""Native personal-to-shared publish and rollback routing tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools import shared_skill_writer
from tools import skill_acl
from tools import skill_manager_tool as manager


READERS = "readers"
EDITORS = "editors"
ADMINS = "admins"
SKILL = """---
name: {name}
description: Publish workflow test.
---

# Test

{body}
"""


@pytest.fixture
def publish_home(tmp_path, monkeypatch):
    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)
    acl = {
        "enabled": True,
        "authority_mode": "groups_only",
        "roles": {},
        "groups": {
            READERS: {"read"},
            EDITORS: {"read", "create", "update"},
            ADMINS: {"read", "create", "update", "delete"},
        },
        "protect_paths": [str(platform)],
        "error": None,
    }
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: acl)
    calls = []

    def _writer(action, name, *, arguments=None):
        calls.append((action, name, arguments or {}))
        from gateway.session_context import get_session_env

        required = "delete" if action == "rollback" else "create"
        permissions = skill_acl.resolve_skill_permissions(
            get_session_env("HERMES_SESSION_USER_ROLE", ""),
            get_session_env("HERMES_SESSION_USER_GROUPS", ""),
            acl,
        )
        if required not in permissions:
            return {
                "success": False,
                "error": "Hermes shared skills ACL denied this native mutation.",
                "error_code": "acl_denied",
            }
        return {
            "success": True,
            "namespace": "platform",
            "qualified_name": f"platform:{name}",
            "transaction_id": "txn-test",
        }

    monkeypatch.setattr(shared_skill_writer, "request_shared_skill_mutation", _writer)
    return tmp_path, platform, calls


def _scope(user_id: str, groups: str):
    return set_session_vars(
        platform="api_server", user_id=user_id, user_role="user", user_groups=groups
    )


def _personal_skill(home: Path, user_id: str, name: str) -> Path:
    root = home / "user-skills" / user_id / name
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL.format(name=name, body=f"owner={user_id}"))
    (root / "references" / "proof.md").write_text(f"proof={user_id}")
    return root


def test_editor_publishes_own_personal_tree_to_explicit_shared_target(publish_home):
    home, _platform, calls = publish_home
    _personal_skill(home, "alice", "draft-one")
    tokens = _scope("alice", EDITORS)
    try:
        result = json.loads(
            manager.skill_manage(
                "publish",
                "user:draft-one",
                target_name="draft-one",
                target_category="devops",
            )
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    action, target, arguments = calls[0]
    assert (action, target) == ("publish", "draft-one")
    assert arguments["category"] == "devops"
    assert "SKILL.md" in arguments["files"]
    assert "references/proof.md" in arguments["files"]
    assert arguments["source_qualified_name"] == "user:draft-one"


def test_publish_never_discovers_sibling_personal_source(publish_home):
    home, _platform, calls = publish_home
    _personal_skill(home, "alice", "private-one")
    tokens = _scope("bob", EDITORS)
    try:
        result = json.loads(
            manager.skill_manage(
                "publish", "user:private-one", target_name="private-one"
            )
        )
    finally:
        clear_session_vars(tokens)
    assert result["success"] is False
    assert calls == []
    assert "alice" not in json.dumps(result)


def test_reader_cannot_publish_and_missing_target_is_rejected(publish_home):
    home, _platform, calls = publish_home
    _personal_skill(home, "alice", "draft-one")
    tokens = _scope("alice", READERS)
    try:
        denied = json.loads(
            manager.skill_manage(
                "publish", "user:draft-one", target_name="draft-one"
            )
        )
    finally:
        clear_session_vars(tokens)
    assert denied["success"] is False
    assert [call[0] for call in calls] == ["publish"]

    tokens = _scope("alice", EDITORS)
    try:
        missing = json.loads(manager.skill_manage("publish", "user:draft-one"))
    finally:
        clear_session_vars(tokens)
    assert missing["success"] is False
    assert "target_name" in missing["error"]
    assert [call[0] for call in calls] == ["publish"]


def test_only_admin_group_can_route_rollback(publish_home):
    _home, _platform, calls = publish_home
    tokens = _scope("alice", EDITORS)
    try:
        denied = json.loads(
            manager.skill_manage(
                "rollback", "shared-one", transaction_id="txn-one"
            )
        )
    finally:
        clear_session_vars(tokens)
    assert denied["success"] is False
    assert [call[0] for call in calls] == ["rollback"]

    tokens = _scope("alice", ADMINS)
    try:
        allowed = json.loads(
            manager.skill_manage(
                "rollback", "shared-one", transaction_id="txn-one"
            )
        )
    finally:
        clear_session_vars(tokens)
    assert allowed["success"] is True
    assert calls[-1][0:2] == ("rollback", "shared-one")
    assert calls[-1][2]["transaction_id"] == "txn-one"


def test_schema_exposes_publish_and_rollback_contract():
    properties = manager.SKILL_MANAGE_SCHEMA["parameters"]["properties"]
    actions = properties["action"]["enum"]
    assert "publish" in actions
    assert "rollback" in actions
    assert "target_name" in properties
    assert "transaction_id" in properties
