import json
from pathlib import Path

from agent import curator, curator_backup
from gateway.session_context import clear_session_vars, set_session_vars
from tools import (
    file_tools,
    shared_skill_writer,
    skill_acl,
    skill_manager_tool as manager,
    skill_usage,
)


SKILL = """---
name: {name}
description: Platform immutability test.
---

# Test

{body}
"""


def _write_skill(root: Path, name: str, body: str = "body") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL.format(name=name, body=body))
    return skill


def test_api_server_platform_native_writes_route_to_writer_for_group_admin(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    platform.mkdir()
    _write_skill(platform, "company-skill", "original")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)
    monkeypatch.setattr(
        skill_acl,
        "load_skill_acl_config",
        lambda config=None: {
            "enabled": True,
            "authority_mode": "groups_only",
            "roles": {},
            "groups": {"skill-admins": {"read", "create", "update", "delete"}},
            "protect_paths": [str(platform)],
            "error": None,
        },
    )
    writer_calls = []

    def _writer(action, name, *, arguments=None):
        writer_calls.append((action, name))
        args = arguments or {}
        if action == "edit":
            return manager._edit_skill(name, args.get("content"), "platform")
        if action == "create":
            return manager._create_skill(
                name, args.get("content"), args.get("category"), "platform"
            )
        raise AssertionError(action)

    monkeypatch.setattr(shared_skill_writer, "request_shared_skill_mutation", _writer)
    tokens = set_session_vars(
        platform="api_server", user_id="admin-user", user_role="user",
        user_groups="skill-admins",
    )
    try:
        edited = json.loads(
            manager.skill_manage(
                "edit",
                "platform:company-skill",
                content=SKILL.format(name="company-skill", body="changed"),
            )
        )
        created = json.loads(
            manager.skill_manage(
                "create",
                "platform:new-platform",
                content=SKILL.format(name="new-platform", body="new"),
            )
        )
        own = json.loads(
            manager.skill_manage(
                "create",
                "own-skill",
                content=SKILL.format(name="own-skill", body="own"),
            )
        )
    finally:
        clear_session_vars(tokens)

    assert edited["success"] is True
    assert created["success"] is True
    assert writer_calls == [("edit", "company-skill"), ("create", "new-platform")]
    assert "changed" in (platform / "company-skill" / "SKILL.md").read_text()
    assert (platform / "new-platform" / "SKILL.md").exists()
    assert own["success"] is True
    assert (tmp_path / "user-skills" / "admin-user" / "own-skill").exists()


def test_file_tool_platform_write_requires_native_skill_manage_independent_of_acl_flag(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    platform.mkdir()
    target = platform / "company" / "SKILL.md"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: {"enabled": False})
    tokens = set_session_vars(
        platform="api_server", user_id="admin-user", user_role="admin"
    )
    try:
        denied = file_tools._acl_protected_path_block(
            str(target), mode="write", permission="create"
        )
        readable = file_tools._acl_protected_path_block(str(target), mode="read")
    finally:
        clear_session_vars(tokens)

    assert "skill_manage" in denied
    assert readable is None


def test_platform_usage_curator_state_and_backups_are_off_content_tree(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    platform.mkdir()
    _write_skill(platform, "company-skill")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with skill_usage.skill_usage_scope(platform):
        skill_usage.bump_view("company-skill")
        curator.save_state(curator._default_state())
        snapshot = curator_backup.snapshot_skills(reason="platform-ro-test")
        archived, message = skill_usage.archive_skill("company-skill")
        rolled, rollback_message, _ = curator_backup.rollback()

    state = tmp_path / "skill-state" / "platform"
    assert (state / "usage.json").is_file()
    assert (state / "curator_state.json").is_file()
    assert snapshot is not None
    assert snapshot.is_relative_to(state)
    assert not (platform / ".usage.json").exists()
    assert not (platform / ".curator_state").exists()
    assert not (platform / ".curator_backups").exists()
    assert archived is False
    assert "read-only" in message
    assert (platform / "company-skill").exists()
    assert rolled is False
    assert "operator" in rollback_message


def test_user_usage_and_curator_state_stay_in_own_user_root(tmp_path, monkeypatch):
    user_root = tmp_path / "user-skills" / "alice"
    user_root.mkdir(parents=True)
    _write_skill(user_root, "alice-skill")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with skill_usage.skill_usage_scope(user_root):
        skill_usage.bump_view("alice-skill")
        curator.save_state(curator._default_state())

    assert (user_root / ".usage.json").is_file()
    assert (user_root / ".curator_state").is_file()
