from gateway.session_context import clear_session_vars, set_session_vars


def _write_skill(root, name, token):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {token}.\n---\n\n# {name}\n\n{token}\n"
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "proof.md").write_text(token)
    return skill_dir


def test_slash_discovery_and_absolute_load_use_only_current_user_root(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    _write_skill(platform, "platform-skill", "PLATFORM")
    alice_dir = _write_skill(
        tmp_path / "user-skills" / "alice", "alice-skill", "ALICE"
    )
    _write_skill(tmp_path / "user-skills" / "bob", "bob-skill", "BOB")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import skills_tool
    from agent import skill_commands

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", platform)
    tokens = set_session_vars(platform="api_server", user_id="alice")
    try:
        commands = skill_commands.scan_skill_commands()
        assert "/platform-skill" in commands
        assert "/alice-skill" in commands
        assert "/bob-skill" not in commands

        loaded = skill_commands._load_skill_payload(str(alice_dir))
        assert loaded is not None
        payload, resolved_dir, display_name = loaded
        assert payload["qualified_name"] == "user:alice-skill"
        assert "ALICE" in payload["content"]
        assert resolved_dir == alice_dir
        assert display_name == "alice-skill"
    finally:
        clear_session_vars(tokens)


def test_remote_skill_file_projection_contains_platform_external_and_own_only(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    external = tmp_path / "external"
    _write_skill(platform, "platform-skill", "PLATFORM")
    _write_skill(external, "external-skill", "EXTERNAL")
    _write_skill(tmp_path / "user-skills" / "alice", "alice-skill", "ALICE")
    _write_skill(tmp_path / "user-skills" / "bob", "bob-skill", "BOB")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs", lambda: [external]
    )

    from tools.credential_files import iter_skills_files

    tokens = set_session_vars(platform="api_server", user_id="alice")
    try:
        projected = iter_skills_files()
        host_paths = {entry["host_path"] for entry in projected}
        container_paths = {entry["container_path"] for entry in projected}
        assert any("platform-skill/SKILL.md" in p for p in host_paths)
        assert any("external-skill/SKILL.md" in p for p in host_paths)
        assert any("alice-skill/SKILL.md" in p for p in host_paths)
        assert not any("bob-skill" in p for p in host_paths)
        assert any("/skills/platform-skill/SKILL.md" in p for p in container_paths)
        assert any("/external_skills/0/external-skill/SKILL.md" in p for p in container_paths)
        assert any("/user-skills/alice/alice-skill/SKILL.md" in p for p in container_paths)
    finally:
        clear_session_vars(tokens)
