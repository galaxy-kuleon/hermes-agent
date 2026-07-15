from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars


def test_user_root_exists_only_for_valid_api_server_subject(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.skill_namespaces import get_current_user_skills_dir

    tokens = set_session_vars(platform="api_server", user_id="user-123")
    try:
        assert get_current_user_skills_dir() == tmp_path / "user-skills" / "user-123"
    finally:
        clear_session_vars(tokens)
    tokens = set_session_vars(platform="cli", user_id="user-123")
    try:
        assert get_current_user_skills_dir() is None
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize(
    "raw",
    ["", ".", "..", "../alice", "alice/bob", "alice bob", "alice!", "a" * 65],
)
def test_user_id_validation_is_lossless_and_rejects_path_values(raw):
    from agent.skill_namespaces import validate_owui_user_id

    with pytest.raises(ValueError):
        validate_owui_user_id(raw)


def test_get_all_roots_orders_platform_external_then_current_user(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    external = tmp_path / "external"
    platform.mkdir()
    external.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from agent import skill_utils

    monkeypatch.setattr(skill_utils, "get_skills_dir", lambda: platform)
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda: [external])
    tokens = set_session_vars(platform="api_server", user_id="alice")
    try:
        roots = skill_utils.get_skill_roots()
        assert [root.namespace for root in roots] == ["platform", "external-0", "user"]
        assert [root.path for root in roots] == [
            platform,
            external,
            tmp_path / "user-skills" / "alice",
        ]
        assert skill_utils.get_all_skills_dirs() == [root.path for root in roots]
    finally:
        clear_session_vars(tokens)


def test_approval_subject_binding_does_not_leak_after_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.skill_namespaces import (
        bind_skill_namespace_user,
        get_current_user_skills_dir,
    )

    tokens = set_session_vars(platform="api_server", user_id="approver")
    try:
        with bind_skill_namespace_user("original-user"):
            assert get_current_user_skills_dir() == (
                tmp_path / "user-skills" / "original-user"
            )
        assert get_current_user_skills_dir() == tmp_path / "user-skills" / "approver"
    finally:
        clear_session_vars(tokens)
