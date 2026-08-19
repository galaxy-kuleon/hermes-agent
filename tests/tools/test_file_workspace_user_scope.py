"""Per-user relative-path isolation for the multi-user api_server."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway import session_context
from tools import file_tools as ft
from tools import terminal_tool
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


@contextmanager
def _session(user_id: str = "", platform: str = "api_server"):
    tokens = session_context.set_session_vars(
        platform=platform,
        user_id=user_id,
    )
    try:
        yield
    finally:
        session_context.clear_session_vars(tokens)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(root))
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda task_id="default": None)
    monkeypatch.setattr(
        ft,
        "_registered_task_cwd_override",
        lambda task_id="default": None,
    )
    with _session(user_id="", platform=""):
        yield root


def test_api_user_gets_created_workspace_and_identity_is_read_at_call_time(workspace):
    with _session(user_id="", platform="api_server"):
        assert ft._resolve_base_dir() == workspace

    user_root = workspace / "user" / "late-bound-user"
    assert not user_root.exists()

    with _session(user_id="late-bound-user"):
        assert ft._resolve_base_dir() == user_root

    assert user_root.is_dir()


@pytest.mark.parametrize("platform", ["", "api_server"])
def test_no_user_id_preserves_legacy_base_and_real_read_argument(
    workspace,
    monkeypatch,
    platform,
):
    (workspace / "legacy.txt").write_text("legacy-shared-content\n", encoding="utf-8")
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )

    with _session(user_id="", platform=platform):
        assert ft._resolve_path_for_task("legacy.txt") == workspace / "legacy.txt"
        result = json.loads(
            ft.read_file_tool("legacy.txt", task_id=f"no-user-{platform}")
        )

    assert "legacy-shared-content" in result["content"]
    recording_operations.read_file.assert_called_once_with("legacy.txt", 1, 2000)
    assert not (workspace / "user").exists()


def test_two_users_cannot_read_each_others_relative_file_via_real_read_file(
    workspace,
    monkeypatch,
):
    shared = workspace / "invoice.txt"
    shared.write_text("old-shared-leak\n", encoding="utf-8")

    for user_id, content in (
        ("alice", "alice-private-invoice\n"),
        ("bob", "bob-private-invoice\n"),
    ):
        with _session(user_id=user_id):
            user_root = ft._resolve_base_dir()
            (user_root / "invoice.txt").write_text(content, encoding="utf-8")

    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with _session(user_id="alice"):
        alice_result = json.loads(
            ft.read_file_tool("invoice.txt", task_id="workspace-alice")
        )
    with _session(user_id="bob"):
        bob_result = json.loads(
            ft.read_file_tool("invoice.txt", task_id="workspace-bob")
        )

    assert "alice-private-invoice" in alice_result["content"]
    assert "bob-private-invoice" not in alice_result["content"]
    assert "bob-private-invoice" in bob_result["content"]
    assert "alice-private-invoice" not in bob_result["content"]
    assert shared.read_text(encoding="utf-8") == "old-shared-leak\n"


@pytest.mark.parametrize(
    ("attack_path", "filename"),
    [
        ("../alice-uuid/read-parent.txt", "read-parent.txt"),
        ("../../user/alice-uuid/read-workspace.txt", "read-workspace.txt"),
        ("./../alice-uuid/read-dot-parent.txt", "read-dot-parent.txt"),
        ("absolute", "read-absolute.txt"),
    ],
)
def test_api_user_cannot_read_sibling_via_traversal_or_workspace_absolute_path(
    workspace,
    monkeypatch,
    attack_path,
    filename,
):
    alice_root = workspace / "user" / "alice-uuid"
    alice_root.mkdir(parents=True)
    secret = f"ALICE-PRIVATE-{filename}\n"
    target = alice_root / filename
    target.write_text(secret, encoding="utf-8")
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )
    supplied_path = str(target) if attack_path == "absolute" else attack_path

    with _session(user_id="bob-uuid"):
        result = json.loads(
            ft.read_file_tool(
                supplied_path,
                task_id=f"boundary-read-{filename}",
            )
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_BOUNDARY_ERROR}
    assert secret.strip() not in json.dumps(result)
    recording_operations.read_file.assert_not_called()


@pytest.mark.parametrize(
    ("attack_path", "filename"),
    [
        ("../alice-uuid/write-parent.txt", "write-parent.txt"),
        ("../../user/alice-uuid/write-workspace.txt", "write-workspace.txt"),
        ("./../alice-uuid/write-dot-parent.txt", "write-dot-parent.txt"),
        ("absolute", "write-absolute.txt"),
    ],
)
def test_api_user_cannot_write_sibling_via_traversal_or_workspace_absolute_path(
    workspace,
    monkeypatch,
    attack_path,
    filename,
):
    alice_root = workspace / "user" / "alice-uuid"
    alice_root.mkdir(parents=True)
    target = alice_root / filename
    target.write_text("ALICE-ORIGINAL\n", encoding="utf-8")
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(ft, "_acl_raw_file_write_block", lambda: None)
    monkeypatch.setattr(
        ft,
        "_acl_protected_path_block",
        lambda *args, **kwargs: None,
    )
    supplied_path = str(target) if attack_path == "absolute" else attack_path

    with _session(user_id="bob-uuid"):
        result = json.loads(
            ft.write_file_tool(
                supplied_path,
                "BOB-WROTE-HERE\n",
                task_id=f"boundary-write-{filename}",
            )
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_BOUNDARY_ERROR}
    assert target.read_text(encoding="utf-8") == "ALICE-ORIGINAL\n"
    recording_operations.write_file.assert_not_called()


def test_api_user_cannot_write_shared_legacy_via_parent_traversal(
    workspace,
    monkeypatch,
):
    target = workspace / "legacy-write-unique.txt"
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with _session(user_id="bob-uuid"):
        result = json.loads(
            ft.write_file_tool(
                "../../legacy-write-unique.txt",
                "BOB-SHARED-WRITE\n",
                task_id="boundary-write-shared-legacy",
            )
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_BOUNDARY_ERROR}
    assert not target.exists()
    operations.write_file.assert_not_called()


def test_intermediate_symlink_into_sibling_is_rejected_for_read_and_write(
    workspace,
    monkeypatch,
):
    alice_root = workspace / "user" / "alice-uuid"
    alice_root.mkdir(parents=True)
    read_target = alice_root / "symlink-read-unique.txt"
    read_target.write_text("ALICE-SYMLINK-SECRET\n", encoding="utf-8")
    write_target = alice_root / "symlink-write-unique.txt"
    write_target.write_text("ALICE-SYMLINK-ORIGINAL\n", encoding="utf-8")
    bob_root = workspace / "user" / "bob-uuid"
    bob_root.mkdir()
    (bob_root / "sibling-link").symlink_to(alice_root, target_is_directory=True)
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(ft, "_acl_raw_file_write_block", lambda: None)
    monkeypatch.setattr(
        ft,
        "_acl_protected_path_block",
        lambda *args, **kwargs: None,
    )

    with _session(user_id="bob-uuid"):
        read_result = json.loads(
            ft.read_file_tool(
                "sibling-link/symlink-read-unique.txt",
                task_id="boundary-symlink-read",
            )
        )
        write_result = json.loads(
            ft.write_file_tool(
                "sibling-link/symlink-write-unique.txt",
                "BOB-SYMLINK-WRITE\n",
                task_id="boundary-symlink-write",
            )
        )

    assert read_result == {"error": ft._PER_USER_WORKSPACE_BOUNDARY_ERROR}
    assert write_result == {"error": ft._PER_USER_WORKSPACE_BOUNDARY_ERROR}
    assert write_target.read_text(encoding="utf-8") == "ALICE-SYMLINK-ORIGINAL\n"
    recording_operations.read_file.assert_not_called()
    recording_operations.write_file.assert_not_called()


def test_user_can_write_and_read_normalized_path_inside_own_base(
    workspace,
    monkeypatch,
):
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(ft, "_acl_raw_file_write_block", lambda: None)
    monkeypatch.setattr(
        ft,
        "_acl_protected_path_block",
        lambda *args, **kwargs: None,
    )

    with _session(user_id="alice-uuid"):
        write_result = json.loads(
            ft.write_file_tool(
                "./sub/own-normalized-unique.txt",
                "ALICE-OWN-NORMALIZED\n",
                task_id="own-normalized-write",
            )
        )
        read_result = json.loads(
            ft.read_file_tool(
                "./sub/own-normalized-unique.txt",
                task_id="own-normalized-read",
            )
        )

    target = workspace / "user" / "alice-uuid" / "sub" / "own-normalized-unique.txt"
    assert not write_result.get("error"), write_result
    assert target.read_text(encoding="utf-8") == "ALICE-OWN-NORMALIZED\n"
    assert "ALICE-OWN-NORMALIZED" in read_result["content"]


def test_workspace_boundary_blocks_search_replace_and_v4a_before_file_ops(
    workspace,
    monkeypatch,
):
    alice_root = workspace / "user" / "alice-uuid"
    alice_root.mkdir(parents=True)
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(ft, "_acl_raw_file_write_block", lambda: None)
    monkeypatch.setattr(
        ft,
        "_acl_protected_path_block",
        lambda *args, **kwargs: None,
    )
    absolute_v4a_target = alice_root / "v4a-boundary-unique.txt"

    with _session(user_id="bob-uuid"):
        results = [
            json.loads(
                ft.search_tool(
                    "needle",
                    path="../alice-uuid/search-boundary-unique",
                    task_id="boundary-search",
                )
            ),
            json.loads(
                ft.patch_tool(
                    mode="replace",
                    path="../alice-uuid/replace-boundary-unique.txt",
                    old_string="old",
                    new_string="new",
                    task_id="boundary-replace",
                )
            ),
            json.loads(
                ft.patch_tool(
                    mode="patch",
                    patch=(
                        "*** Begin Patch\n"
                        f"*** Add File: {absolute_v4a_target}\n"
                        "+private\n"
                        "*** End Patch\n"
                    ),
                    task_id="boundary-v4a",
                )
            ),
        ]

    assert results == [{"error": ft._PER_USER_WORKSPACE_BOUNDARY_ERROR}] * 3
    operations.search.assert_not_called()
    operations.patch_replace.assert_not_called()
    operations.patch_v4a.assert_not_called()
    assert not absolute_v4a_target.exists()


def test_user_scoped_search_dispatches_the_isolated_absolute_path(
    workspace,
    monkeypatch,
):
    class EmptySearchResult:
        matches = []
        files = []
        counts = {}
        total_count = 0
        truncated = False

        def to_dict(self, densify=False):
            return {"total_count": 0, "truncated": False}

    operations = Mock()
    operations.search.return_value = EmptySearchResult()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)
    monkeypatch.setattr(ft, "_acl_filter_search_result", lambda result, task_id: result)

    with _session(user_id="alice"):
        result = json.loads(ft.search_tool("invoice", path=".", task_id="search-alice"))

    assert result["total_count"] == 0
    assert operations.search.call_args.kwargs["path"] == str(
        workspace / "user" / "alice"
    )


def test_user_scoped_v4a_patch_dispatches_resolved_operation_paths(
    workspace,
    monkeypatch,
):
    operations = Mock()
    operations.env = LocalEnvironment(cwd=str(workspace), timeout=15)
    patch_result = Mock()
    patch_result.to_dict.return_value = {"success": True}
    operations.patch_v4a.return_value = patch_result

    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(ft, "_acl_raw_file_write_block", lambda: None)
    monkeypatch.setattr(
        ft,
        "_acl_protected_path_block",
        lambda *args, **kwargs: None,
    )
    patch = "*** Begin Patch\n*** Add File: note.txt\n+private\n*** End Patch\n"
    with _session(user_id="alice"):
        result = json.loads(
            ft.patch_tool(mode="patch", patch=patch, task_id="patch-alice")
        )

    assert result.get("success") is True, result
    rewritten = operations.patch_v4a.call_args.args[0]
    assert f"*** Add File: {workspace / 'user' / 'alice' / 'note.txt'}" in rewritten


@pytest.mark.parametrize(
    "user_id",
    [
        "",
        "   ",
        " alice ",
        ".",
        "..",
        "../alice",
        "alice/../../bob",
        "alice/bob",
        "💣",
        "a" * (ft._WORKSPACE_USER_ID_MAX_CHARS + 1),
    ],
)
def test_invalid_user_id_falls_back_without_path_escape(workspace, user_id):
    with _session(user_id=user_id):
        assert ft._workspace_user_scope() == ""
        assert ft._resolve_base_dir() == workspace

    assert not (workspace.parent / "alice").exists()
    assert not (workspace.parent / "bob").exists()
    assert not (workspace / "user").exists()


def test_live_cwd_outside_workspace_is_not_rewritten(workspace, tmp_path, monkeypatch):
    worktree = tmp_path / "explicit-worktree"
    worktree.mkdir()
    (worktree / "target.txt").write_text(
        "explicit-worktree-content\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        terminal_tool,
        "get_session_cwd",
        lambda task_id="default": str(worktree),
    )
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(worktree), timeout=15),
        cwd=str(worktree),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )

    with _session(user_id="alice"):
        assert ft._resolve_base_dir() == worktree
        result = json.loads(
            ft.read_file_tool("target.txt", task_id="explicit-worktree")
        )

    assert "explicit-worktree-content" in result["content"]
    recording_operations.read_file.assert_called_once_with("target.txt", 1, 2000)
    assert not (workspace / "user" / "alice").exists()


def test_api_user_absolute_path_outside_configured_workspace_is_unchanged(
    workspace,
    tmp_path,
    monkeypatch,
):
    handoff = tmp_path / "absolute-handoff-unique.txt"
    handoff.write_text("EXTERNAL-HANDOFF-CONTENT\n", encoding="utf-8")
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )

    with _session(user_id="alice-uuid"):
        result = json.loads(
            ft.read_file_tool(
                str(handoff),
                task_id="absolute-external-handoff",
            )
        )

    assert "EXTERNAL-HANDOFF-CONTENT" in result["content"]
    recording_operations.read_file.assert_called_once_with(str(handoff), 1, 2000)
    assert not (workspace / "user" / "alice-uuid").exists()


def test_cli_without_user_id_keeps_legacy_parent_traversal_behavior(
    workspace,
    tmp_path,
    monkeypatch,
):
    outside_target = tmp_path / "cli-parent-read-unique.txt"
    outside_target.write_text("CLI-LEGACY-PARENT\n", encoding="utf-8")
    operations = ShellFileOperations(
        LocalEnvironment(cwd=str(workspace), timeout=15),
        cwd=str(workspace),
    )
    recording_operations = Mock(wraps=operations)
    monkeypatch.setattr(
        ft, "_get_file_ops", lambda task_id="default": recording_operations
    )

    with _session(user_id="", platform=""):
        result = json.loads(
            ft.read_file_tool(
                "../cli-parent-read-unique.txt",
                task_id="cli-parent-traversal-legacy",
            )
        )

    assert "CLI-LEGACY-PARENT" in result["content"]
    recording_operations.read_file.assert_called_once_with(
        "../cli-parent-read-unique.txt",
        1,
        2000,
    )
    assert not (workspace / "user").exists()


def test_concurrent_requests_create_one_user_directory_safely(workspace):
    user_ids = ["alice", "bob"] * 12

    def resolve_for_request(user_id):
        with _session(user_id=user_id):
            return user_id, ft._resolve_base_dir()

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(resolve_for_request, user_ids))

    assert results == [(user_id, workspace / "user" / user_id) for user_id in user_ids]
    assert (workspace / "user" / "alice").is_dir()
    assert (workspace / "user" / "bob").is_dir()


def test_blocking_file_at_user_scope_fails_closed_without_shared_read(
    workspace,
    monkeypatch,
    caplog,
):
    (workspace / "shared.txt").write_text(
        "ALICE-SHARED-ERA-SECRET\n",
        encoding="utf-8",
    )
    user_parent = workspace / "user"
    user_parent.mkdir()
    (user_parent / "alice").write_text("attacker-created blocker\n", encoding="utf-8")
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with (
        _session(user_id="alice"),
        caplog.at_level(logging.ERROR, logger="tools.file_tools"),
    ):
        result = json.loads(
            ft.read_file_tool("shared.txt", task_id="blocked-user-file")
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_UNAVAILABLE_ERROR}
    assert "ALICE-SHARED-ERA-SECRET" not in json.dumps(result)
    operations.read_file.assert_not_called()
    assert any(
        record.levelno == logging.ERROR
        and record.getMessage()
        == "per_user_workspace_isolation_failed "
        "reason=mkdir_failed error_type=FileExistsError"
        for record in caplog.records
    )


def test_blocked_user_scope_rejects_every_relative_file_entrypoint(
    workspace,
    monkeypatch,
):
    user_parent = workspace / "user"
    user_parent.mkdir()
    (user_parent / "alice").write_text("attacker-created blocker\n", encoding="utf-8")
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with _session(user_id="alice"):
        results = [
            json.loads(ft.read_file_tool("note.txt", task_id="blocked-read")),
            json.loads(ft.search_tool("needle", path=".", task_id="blocked-search")),
            json.loads(
                ft.write_file_tool("note.txt", "private\n", task_id="blocked-write")
            ),
            json.loads(
                ft.patch_tool(
                    mode="replace",
                    path="note.txt",
                    old_string="old",
                    new_string="new",
                    task_id="blocked-replace",
                )
            ),
            json.loads(
                ft.patch_tool(
                    mode="patch",
                    patch=(
                        "*** Begin Patch\n"
                        "*** Add File: note.txt\n"
                        "+private\n"
                        "*** End Patch\n"
                    ),
                    task_id="blocked-v4a",
                )
            ),
        ]

    assert results == [{"error": ft._PER_USER_WORKSPACE_UNAVAILABLE_ERROR}] * len(
        results
    )
    operations.read_file.assert_not_called()
    operations.search.assert_not_called()
    operations.write_file.assert_not_called()
    operations.patch_replace.assert_not_called()
    operations.patch_v4a.assert_not_called()


def test_symlink_at_user_scope_fails_closed_without_shared_or_external_read(
    workspace,
    tmp_path,
    monkeypatch,
    caplog,
):
    (workspace / "shared.txt").write_text("SHARED-BASE-MARKER\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shared.txt").write_text("OUTSIDE-MARKER\n", encoding="utf-8")
    user_parent = workspace / "user"
    user_parent.mkdir()
    (user_parent / "alice").symlink_to(outside, target_is_directory=True)
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with (
        _session(user_id="alice"),
        caplog.at_level(logging.ERROR, logger="tools.file_tools"),
    ):
        result = json.loads(
            ft.read_file_tool("shared.txt", task_id="blocked-user-symlink")
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_UNAVAILABLE_ERROR}
    assert "SHARED-BASE-MARKER" not in json.dumps(result)
    assert "OUTSIDE-MARKER" not in json.dumps(result)
    operations.read_file.assert_not_called()
    assert any(
        record.levelno == logging.ERROR
        and record.getMessage()
        == "per_user_workspace_isolation_failed "
        "reason=invalid_scope error_type=ValueError"
        for record in caplog.records
    )


def test_symlink_at_user_scope_into_workspace_also_fails_closed(
    workspace,
    monkeypatch,
):
    user_parent = workspace / "user"
    bob_root = user_parent / "bob"
    bob_root.mkdir(parents=True)
    (bob_root / "sibling-root-secret.txt").write_text(
        "BOB-SIBLING-ROOT-SECRET\n",
        encoding="utf-8",
    )
    (user_parent / "alice").symlink_to(bob_root, target_is_directory=True)
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with _session(user_id="alice"):
        result = json.loads(
            ft.read_file_tool(
                "sibling-root-secret.txt",
                task_id="blocked-user-internal-symlink",
            )
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_UNAVAILABLE_ERROR}
    assert "BOB-SIBLING-ROOT-SECRET" not in json.dumps(result)
    operations.read_file.assert_not_called()


def test_directory_creation_failure_logs_error_and_fails_closed(
    workspace,
    monkeypatch,
    caplog,
):
    candidate = workspace / "user" / "alice"
    original_mkdir = Path.mkdir

    def deny_candidate(path, *args, **kwargs):
        if path == candidate:
            raise PermissionError("read-only workspace")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_candidate)
    operations = Mock()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": operations)

    with (
        _session(user_id="alice"),
        caplog.at_level(logging.ERROR, logger="tools.file_tools"),
    ):
        result = json.loads(
            ft.read_file_tool("shared.txt", task_id="mkdir-permission-denied")
        )

    assert result == {"error": ft._PER_USER_WORKSPACE_UNAVAILABLE_ERROR}
    operations.read_file.assert_not_called()
    assert not candidate.exists()
    assert any(
        record.levelno == logging.ERROR
        and record.getMessage()
        == "per_user_workspace_isolation_failed "
        "reason=mkdir_failed error_type=PermissionError"
        for record in caplog.records
    )
