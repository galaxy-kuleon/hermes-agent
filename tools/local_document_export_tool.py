"""Local document export tool for OpenWebUI/Hermes conversations."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse, request

from tools.registry import registry


SIGNATURE_VERSION = "v1"
DEFAULT_EXPORT_DIR = "/handoff/exports"
DEFAULT_SOFFICE_URL = "http://soffice:2004"
DEFAULT_PUBLIC_BASE_URL = "http://localhost:8642"
DEFAULT_TTL_HOURS = 24
DEFAULT_MAX_CHARS = 500_000
MAX_FILENAME_STEM = 80
MAX_CLEANUP_MANIFESTS = 500
_EXPORT_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "hermes_local_document_export_context", default={}
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BIDI_CONTROLS_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_FILENAME_RE = re.compile(r"[<>:\"/\\|?*`$&;!{}\[\]()]")
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def set_trusted_export_context(context: dict[str, Any]):
    """Set trusted platform/user/chat context for the current execution flow."""
    clean = {
        "platform": str(context.get("platform") or "").strip(),
        "user_id": str(context.get("user_id") or "").strip(),
        "chat_id": str(context.get("chat_id") or "").strip(),
        "session_id": str(context.get("session_id") or "").strip(),
        "gateway_session_key": str(context.get("gateway_session_key") or "").strip(),
    }
    return _EXPORT_CONTEXT.set(clean)


def reset_trusted_export_context(token) -> None:
    _EXPORT_CONTEXT.reset(token)


def get_trusted_export_context() -> dict[str, str]:
    context = dict(_EXPORT_CONTEXT.get() or {})
    if not context.get("user_id"):
        context["user_id"] = os.environ.get("HERMES_SESSION_USER_ID", "").strip()
    if not context.get("chat_id"):
        context["chat_id"] = os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
    if not context.get("platform"):
        context["platform"] = (
            os.environ.get("HERMES_SESSION_PLATFORM")
            or os.environ.get("HERMES_SESSION_SOURCE")
            or ""
        ).strip()
    return context


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _export_root() -> Path:
    return Path(os.environ.get("LOCAL_EXPORT_DIR", DEFAULT_EXPORT_DIR)).resolve()


def _soffice_url() -> str:
    return os.environ.get("LOCAL_EXPORT_SOFFICE_URL", DEFAULT_SOFFICE_URL).rstrip("/")


def _public_base_url() -> str:
    return os.environ.get("LOCAL_EXPORT_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")


def _signing_key() -> str:
    return os.environ.get("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", "")


def owner_hash_for_scope(user_id: str, chat_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}\0{chat_id}".encode("utf-8")).hexdigest()
    return digest[:24]


def sanitize_filename_stem(value: Any) -> str:
    original = str(value or "").strip()
    if not original:
        return "export"
    if ".." in original or "/" in original or "\\" in original:
        return "export"

    cleaned = _BIDI_CONTROLS_RE.sub("", original)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _UNSAFE_FILENAME_RE.sub("-", cleaned)
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip(" ._-")
    if not cleaned:
        return "export"

    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        return "export"
    if cleaned.startswith("."):
        return "export"
    return cleaned[:MAX_FILENAME_STEM].rstrip(" ._-") or "export"


def validate_docx_bytes(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError("DOCX is missing word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX is not a valid ZIP archive") from exc


def validate_pdf_bytes(data: bytes) -> None:
    if not data.startswith(b"%PDF"):
        raise ValueError("PDF does not start with %PDF")
    if len(data) < 256:
        raise ValueError("PDF is too small to be a valid export")


def build_artifact_signature(
    *,
    key: str,
    version: str,
    user_id: str,
    chat_id: str,
    artifact_id: str,
    filename: str,
    sha256: str,
    expires_at: str,
) -> str:
    payload = "\0".join(
        [version, user_id, chat_id, artifact_id, filename, sha256, expires_at]
    ).encode("utf-8")
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _post_markdown_to_docx(markdown: str) -> bytes:
    req = request.Request(
        f"{_soffice_url()}/convert-markdown?to=docx",
        data=markdown.encode("utf-8"),
        headers={"Content-Type": "text/markdown; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Markdown to DOCX conversion failed: HTTP {exc.code}: {body}") from exc


def _post_docx_to_pdf(docx_bytes: bytes) -> bytes:
    boundary = f"----hermes-local-export-{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            'Content-Disposition: form-data; name="file"; filename="source.docx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
        ).encode("utf-8"),
        docx_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req = request.Request(
        f"{_soffice_url()}/convert?to=pdf",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urlerror.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DOCX to PDF conversion failed: HTTP {exc.code}: {body_text}") from exc


def _requested_formats(value: Any) -> list[str]:
    if value in (None, "", []):
        raw = ["docx"]
    elif isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw = value
    else:
        raise ValueError("formats must be a list or comma-separated string")

    formats: list[str] = []
    for item in raw:
        fmt = str(item or "").lower().lstrip(".").strip()
        if fmt not in {"docx", "pdf"}:
            raise ValueError("formats must contain only docx and/or pdf")
        if fmt not in formats:
            formats.append(fmt)
    if not formats:
        raise ValueError("formats must contain docx and/or pdf")
    return formats


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


def _cleanup_expired_exports(root: Path, now: datetime) -> None:
    if not root.exists():
        return
    checked = 0
    for manifest_path in root.glob("*/*/manifest.json"):
        checked += 1
        if checked > MAX_CLEANUP_MANIFESTS:
            break
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expires_at = _parse_utc(str(manifest.get("expires_at") or ""))
        except Exception:
            continue
        if expires_at <= now:
            shutil.rmtree(manifest_path.parent, ignore_errors=True)


def _artifact_url(artifact_id: str, filename: str, expires_at: str, sig: str) -> str:
    quoted_filename = parse.quote(filename, safe="")
    expires_epoch = int(_parse_utc(expires_at).timestamp())
    return (
        f"{_public_base_url()}/v1/artifacts/{artifact_id}/{quoted_filename}"
        f"/download/{expires_epoch}/{sig}"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_source_markdown(content: str, title: str) -> str:
    if not title:
        return content
    clean_title = str(title).strip()
    if not clean_title:
        return content
    return f"# {clean_title}\n\n{content.lstrip()}"


def _json_error(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def local_document_export(args: dict[str, Any] | str, task_id: str | None = None) -> str:
    """Generate local DOCX/PDF artifacts and return signed markdown links."""
    try:
        if isinstance(args, str):
            args = json.loads(args or "{}")
        if not isinstance(args, dict):
            return _json_error("arguments must be a JSON object")

        context = get_trusted_export_context()
        user_id = context.get("user_id", "")
        chat_id = context.get("chat_id", "")
        if not user_id or not chat_id:
            return _json_error("local_document_export requires trusted user/chat context")

        key = _signing_key()
        if not key:
            return _json_error("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY is required")

        content = str(args.get("content_markdown") or "")
        max_chars = _env_int("LOCAL_EXPORT_MAX_CHARS", DEFAULT_MAX_CHARS)
        if not content.strip():
            return _json_error("content_markdown is required")
        if len(content) > max_chars:
            return _json_error(f"content_markdown exceeds LOCAL_EXPORT_MAX_CHARS ({max_chars})")

        formats = _requested_formats(args.get("formats"))
        stem = sanitize_filename_stem(args.get("filename_stem") or args.get("title") or "export")
        markdown = _build_source_markdown(content, str(args.get("title") or ""))

        root = _export_root()
        now = _utc_now()
        _cleanup_expired_exports(root, now)
        ttl_hours = max(1, _env_int("LOCAL_EXPORT_TTL_HOURS", DEFAULT_TTL_HOURS))
        expires_at = _format_utc(now + timedelta(hours=ttl_hours))

        owner_hash = owner_hash_for_scope(user_id, chat_id)
        artifact_id = uuid.uuid4().hex
        artifact_dir = root / owner_hash / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=False)

        debug_keep_source = os.environ.get("LOCAL_EXPORT_DEBUG_KEEP_SOURCE", "").lower() in {
            "1",
            "true",
            "yes",
        }
        source_path = artifact_dir / f"{stem}.source.md"
        if debug_keep_source:
            source_path.write_text(markdown, encoding="utf-8")
        try:
            docx_bytes = _post_markdown_to_docx(markdown)
            validate_docx_bytes(docx_bytes)

            outputs: list[tuple[str, str, bytes]] = []
            if "docx" in formats:
                outputs.append(("docx", f"{stem}.docx", docx_bytes))
            if "pdf" in formats:
                pdf_bytes = _post_docx_to_pdf(docx_bytes)
                validate_pdf_bytes(pdf_bytes)
                outputs.append(("pdf", f"{stem}.pdf", pdf_bytes))

            artifact_entries = []
            markdown_links = []
            for fmt, filename, data in outputs:
                out_path = artifact_dir / filename
                out_path.write_bytes(data)
                sha = _sha256(data)
                sig = build_artifact_signature(
                    key=key,
                    version=SIGNATURE_VERSION,
                    user_id=user_id,
                    chat_id=chat_id,
                    artifact_id=artifact_id,
                    filename=filename,
                    sha256=sha,
                    expires_at=expires_at,
                )
                url = _artifact_url(artifact_id, filename, expires_at, sig)
                artifact_entries.append(
                    {
                        "format": fmt,
                        "filename": filename,
                        "size": len(data),
                        "sha256": sha,
                        "expires_at": expires_at,
                        "signature": sig,
                        "url": url,
                    }
                )
                markdown_links.append(f"[Download {filename}]({url})")

            source_path.unlink(missing_ok=True)
            manifest = {
                "artifact_id": artifact_id,
                "created_at": _format_utc(now),
                "expires_at": expires_at,
                "signature_version": SIGNATURE_VERSION,
                "owner": {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "owner_hash": owner_hash,
                    "platform": context.get("platform", ""),
                    "session_id": context.get("session_id", ""),
                },
                "artifacts": artifact_entries,
            }
            _write_atomic_json(artifact_dir / "manifest.json", manifest)
            return json.dumps(
                {
                    "success": True,
                    "artifact_id": artifact_id,
                    "expires_at": expires_at,
                    "artifacts": [
                        {
                            key_: value
                            for key_, value in entry.items()
                            if key_ != "signature"
                        }
                        for entry in artifact_entries
                    ],
                    "markdown": "\n".join(markdown_links),
                }
            )
        except Exception as exc:
            if not debug_keep_source:
                shutil.rmtree(artifact_dir, ignore_errors=True)
            return _json_error(str(exc))
    except Exception as exc:
        return _json_error(str(exc))


def _find_manifest(root: Path, artifact_id: str) -> Path | None:
    if not _ARTIFACT_ID_RE.match(artifact_id):
        return None
    for manifest_path in root.glob(f"*/{artifact_id}/manifest.json"):
        return manifest_path
    return None


def resolve_local_export_download(
    *,
    artifact_id: str,
    filename: str,
    expires: str,
    sig: str,
) -> dict[str, Any]:
    key = _signing_key()
    if not key:
        return {"ok": False, "status": 503, "error": "artifact signing key is not configured"}
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        return {"ok": False, "status": 404, "error": "artifact not found"}
    if not filename or "/" in filename or "\\" in filename:
        return {"ok": False, "status": 400, "error": "invalid filename"}
    lowered = filename.lower()
    if lowered == "manifest.json" or lowered.endswith(".md"):
        return {"ok": False, "status": 403, "error": "requested file is not downloadable"}

    root = _export_root()
    manifest_path = _find_manifest(root, artifact_id)
    if manifest_path is None:
        return {"ok": False, "status": 404, "error": "artifact not found"}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "status": 404, "error": "artifact manifest is invalid"}

    artifact_dir = manifest_path.parent.resolve()
    root_resolved = root.resolve()
    if artifact_dir != root_resolved and root_resolved not in artifact_dir.parents:
        return {"ok": False, "status": 403, "error": "artifact path is outside export root"}

    artifacts = manifest.get("artifacts") or []
    entry = next((item for item in artifacts if item.get("filename") == filename), None)
    if not entry:
        return {"ok": False, "status": 404, "error": "file not found in manifest"}
    if expires != entry.get("expires_at"):
        return {"ok": False, "status": 403, "error": "artifact expiry mismatch"}

    try:
        expires_at = _parse_utc(expires)
    except Exception:
        return {"ok": False, "status": 400, "error": "invalid expiry"}
    if expires_at <= _utc_now():
        return {"ok": False, "status": 410, "error": "artifact expired"}

    owner = manifest.get("owner") or {}
    expected_sig = build_artifact_signature(
        key=key,
        version=str(manifest.get("signature_version") or SIGNATURE_VERSION),
        user_id=str(owner.get("user_id") or ""),
        chat_id=str(owner.get("chat_id") or ""),
        artifact_id=artifact_id,
        filename=filename,
        sha256=str(entry.get("sha256") or ""),
        expires_at=expires,
    )
    if not hmac.compare_digest(expected_sig, sig or ""):
        return {"ok": False, "status": 403, "error": "invalid artifact signature"}
    if entry.get("signature") and not hmac.compare_digest(str(entry["signature"]), sig or ""):
        return {"ok": False, "status": 403, "error": "manifest signature mismatch"}

    file_path = artifact_dir / filename
    if file_path.is_symlink():
        return {"ok": False, "status": 403, "error": "symlink artifacts are not served"}
    try:
        resolved = file_path.resolve(strict=True)
    except OSError:
        return {"ok": False, "status": 404, "error": "artifact file is missing"}
    if resolved != artifact_dir and artifact_dir not in resolved.parents:
        return {"ok": False, "status": 403, "error": "artifact path escape rejected"}

    data = resolved.read_bytes()
    actual_sha = _sha256(data)
    if not hmac.compare_digest(actual_sha, str(entry.get("sha256") or "")):
        return {"ok": False, "status": 403, "error": "artifact checksum mismatch"}
    if int(entry.get("size") or -1) != len(data):
        return {"ok": False, "status": 403, "error": "artifact size mismatch"}

    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if filename.lower().endswith(".docx"):
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif filename.lower().endswith(".pdf"):
        mime = "application/pdf"
    return {"ok": True, "path": resolved, "mime": mime, "filename": filename}


LOCAL_DOCUMENT_EXPORT_SCHEMA = {
    "name": "local_document_export",
    "description": (
        "Export conversation or revised Markdown content to local DOCX/PDF "
        "artifacts and return signed markdown download links."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content_markdown": {
                "type": "string",
                "description": "Markdown content to export after any requested edits are complete.",
            },
            "formats": {
                "type": "array",
                "items": {"type": "string", "enum": ["docx", "pdf"]},
                "description": "Output formats. Use docx, pdf, or both.",
            },
            "filename_stem": {
                "type": "string",
                "description": "Optional safe base filename without extension.",
            },
            "title": {
                "type": "string",
                "description": "Optional document title prepended as a heading.",
            },
        },
        "required": ["content_markdown"],
        "additionalProperties": False,
    },
}


registry.register(
    name="local_document_export",
    # The export writes only inside the request-owned artifact store and
    # returns signed, expiring links. It is safe for API callers whose raw
    # filesystem surface is projected from ``file`` to ``file_read`` by the
    # shared-skill ACL; unlike write_file/patch it cannot mutate skills.
    toolset="file_read",
    schema=LOCAL_DOCUMENT_EXPORT_SCHEMA,
    handler=lambda args, **kw: local_document_export(args, task_id=kw.get("task_id")),
    description=LOCAL_DOCUMENT_EXPORT_SCHEMA["description"],
    max_result_size_chars=100_000,
)
