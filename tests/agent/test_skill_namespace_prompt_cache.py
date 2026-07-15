from gateway.session_context import clear_session_vars, set_session_vars


def _write_skill(root, name, token):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {token}.\n---\n\n# {name}\n\n{token}\n"
    )


def test_prompt_cache_is_keyed_by_current_user_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path / "skills", "platform-skill", "PLATFORM")
    _write_skill(tmp_path / "user-skills" / "alice", "alice-skill", "ALICE")
    _write_skill(tmp_path / "user-skills" / "bob", "bob-skill", "BOB")

    from agent.prompt_builder import (
        build_skills_system_prompt,
        clear_skills_system_prompt_cache,
    )

    clear_skills_system_prompt_cache(clear_snapshot=True)
    alice = set_session_vars(platform="api_server", user_id="alice")
    try:
        prompt_a = build_skills_system_prompt()
        assert "platform-skill" in prompt_a
        assert "alice-skill" in prompt_a
        assert "bob-skill" not in prompt_a
    finally:
        clear_session_vars(alice)

    bob = set_session_vars(platform="api_server", user_id="bob")
    try:
        prompt_b = build_skills_system_prompt()
        assert "platform-skill" in prompt_b
        assert "bob-skill" in prompt_b
        assert "alice-skill" not in prompt_b
        assert prompt_b != prompt_a
    finally:
        clear_session_vars(bob)
