"""Tests for issue #13 — prevent file/terminal BYPASS of /home/hermes/skills.

Covers (1) toolset coupling: api_server skill ACL grants never grant `terminal`
or raw file-write tools;
(2) the `file` toolset split (backward compatible); and (3) the protected-path
guard at the file-tool execution layer — raw WRITE bypass is always denied,
READ bypass needs read for shared/protected roots, sibling user roots stay hidden,
all raw api_server writes fail closed when ACL is enabled, traversal is
normalized, and local/ACL-disabled compatibility remains intact.
"""

import json
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
import tools.skill_acl as skill_acl
import tools.file_tools as ft
from tools.file_operations import SearchMatch, SearchResult
from gateway.platforms.api_server import _apply_skill_acl_toolset_minimization as minim
from toolsets import resolve_toolset

G_READERS = "grp-readers"          # read
G_CREATORS = "grp-creators"        # read, create only
G_UPDATERS = "grp-updaters"        # read, update only
G_EDITORS = "grp-editors"          # read, create, update (manage, no delete)
G_DELETERS = "grp-deleters"        # read, delete only

ENABLED_ACL = {
    "enabled": True,
    "roles": {"admin": {"read", "create", "update", "delete"}, "user": set()},
    "groups": {
        G_READERS: {"read"},
        G_CREATORS: {"read", "create"},
        G_UPDATERS: {"read", "update"},
        G_EDITORS: {"read", "create", "update"},
        G_DELETERS: {"read", "delete"},
    },
    "protect_paths": [],
    "error": None,
}


def _acl_cfg(protect):
    cfg = dict(ENABLED_ACL)
    cfg["protect_paths"] = [str(protect)]
    return cfg


@pytest.fixture
def acl_enabled(monkeypatch):
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: ENABLED_ACL)
    yield


@pytest.fixture(autouse=True)
def _clean_session():
    tokens = set_session_vars()
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.fixture
def identity_resolve(monkeypatch):
    # Isolate the guard's containment logic from path-resolution quirks.
    monkeypatch.setattr(ft, "_resolve_path_for_task", lambda p, task_id="default": Path(p))
    yield


def _scope(role="", groups="", platform="api_server"):
    return set_session_vars(platform=platform, user_role=role, user_groups=groups)


def _denied(result: str) -> bool:
    # file_tools use tool_error(msg) (no success field); the ACL reason in the
    # error message is the reliable denial signal.
    obj = json.loads(result)
    return "skills acl" in obj.get("error", "").lower()


# ── file toolset split (backward compatible) ─────────────────────────────────

def test_file_toolset_split_backward_compatible():
    # `attachments` is read-only introspection over the caller's own request
    # grants, so it sits in file_read and must never reach file_write.
    assert set(resolve_toolset("file_read")) == {
        "read_file", "search_files", "local_document_export", "attachments",
    }
    assert set(resolve_toolset("file_write")) == {"write_file", "patch"}
    assert set(resolve_toolset("file")) == {
        "read_file", "search_files", "local_document_export", "attachments",
        "write_file", "patch",
    }


# ── (1) toolset coupling ─────────────────────────────────────────────────────

def test_coupling_reader_loses_terminal_and_write_file(acl_enabled):
    out = minim(["web", "terminal", "file", "skills", "todo"], "user", G_READERS)
    assert "terminal" not in out
    assert "file" not in out and "file_write" not in out
    assert "file_read" in out                 # read-only file kept
    # Reader still loses arbitrary filesystem/terminal writes, but Increment 1
    # exposes the native manager for full CRUD on only the caller-owned root.
    assert "skills_read" in out and "skills_manage" in out
    assert "web" in out and "todo" in out


def test_coupling_no_perm_keeps_only_read_file(acl_enabled):
    out = minim(["web", "terminal", "file", "skills"], "user", "")
    assert "terminal" not in out
    assert "skills_read" not in out and "skills_manage" not in out
    assert "file" not in out and "file_write" not in out
    assert "file_read" in out                 # read-only file cannot mutate skills
    assert "web" in out


def test_coupling_update_manage_keeps_only_file_read_and_loses_terminal(acl_enabled):
    # Update/create can use native skill_manage, but skill ACL grants never imply
    # raw filesystem or shell authority.
    out_editor = minim(["web", "terminal", "file", "skills"], "user", G_EDITORS)
    assert "terminal" not in out_editor
    assert "file_read" in out_editor
    assert "file" not in out_editor and "file_write" not in out_editor
    assert "skills_read" in out_editor and "skills_manage" in out_editor


def test_coupling_delete_only_loses_file_write_and_terminal(acl_enabled):
    # Delete-only users may call skill_manage(delete) through the action gate,
    # but must not get raw file/terminal bypasses that can update arbitrary skill
    # contents or perform shell-level deletes outside the ACL action model.
    out = minim(["web", "terminal", "file", "skills"], "user", G_DELETERS)
    assert "skills_read" in out and "skills_manage" in out
    assert "file_read" in out
    assert "file" not in out and "file_write" not in out
    assert "terminal" not in out


def test_coupling_full_admin_keeps_only_file_read_and_not_terminal(acl_enabled):
    out_admin = minim(["web", "terminal", "file", "skills"], "admin", "")
    assert "terminal" not in out_admin
    assert "file_read" in out_admin
    assert "file" not in out_admin and "file_write" not in out_admin


@pytest.mark.parametrize("requested_file_toolset", ["file", "file_write", "file_read"])
def test_coupling_every_file_toolset_projects_to_read_only(
    acl_enabled, requested_file_toolset
):
    out = minim([requested_file_toolset], "user", G_EDITORS)
    assert out.count("file_read") == 1
    assert "file" not in out and "file_write" not in out


def test_coupling_disabled_unchanged():
    base = ["web", "terminal", "file", "skills"]
    assert minim(base, "user", "") == base


def test_coupling_failsafe_keeps_only_read_file(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    out = minim(["web", "terminal", "file", "skills", "skills_manage"], "admin", "")
    assert "terminal" not in out
    assert "skills" not in out and "skills_manage" not in out
    assert "file" not in out and "file_write" not in out
    assert "file_read" in out                 # fail-safe keeps read-only file
    assert "web" in out


# ── (3) protected-path guard ─────────────────────────────────────────────────

def test_protected_write_denied_for_reader_read_allowed(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups=G_READERS)  # read, NOT manage
    try:
        target = str(protect / "foo" / "SKILL.md")
        assert ft._acl_protected_path_block(target, mode="write") is not None  # write denied
        assert ft._acl_protected_path_block(target, mode="read") is None       # read allowed
    finally:
        clear_session_vars(tokens)


def test_protected_read_denied_for_no_read(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")  # no perms => READ bypass must be denied
    try:
        target = str(protect / "secret" / "SKILL.md")
        assert ft._acl_protected_path_block(target, mode="read") is not None
        assert ft._acl_protected_path_block(target, mode="write") is not None
    finally:
        clear_session_vars(tokens)


def test_protected_raw_write_denied_even_with_create_or_update(
    monkeypatch, identity_resolve, tmp_path
):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    existing = protect / "x" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    cases = [
        ("user", G_EDITORS, existing, "update"),
        ("user", G_CREATORS, protect / "new" / "SKILL.md", "create"),
        ("admin", "", existing, "update"),
    ]
    for role, groups, target, permission in cases:
        tokens = _scope(role=role, groups=groups)
        try:
            denial = ft._acl_protected_path_block(
                str(target), mode="write", task_id="t", permission=permission
            )
            assert denial is not None
            assert "skill_manage" in denial
        finally:
            clear_session_vars(tokens)


def test_protected_write_denied_for_delete_only(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    existing = protect / "x" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups=G_DELETERS)  # read+delete, NOT update
    try:
        assert ft._acl_protected_path_block(str(existing), mode="write", task_id="t", permission="update") is not None
    finally:
        clear_session_vars(tokens)


def test_user_skills_raw_paths_are_ownership_aware(monkeypatch, identity_resolve, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform = tmp_path / "skills"
    alice_skill = tmp_path / "user-skills" / "alice" / "private" / "SKILL.md"
    bob_skill = tmp_path / "user-skills" / "bob" / "private" / "SKILL.md"
    alice_skill.parent.mkdir(parents=True)
    bob_skill.parent.mkdir(parents=True)
    alice_skill.write_text("alice")
    bob_skill.write_text("bob")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(platform))

    tokens = set_session_vars(
        platform="api_server", user_id="alice", user_role="user", user_groups=G_EDITORS
    )
    try:
        # Own personal drafts can be read, but raw writes still cannot bypass the
        # native skill API. Sibling user roots are hidden regardless of read/update
        # grants.
        assert ft._acl_protected_path_block(str(alice_skill), mode="read") is None
        assert ft._acl_protected_path_block(str(alice_skill), mode="write") is not None
        assert ft._acl_protected_path_block(str(bob_skill), mode="read") is not None
        assert ft._acl_protected_path_block(str(bob_skill), mode="write") is not None
        assert ft._acl_protected_path_block(
            str(tmp_path / "user-skills"), mode="read"
        ) is not None
    finally:
        clear_session_vars(tokens)


def test_vision_local_file_ingress_uses_same_ownership_guard(
    monkeypatch, identity_resolve, tmp_path
):
    from tools.vision_tools import _acl_local_vision_path_block

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform = tmp_path / "skills"
    alice_image = tmp_path / "user-skills" / "alice" / "draft" / "assets" / "own.png"
    bob_image = tmp_path / "user-skills" / "bob" / "draft" / "assets" / "private.png"
    alice_image.parent.mkdir(parents=True)
    bob_image.parent.mkdir(parents=True)
    alice_image.write_bytes(b"own")
    bob_image.write_bytes(b"private")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(platform))

    tokens = set_session_vars(
        platform="api_server", user_id="alice", user_role="user", user_groups=G_EDITORS
    )
    try:
        assert _acl_local_vision_path_block(str(alice_image)) is None
        assert _acl_local_vision_path_block(f"file://{bob_image}") is not None
        assert _acl_local_vision_path_block("https://example.test/image.png") is None
    finally:
        clear_session_vars(tokens)


def test_video_local_file_ingress_uses_same_ownership_guard(
    monkeypatch, identity_resolve, tmp_path
):
    import asyncio
    import json

    from tools.vision_tools import video_analyze_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform = tmp_path / "skills"
    bob_video = tmp_path / "user-skills" / "bob" / "draft" / "assets" / "private.mp4"
    bob_video.parent.mkdir(parents=True)
    bob_video.write_bytes(b"private")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(platform))

    tokens = set_session_vars(
        platform="api_server", user_id="alice", user_role="user", user_groups=G_EDITORS
    )
    try:
        result = json.loads(
            asyncio.run(video_analyze_tool(f"file://{bob_video}", "inspect"))
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert "ACL" in result["error"]


def test_writer_socket_and_auth_material_are_hidden_even_from_skill_admin(
    monkeypatch, identity_resolve, tmp_path
):
    platform = tmp_path / "skills"
    socket_path = tmp_path / "writer-control" / "writer.sock"
    secret_path = tmp_path / "secrets" / "writer.key"
    socket_path.parent.mkdir(parents=True)
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text("not-a-real-secret")
    monkeypatch.setenv("HERMES_SKILL_WRITER_SOCKET", str(socket_path))
    monkeypatch.setenv("HERMES_SKILL_WRITER_SECRET_FILE", str(secret_path))
    monkeypatch.setattr(
        skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(platform)
    )
    tokens = _scope(role="admin", groups="")
    try:
        for path in (socket_path, socket_path.parent, secret_path):
            assert ft._acl_protected_path_block(str(path), mode="read") is not None
            assert ft._acl_protected_path_block(str(path), mode="write") is not None
    finally:
        clear_session_vars(tokens)


def test_non_protected_path_allowed(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")
    try:
        other = str(tmp_path / "other" / "doc.txt")
        assert ft._acl_protected_path_block(other, mode="write") is None
        assert ft._acl_protected_path_block(other, mode="read") is None
    finally:
        clear_session_vars(tokens)


def test_raw_write_runtime_gate_denies_same_uid_code_persistence(
    monkeypatch, identity_resolve, tmp_path
):
    """Direct dispatch cannot plant executable/plugin code outside skill roots."""
    protect = tmp_path / "skills"
    monkeypatch.setattr(
        skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect)
    )
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    executable_target = Path("/opt/hermes/.kg-acl-raw-write-regression")
    plugin_target = hermes_home / "plugins" / "persistence.py"
    plugin_target.parent.mkdir(parents=True)
    plugin_target.write_text("SAFE = True\n")
    tokens = _scope(role="user", groups=G_EDITORS)
    try:
        assert not executable_target.exists()
        assert _denied(
            ft.write_file_tool(
                str(executable_target), "raise SystemExit('pwned')\n", task_id="t"
            )
        )
        assert not executable_target.exists()
        assert _denied(
            ft.patch_tool(
                mode="replace",
                path=str(plugin_target),
                old_string="SAFE = True",
                new_string="PWNED = True",
                task_id="t",
            )
        )
        assert plugin_target.read_text() == "SAFE = True\n"
    finally:
        clear_session_vars(tokens)


def test_raw_write_runtime_gate_preserves_local_and_acl_disabled_behavior(
    monkeypatch, identity_resolve, tmp_path
):
    protect = tmp_path / "skills"
    monkeypatch.setattr(
        skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect)
    )
    tokens = _scope(role="admin", groups="", platform="cli")
    try:
        assert ft._acl_raw_file_write_block() is None
    finally:
        clear_session_vars(tokens)

    disabled = dict(_acl_cfg(protect), enabled=False)
    monkeypatch.setattr(
        skill_acl, "load_skill_acl_config", lambda config=None: disabled
    )
    tokens = _scope(role="admin", groups="")
    try:
        assert ft._acl_raw_file_write_block() is None
    finally:
        clear_session_vars(tokens)


def test_raw_write_runtime_gate_fails_closed_on_acl_config_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    tokens = _scope(role="admin", groups="")
    try:
        assert ft._acl_raw_file_write_block() is not None
    finally:
        clear_session_vars(tokens)


def test_traversal_into_protected_still_denied(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")
    try:
        sneaky = str(tmp_path / "other" / ".." / "skills" / "x" / "SKILL.md")
        assert ft._acl_protected_path_block(sneaky, mode="write") is not None
    finally:
        clear_session_vars(tokens)


def test_non_api_server_exempt(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="", platform="cli")
    try:
        assert ft._acl_protected_path_block(str(protect / "x" / "SKILL.md"), mode="write") is None
    finally:
        clear_session_vars(tokens)


def test_acl_disabled_exempt(identity_resolve, tmp_path):
    # No monkeypatch => live config disabled => allow.
    tokens = _scope(role="user", groups="")
    try:
        assert ft._acl_protected_path_block(str(tmp_path / "skills" / "x" / "SKILL.md"), mode="write") is None
    finally:
        clear_session_vars(tokens)


def test_failclosed_on_error(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    tokens = _scope(role="admin", groups="")  # even admin denied when unresolvable
    try:
        assert ft._acl_protected_path_block(str(protect / "x" / "SKILL.md"), mode="write") is not None
    finally:
        clear_session_vars(tokens)


# ── end-to-end wire-in on the actual file tools ──────────────────────────────

def test_write_file_tool_blocks_protected_and_does_not_write(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    # Bypass the unrelated sensitive-system-path guard (macOS tmp lives under
    # /private/var); real /home/hermes/skills is not a sensitive system path, so
    # this isolates the ACL guard as the blocker.
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    tokens = _scope(role="user", groups=G_READERS)  # read only, no manage
    try:
        target = protect / "evil" / "SKILL.md"
        assert _denied(ft.write_file_tool(str(target), "pwned", task_id="t"))
        assert not target.exists()  # the write never happened
    finally:
        clear_session_vars(tokens)


def test_write_file_tool_blocks_delete_only_from_overwriting_existing_skill(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    target = protect / "victim" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("safe")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    tokens = _scope(role="user", groups=G_DELETERS)  # delete, NOT update
    try:
        assert _denied(ft.write_file_tool(str(target), "pwned", task_id="t"))
        assert target.read_text() == "safe"
    finally:
        clear_session_vars(tokens)


def test_write_file_tool_blocks_create_only_from_overwriting_existing_skill(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    target = protect / "victim" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("safe")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    tokens = _scope(role="user", groups=G_CREATORS)  # create, NOT update
    try:
        assert _denied(ft.write_file_tool(str(target), "pwned", task_id="t"))
        assert target.read_text() == "safe"
    finally:
        clear_session_vars(tokens)


def test_write_file_tool_blocks_update_only_from_creating_new_skill_file(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    target = protect / "new" / "SKILL.md"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    tokens = _scope(role="user", groups=G_UPDATERS)  # update, NOT create
    try:
        assert _denied(ft.write_file_tool(str(target), "created", task_id="t"))
        assert not target.exists()
    finally:
        clear_session_vars(tokens)


def test_write_file_tool_denies_creator_for_new_protected_skill_file(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    target = protect / "new" / "SKILL.md"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    tokens = _scope(role="user", groups=G_CREATORS)  # create, NOT update/delete
    try:
        assert _denied(ft.write_file_tool(str(target), "created", task_id="t"))
        assert not target.exists()
    finally:
        clear_session_vars(tokens)


def test_v4a_delete_protected_requires_delete_not_update(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    target = protect / "victim" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("safe")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    patch = f"""*** Begin Patch
*** Delete File: {target}
*** End Patch
"""
    tokens = _scope(role="user", groups=G_EDITORS)  # update, NOT delete
    try:
        assert _denied(ft.patch_tool(mode="patch", patch=patch, task_id="t"))
        assert target.exists() and target.read_text() == "safe"
    finally:
        clear_session_vars(tokens)


def test_v4a_move_protected_requires_delete_and_create(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    src = protect / "old" / "SKILL.md"
    dst = protect / "new" / "SKILL.md"
    src.parent.mkdir(parents=True)
    src.write_text("safe")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    patch = f"""*** Begin Patch
*** Move File: {src} -> {dst}
*** End Patch
"""
    tokens = _scope(role="user", groups=G_EDITORS)  # create/update, NOT delete
    try:
        assert _denied(ft.patch_tool(mode="patch", patch=patch, task_id="t"))
        assert src.exists() and not dst.exists()
    finally:
        clear_session_vars(tokens)


def test_v4a_add_existing_protected_requires_update_not_create(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    target = protect / "victim" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("safe")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    patch = f"""*** Begin Patch
*** Add File: {target}
+pwned
*** End Patch
"""
    tokens = _scope(role="user", groups=G_CREATORS)  # create, NOT update
    try:
        assert _denied(ft.patch_tool(mode="patch", patch=patch, task_id="t"))
        assert target.read_text() == "safe"
    finally:
        clear_session_vars(tokens)


def test_v4a_add_new_protected_denies_raw_create(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    target = protect / "new" / "SKILL.md"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    patch = f"""*** Begin Patch
*** Add File: {target}
+created
*** End Patch
"""
    tokens = _scope(role="user", groups=G_CREATORS)  # create, NOT update/delete
    try:
        assert _denied(ft.patch_tool(mode="patch", patch=patch, task_id="t"))
        assert not target.exists()
    finally:
        clear_session_vars(tokens)


def test_v4a_add_existing_protected_denies_raw_update(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    target = protect / "victim" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("safe")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    patch = f"""*** Begin Patch
*** Add File: {target}
+updated
*** End Patch
"""
    tokens = _scope(role="user", groups=G_UPDATERS)  # update, NOT create/delete
    try:
        assert _denied(ft.patch_tool(mode="patch", patch=patch, task_id="t"))
        assert target.read_text() == "safe"
    finally:
        clear_session_vars(tokens)


def test_v4a_add_new_protected_denies_update_only(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    target = protect / "new" / "SKILL.md"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, task_id="default": None)
    patch = f"""*** Begin Patch
*** Add File: {target}
+created
*** End Patch
"""
    tokens = _scope(role="user", groups=G_UPDATERS)  # update, NOT create/delete
    try:
        assert _denied(ft.patch_tool(mode="patch", patch=patch, task_id="t"))
        assert not target.exists()
    finally:
        clear_session_vars(tokens)


def _mk_search_result(protect, tmp_path):
    """A SearchResult with one PROTECTED skill match and one public match —
    as if search ran from an ancestor root spanning into the protected dir."""
    prot = str(protect / "secret" / "SKILL.md")
    pub = str(tmp_path / "docs" / "readme.md")
    return SearchResult(
        matches=[
            SearchMatch(path=prot, line_number=3, content="TOPSECRET-SKILL-BODY"),
            SearchMatch(path=pub, line_number=1, content="public note"),
        ],
        files=[prot, pub],
        counts={prot: 1, pub: 1},
        total_count=2,
    )


def _mk_user_search_result(home, tmp_path):
    alice = str(home / "user-skills" / "alice" / "private" / "SKILL.md")
    bob = str(home / "user-skills" / "bob" / "private" / "SKILL.md")
    pub = str(tmp_path / "docs" / "readme.md")
    return SearchResult(
        matches=[
            SearchMatch(path=alice, line_number=1, content="ALICE-SECRET"),
            SearchMatch(path=bob, line_number=1, content="BOB-SECRET"),
            SearchMatch(path=pub, line_number=1, content="public note"),
        ],
        files=[alice, bob, pub],
        counts={alice: 1, bob: 1, pub: 1},
        total_count=3,
    )


def test_search_filter_excludes_protected_for_no_read(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")  # ancestor-root search, NO read perm
    try:
        out = ft._acl_filter_search_result(_mk_search_result(protect, tmp_path), "t")
        blob = json.dumps(out.to_dict())
        # protected path, content AND snippet all withheld
        assert "SKILL.md" not in blob
        assert "TOPSECRET" not in blob
        assert "secret" not in blob
        # legitimate non-protected match still returned
        assert "readme.md" in blob and "public note" in blob
        # public-only remains: 1 match + 1 file + 1 count (synthetic populates all)
        assert out.total_count == 3
    finally:
        clear_session_vars(tokens)


def test_search_filter_hides_sibling_user_skills_even_for_reader(
    monkeypatch, identity_resolve, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(platform))
    tokens = set_session_vars(
        platform="api_server", user_id="alice", user_role="user", user_groups=G_READERS
    )
    try:
        out = ft._acl_filter_search_result(_mk_user_search_result(tmp_path, tmp_path), "t")
        assert out is not None
        blob = json.dumps(out.to_dict())
        assert "ALICE-SECRET" in blob
        assert "BOB-SECRET" not in blob
        assert "public note" in blob
        assert out.total_count == 6  # alice + public, each represented 3 ways
    finally:
        clear_session_vars(tokens)


def test_search_filter_keeps_protected_for_reader(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    for role, groups in [("user", G_READERS), ("admin", "")]:
        tokens = _scope(role=role, groups=groups)
        try:
            out = ft._acl_filter_search_result(_mk_search_result(protect, tmp_path), "t")
            assert any("SKILL.md" in m.path for m in out.matches)  # read perm => kept
        finally:
            clear_session_vars(tokens)


def test_search_filter_non_api_server_exempt(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="", platform="cli")  # exempt even with ACL on
    try:
        out = ft._acl_filter_search_result(_mk_search_result(protect, tmp_path), "t")
        assert any("SKILL.md" in m.path for m in out.matches)
    finally:
        clear_session_vars(tokens)


def test_search_filter_disabled_unchanged(identity_resolve, tmp_path):
    # No monkeypatch => live config disabled => unchanged.
    protect = tmp_path / "skills"
    tokens = _scope(role="user", groups="")
    try:
        out = ft._acl_filter_search_result(_mk_search_result(protect, tmp_path), "t")
        assert any("SKILL.md" in m.path for m in out.matches)
    finally:
        clear_session_vars(tokens)


def test_search_filter_failclosed_on_error(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(skill_acl, "load_skill_acl_config", _raise)
    tokens = _scope(role="admin", groups="")  # enabled-state unknown => drop all
    try:
        out = ft._acl_filter_search_result(_mk_search_result(protect, tmp_path), "t")
        assert out.matches == [] and out.files == [] and out.total_count == 0
    finally:
        clear_session_vars(tokens)


def test_search_tool_end_to_end_filters_protected(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    res = _mk_search_result(protect, tmp_path)

    class _FakeOps:
        def search(self, **kw):
            return res

    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": _FakeOps())
    tokens = _scope(role="user", groups="")  # ancestor-root search, no read
    try:
        out = ft.search_tool(pattern="x", path=str(tmp_path), task_id="t-e2e")
        assert "TOPSECRET" not in out and "SKILL.md" not in out  # protected withheld
        assert "readme.md" in out and "public note" in out       # public retained
    finally:
        clear_session_vars(tokens)


def test_read_file_tool_blocks_protected_without_leak(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    (protect / "secret").mkdir(parents=True)
    secret = protect / "secret" / "SKILL.md"
    secret.write_text("TOP-SECRET-SKILL-CONTENT")
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")  # no read
    try:
        res = ft.read_file_tool(str(secret), task_id="t")
        assert _denied(res)
        assert "TOP-SECRET" not in res  # content not leaked
    finally:
        clear_session_vars(tokens)


# ── code-exec MCP bypass closure (opencode_runner protected-path guard) ───────
# opencode_runner runs `opencode` as a subprocess with hermes-container FS access
# and a caller-supplied cwd; it is NOT in the ACL-managed toolset, so without a
# guard an api_server caller could point it at /home/hermes/skills to read/mutate
# skills. Raw code execution is never a trusted native skill transaction, so the
# protected cwd is denied even for a shared-skill Admin. Remote MCP (soc_v2) and
# non-protected cwds stay unaffected.

def test_opencode_runner_protected_cwd_denied_for_ungrouped(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")  # ungrouped, zero perms
    try:
        denial = ft._acl_guard_code_exec_mcp_call(
            "opencode_runner", {"cwd": str(protect)}, task_id="t")
        assert denial is not None
    finally:
        clear_session_vars(tokens)


def test_opencode_runner_protected_cwd_denied_for_editor_without_delete(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    (protect / "x").mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups=G_EDITORS)  # read+create+update, NO delete
    try:
        denial = ft._acl_guard_code_exec_mcp_call(
            "opencode_runner", {"cwd": str(protect / "x")}, task_id="t")
        assert denial is not None  # arbitrary code can delete -> needs delete perm
    finally:
        clear_session_vars(tokens)


def test_opencode_runner_protected_cwd_denied_for_admin_role(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="admin", groups="")  # full perms
    try:
        denial = ft._acl_guard_code_exec_mcp_call(
            "opencode_runner", {"cwd": str(protect)}, task_id="t"
        )
        assert denial is not None
        assert "arbitrary-code" in denial
    finally:
        clear_session_vars(tokens)


def test_opencode_runner_nonprotected_cwd_still_denied_on_api_server(
    monkeypatch, identity_resolve, tmp_path
):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    work = tmp_path / "work"  # legit cwd (e.g. /tmp, /home/hermes/workspace)
    work.mkdir()
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")
    try:
        denial = ft._acl_guard_code_exec_mcp_call(
            "opencode_runner", {"cwd": str(work)}, task_id="t"
        )
        assert denial is not None
        assert "arbitrary-code" in denial
    finally:
        clear_session_vars(tokens)


def test_soc_v2_remote_mcp_not_guarded(monkeypatch, identity_resolve, tmp_path):
    # SOCv2 is a remote MCP in its own container; even a path under protect must
    # NOT be guarded here (it cannot reach a hermes protect_path), so SOCv2 keeps
    # working normally for unprivileged users.
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")
    try:
        assert ft._acl_guard_code_exec_mcp_call(
            "soc_v2", {"path": str(protect)}, task_id="t") is None
    finally:
        clear_session_vars(tokens)


def test_opencode_runner_non_api_server_platform_exempt(monkeypatch, identity_resolve, tmp_path):
    # CLI/cron/chat (non-api_server) are the trusted owner and exempt.
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="", platform="cli")
    try:
        assert ft._acl_guard_code_exec_mcp_call(
            "opencode_runner", {"cwd": str(protect)}, task_id="t") is None
    finally:
        clear_session_vars(tokens)


def test_opencode_runner_missing_and_nondict_args_fail_closed(monkeypatch, identity_resolve, tmp_path):
    protect = tmp_path / "skills"
    protect.mkdir(parents=True)
    monkeypatch.setattr(skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(protect))
    tokens = _scope(role="user", groups="")
    try:
        assert ft._acl_guard_code_exec_mcp_call("opencode_runner", {}, task_id="t") is not None
        assert ft._acl_guard_code_exec_mcp_call("opencode_runner", None, task_id="t") is not None
        assert ft._acl_guard_code_exec_mcp_call(
            "opencode_runner", {"run_id": "abc"}, task_id="t"
        ) is not None
    finally:
        clear_session_vars(tokens)


def test_universal_mcp_dispatch_short_circuits_opencode_before_subprocess(
    monkeypatch, identity_resolve, tmp_path
):
    from tools.mcp_tool import _make_tool_handler

    platform = tmp_path / "skills"
    platform.mkdir()
    monkeypatch.setattr(
        skill_acl, "load_skill_acl_config", lambda config=None: _acl_cfg(platform)
    )
    tokens = _scope(role="admin", groups="")
    try:
        handler = _make_tool_handler("opencode_runner", "opencode_run_start", 30.0)
        result = json.loads(handler({"cwd": str(tmp_path / "workspace")}))
    finally:
        clear_session_vars(tokens)
    assert "arbitrary-code" in result["error"]
