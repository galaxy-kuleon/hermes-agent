import os
import stat
from pathlib import Path

import pytest

from tools import skills_sync


SKILL = """---
name: bundled-one
description: Bundled test.
---

# Bundled
"""


def _paths(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    source = bundled / "category" / "bundled-one"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(SKILL)
    platform = tmp_path / "skills"
    platform.mkdir()
    state = tmp_path / "skill-state" / "platform"
    monkeypatch.setattr(skills_sync, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(skills_sync, "SKILLS_DIR", platform)
    monkeypatch.setattr(skills_sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(
        skills_sync, "_get_optional_dir", lambda: tmp_path / "optional-missing"
    )
    return bundled, platform, state


def test_direct_platform_sync_is_operator_only_and_does_not_write(
    tmp_path, monkeypatch
):
    _bundled, platform, state = _paths(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_PLATFORM_SKILLS_IMMUTABLE", "true")

    result = skills_sync.sync_skills(
        quiet=True, target_root=platform, state_dir=state
    )

    assert result["skipped_operator_only"] is True
    assert not (platform / "category" / "bundled-one" / "SKILL.md").exists()
    assert not (state / "bundled_manifest").exists()
    assert not (platform / ".bundled_manifest").exists()
    assert not (platform / ".hub").exists()


def test_startup_sync_false_is_a_noop_even_when_platform_is_writable(
    tmp_path, monkeypatch
):
    _bundled, platform, state = _paths(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_SKILLS_SYNC_ON_START", "false")

    result = skills_sync.sync_skills(
        quiet=True, target_root=platform, state_dir=state, startup=True
    )

    assert result["skipped_startup_policy"] is True
    assert list(platform.iterdir()) == []
    assert not state.exists()


def test_startup_auto_skips_read_only_platform_without_writing(
    tmp_path, monkeypatch
):
    _bundled, platform, state = _paths(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_SKILLS_SYNC_ON_START", raising=False)
    platform.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = skills_sync.sync_skills(
            quiet=True, target_root=platform, state_dir=state, startup=True
        )
    finally:
        platform.chmod(stat.S_IRWXU)

    assert result["skipped_read_only"] is True
    assert list(platform.iterdir()) == []
    assert not state.exists()


def test_explicit_startup_true_fails_loudly_on_read_only_platform(
    tmp_path, monkeypatch
):
    _bundled, platform, state = _paths(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_SKILLS_SYNC_ON_START", "true")
    platform.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            skills_sync.sync_skills(
                quiet=True, target_root=platform, state_dir=state, startup=True
            )
    finally:
        platform.chmod(stat.S_IRWXU)


def test_named_profile_sync_remains_writable_under_gateway_policy(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "coder"
    bundled = tmp_path / "bundled"
    source = bundled / "bundled-one"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(SKILL)
    platform = profile_home / "skills"
    state = profile_home / "skill-state" / "platform"
    monkeypatch.setattr(skills_sync, "HERMES_HOME", profile_home)
    monkeypatch.setattr(skills_sync, "SKILLS_DIR", platform)
    monkeypatch.setattr(skills_sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(
        skills_sync, "_get_optional_dir", lambda: tmp_path / "optional-missing"
    )
    monkeypatch.setenv("HERMES_PLATFORM_SKILLS_IMMUTABLE", "true")

    result = skills_sync.sync_skills(
        quiet=True,
        target_root=platform,
        state_dir=state,
    )

    assert result["copied"] == ["bundled-one"]
    assert (platform / "bundled-one" / "SKILL.md").is_file()
