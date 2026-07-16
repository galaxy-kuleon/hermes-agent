"""Integration tests for the authenticated isolated shared-skill writer."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools import shared_skill_writer as writer
from tools import skill_acl
from tools import skill_manager_tool as manager


EDITOR_GROUP = "editor-group"
ADMIN_GROUP = "admin-group"
VALID_SKILL = """---
name: {name}
description: Shared writer integration test.
---

# Test

{body}
"""


@pytest.fixture
def writer_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    platform = home / "skills"
    platform.mkdir(parents=True)
    socket_id = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    socket_path = Path("/tmp") / f"hermes-skill-writer-{socket_id}.sock"
    socket_path.unlink(missing_ok=True)
    secret_path = tmp_path / "writer.key"
    secret_path.write_bytes(b"writer-test-key-32-bytes-minimum-value")
    audit_path = tmp_path / "audit" / "shared.jsonl"

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PLATFORM_SKILL_WRITER", "1")
    monkeypatch.setenv(writer.SOCKET_PATH_ENV, str(socket_path))
    monkeypatch.setenv(writer.SECRET_PATH_ENV, str(secret_path))
    monkeypatch.setenv(writer.AUDIT_LOG_PATH_ENV, str(audit_path))
    monkeypatch.setattr(manager, "SKILLS_DIR", platform)
    normalized_acl = {
        "enabled": True,
        "authority_mode": "groups_only",
        "roles": {},
        "groups": {
            EDITOR_GROUP: {"read", "create", "update"},
            ADMIN_GROUP: {"read", "create", "update", "delete"},
        },
        "protect_paths": [str(platform)],
        "error": None,
    }
    monkeypatch.setattr(
        skill_acl, "load_skill_acl_config", lambda config=None: normalized_acl
    )
    with writer._replay_cache._lock:
        writer._replay_cache._seen.clear()

    server = writer._ThreadingUnixServer(
        str(socket_path), writer._WriterRequestHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert socket_path.is_socket()
    try:
        yield {
            "home": home,
            "platform": platform,
            "socket": socket_path,
            "secret": secret_path,
            "audit": audit_path,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        socket_path.unlink(missing_ok=True)


def _scope(groups: str):
    return set_session_vars(
        platform="api_server",
        user_id="alice",
        user_role="admin",  # ignored by groups_only
        user_groups=groups,
        chat_id="chat-1",
        session_id="session-1",
        session_key="chat-1",
    )


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_editor_create_update_denied_delete_and_structured_audit(writer_runtime):
    platform = writer_runtime["platform"]
    tokens = _scope(EDITOR_GROUP)
    try:
        created = writer.request_shared_skill_mutation(
            "create",
            "shared-one",
            arguments={
                "content": VALID_SKILL.format(name="shared-one", body="version-one")
            },
        )
        updated = writer.request_shared_skill_mutation(
            "edit",
            "shared-one",
            arguments={
                "content": VALID_SKILL.format(name="shared-one", body="version-two")
            },
        )
        denied = writer.request_shared_skill_mutation(
            "delete", "shared-one", arguments={}
        )
    finally:
        clear_session_vars(tokens)

    assert created["success"] is True
    assert updated["success"] is True
    assert denied == {
        "audit_index_status": "ok",
        "error": "Hermes shared skill ACL denied this native mutation.",
        "error_code": "acl_denied",
        "request_id": denied["request_id"],
        "success": False,
    }
    content = (platform / "shared-one" / "SKILL.md").read_text()
    assert "version-two" in content
    assert created["transaction_id"] != updated["transaction_id"]

    events = _events(writer_runtime["audit"])
    assert [event["result"] for event in events] == ["success", "success", "deny"]
    assert all(event["actor"] == "alice" for event in events)
    assert all(event["groups"] == [EDITOR_GROUP] for event in events)
    assert events[0]["before_hash"] is None
    assert events[0]["after_hash"]
    assert events[1]["before_hash"] == events[0]["after_hash"]
    assert events[1]["after_hash"] != events[1]["before_hash"]
    assert "version-one" not in writer_runtime["audit"].read_text()
    assert "version-two" not in writer_runtime["audit"].read_text()

    receipt_path = (
        writer_runtime["home"]
        / "platform-skill-transactions"
        / created["transaction_id"]
        / "receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    assert receipt["request"] == {
        "action": "create",
        "actor": "alice",
        "groups": [EDITOR_GROUP],
        "request_id": created["request_id"],
        "target": "shared-one",
    }
    assert receipt["result"]["before_hash"] is None
    assert receipt["result"]["after_hash"] == created["after_hash"]
    assert "_change" not in receipt["result"]
    assert "version-one" not in receipt_path.read_text()


def test_admin_delete_and_rollback_restore_content(writer_runtime):
    platform = writer_runtime["platform"]
    skill = platform / "restore-me"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        VALID_SKILL.format(name="restore-me", body="must-return")
    )
    tokens = _scope(ADMIN_GROUP)
    try:
        deleted = writer.request_shared_skill_mutation(
            "delete", "restore-me", arguments={}
        )
        assert deleted["success"] is True
        assert not skill.exists()
        mislabeled = writer.request_shared_skill_mutation(
            "rollback",
            "different-skill",
            arguments={"transaction_id": deleted["transaction_id"]},
        )
        assert mislabeled["success"] is False
        assert not skill.exists()
        rolled_back = writer.request_shared_skill_mutation(
            "rollback",
            "restore-me",
            arguments={"transaction_id": deleted["transaction_id"]},
        )
    finally:
        clear_session_vars(tokens)

    assert rolled_back["success"] is True
    assert "must-return" in (skill / "SKILL.md").read_text()
    events = _events(writer_runtime["audit"])
    assert [event["action"] for event in events] == ["delete", "rollback", "rollback"]
    assert [event["result"] for event in events] == ["success", "error", "success"]
    assert events[1]["target"] == "different-skill"
    assert events[2]["target"] == "restore-me"
    assert events[2]["before_hash"] is None
    assert events[2]["after_hash"]
    receipt_path = (
        writer_runtime["home"]
        / "platform-skill-transactions"
        / deleted["transaction_id"]
        / "receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    assert receipt["rollback_request"]["actor"] == "alice"
    assert receipt["rollback_request"]["groups"] == [ADMIN_GROUP]


def test_publish_copies_only_explicit_personal_tree(writer_runtime):
    personal = writer_runtime["home"] / "user-skills" / "alice" / "draft-one"
    (personal / "references").mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        VALID_SKILL.format(name="draft-one", body="published-body")
    )
    (personal / "references" / "proof.md").write_text("published-support")

    tokens = _scope(EDITOR_GROUP)
    try:
        result = writer.request_shared_skill_mutation(
            "publish",
            "draft-one",
            arguments={
                "category": "devops",
                "files": writer.serialize_skill_tree(personal),
                "source_qualified_name": "user:draft-one",
            },
        )
        (personal / "SKILL.md").write_text(
            VALID_SKILL.format(name="draft-one", body="published-body-v2")
        )
        updated = writer.request_shared_skill_mutation(
            "publish",
            "draft-one",
            arguments={
                "category": "devops",
                "files": writer.serialize_skill_tree(personal),
                "source_qualified_name": "user:draft-one",
            },
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert updated["success"] is True
    shared = writer_runtime["platform"] / "devops" / "draft-one"
    assert "published-body-v2" in (shared / "SKILL.md").read_text()
    assert (shared / "references" / "proof.md").read_text() == "published-support"
    events = _events(writer_runtime["audit"])
    assert [event["action"] for event in events] == ["publish", "publish"]
    assert all(event["target"] == "draft-one" for event in events)


def test_writer_rejects_publish_traversal_before_shared_tree_changes(
    writer_runtime,
):
    encoded_skill = base64.b64encode(
        VALID_SKILL.format(name="traversal", body="safe-body").encode()
    ).decode("ascii")
    encoded_escape = base64.b64encode(b"must-not-escape").decode("ascii")
    tokens = _scope(EDITOR_GROUP)
    try:
        result = writer.request_shared_skill_mutation(
            "publish",
            "traversal",
            arguments={
                "files": {
                    "SKILL.md": encoded_skill,
                    "../escape": encoded_escape,
                },
                "source_qualified_name": "user:traversal",
            },
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert result["error_code"] == "invalid_publish_source"
    assert not (writer_runtime["platform"] / "traversal").exists()
    assert not (writer_runtime["platform"].parent / "escape").exists()
    assert not list(writer_runtime["platform"].glob(".shared-publish-*"))


def test_publish_rejects_category_relocation_of_existing_shared_skill(
    writer_runtime,
):
    existing = writer_runtime["platform"] / "original" / "fixed-location"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(
        VALID_SKILL.format(name="fixed-location", body="original")
    )
    encoded_skill = base64.b64encode(
        VALID_SKILL.format(name="fixed-location", body="changed").encode()
    ).decode("ascii")
    tokens = _scope(EDITOR_GROUP)
    try:
        result = writer.request_shared_skill_mutation(
            "publish",
            "fixed-location",
            arguments={
                "category": "different",
                "files": {"SKILL.md": encoded_skill},
                "source_qualified_name": "user:fixed-location",
            },
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert result["error_code"] == "publish_category_mismatch"
    assert "original" in (existing / "SKILL.md").read_text()


def test_publish_serializer_rejects_symlink_source(writer_runtime):
    personal = writer_runtime["home"] / "user-skills" / "alice" / "linked"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        VALID_SKILL.format(name="linked", body="safe")
    )
    outside = writer_runtime["home"] / "outside-secret"
    outside.write_text("must-not-publish")
    (personal / "references").mkdir()
    (personal / "references" / "escape").symlink_to(outside)

    with pytest.raises(writer.SharedSkillWriterError) as raised:
        writer.serialize_skill_tree(personal)

    assert raised.value.code == "invalid_publish_source"


def test_signed_protocol_rejects_tamper_and_replay(writer_runtime):
    payload = {
        "version": writer.PROTOCOL_VERSION,
        "issued_at": time.time(),
        "nonce": "unique-nonce-for-replay-test",
        "request_id": "request-id",
        "action": "create",
        "name": "demo",
        "namespace": "platform",
        "identity": {
            "platform": "api_server",
            "actor": "alice",
            "role": "user",
            "groups": [EDITOR_GROUP],
        },
        "arguments": {"content": "secret-content-not-logged"},
    }
    secret = writer_runtime["secret"].read_bytes().strip()
    envelope = {"payload": payload, "signature": writer._signature(payload, secret)}
    assert writer._verify_envelope(envelope) is payload
    with pytest.raises(writer.SharedSkillWriterError, match="replay"):
        writer._verify_envelope(envelope)

    tampered = dict(payload)
    tampered["nonce"] = "different-nonce"
    tampered["name"] = "tampered"
    with pytest.raises(writer.SharedSkillWriterError, match="authentication"):
        writer._verify_envelope({"payload": tampered, "signature": envelope["signature"]})


def test_replay_retention_covers_the_full_configured_timestamp_window(
    writer_runtime, monkeypatch
):
    monkeypatch.setenv(writer.REQUEST_MAX_AGE_ENV, "300")
    clock = {"now": 1_000.0}
    monkeypatch.setattr(writer.time, "time", lambda: clock["now"])
    payload = {
        "version": writer.PROTOCOL_VERSION,
        # Initially accepted at the far future edge. It remains timestamp-valid
        # for another 600 seconds, so nonce retention must cover that full span.
        "issued_at": 1_300.0,
        "nonce": "wide-window-replay",
        "request_id": "wide-window-request",
        "action": "create",
        "name": "wide-window",
        "namespace": "platform",
        "identity": {
            "platform": "api_server",
            "actor": "alice",
            "role": "user",
            "groups": [EDITOR_GROUP],
        },
        "arguments": {},
    }
    secret = writer_runtime["secret"].read_bytes().strip()
    envelope = {"payload": payload, "signature": writer._signature(payload, secret)}

    assert writer._verify_envelope(envelope) is payload
    clock["now"] = 1_400.0
    with pytest.raises(writer.SharedSkillWriterError, match="replay"):
        writer._verify_envelope(envelope)


def test_client_requires_authenticated_api_server_subject(writer_runtime):
    tokens = set_session_vars(platform="api_server", user_id="")
    try:
        with pytest.raises(writer.SharedSkillWriterError) as raised:
            writer.request_shared_skill_mutation(
                "create", "demo", arguments={"content": "x"}
            )
    finally:
        clear_session_vars(tokens)
    assert raised.value.code == "missing_subject"


def test_health_probe_requires_a_live_responding_writer(writer_runtime):
    assert writer.probe_writer_socket() is True

    missing_socket = writer_runtime["socket"].with_name("missing-writer.sock")
    original_socket = writer.writer_socket_path
    try:
        writer.writer_socket_path = lambda: missing_socket
        assert writer.probe_writer_socket() is False
    finally:
        writer.writer_socket_path = original_socket


def test_idle_client_is_failed_closed_after_bounded_read_timeout(
    writer_runtime, monkeypatch
):
    monkeypatch.setenv(writer.SERVER_READ_TIMEOUT_ENV, "0.05")
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        client.connect(str(writer_runtime["socket"]))
        response = json.loads(
            writer._recv_line(client, writer.DEFAULT_MAX_REQUEST_BYTES).decode("utf-8")
        )

    assert time.monotonic() - started < 1.0
    assert response["success"] is False
    assert response["error_code"] == "invalid_request"


def test_writer_independently_ignores_openwebui_admin_role_without_km_group(
    writer_runtime,
):
    tokens = _scope("")  # helper deliberately sets role=admin
    try:
        result = writer.request_shared_skill_mutation(
            "create",
            "role-admin-denied",
            arguments={
                "content": VALID_SKILL.format(
                    name="role-admin-denied", body="must-not-exist"
                )
            },
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is False
    assert result["error_code"] == "acl_denied"
    assert not (writer_runtime["platform"] / "role-admin-denied").exists()
    event = _events(writer_runtime["audit"])[0]
    assert event["role"] == "admin"
    assert event["groups"] == []
    assert event["result"] == "deny"


def test_committed_receipt_remains_authoritative_when_audit_index_degrades(
    writer_runtime, monkeypatch
):
    monkeypatch.setattr(
        writer,
        "_append_audit",
        lambda _event: (_ for _ in ()).throw(OSError("audit disk unavailable")),
    )
    tokens = _scope(EDITOR_GROUP)
    try:
        result = writer.request_shared_skill_mutation(
            "create",
            "audit-degraded",
            arguments={
                "content": VALID_SKILL.format(
                    name="audit-degraded", body="committed-with-receipt"
                )
            },
        )
    finally:
        clear_session_vars(tokens)

    assert result["success"] is True
    assert result["audit_index_status"] == "degraded"
    assert (writer_runtime["platform"] / "audit-degraded" / "SKILL.md").is_file()
    receipt = (
        writer_runtime["home"]
        / "platform-skill-transactions"
        / result["transaction_id"]
        / "receipt.json"
    )
    assert json.loads(receipt.read_text())["status"] == "committed"
