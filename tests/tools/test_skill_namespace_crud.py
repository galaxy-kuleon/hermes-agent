import json
import stat
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools import skill_acl
from tools import skill_manager_tool as manager
from tools import skills_tool


VALID = """---
name: {name}
description: Namespace test.
---

# Test

{body}
"""

ACL = {
    "enabled": True,
    "roles": {"admin": {"read", "create", "update", "delete"}, "user": set()},
    "groups": {
        "readers": {"read"},
        "editors": {"read", "create", "update"},
    },
    "protect_paths": [],
    "error": None,
}


@pytest.fixture
def namespace_home(tmp_path, monkeypatch):
    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", platform)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ACL)
    return tmp_path, platform


def _scope(user_id, groups="readers"):
    return set_session_vars(
        platform="api_server", user_id=user_id, user_role="user", user_groups=groups
    )


def _write_skill(root: Path, name: str, body: str):
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(VALID.format(name=name, body=body))


def test_unqualified_create_defaults_to_own_root_and_reader_has_own_full_crud(
    namespace_home,
):
    home, _platform = namespace_home
    tokens = _scope("alice")
    try:
        created = json.loads(
            manager.skill_manage(
                "create", "alice-skill", content=VALID.format(name="alice-skill", body="v1")
            )
        )
        assert created["success"] is True
        assert created["namespace"] == "user"
        assert created["qualified_name"] == "user:alice-skill"
        user_root = home / "user-skills" / "alice"
        skill_md = user_root / "alice-skill" / "SKILL.md"
        assert skill_md.exists()
        assert stat.S_IMODE(user_root.stat().st_mode) == 0o700

        edited = json.loads(
            manager.skill_manage(
                "edit",
                "user:alice-skill",
                content=VALID.format(name="alice-skill", body="v2"),
            )
        )
        assert edited["success"] is True
        patched = json.loads(
            manager.skill_manage(
                "patch", "user:alice-skill", old_string="v2", new_string="v3"
            )
        )
        assert patched["success"] is True
        written = json.loads(
            manager.skill_manage(
                "write_file",
                "user:alice-skill",
                file_path="references/proof.md",
                file_content="ALICE-SUPPORT",
            )
        )
        assert written["success"] is True
        assert (user_root / "alice-skill" / "references" / "proof.md").exists()
        removed = json.loads(
            manager.skill_manage(
                "remove_file",
                "user:alice-skill",
                file_path="references/proof.md",
            )
        )
        assert removed["success"] is True
        assert "v3" in skill_md.read_text()

        deleted = json.loads(manager.skill_manage("delete", "user:alice-skill"))
        assert deleted["success"] is True
        assert not (home / "user-skills" / "alice" / "alice-skill").exists()
    finally:
        clear_session_vars(tokens)


def test_platform_is_immutable_for_reader_and_editor(namespace_home):
    _home, platform = namespace_home
    _write_skill(platform, "platform-skill", "old")

    reader = _scope("alice", "readers")
    try:
        denied = json.loads(
            manager.skill_manage(
                "edit",
                "platform:platform-skill",
                content=VALID.format(name="platform-skill", body="reader edit"),
            )
        )
        assert denied["success"] is False
        assert denied["error_code"] == "immutable_platform"
    finally:
        clear_session_vars(reader)

    editor = _scope("bob", "editors")
    try:
        updated = json.loads(
            manager.skill_manage(
                "edit",
                "platform:platform-skill",
                content=VALID.format(name="platform-skill", body="editor edit"),
            )
        )
        assert updated["success"] is False
        assert updated["error_code"] == "immutable_platform"
        assert "old" in (platform / "platform-skill" / "SKILL.md").read_text()
    finally:
        clear_session_vars(editor)


def test_two_users_with_same_name_are_isolated_in_list_and_view(namespace_home):
    home, _platform = namespace_home
    _write_skill(home / "user-skills" / "alice", "private", "ALICE-TOKEN")
    _write_skill(home / "user-skills" / "bob", "private", "BOB-TOKEN")

    alice = _scope("alice")
    try:
        listed = json.loads(skills_tool.skills_list())
        alice_entry = next(s for s in listed["skills"] if s["name"] == "private")
        assert alice_entry["namespace"] == "user"
        assert alice_entry["qualified_name"] == "user:private"
        viewed = json.loads(skills_tool.skill_view("user:private"))
        assert viewed["success"] is True
        assert "ALICE-TOKEN" in viewed["content"]
        assert "BOB-TOKEN" not in viewed["content"]
        patched = json.loads(
            manager.skill_manage(
                "patch",
                "user:private",
                old_string="ALICE-TOKEN",
                new_string="ALICE-UPDATED",
            )
        )
        assert patched["success"] is True
    finally:
        clear_session_vars(alice)

    bob = _scope("bob")
    try:
        viewed = json.loads(skills_tool.skill_view("user:private"))
        assert viewed["success"] is True
        assert "BOB-TOKEN" in viewed["content"]
        assert "ALICE-UPDATED" not in viewed["content"]
    finally:
        clear_session_vars(bob)


def test_user_create_cannot_shadow_platform_or_external(namespace_home, tmp_path, monkeypatch):
    _home, platform = namespace_home
    _write_skill(platform, "shared-name", "platform")
    external = tmp_path / "external"
    _write_skill(external, "external-name", "external")
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [external])

    tokens = _scope("alice", "editors")
    try:
        for name in ("shared-name", "external-name"):
            result = json.loads(
                manager.skill_manage(
                    "create", name, content=VALID.format(name=name, body="user")
                )
            )
            assert result["success"] is False
            assert "already exists" in result["error"]
    finally:
        clear_session_vars(tokens)


def test_explicit_platform_create_is_immutable_for_editor(namespace_home):
    _home, platform = namespace_home
    tokens = _scope("alice", "editors")
    try:
        result = json.loads(
            manager.skill_manage(
                "create",
                "platform-new",
                namespace="platform",
                content=VALID.format(name="platform-new", body="platform"),
            )
        )
        assert result["success"] is False
        assert result["error_code"] == "immutable_platform"
        assert not (platform / "platform-new").exists()
    finally:
        clear_session_vars(tokens)


def test_invalid_or_missing_subject_cannot_create_user_namespace(namespace_home):
    home, _platform = namespace_home
    for user_id in ("", "alice/bob", "../alice"):
        tokens = _scope(user_id)
        try:
            result = json.loads(
                manager.skill_manage(
                    "create",
                    "denied-skill",
                    namespace="user",
                    content=VALID.format(name="denied-skill", body="no"),
                )
            )
            assert result["success"] is False
        finally:
            clear_session_vars(tokens)
    assert not (home / "user-skills").exists()


def test_symlinked_user_namespace_parent_is_rejected(namespace_home, tmp_path):
    home, _platform = namespace_home
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (home / "user-skills").symlink_to(redirected, target_is_directory=True)

    tokens = _scope("alice")
    try:
        result = json.loads(
            manager.skill_manage(
                "create",
                "redirected-skill",
                content=VALID.format(name="redirected-skill", body="no"),
            )
        )
        assert result["success"] is False
        assert "symlink" in result["error"].lower()
        assert not (redirected / "alice" / "redirected-skill").exists()
    finally:
        clear_session_vars(tokens)
