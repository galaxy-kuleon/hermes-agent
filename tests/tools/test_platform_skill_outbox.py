"""Tests for the read-only platform-skill governance outbox export."""

from __future__ import annotations

import base64
import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from tools import platform_skill_outbox as outbox
from tools import platform_skill_store as store


SKILL = """---
name: governed
description: Governance outbox test.
---

# Governed

{body}
"""


@pytest.fixture
def journal(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    state = tmp_path / "state"
    transactions = tmp_path / "transactions"
    root.mkdir()
    monkeypatch.setenv("HERMES_PLATFORM_SKILL_WRITER", "1")
    return root, state, transactions


def _commit_present(journal):
    root, state, transactions = journal

    def mutate(transaction_id):
        skill = root / "legal" / "governed"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL.format(body="exact bytes"))
        return {
            "before_hash": None,
            "after_hash": store.skill_tree_hash(skill),
            "governance_outbox": store.capture_transaction_post_state(
                transaction_id,
                "legal/governed",
                target_root=root,
                transactions_dir=transactions,
            ),
        }

    return store.apply_transaction(
        "create",
        mutate,
        target_root=root,
        state_dir=state,
        transactions_dir=transactions,
        receipt_metadata={"target": "governed"},
    )


def test_lists_and_exports_exact_committed_tree(journal):
    committed = _commit_present(journal)
    root, _state, transactions = journal

    events = outbox.collect_events(transactions)

    assert events == [
        {
            "schema_version": 1,
            "event_id": committed["transaction_id"],
            "transaction_id": committed["transaction_id"],
            "generation": 1,
            "operation": "create",
            "target": "governed",
            "state": "present",
            "destination": "legal/governed",
            "tree_hash": store.skill_tree_hash(root / "legal" / "governed"),
            "before_hash": None,
            "after_hash": store.skill_tree_hash(root / "legal" / "governed"),
            "archive": store.POST_STATE_ARCHIVE_FILENAME,
            "archive_sha256": events[0]["archive_sha256"],
        }
    ]
    exported = outbox.export_event(transactions, committed["transaction_id"])
    archive = base64.b64decode(exported["archive_b64"])
    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as handle:
        member = handle.extractfile("legal/governed/SKILL.md")
        assert member is not None
        assert member.read().decode() == SKILL.format(body="exact bytes")


def test_deleted_target_is_an_explicit_tombstone(journal):
    root, state, transactions = journal
    skill = root / "legal" / "governed"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL.format(body="delete me"))

    def mutate(transaction_id):
        for path in sorted(skill.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        skill.rmdir()
        return {
            "before_hash": "before",
            "after_hash": None,
            "governance_outbox": store.capture_transaction_post_state(
                transaction_id,
                "legal/governed",
                target_root=root,
                transactions_dir=transactions,
            ),
        }

    committed = store.apply_transaction(
        "delete",
        mutate,
        target_root=root,
        state_dir=state,
        transactions_dir=transactions,
        receipt_metadata={"target": "governed"},
    )

    exported = outbox.export_event(transactions, committed["transaction_id"])
    assert exported["state"] == "deleted"
    assert exported["archive"] is None
    assert exported["archive_b64"] is None


def test_export_rejects_archive_tampering(journal):
    committed = _commit_present(journal)
    _root, _state, transactions = journal
    receipt_path = (
        transactions / committed["transaction_id"] / store.TRANSACTION_RECEIPT_FILENAME
    )
    receipt = json.loads(receipt_path.read_text())
    archive = (
        receipt_path.parent
        / receipt["result"]["governance_outbox"]["archive"]
    )
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(
        store.PlatformSkillStoreError,
        match="digest mismatch",
    ):
        outbox.export_event(transactions, committed["transaction_id"])


def test_failed_rollback_outbox_restores_committed_state(journal, monkeypatch):
    root, state, transactions = journal
    skill = root / "legal" / "governed"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL.format(body="before"))

    def mutate(transaction_id):
        (skill / "SKILL.md").write_text(SKILL.format(body="committed"))
        return {
            "before_hash": "a" * 64,
            "after_hash": store.skill_tree_hash(skill),
            "governance_outbox": store.capture_transaction_post_state(
                transaction_id,
                "legal/governed",
                target_root=root,
                transactions_dir=transactions,
            ),
        }

    committed = store.apply_transaction(
        "edit",
        mutate,
        target_root=root,
        state_dir=state,
        transactions_dir=transactions,
        receipt_metadata={"target": "governed"},
    )
    original_capture = store.capture_transaction_post_state

    def fail_rollback_capture(*args, **kwargs):
        if (
            kwargs.get("archive_filename")
            == store.ROLLBACK_POST_STATE_ARCHIVE_FILENAME
        ):
            raise OSError("rollback outbox unavailable")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "capture_transaction_post_state",
        fail_rollback_capture,
    )
    with pytest.raises(OSError, match="rollback outbox unavailable"):
        store.rollback_transaction(
            committed["transaction_id"],
            target_root=root,
            state_dir=state,
            transactions_dir=transactions,
        )

    assert "committed" in (skill / "SKILL.md").read_text()
    assert store.read_generation(state) == 1
    receipt = json.loads(
        (
            transactions
            / committed["transaction_id"]
            / store.TRANSACTION_RECEIPT_FILENAME
        ).read_text()
    )
    assert receipt["status"] == "committed"


def test_successful_rollback_exports_forward_and_reverse_events(journal):
    root, state, transactions = journal
    skill = root / "legal" / "governed"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL.format(body="before"))
    before_hash = store.skill_tree_hash(skill)

    def mutate(transaction_id):
        (skill / "SKILL.md").write_text(SKILL.format(body="after"))
        after_hash = store.skill_tree_hash(skill)
        return {
            "before_hash": before_hash,
            "after_hash": after_hash,
            "governance_outbox": store.capture_transaction_post_state(
                transaction_id,
                "legal/governed",
                target_root=root,
                transactions_dir=transactions,
            ),
        }

    committed = store.apply_transaction(
        "edit",
        mutate,
        target_root=root,
        state_dir=state,
        transactions_dir=transactions,
        receipt_metadata={"target": "governed"},
    )
    store.rollback_transaction(
        committed["transaction_id"],
        target_root=root,
        state_dir=state,
        transactions_dir=transactions,
    )

    events = outbox.collect_events(transactions)
    assert [event["event_id"] for event in events] == [
        committed["transaction_id"],
        f"rollback:{committed['transaction_id']}",
    ]
    assert [event["generation"] for event in events] == [1, 2]
    assert events[1]["before_hash"] == events[0]["after_hash"]
    assert events[1]["after_hash"] == events[0]["before_hash"]
    assert "before" in (skill / "SKILL.md").read_text()


def test_cli_surface_contains_no_acknowledge_or_delete_command():
    parser = outbox.parse_args(["list"])
    assert parser.command == "list"
    with pytest.raises(SystemExit):
        outbox.parse_args(["acknowledge"])
    with pytest.raises(SystemExit):
        outbox.parse_args(["delete"])
