import json

from gateway.session_context import clear_session_vars, set_session_vars
from tools import skill_acl
from tools import skills_tool


ACL = {
    "enabled": True,
    "roles": {"user": set()},
    "groups": {"readers": {"read"}},
    "protect_paths": [],
    "error": None,
}


def test_view_usage_and_curator_state_are_scoped_to_resolved_user_root(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    platform.mkdir()
    user_root = tmp_path / "user-skills" / "alice"
    skill_dir = user_root / "private"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: private\ndescription: Private.\n---\n\n# Private\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", platform)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ACL)

    tokens = set_session_vars(
        platform="api_server",
        user_id="alice",
        user_role="user",
        user_groups="readers",
    )
    try:
        result = json.loads(
            skills_tool._skill_view_with_bump({"name": "user:private"})
        )
        assert result["success"] is True

        usage = json.loads((user_root / ".usage.json").read_text())
        assert usage["private"]["view_count"] == 1
        assert usage["private"]["use_count"] == 1
        assert not (platform / ".usage.json").exists()

        from agent import curator, curator_backup

        assert curator._state_file() == user_root / ".curator_state"
        assert curator_backup._skills_dir() == user_root
        assert curator_backup._backups_dir() == user_root / ".curator_backups"
    finally:
        clear_session_vars(tokens)
