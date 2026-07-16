import json
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools import platform_skill_store as store


SKILL = """---
name: {name}
description: Platform store test.
---

# {name}

{body}
"""


def _write_skill(root: Path, name: str, body: str = "body") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        SKILL.format(name=name, body=body), encoding="utf-8"
    )
    return skill


@pytest.fixture
def store_paths(tmp_path, monkeypatch):
    platform = tmp_path / "skills"
    state = tmp_path / "skill-state" / "platform"
    transactions = tmp_path / "platform-skill-transactions"
    platform.mkdir()
    monkeypatch.setenv(store.PLATFORM_SKILL_WRITER_ENV, "1")
    return platform, state, transactions


def test_build_plan_is_read_only(store_paths, tmp_path):
    platform, state, transactions = store_paths
    source = tmp_path / "source"
    source.mkdir()
    _write_skill(source, "new-skill")

    before = store.store_manifest(platform)
    plan = store.build_plan(source, platform)

    assert plan["add"] == ["new-skill"]
    assert plan["update"] == []
    assert store.store_manifest(platform) == before
    assert not state.exists()
    assert not transactions.exists()


def test_put_commits_receipt_and_increments_generation(store_paths, tmp_path):
    platform, state, transactions = store_paths
    source = _write_skill(tmp_path / "incoming", "canary", "v1")

    result = store.put_skill(
        source,
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
        expected_generation=0,
    )

    assert result["ok"] is True
    assert result["before_generation"] == 0
    assert result["after_generation"] == 1
    assert store.read_generation(state) == 1
    assert (platform / "canary" / "SKILL.md").is_file()
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["status"] == "committed"
    assert receipt["before_manifest"] != receipt["after_manifest"]
    assert receipt["operation"] == "put"


def test_expected_generation_mismatch_is_no_write(store_paths, tmp_path):
    platform, state, transactions = store_paths
    source = _write_skill(tmp_path / "incoming", "canary")

    with pytest.raises(store.PlatformSkillStoreError, match="generation"):
        store.put_skill(
            source,
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
            expected_generation=7,
        )

    assert not (platform / "canary").exists()
    assert store.read_generation(state) == 0


def test_post_mutation_validation_failure_restores_original(store_paths, tmp_path):
    platform, state, transactions = store_paths
    _write_skill(platform, "existing", "original")
    duplicate = _write_skill(tmp_path / "incoming", "existing", "duplicate")

    with pytest.raises(store.PlatformSkillStoreError, match="validation"):
        store.put_skill(
            duplicate,
            destination="other-folder",
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
        )

    assert "original" in (platform / "existing" / "SKILL.md").read_text()
    assert not (platform / "other-folder").exists()
    assert store.read_generation(state) == 0


def test_verify_rejects_symlinks_special_files_and_duplicate_names(store_paths):
    platform, _state, _transactions = store_paths
    _write_skill(platform, "one", "one")
    other = _write_skill(platform, "two", "two")
    (other / "SKILL.md").write_text(SKILL.format(name="one", body="dup"))
    (platform / "redirect").symlink_to(platform / "one", target_is_directory=True)

    verified = store.verify_store(platform)

    assert verified["ok"] is False
    joined = "\n".join(verified["errors"])
    assert "duplicate" in joined
    assert "symlink" in joined


def test_rollback_requires_exact_current_generation_and_is_one_shot(
    store_paths, tmp_path
):
    platform, state, transactions = store_paths
    first = _write_skill(tmp_path / "incoming-a", "a")
    second = _write_skill(tmp_path / "incoming-b", "b")
    tx_a = store.put_skill(
        first,
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
    )
    tx_b = store.put_skill(
        second,
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
    )
    state.mkdir(parents=True, exist_ok=True)
    usage = state / "usage.json"
    usage.write_text('{"a":{"view_count":9}}')

    with pytest.raises(store.PlatformSkillStoreError, match="base generation"):
        store.rollback_transaction(
            tx_a["transaction_id"],
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
        )

    rolled = store.rollback_transaction(
        tx_b["transaction_id"],
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
    )
    assert rolled["ok"] is True
    assert (platform / "a").exists()
    assert not (platform / "b").exists()
    assert json.loads(usage.read_text())["a"]["view_count"] == 9
    with pytest.raises(store.PlatformSkillStoreError, match="already rolled back"):
        store.rollback_transaction(
            tx_b["transaction_id"],
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
        )


@pytest.mark.parametrize(
    "transaction_id",
    [
        "../crafted",
        "../../skills/crafted",
        "/tmp/crafted",
        "20260716T120000Z-deadbeefcafe/../crafted",
        "not-a-platform-transaction",
    ],
)
def test_rollback_rejects_noncanonical_transaction_ids_before_path_access(
    store_paths, transaction_id
):
    platform, state, transactions = store_paths
    _write_skill(platform, "must-survive", "original")

    with pytest.raises(store.PlatformSkillStoreError, match="invalid transaction id"):
        store.rollback_transaction(
            transaction_id,
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
        )

    assert "original" in (platform / "must-survive" / "SKILL.md").read_text()
    assert not transactions.exists()


def test_api_server_context_cannot_activate_operator_writer(store_paths, tmp_path):
    platform, state, transactions = store_paths
    source = _write_skill(tmp_path / "incoming", "blocked")
    tokens = set_session_vars(platform="api_server", user_id="admin")
    try:
        with pytest.raises(store.PlatformSkillStoreError, match="out-of-band"):
            store.put_skill(
                source,
                target_root=platform,
                state_dir=state,
                transactions_dir=transactions,
            )
    finally:
        clear_session_vars(tokens)

    assert not (platform / "blocked").exists()


def test_next_writer_recovers_interrupted_transaction_without_losing_usage(
    store_paths, tmp_path
):
    platform, state, transactions = store_paths
    source = _write_skill(tmp_path / "incoming", "interrupted")
    committed = store.put_skill(
        source,
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
    )
    receipt_path = Path(committed["receipt_path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["status"] = "applying"
    receipt_path.write_text(json.dumps(receipt))
    (state / "usage.json").write_text('{"live":{"view_count":3}}')
    publish_stage = platform / ".shared-publish-interrupted"
    publish_backup = platform / ".shared-publish-backup-interrupted"
    publish_stage.mkdir()
    publish_backup.mkdir()
    (publish_stage / "partial").write_text("partial")
    (publish_backup / "old").write_text("old")

    recovered = store.recover_incomplete_transactions(
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
    )

    assert recovered["recovered"] == [committed["transaction_id"]]
    assert not (platform / "interrupted").exists()
    assert not publish_stage.exists()
    assert not publish_backup.exists()
    assert json.loads((state / "usage.json").read_text())["live"]["view_count"] == 3
    assert store.read_generation(state) == 2


def test_legacy_generated_state_moves_off_platform_content(store_paths):
    platform, state, _transactions = store_paths
    (platform / ".usage.json").write_text('{"legacy":{}}')
    (platform / ".bundled_manifest").write_text("legacy:hash\n")
    (platform / ".hub").mkdir()
    (platform / ".hub" / "lock.json").write_text("{}")

    moved = store.migrate_legacy_generated_state(platform, state)

    assert set(moved) == {".usage.json", ".bundled_manifest", ".hub"}
    assert (state / "usage.json").is_file()
    assert (state / "bundled_manifest").is_file()
    assert (state / "hub" / "lock.json").is_file()
    assert store.verify_store(platform)["ok"] is True


def test_bundled_sync_partial_copy_failure_rolls_back_transaction(
    store_paths, tmp_path, monkeypatch
):
    platform, state, transactions = store_paths
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "will-fail")

    from tools import skills_sync

    monkeypatch.setattr(skills_sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(
        skills_sync, "_get_optional_dir", lambda: tmp_path / "optional-missing"
    )

    def _fail_copy(*_args, **_kwargs):
        raise OSError("injected copy failure")

    monkeypatch.setattr(skills_sync.shutil, "copytree", _fail_copy)

    with pytest.raises(store.PlatformSkillStoreError, match="injected copy failure"):
        store.sync_bundled(
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
        )

    assert list(platform.iterdir()) == []
    assert store.read_generation(state) == 0


def test_bundled_sync_commits_only_through_operator_transaction(
    store_paths, tmp_path, monkeypatch
):
    platform, state, transactions = store_paths
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "bundled-one")

    from tools import skills_sync

    monkeypatch.setattr(skills_sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(
        skills_sync, "_get_optional_dir", lambda: tmp_path / "optional-missing"
    )

    result = store.sync_bundled(
        target_root=platform,
        state_dir=state,
        transactions_dir=transactions,
        expected_generation=0,
    )

    assert result["after_generation"] == 1
    assert result["result"]["copied"] == ["bundled-one"]
    assert (platform / "bundled-one" / "SKILL.md").is_file()
    assert (state / "bundled_manifest").is_file()


def test_rejected_api_writer_does_not_create_missing_platform_root(
    tmp_path, monkeypatch
):
    platform = tmp_path / "missing-skills"
    state = tmp_path / "state"
    transactions = tmp_path / "transactions"
    source = _write_skill(tmp_path / "incoming", "blocked")
    monkeypatch.setenv(store.PLATFORM_SKILL_WRITER_ENV, "1")
    tokens = set_session_vars(platform="api_server", user_id="admin")
    try:
        with pytest.raises(store.PlatformSkillStoreError, match="out-of-band"):
            store.put_skill(
                source,
                target_root=platform,
                state_dir=state,
                transactions_dir=transactions,
            )
    finally:
        clear_session_vars(tokens)

    assert not platform.exists()
    assert not state.exists()
    assert not transactions.exists()


def test_state_update_failure_rolls_back_content_and_operator_state(
    store_paths, tmp_path
):
    platform, state, transactions = store_paths
    source = _write_skill(tmp_path / "incoming", "canary")
    hub = state / "hub"
    hub.mkdir(parents=True)
    lock = hub / "lock.json"
    lock.write_text('{"installed":{}}')

    def _failing_state_update(_installed_path: Path):
        lock.write_text('{"installed":{"canary":{}}}')
        raise OSError("injected lock write failure")

    with pytest.raises(store.PlatformSkillStoreError, match="lock write failure"):
        store.put_skill(
            source,
            state_update=_failing_state_update,
            target_root=platform,
            state_dir=state,
            transactions_dir=transactions,
        )

    assert not (platform / "canary").exists()
    assert json.loads(lock.read_text()) == {"installed": {}}
    assert store.read_generation(state) == 0
