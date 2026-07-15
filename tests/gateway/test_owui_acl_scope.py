"""Focused tests for OpenWebUI role/group ACL identity extraction (issue #9).

Covers ``_sanitize_owui_role``, ``_sanitize_owui_groups`` and the
``_extract_owui_scope`` additions that feed the Hermes skill ACL. Identity is
taken from trusted request headers only.
"""

from gateway.platforms.api_server import (
    _extract_owui_scope,
    _sanitize_owui_groups,
    _sanitize_owui_id,
    _sanitize_owui_role,
)


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def test_sanitize_role_lowercases_and_strips():
    assert _sanitize_owui_role("Admin") == "admin"
    assert _sanitize_owui_role("  USER  ") == "user"
    # internal spaces / punctuation / control chars stripped
    assert _sanitize_owui_role("ad min!@#") == "admin"
    assert _sanitize_owui_role("") == ""


def test_user_id_validation_is_lossless_for_namespace_identity():
    assert _sanitize_owui_id("  u-123  ") == "u-123"
    assert _sanitize_owui_id("alice/bob") == ""
    assert _sanitize_owui_id("alice bob") == ""
    assert _sanitize_owui_id("a" * 65) == ""
    assert _sanitize_owui_id("../alice") == ""


def test_sanitize_groups_parses_dedupes_and_trims():
    assert _sanitize_owui_groups("g1,g2,g1") == "g1,g2"
    assert _sanitize_owui_groups(" g1 , g2 ") == "g1,g2"
    assert _sanitize_owui_groups("") == ""
    # empty / whitespace-only elements dropped
    assert _sanitize_owui_groups("g1,,  ,g2") == "g1,g2"
    # path-traversal style element rejected (sanitize rejects "..")
    assert _sanitize_owui_groups("..,g3") == "g3"


def test_sanitize_groups_caps_count():
    raw = ",".join(f"g{i}" for i in range(200))
    out = _sanitize_owui_groups(raw)
    assert 0 < len(out.split(",")) <= 64


def test_extract_scope_missing_headers_safe_defaults():
    scope = _extract_owui_scope(_FakeRequest({}))
    assert scope["user_id"] == ""
    assert scope["user_role"] == ""
    assert scope["user_groups"] == ""


def test_extract_scope_parses_role_and_groups():
    req = _FakeRequest(
        {
            "X-OpenWebUI-User-Id": "u-123",
            "X-OpenWebUI-User-Role": "Admin",
            "X-OpenWebUI-User-Groups": "grp-a, grp-b, grp-a",
        }
    )
    scope = _extract_owui_scope(req)
    assert scope["user_id"] == "u-123"
    assert scope["user_role"] == "admin"
    assert scope["user_groups"] == "grp-a,grp-b"


def test_extract_scope_malformed_groups_sanitised():
    req = _FakeRequest({"X-OpenWebUI-User-Groups": "ok-1, ../etc, , ok-2"})
    scope = _extract_owui_scope(req)
    assert scope["user_groups"] == "ok-1,ok-2"
