"""Focused tests for ACL session contextvars (issue #9).

Verifies ``set_session_vars`` accepts ``user_role`` / ``user_groups`` while
remaining backward compatible, and that ``clear_session_vars`` resets them.
"""

from gateway.session_context import (
    clear_session_vars,
    get_session_env,
    set_session_vars,
)


def test_set_session_vars_backward_compatible_without_acl_args():
    # Legacy call (no user_role/user_groups) must still work; ACL vars empty.
    tokens = set_session_vars(platform="api_server", user_id="u1")
    try:
        assert get_session_env("HERMES_SESSION_PLATFORM") == "api_server"
        assert get_session_env("HERMES_SESSION_USER_ID") == "u1"
        assert get_session_env("HERMES_SESSION_USER_ROLE") == ""
        assert get_session_env("HERMES_SESSION_USER_GROUPS") == ""
    finally:
        clear_session_vars(tokens)


def test_set_session_vars_with_role_and_groups():
    tokens = set_session_vars(
        platform="api_server",
        user_id="u1",
        user_role="admin",
        user_groups="g1,g2",
    )
    try:
        assert get_session_env("HERMES_SESSION_USER_ROLE") == "admin"
        assert get_session_env("HERMES_SESSION_USER_GROUPS") == "g1,g2"
    finally:
        clear_session_vars(tokens)


def test_clear_session_vars_resets_acl_vars():
    tokens = set_session_vars(user_role="admin", user_groups="g1")
    clear_session_vars(tokens)
    # After explicit clear, values are "" (no os.environ fallback).
    assert get_session_env("HERMES_SESSION_USER_ROLE") == ""
    assert get_session_env("HERMES_SESSION_USER_GROUPS") == ""
