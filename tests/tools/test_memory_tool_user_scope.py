"""Regression tests for the P0 cross-user file-memory leak fix (2026-07-09).

The built-in `memory` file tool writes MEMORY.md/USER.md and that content is
injected into every gateway user's system prompt. On a multi-user platform
(OpenWebUI 8083) a single shared `memories/` dir therefore leaks one user's
notes to all others. `get_memory_dir()` must scope per bound gateway user id
(HERMES_SESSION_USER_ID) while single-tenant contexts (CLI/cron/tests) keep the
flat dir byte-identically.

These tests lock that behaviour in so an image rebuild / refactor cannot
silently regress the P0. See reports/pov-c1-verify-20260709.md.
"""

import os

import pytest

from gateway import session_context
from tools import memory_tool


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and clear any bound session user."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tokens = session_context.set_session_vars(user_id="")
    try:
        yield tmp_path
    finally:
        session_context.clear_session_vars(tokens)


def _scope_to(user_id: str):
    return session_context.set_session_vars(user_id=user_id)


def _add_memory(content: str):
    store = memory_tool.MemoryStore()
    store.load_from_disk()
    res = store.add("memory", content)
    assert res.get("success") is True, res


def _read_memory() -> str:
    store = memory_tool.MemoryStore()
    store.load_from_disk()
    return "\n".join(store.memory_entries)


class TestPerUserMemoryScope:
    def test_no_bound_user_uses_flat_dir(self, hermes_home):
        # Single-tenant (CLI/cron/tests): byte-identical legacy layout.
        assert memory_tool.get_memory_dir() == hermes_home / "memories"

    def test_bound_user_scopes_under_users_subdir(self, hermes_home):
        tokens = _scope_to("user-alice")
        try:
            assert memory_tool.get_memory_dir() == hermes_home / "memories" / "users" / "user-alice"
        finally:
            session_context.clear_session_vars(tokens)

    def test_two_users_get_distinct_dirs(self, hermes_home):
        ta = _scope_to("alice")
        dir_a = memory_tool.get_memory_dir()
        session_context.clear_session_vars(ta)
        tb = _scope_to("bob")
        dir_b = memory_tool.get_memory_dir()
        session_context.clear_session_vars(tb)
        assert dir_a != dir_b
        assert dir_a.name == "alice" and dir_b.name == "bob"

    def test_writes_land_only_in_own_scope_no_cross_leak(self, hermes_home):
        """The actual leak surface: A's note must not appear in B's store."""
        ta = _scope_to("alice")
        try:
            _add_memory("alice-secret-token")
        finally:
            session_context.clear_session_vars(ta)

        tb = _scope_to("bob")
        try:
            b_view = _read_memory()
        finally:
            session_context.clear_session_vars(tb)

        assert "alice-secret-token" not in b_view

        # And the flat shared dir was never written by bound-user traffic.
        flat = hermes_home / "memories"
        flat_files = [p for p in flat.glob("*.md") if p.is_file()]
        assert flat_files == [], f"bound-user traffic wrote the shared dir: {flat_files}"

    def test_same_user_recall_across_sessions(self, hermes_home):
        ta = _scope_to("carol")
        try:
            _add_memory("carol-note-42")
        finally:
            session_context.clear_session_vars(ta)
        # New "session", same user id → same dir → note is recalled.
        ta2 = _scope_to("carol")
        try:
            view = _read_memory()
        finally:
            session_context.clear_session_vars(ta2)
        assert "carol-note-42" in view


class TestUserScopeSanitisation:
    @pytest.mark.parametrize("evil", ["../../etc", "..", ".", "a/b/c", "user/../../x"])
    def test_traversal_and_separators_never_escape(self, hermes_home, evil):
        tokens = _scope_to(evil)
        try:
            d = memory_tool.get_memory_dir()
        finally:
            session_context.clear_session_vars(tokens)
        base = (hermes_home / "memories").resolve()
        # Whatever the input, the resolved dir stays under memories/ and never
        # climbs above it (no bare '..' segment reaches the filesystem).
        assert str(d.resolve()).startswith(str(base) + os.sep) or d.resolve() == base

    def test_pure_punctuation_uid_falls_back_to_flat(self, hermes_home):
        # A uid that sanitises to empty must NOT create a weird dir — flat dir.
        tokens = _scope_to("...")
        try:
            assert memory_tool.get_memory_dir() == hermes_home / "memories"
        finally:
            session_context.clear_session_vars(tokens)
