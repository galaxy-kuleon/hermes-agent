"""Authenticated client/server boundary for shared skill mutations.

The gateway mounts ``/home/hermes/skills`` read-only.  Native ``skill_manage``
calls cross a Unix-domain socket to an isolated writer that alone has the
read-write platform volume and transaction journal.  The writer independently
re-evaluates group-to-permission policy from gateway-signed stable OpenWebUI
group IDs, applies one transactional mutation, and records a content-free
structured audit event.

This module intentionally exposes no shell command execution and the writer
container has no network, user-skill volume, Docker socket, or credential file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import socketserver
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional

from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_SOCKET_PATH = "/run/hermes-skill-writer/writer.sock"
DEFAULT_SECRET_PATH = "/run/secrets/hermes_skill_writer_key"
DEFAULT_CLIENT_TIMEOUT_SECONDS = 10.0
DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS = 2.0
DEFAULT_SERVER_READ_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_PUBLISH_FILES = 256
DEFAULT_MAX_PUBLISH_BYTES = 6 * 1024 * 1024
DEFAULT_REQUEST_MAX_AGE_SECONDS = 30.0
DEFAULT_REPLAY_CACHE_TTL_SECONDS = 120.0
REPLAY_RETENTION_WINDOW_MULTIPLIER = 2.0
DEFAULT_AUDIT_FILENAME = "shared-skill-audit.jsonl"
ALLOWED_PUBLISH_TOP_LEVEL = frozenset(
    {"SKILL.md", "references", "templates", "scripts", "assets"}
)

SOCKET_PATH_ENV = "HERMES_SKILL_WRITER_SOCKET"
SECRET_PATH_ENV = "HERMES_SKILL_WRITER_SECRET_FILE"
CLIENT_TIMEOUT_ENV = "HERMES_SKILL_WRITER_TIMEOUT_SECONDS"
SERVER_READ_TIMEOUT_ENV = "HERMES_SKILL_WRITER_READ_TIMEOUT_SECONDS"
MAX_REQUEST_BYTES_ENV = "HERMES_SKILL_WRITER_MAX_REQUEST_BYTES"
MAX_PUBLISH_FILES_ENV = "HERMES_SKILL_WRITER_MAX_PUBLISH_FILES"
MAX_PUBLISH_BYTES_ENV = "HERMES_SKILL_WRITER_MAX_PUBLISH_BYTES"
REQUEST_MAX_AGE_ENV = "HERMES_SKILL_WRITER_REQUEST_MAX_AGE_SECONDS"
AUDIT_LOG_PATH_ENV = "HERMES_SKILL_AUDIT_LOG_PATH"

_MUTATION_ACTIONS = frozenset(
    {
        "create",
        "edit",
        "patch",
        "delete",
        "write_file",
        "remove_file",
        "publish",
        "rollback",
    }
)
_UPDATE_ACTIONS = frozenset({"edit", "patch", "write_file", "remove_file"})


class SharedSkillWriterError(RuntimeError):
    """A broker request was rejected or could not complete."""

    def __init__(self, message: str, *, code: str = "writer_error") -> None:
        super().__init__(message)
        self.code = code


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SharedSkillWriterError(
            f"invalid integer setting {name}", code="writer_config_error"
        ) from exc
    if value < minimum:
        raise SharedSkillWriterError(
            f"setting {name} must be at least {minimum}",
            code="writer_config_error",
        )
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.001) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SharedSkillWriterError(
            f"invalid numeric setting {name}", code="writer_config_error"
        ) from exc
    if value < minimum:
        raise SharedSkillWriterError(
            f"setting {name} must be at least {minimum}",
            code="writer_config_error",
        )
    return value


def writer_socket_path() -> Path:
    return Path(os.getenv(SOCKET_PATH_ENV, DEFAULT_SOCKET_PATH))


def writer_secret_path() -> Path:
    return Path(os.getenv(SECRET_PATH_ENV, DEFAULT_SECRET_PATH))


def audit_log_path() -> Path:
    configured = os.getenv(AUDIT_LOG_PATH_ENV)
    if configured:
        return Path(configured)
    from tools.platform_skill_store import default_transactions_dir

    return default_transactions_dir() / DEFAULT_AUDIT_FILENAME


def _load_secret() -> bytes:
    path = writer_secret_path()
    try:
        secret = path.read_bytes().strip()
    except OSError as exc:
        raise SharedSkillWriterError(
            "shared skill writer authentication is unavailable",
            code="writer_auth_unavailable",
        ) from exc
    if len(secret) < 32:
        raise SharedSkillWriterError(
            "shared skill writer authentication is misconfigured",
            code="writer_auth_unavailable",
        )
    return secret


def _canonical_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _signature(payload: Dict[str, Any], secret: bytes) -> str:
    return hmac.new(secret, _canonical_bytes(payload), hashlib.sha256).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _group_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = ()
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _identity_from_session() -> Dict[str, Any]:
    from gateway.session_context import get_session_env

    return {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
        "actor": get_session_env("HERMES_SESSION_USER_ID", ""),
        "role": get_session_env("HERMES_SESSION_USER_ROLE", ""),
        "groups": _group_list(
            get_session_env("HERMES_SESSION_USER_GROUPS", "")
        ),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""),
        "session_id": get_session_env("HERMES_SESSION_ID", ""),
        "session_key": get_session_env("HERMES_SESSION_KEY", ""),
    }


def serialize_skill_tree(skill_dir: Path) -> Dict[str, str]:
    """Serialize one caller-owned skill tree for a publish request.

    Paths are relative POSIX names and values are base64. Symlinks, devices,
    traversal, excessive file counts, and excessive aggregate bytes are denied.
    """

    root = Path(skill_dir)
    if root.is_symlink() or not root.is_dir() or not (root / "SKILL.md").is_file():
        raise SharedSkillWriterError(
            "publish source must be one personal skill directory",
            code="invalid_publish_source",
        )
    max_files = _env_int(MAX_PUBLISH_FILES_ENV, DEFAULT_MAX_PUBLISH_FILES)
    max_bytes = _env_int(MAX_PUBLISH_BYTES_ENV, DEFAULT_MAX_PUBLISH_BYTES)
    encoded: Dict[str, str] = {}
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SharedSkillWriterError(
                "publish source contains a symlink", code="invalid_publish_source"
            )
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise SharedSkillWriterError(
                "publish source contains a non-regular file",
                code="invalid_publish_source",
            )
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        total_bytes += len(data)
        if len(encoded) + 1 > max_files or total_bytes > max_bytes:
            raise SharedSkillWriterError(
                "publish source exceeds the configured transfer limit",
                code="publish_source_too_large",
            )
        encoded[relative] = base64.b64encode(data).decode("ascii")
    return encoded


def request_shared_skill_mutation(
    action: str,
    name: str,
    *,
    arguments: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send one signed native mutation request to the isolated writer."""

    if action not in _MUTATION_ACTIONS:
        raise SharedSkillWriterError(
            f"unsupported shared skill action: {action}", code="unsupported_action"
        )
    identity = _identity_from_session()
    if identity["platform"] != "api_server" or not identity["actor"]:
        raise SharedSkillWriterError(
            "an authenticated OpenWebUI subject is required",
            code="missing_subject",
        )
    payload: Dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "issued_at": time.time(),
        "nonce": secrets.token_hex(16),
        "request_id": secrets.token_hex(16),
        "action": action,
        "name": name,
        "namespace": "platform",
        "identity": identity,
        "arguments": arguments or {},
    }
    envelope = {"payload": payload, "signature": _signature(payload, _load_secret())}
    wire = _canonical_bytes(envelope) + b"\n"
    max_request_bytes = _env_int(MAX_REQUEST_BYTES_ENV, DEFAULT_MAX_REQUEST_BYTES)
    if len(wire) > max_request_bytes:
        raise SharedSkillWriterError(
            "shared skill request exceeds the configured transfer limit",
            code="request_too_large",
        )

    timeout = _env_float(CLIENT_TIMEOUT_ENV, DEFAULT_CLIENT_TIMEOUT_SECONDS)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(writer_socket_path()))
            client.sendall(wire)
            response_wire = _recv_line(client, max_request_bytes)
    except (OSError, TimeoutError) as exc:
        raise SharedSkillWriterError(
            "shared skill writer is unavailable; no platform change was made",
            code="writer_unavailable",
        ) from exc
    try:
        response = json.loads(response_wire)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SharedSkillWriterError(
            "shared skill writer returned an invalid response",
            code="invalid_writer_response",
        ) from exc
    if not isinstance(response, dict):
        raise SharedSkillWriterError(
            "shared skill writer returned an invalid response",
            code="invalid_writer_response",
        )
    return response


def probe_writer_socket() -> bool:
    """Return True only when the writer accepts and answers a protocol frame."""

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(DEFAULT_HEALTHCHECK_TIMEOUT_SECONDS)
            client.connect(str(writer_socket_path()))
            # An intentionally unauthenticated frame exercises accept, parse,
            # response serialization, and the auth fail-closed path without
            # performing or auditing a mutation.
            client.sendall(b"{}\n")
            response = json.loads(
                _recv_line(client, DEFAULT_MAX_REQUEST_BYTES).decode("utf-8")
            )
        return bool(
            isinstance(response, dict)
            and response.get("success") is False
            and response.get("error_code") == "invalid_request"
        )
    except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _recv_line(connection: socket.socket, maximum: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = connection.recv(min(65536, maximum + 1 - received))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunk = chunk[:newline]
            chunks.append(chunk)
            received += len(chunk)
            break
        chunks.append(chunk)
        received += len(chunk)
        if received > maximum:
            raise SharedSkillWriterError(
                "shared skill writer message exceeds limit", code="request_too_large"
            )
    if received > maximum:
        raise SharedSkillWriterError(
            "shared skill writer message exceeds limit", code="request_too_large"
        )
    return b"".join(chunks)


def _find_platform_skill(name: str) -> Optional[Dict[str, Any]]:
    from tools import skill_manager_tool as manager

    return manager._find_skill(name, "platform")


def _safe_relative_path(value: str) -> Path:
    pure = PurePosixPath(str(value).replace("\\", "/"))
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if (
        pure.is_absolute()
        or not parts
        or ".." in parts
        or any(part.startswith(".") for part in parts)
        or parts[0] not in ALLOWED_PUBLISH_TOP_LEVEL
        or (parts[0] == "SKILL.md" and len(parts) != 1)
    ):
        raise SharedSkillWriterError(
            "unsafe published skill path", code="invalid_publish_source"
        )
    return Path(*parts)


def _materialize_publish_tree(files: Dict[str, str], destination: Path) -> None:
    if not isinstance(files, dict) or "SKILL.md" not in files:
        raise SharedSkillWriterError(
            "publish source is missing SKILL.md", code="invalid_publish_source"
        )
    max_files = _env_int(MAX_PUBLISH_FILES_ENV, DEFAULT_MAX_PUBLISH_FILES)
    max_bytes = _env_int(MAX_PUBLISH_BYTES_ENV, DEFAULT_MAX_PUBLISH_BYTES)
    if len(files) > max_files:
        raise SharedSkillWriterError(
            "publish source exceeds the configured file limit",
            code="publish_source_too_large",
        )
    total_bytes = 0
    destination.mkdir(parents=True, exist_ok=False)
    for relative_text, encoded in sorted(files.items()):
        relative = _safe_relative_path(relative_text)
        if not isinstance(encoded, str):
            raise SharedSkillWriterError(
                "publish source contains invalid file data",
                code="invalid_publish_source",
            )
        try:
            data = base64.b64decode(encoded.encode("ascii"), validate=True)
        except Exception as exc:
            raise SharedSkillWriterError(
                "publish source contains invalid file data",
                code="invalid_publish_source",
            ) from exc
        total_bytes += len(data)
        if total_bytes > max_bytes:
            raise SharedSkillWriterError(
                "publish source exceeds the configured byte limit",
                code="publish_source_too_large",
            )
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _publish_tree(
    *, name: str, category: Optional[str], files: Dict[str, str], transaction_id: str
) -> Dict[str, Any]:
    from tools import skill_manager_tool as manager
    from tools.skill_state import platform_skills_dir

    name_error = manager._validate_name(name)
    category_error = manager._validate_category(category)
    if name_error or category_error:
        raise SharedSkillWriterError(
            name_error or category_error or "invalid publish target",
            code="invalid_publish_target",
        )
    root = platform_skills_dir()
    existing = _find_platform_skill(name)
    if existing:
        relative = existing["path"].relative_to(root)
        existing_category = "" if relative.parent == Path(".") else relative.parent.as_posix()
        if category is not None and category != existing_category:
            raise SharedSkillWriterError(
                "target_category must match the existing shared skill location",
                code="publish_category_mismatch",
            )
    else:
        relative = Path(category) / name if category else Path(name)
    target = root / relative
    stage_parent = Path(
        tempfile.mkdtemp(prefix=f".shared-publish-{transaction_id}-", dir=root)
    )
    staged = stage_parent / name
    backup = target.parent / f".shared-publish-backup-{transaction_id}-{name}"
    try:
        _materialize_publish_tree(files, staged)
        content = (staged / "SKILL.md").read_text(encoding="utf-8")
        frontmatter_error = manager._validate_frontmatter(content)
        content_error = manager._validate_content_size(content)
        if frontmatter_error or content_error:
            raise SharedSkillWriterError(
                frontmatter_error or content_error or "invalid SKILL.md",
                code="invalid_publish_source",
            )
        parsed = manager.yaml.safe_load(content.split("---", 2)[1])
        if not isinstance(parsed, dict) or parsed.get("name") != name:
            raise SharedSkillWriterError(
                "published SKILL.md name must match target_name",
                code="publish_name_mismatch",
            )
        scan_error = manager._security_scan_skill(staged)
        if scan_error:
            raise SharedSkillWriterError(scan_error, code="security_scan_denied")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            os.replace(target, backup)
        os.replace(staged, target)
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
        if backup.exists() and not target.exists():
            os.replace(backup, target)
    return {
        "success": True,
        "message": f"Personal skill published to shared skill '{name}'.",
        "namespace": "platform",
        "qualified_name": f"platform:{name}",
        "path": relative.as_posix(),
    }


def _target_hash(name: str) -> Optional[str]:
    from tools.platform_skill_store import skill_tree_hash

    found = _find_platform_skill(name)
    return skill_tree_hash(found["path"]) if found else None


def _platform_destination(found: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return one found platform skill's stable store-relative destination."""

    if not found:
        return None
    from tools.skill_state import platform_skills_dir

    root = platform_skills_dir()
    try:
        return found["path"].relative_to(root).as_posix()
    except (KeyError, TypeError, ValueError) as exc:
        raise SharedSkillWriterError(
            "shared skill resolved outside the platform store",
            code="invalid_platform_target",
        ) from exc


def _audit_event(
    payload: Dict[str, Any], *, result: str, reason_code: str = "", **extra: Any
) -> Dict[str, Any]:
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    event: Dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "timestamp": _utc_now(),
        "request_id": str(payload.get("request_id") or ""),
        "actor": str(identity.get("actor") or ""),
        "role": str(identity.get("role") or ""),
        "groups": _group_list(identity.get("groups")),
        "platform": str(identity.get("platform") or ""),
        "chat_id": str(identity.get("chat_id") or ""),
        "session_id": str(identity.get("session_id") or ""),
        "action": str(payload.get("action") or ""),
        "namespace": "platform",
        "target": str(payload.get("name") or ""),
        "result": result,
        "reason_code": reason_code,
    }
    event.update(extra)
    return event


_audit_lock = threading.Lock()


def _append_audit(event: Dict[str, Any]) -> None:
    path = audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _canonical_bytes(event) + b"\n"
    with _audit_lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


def _safe_append_audit(event: Dict[str, Any]) -> bool:
    """Append the searchable audit index without hiding transaction outcome.

    Committed/rolled-back mutations also have an authoritative transaction
    receipt. If the JSONL index is unavailable, keep the mutation result honest,
    emit a distinct telemetry signal, and mark the response degraded.
    """

    try:
        _append_audit(event)
        return True
    except OSError:
        logger.exception(
            "shared_skill_audit_index_failed request_id=%s action=%s target=%s",
            event.get("request_id"),
            event.get("action"),
            event.get("target"),
        )
        return False


def _required_permission(payload: Dict[str, Any]) -> str:
    action = str(payload.get("action") or "")
    if action == "publish":
        return "update" if _find_platform_skill(str(payload.get("name") or "")) else "create"
    if action == "create":
        return "create"
    if action in _UPDATE_ACTIONS:
        return "update"
    if action in {"delete", "rollback"}:
        return "delete"
    raise SharedSkillWriterError("unsupported action", code="unsupported_action")


def _authorize(payload: Dict[str, Any]) -> tuple[bool, str]:
    from tools.skill_acl import load_skill_acl_config, resolve_skill_permissions

    identity = payload.get("identity")
    if not isinstance(identity, dict):
        return False, "missing_subject"
    if identity.get("platform") != "api_server" or not str(identity.get("actor") or ""):
        return False, "missing_subject"
    cfg = load_skill_acl_config()
    if not cfg.get("enabled") or cfg.get("error"):
        return False, "acl_unavailable"
    required = _required_permission(payload)
    permissions = resolve_skill_permissions(
        str(identity.get("role") or ""), identity.get("groups"), cfg
    )
    return (required in permissions, "" if required in permissions else "acl_denied")


def _execute_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    from tools import platform_skill_store as store
    from tools import skill_manager_tool as manager

    action = str(payload.get("action") or "")
    name = str(payload.get("name") or "")
    arguments = payload.get("arguments")
    if action not in _MUTATION_ACTIONS or not name or not isinstance(arguments, dict):
        raise SharedSkillWriterError("invalid mutation request", code="invalid_request")

    allowed, denial_code = _authorize(payload)
    if not allowed:
        event = _audit_event(payload, result="deny", reason_code=denial_code)
        audit_index_ok = _safe_append_audit(event)
        return {
            "success": False,
            "error": "Hermes shared skill ACL denied this native mutation.",
            "error_code": denial_code,
            "request_id": payload.get("request_id"),
            "audit_index_status": "ok" if audit_index_ok else "degraded",
        }

    found_before = _find_platform_skill(name)
    before_hash = _target_hash(name)
    before_destination = _platform_destination(found_before)
    started = time.monotonic()
    transaction_context: Dict[str, Optional[str]] = {"id": None}
    try:
        if action == "rollback":
            transaction_id = str(arguments.get("transaction_id") or name)
            transaction_context["id"] = transaction_id
            identity = payload["identity"]
            transaction = store.rollback_transaction(
                transaction_id,
                rollback_metadata={
                    "request_id": payload.get("request_id"),
                    "actor": identity.get("actor"),
                    "groups": _group_list(identity.get("groups")),
                    "action": action,
                    "target": name,
                },
                expected_request_target=name,
            )
            result = {
                "success": True,
                "message": f"Shared skill transaction '{transaction_id}' rolled back.",
                "namespace": "platform",
                "transaction_id": transaction_id,
                "after_generation": transaction.get("after_generation"),
            }
            after_hash = _target_hash(name)
        else:
            def mutate(transaction_id: str) -> Dict[str, Any]:
                transaction_context["id"] = transaction_id
                if action == "create":
                    output = manager._create_skill(
                        name,
                        str(arguments.get("content") or ""),
                        arguments.get("category"),
                        "platform",
                    )
                elif action == "edit":
                    output = manager._edit_skill(
                        name, str(arguments.get("content") or ""), "platform"
                    )
                elif action == "patch":
                    output = manager._patch_skill(
                        name,
                        str(arguments.get("old_string") or ""),
                        arguments.get("new_string"),
                        arguments.get("file_path"),
                        bool(arguments.get("replace_all", False)),
                        "platform",
                    )
                elif action == "delete":
                    output = manager._delete_skill(
                        name, arguments.get("absorbed_into"), "platform"
                    )
                elif action == "write_file":
                    output = manager._write_file(
                        name,
                        str(arguments.get("file_path") or ""),
                        arguments.get("file_content"),
                        "platform",
                    )
                elif action == "remove_file":
                    output = manager._remove_file(
                        name, str(arguments.get("file_path") or ""), "platform"
                    )
                elif action == "publish":
                    # Re-authorize while the transaction lock is held. This
                    # closes create-vs-update TOCTOU if the target appears after
                    # the initial request check but before mutation.
                    still_allowed, _reason = _authorize(payload)
                    if not still_allowed:
                        raise SharedSkillWriterError(
                            "shared skill permission changed before commit",
                            code="acl_race_denied",
                        )
                    output = _publish_tree(
                        name=name,
                        category=arguments.get("category"),
                        files=arguments.get("files"),
                        transaction_id=transaction_id,
                    )
                else:  # guarded above
                    raise SharedSkillWriterError(
                        "unsupported action", code="unsupported_action"
                    )
                if not output.get("success"):
                    raise SharedSkillWriterError(
                        str(output.get("error") or "shared skill mutation failed"),
                        code="mutation_rejected",
                    )
                sanitized = dict(output)
                sanitized.pop("_skills_root", None)
                sanitized.pop("_change", None)
                sanitized.pop("file_preview", None)
                sanitized["before_hash"] = before_hash
                sanitized["after_hash"] = _target_hash(name)
                after_destination = _platform_destination(_find_platform_skill(name))
                destination = after_destination or before_destination
                if not destination:
                    raise SharedSkillWriterError(
                        "shared skill destination is unavailable after mutation",
                        code="invalid_platform_target",
                    )
                sanitized["governance_outbox"] = (
                    store.capture_transaction_post_state(
                        transaction_id,
                        destination,
                    )
                )
                return sanitized

            identity = payload["identity"]
            transaction = store.apply_transaction(
                action,
                mutate,
                receipt_metadata={
                    "request_id": payload.get("request_id"),
                    "actor": identity.get("actor"),
                    "groups": _group_list(identity.get("groups")),
                    "action": action,
                    "target": name,
                },
            )
            result = dict(transaction.get("result") or {})
            result.update(
                {
                    "success": True,
                    "transaction_id": transaction.get("transaction_id"),
                    "before_generation": transaction.get("before_generation"),
                    "after_generation": transaction.get("after_generation"),
                }
            )
            after_hash = result.get("after_hash") or _target_hash(name)

        duration_ms = round((time.monotonic() - started) * 1000, 3)
        event = _audit_event(
            payload,
            result="success",
            before_hash=before_hash,
            after_hash=after_hash,
            transaction_id=result.get("transaction_id"),
            duration_ms=duration_ms,
        )
        audit_index_ok = _safe_append_audit(event)
        logger.info("shared_skill_mutation %s", _canonical_bytes(event).decode("utf-8"))
        result["request_id"] = payload.get("request_id")
        result["audit_index_status"] = "ok" if audit_index_ok else "degraded"
        result["audit"] = {
            "actor": event["actor"],
            "groups": event["groups"],
            "action": event["action"],
            "target": event["target"],
            "before_hash": before_hash,
            "after_hash": after_hash,
            "result": "success",
        }
        result.pop("_skills_root", None)
        result.pop("_change", None)
        return result
    except Exception as exc:
        cause: Optional[BaseException] = exc
        code = "mutation_failed"
        while cause is not None:
            if isinstance(cause, SharedSkillWriterError):
                code = cause.code
                break
            cause = cause.__cause__
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        event = _audit_event(
            payload,
            result="error",
            reason_code=code,
            before_hash=before_hash,
            after_hash=_target_hash(name),
            transaction_id=transaction_context["id"],
            duration_ms=duration_ms,
        )
        audit_index_ok = _safe_append_audit(event)
        logger.warning("shared_skill_mutation %s", _canonical_bytes(event).decode("utf-8"))
        return {
            "success": False,
            "error": str(exc),
            "error_code": code,
            "request_id": payload.get("request_id"),
            "transaction_id": transaction_context["id"],
            "audit_index_status": "ok" if audit_index_ok else "degraded",
        }


class _ReplayCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: Dict[str, float] = {}

    def accept(self, nonce: str, now: float, ttl_seconds: float) -> bool:
        with self._lock:
            self._seen = {
                value: timestamp
                for value, timestamp in self._seen.items()
                if now - timestamp <= ttl_seconds
            }
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


_replay_cache = _ReplayCache()


def _verify_envelope(envelope: Any) -> Dict[str, Any]:
    if not isinstance(envelope, dict):
        raise SharedSkillWriterError("invalid request", code="invalid_request")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise SharedSkillWriterError("invalid request", code="invalid_request")
    if payload.get("version") != PROTOCOL_VERSION:
        raise SharedSkillWriterError("unsupported protocol", code="invalid_request")
    expected = _signature(payload, _load_secret())
    if not hmac.compare_digest(signature, expected):
        raise SharedSkillWriterError("authentication failed", code="auth_failed")
    try:
        issued_at = float(payload.get("issued_at"))
    except (TypeError, ValueError) as exc:
        raise SharedSkillWriterError("invalid timestamp", code="invalid_request") from exc
    now = time.time()
    max_age = _env_float(REQUEST_MAX_AGE_ENV, DEFAULT_REQUEST_MAX_AGE_SECONDS)
    if abs(now - issued_at) > max_age:
        raise SharedSkillWriterError("request expired", code="request_expired")
    nonce = str(payload.get("nonce") or "")
    # A request may first arrive at the future edge of the accepted clock-skew
    # window, then remain timestamp-valid until the equally distant past edge.
    # Retain its nonce for that full 2*max_age span, never less than the default.
    replay_ttl = max(
        DEFAULT_REPLAY_CACHE_TTL_SECONDS,
        REPLAY_RETENTION_WINDOW_MULTIPLIER * max_age,
    )
    if not nonce or not _replay_cache.accept(nonce, now, replay_ttl):
        raise SharedSkillWriterError("request replay denied", code="replay_denied")
    return payload


class _WriterRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            maximum = _env_int(MAX_REQUEST_BYTES_ENV, DEFAULT_MAX_REQUEST_BYTES)
            read_timeout = _env_float(
                SERVER_READ_TIMEOUT_ENV, DEFAULT_SERVER_READ_TIMEOUT_SECONDS
            )
            self.request.settimeout(read_timeout)
            raw = _recv_line(self.request, maximum)
            envelope = json.loads(raw)
            payload = _verify_envelope(envelope)
            response = _execute_payload(payload)
        except Exception as exc:
            code = exc.code if isinstance(exc, SharedSkillWriterError) else "invalid_request"
            response = {"success": False, "error": str(exc), "error_code": code}
        self.request.sendall(_canonical_bytes(response) + b"\n")


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_forever() -> None:
    """Run the authenticated isolated writer until the container stops."""

    # Fail startup before opening the socket if authentication is missing.
    _load_secret()
    path = writer_socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_socket():
            path.unlink()
        else:
            raise SharedSkillWriterError(
                "writer socket path is occupied by a non-socket",
                code="writer_config_error",
            )
    old_umask = os.umask(0o117)
    try:
        server = _ThreadingUnixServer(str(path), _WriterRequestHandler)
    finally:
        os.umask(old_umask)
    path.chmod(0o660)
    logger.info("shared_skill_writer_ready socket=%s", path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    logging.basicConfig(level=logging.INFO)
    serve_forever()
