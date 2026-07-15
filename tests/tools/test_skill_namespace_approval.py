import json

from gateway.session_context import clear_session_vars, set_session_vars
from tools import write_approval as wa
from tools import skill_manager_tool as manager


CONTENT = """---
name: staged-skill
description: Staged namespace test.
---

# Staged

Original subject.
"""


def test_approved_user_write_replays_to_original_subject(tmp_path, monkeypatch):
    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)

    approver = set_session_vars(platform="api_server", user_id="approver")
    try:
        result = json.loads(
            manager.apply_skill_pending(
                {
                    "action": "create",
                    "name": "staged-skill",
                    "namespace": "user",
                    "subject_user_id": "original-user",
                    "content": CONTENT,
                }
            )
        )
        assert result["success"] is True
        assert result["namespace"] == "user"
        assert (
            tmp_path
            / "user-skills"
            / "original-user"
            / "staged-skill"
            / "SKILL.md"
        ).exists()
        assert not (tmp_path / "user-skills" / "approver" / "staged-skill").exists()
    finally:
        clear_session_vars(approver)


def test_user_pending_replay_without_original_subject_fails_closed(tmp_path, monkeypatch):
    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)

    result = json.loads(
        manager.apply_skill_pending(
            {
                "action": "create",
                "name": "staged-skill",
                "namespace": "user",
                "content": CONTENT,
            }
        )
    )
    assert result["success"] is False
    assert "original subject" in result["error"].lower()


def test_pending_store_is_partitioned_but_replay_keeps_original_subject(
    tmp_path, monkeypatch
):
    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)
    monkeypatch.setattr(
        wa,
        "evaluate_gate",
        lambda subsystem: wa.GateDecision(stage=True, message="staged"),
    )

    alice = set_session_vars(platform="api_server", user_id="alice")
    try:
        staged = json.loads(
            manager.skill_manage("create", "staged-skill", content=CONTENT)
        )
        assert staged["staged"] is True
        alice_record = wa.get_pending(wa.SKILLS, staged["pending_id"])
        assert alice_record["payload"]["subject_user_id"] == "alice"
        assert alice_record["payload"]["namespace"] == "user"
    finally:
        clear_session_vars(alice)

    bob = set_session_vars(platform="api_server", user_id="bob")
    try:
        assert wa.list_pending(wa.SKILLS) == []
        assert wa.get_pending(wa.SKILLS, staged["pending_id"]) is None
        applied = json.loads(manager.apply_skill_pending(alice_record["payload"]))
        assert applied["success"] is True
    finally:
        clear_session_vars(bob)

    assert (tmp_path / "user-skills" / "alice" / "staged-skill").exists()
    assert not (tmp_path / "user-skills" / "bob" / "staged-skill").exists()


def test_pending_diff_resolves_original_subject_not_reviewer(tmp_path, monkeypatch):
    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)

    alice_skill = tmp_path / "user-skills" / "alice" / "staged-skill"
    bob_skill = tmp_path / "user-skills" / "bob" / "staged-skill"
    alice_skill.mkdir(parents=True)
    bob_skill.mkdir(parents=True)
    (alice_skill / "SKILL.md").write_text(CONTENT.replace("Original", "ALICE"))
    (bob_skill / "SKILL.md").write_text(CONTENT.replace("Original", "BOB"))
    record = {
        "payload": {
            "action": "edit",
            "name": "staged-skill",
            "namespace": "user",
            "subject_user_id": "alice",
            "content": CONTENT.replace("Original", "UPDATED"),
        }
    }

    reviewer = set_session_vars(platform="api_server", user_id="bob")
    try:
        diff = wa.skill_pending_diff(record)
        assert "ALICE subject" in diff
        assert "BOB subject" not in diff
    finally:
        clear_session_vars(reviewer)
