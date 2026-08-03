"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header; opt-in long-term memory scoping via X-Hermes-Session-Key header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id; X-Hermes-Session-Key supported)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent as an available model
- GET  /v1/capabilities            — machine-readable API capabilities for external UIs
- GET  /api/sessions               — list client-visible Hermes sessions
- POST /api/sessions               — create an empty Hermes session
- GET/PATCH/DELETE /api/sessions/{session_id} — read/update/delete a session
- GET  /api/sessions/{session_id}/messages — read session message history
- POST /api/sessions/{session_id}/fork — branch a session using SessionDB lineage
- POST /api/sessions/{session_id}/chat[/stream] — chat with a persisted session
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}           — retrieve current run status
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- POST /v1/runs/{run_id}/approval — resolve a pending run approval
- POST /v1/runs/{run_id}/stop       — interrupt a running agent
- GET  /health                     — health check
- GET  /health/detailed            — rich status for cross-container dashboard probing

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1 and
authenticating with API_SERVER_KEY.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import hashlib
import html
import hmac
import json
import logging
import os
import socket as _socket
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
)
from tools.file_reader_routing import reader_guidance

logger = logging.getLogger(__name__)


def emit_chat_completion_coverage_suffix(result: dict | None) -> str:
    """Production adapter: coverage text for chat-completions SSE terminal.

    Mutation target (U1D all-exit gate): tests must call this function; replacing
    its body with ``return ""`` must red application-boundary harness.
    """
    from tools.attachment_ledger import terminal_coverage_suffix

    if not isinstance(result, dict):
        return ""
    suffix = terminal_coverage_suffix("", result)
    if not suffix:
        footer = result.get("coverage_footer") or ""
        if footer:
            suffix = footer if str(footer).startswith("\n") else "\n" + str(footer)
    return suffix or ""


def emit_responses_coverage_suffix(streamed_so_far: str, result: dict | None) -> str:
    """Production adapter: coverage text for Responses SSE terminal.

    Mutation target: same as emit_chat_completion_coverage_suffix.
    """
    from tools.attachment_ledger import terminal_coverage_suffix

    if not isinstance(result, dict):
        return ""
    return terminal_coverage_suffix(streamed_so_far or "", result) or ""


def _hermes_version() -> str:
    """Return the hermes-agent version string, or "dev" if it can't be resolved.

    Tries the installed package metadata first (authoritative for a pip/uv
    install), then the in-tree ``hermes_cli.__version__`` (covers editable /
    source checkouts where metadata may be stale or absent). Never raises —
    a version probe must not be able to break the health endpoint.
    """
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        pass
    try:
        from hermes_cli import __version__

        return __version__
    except Exception:
        return "dev"


# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 10_000_000  # 10 MB — accommodates long agent conversations with tool calls
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array
TOOL_PROGRESS_SSE_LINE_MAX_BYTES = 65_536
# Two 2 KiB previews leave headroom for worst-case quote HTML-entity expansion,
# outer JSON escaping, lifecycle metadata, and the completion wrapper.
TOOL_PROGRESS_VALUE_PREVIEW_MAX_BYTES = 2_048
TOOL_PROGRESS_SSE_DATA_PREFIX = b"data: "
HANDOFF_DIR = Path(os.environ.get("SKIP_RAG_HANDOFF_DIR", "/handoff")).resolve()
HANDOFF_SIGNING_KEY = os.environ.get(
    "SKIP_RAG_HANDOFF_SIGNING_KEY",
    os.environ.get("OPENWEBUI_BRIDGE_API_KEY", ""),
)


def _json_safe_tool_progress_value(value: Any) -> tuple[Any, str, bool]:
    """Return a JSON-native value, canonical text, and coercion indicator."""
    if isinstance(value, str):
        return value, value, False
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return json.loads(text), text, False
    except (TypeError, ValueError):
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                default=str,
                allow_nan=False,
                separators=(",", ":"),
            )
            return json.loads(text), text, True
        except Exception:
            # A hostile __str__ must not abort the assistant stream. Type is
            # diagnostic enough; repr(value) may contain document content.
            text = f"<unserializable {type(value).__name__}>"
            return text, text, True
    except Exception:
        # A hostile __str__ must not abort the assistant stream. Type is
        # diagnostic enough; repr(value) may contain document content.
        text = f"<unserializable {type(value).__name__}>"
        return text, text, True


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate text without emitting an invalid partial UTF-8 code point."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _bound_tool_progress_field(field: str, value: Any) -> tuple[Any, Dict[str, Any]]:
    """Build one JSON-safe transport field with explicit truncation metadata."""
    safe_value, original_text, json_coerced = _json_safe_tool_progress_value(value)
    original_bytes = original_text.encode("utf-8")
    metadata: Dict[str, Any] = {}
    if json_coerced:
        metadata.update({
            f"{field}JsonCoerced": True,
            f"{field}OriginalChars": len(original_text),
            f"{field}OriginalUtf8Bytes": len(original_bytes),
        })
    if len(original_bytes) <= TOOL_PROGRESS_VALUE_PREVIEW_MAX_BYTES:
        return safe_value, metadata

    preview = _truncate_utf8_bytes(
        original_text,
        TOOL_PROGRESS_VALUE_PREVIEW_MAX_BYTES,
    )
    metadata.update({
        f"{field}Truncated": True,
        f"{field}OriginalChars": len(original_text),
        f"{field}OriginalUtf8Bytes": len(original_bytes),
    })
    return preview, metadata


def _bounded_tool_progress_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy and bound document-bearing fields without mutating agent values."""
    bounded = dict(payload)
    for field in ("arguments", "result"):
        if field not in bounded:
            continue
        bounded_value, metadata = _bound_tool_progress_field(field, bounded[field])
        bounded[field] = bounded_value
        bounded.update(metadata)
        if metadata:
            logger.info(
                "tool_progress_payload_normalized tool=%s status=%s field=%s "
                "truncated=%s json_coerced=%s original_chars=%d "
                "original_utf8_bytes=%d preview_max_bytes=%d",
                bounded.get("tool", ""),
                bounded.get("status", ""),
                field,
                bool(metadata.get(f"{field}Truncated")),
                bool(metadata.get(f"{field}JsonCoerced")),
                metadata[f"{field}OriginalChars"],
                metadata[f"{field}OriginalUtf8Bytes"],
                TOOL_PROGRESS_VALUE_PREVIEW_MAX_BYTES,
            )
    return bounded


def _minimal_tool_progress_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Retain lifecycle correlation when the normal bounded event is still large."""
    tool = str(payload.get("tool", ""))
    tool_bytes = tool.encode("utf-8")
    bounded_tool = _truncate_utf8_bytes(
        tool,
        TOOL_PROGRESS_VALUE_PREVIEW_MAX_BYTES,
    )
    fallback: Dict[str, Any] = {
        "tool": bounded_tool,
        "toolCallId": str(payload.get("toolCallId", "")),
        "status": str(payload.get("status", "")),
        "transportTruncated": True,
    }
    if (
        payload.get("toolTruncated")
        or len(tool_bytes) > TOOL_PROGRESS_VALUE_PREVIEW_MAX_BYTES
    ):
        fallback.update({
            "toolTruncated": True,
            "toolOriginalChars": payload.get("toolOriginalChars", len(tool)),
            "toolOriginalUtf8Bytes": payload.get(
                "toolOriginalUtf8Bytes",
                len(tool_bytes),
            ),
        })
    for field in ("arguments", "result"):
        if field not in payload:
            continue
        _, bounded_text, _ = _json_safe_tool_progress_value(payload[field])
        fallback[f"{field}Truncated"] = True
        fallback[f"{field}OriginalChars"] = payload.get(
            f"{field}OriginalChars",
            len(bounded_text),
        )
        fallback[f"{field}OriginalUtf8Bytes"] = payload.get(
            f"{field}OriginalUtf8Bytes",
            len(bounded_text.encode("utf-8")),
        )
        if payload.get(f"{field}JsonCoerced"):
            fallback[f"{field}JsonCoerced"] = True
    return fallback


def _serialize_tool_progress_payload(
    payload: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    """Serialize and return the exact bounded payload written to the wire."""
    event_data = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    )
    line_bytes = len(TOOL_PROGRESS_SSE_DATA_PREFIX) + len(event_data.encode("utf-8"))
    if line_bytes <= TOOL_PROGRESS_SSE_LINE_MAX_BYTES:
        return event_data, payload

    fallback = _minimal_tool_progress_payload(payload)
    fallback_data = json.dumps(
        fallback,
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    )
    fallback_line_bytes = (
        len(TOOL_PROGRESS_SSE_DATA_PREFIX)
        + len(fallback_data.encode("utf-8"))
    )
    if fallback_line_bytes > TOOL_PROGRESS_SSE_LINE_MAX_BYTES:
        # Provider tool-call IDs are short correlation tokens. Refuse an
        # impossible oversized identity rather than writing an invalid line.
        raise ValueError("minimal tool-progress lifecycle payload exceeds SSE limit")

    logger.warning(
        "tool_progress_event_fallback tool=%s status=%s "
        "serialized_bytes=%d limit_bytes=%d",
        "<truncated>" if fallback.get("toolTruncated") else fallback["tool"],
        fallback["status"],
        line_bytes,
        TOOL_PROGRESS_SSE_LINE_MAX_BYTES,
    )
    return fallback_data, fallback


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """Parse a listen port without letting malformed env/config values crash startup."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like API payload values.

    External clients should send real JSON booleans, but some OpenAI-compatible
    frontends and middleware serialize flags like ``stream`` as strings.  Using
    Python truthiness on those values misroutes requests because ``"false"`` is
    still truthy.  Treat only explicit bool-ish scalars as booleans; everything
    else falls back to the caller's default.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """Normalize OpenAI chat message content into a plain text string.

    Some clients (Open WebUI, LobeChat, etc.) send content as an array of
    typed parts instead of a plain string::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    This function flattens those into a single string so the agent pipeline
    (which expects strings) doesn't choke.

    Defensive limits prevent abuse: recursion depth, list size, and output
    length are all bounded.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        total_len = 0
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    part = item[:MAX_NORMALIZED_TEXT_LENGTH]
                    parts.append(part)
                    total_len += len(part)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            part = str(text)[:MAX_NORMALIZED_TEXT_LENGTH]
                            parts.append(part)
                            total_len += len(part)
                        except Exception:
                            pass
                # Silently skip image_url / other non-text parts
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
                    total_len += len(nested)
            # Check accumulated size
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # Fallback for unexpected types (int, float, bool, etc.)
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


_FILES_BLOCK_RE = re.compile(r"<files>\s*(?P<body>.*?)\s*</files>", re.DOTALL)
_FILE_TAG_RE = re.compile(r"<file\b(?P<attrs>[^>]*)/?>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Path B basenames are "<3-digit ordinal>-<8-hex nonce>-<sanitised name>".
_HANDOFF_NAME_PREFIX_RE = re.compile(r"^\d{3}-[0-9a-f]{8}-")


def _sign_handoff_entry(
    user_id: str,
    chat_id: str,
    original_path: str,
    file_id: str = "",
    sha256: str = "",
) -> str:
    """Return the expected OWUI handoff HMAC for one file entry."""
    if not HANDOFF_SIGNING_KEY:
        return ""
    parts = [user_id, chat_id, original_path]
    if file_id or sha256:
        parts.extend([file_id, sha256])
    payload = "\0".join(parts).encode("utf-8")
    return hmac.new(HANDOFF_SIGNING_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _sign_legacy_handoff_entry(user_id: str, chat_id: str, original_path: str, markdown_path: str) -> str:
    """Return the legacy original+markdown handoff HMAC for backward compatibility."""
    if not HANDOFF_SIGNING_KEY:
        return ""
    payload = "\0".join([user_id, chat_id, original_path, markdown_path]).encode("utf-8")
    return hmac.new(HANDOFF_SIGNING_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _safe_handoff_path(path_text: str, scope: Dict[str, str]) -> Optional[Path]:
    """Return a resolved caller-scoped /handoff path, or None if invalid."""
    if not path_text:
        return None
    try:
        candidate = Path(html.unescape(path_text)).resolve()
    except (OSError, RuntimeError):
        return None
    user_id = scope.get("user_id", "")
    chat_id = scope.get("chat_id", "") or "nochat"
    allowed_root = (HANDOFF_DIR / "user" / user_id / "chat" / chat_id).resolve()
    if candidate == allowed_root or allowed_root in candidate.parents:
        return candidate
    logger.warning(
        "Rejecting skip-rag handoff path outside scoped root %s: %s",
        allowed_root,
        candidate,
    )
    return None


def _display_handoff_name(path: Path) -> str:
    """Return the human filename from a handoff basename.

    Path B writes files as ``<ordinal>-<nonce>-<sanitised name>`` to keep
    same-named uploads distinct on disk. Only the trailing part means anything
    to a reader, and showing the nonce invites the model to treat it as part of
    an address it should reconstruct.
    """
    return _HANDOFF_NAME_PREFIX_RE.sub("", path.name, count=1) or path.name


def _parse_handoff_file_entries(text: str) -> List[Dict[str, str]]:
    """Parse OWUI skip-rag <files><file .../></files> entries from a message."""
    entries: List[Dict[str, str]] = []
    for block in _FILES_BLOCK_RE.finditer(text or ""):
        for tag in _FILE_TAG_RE.finditer(block.group("body")):
            attrs = {
                key: html.unescape(value)
                for key, value in _ATTR_RE.findall(tag.group("attrs"))
            }
            name = attrs.get("name", "file")
            original = attrs.get("original", "")
            markdown = attrs.get("markdown", "")  # legacy only; never hydrated
            if original:
                entries.append(
                    {
                        "name": name,
                        "original": original,
                        "markdown": markdown,
                        "file_id": attrs.get("file_id", ""),
                        "sha256": attrs.get("sha256", ""),
                        "user": attrs.get("user", ""),
                        "chat": attrs.get("chat", ""),
                        "sig": attrs.get("sig", ""),
                    }
                )
    return entries


def _verify_handoff_entry(entry: Dict[str, str], scope: Dict[str, str]) -> bool:
    """Verify user/chat scope and OWUI HMAC before accepting a handoff entry."""
    user_id = scope.get("user_id", "")
    chat_id = scope.get("chat_id", "") or "nochat"
    if not user_id:
        logger.warning("Rejecting skip-rag handoff: missing OpenWebUI user scope")
        return False
    # New-format OWUI entries may omit user/chat attrs. In that case the HMAC
    # below binds the entry to the trusted request user_id/chat_id scope.
    entry_user = entry.get("user", "")
    entry_chat = entry.get("chat", "")
    if entry_user and entry_user != user_id:
        logger.warning(
            "Rejecting skip-rag handoff: entry user=%r does not match request user=%r",
            entry_user,
            user_id,
        )
        return False
    if entry_chat and (entry_chat or "nochat") != chat_id:
        logger.warning(
            "Rejecting skip-rag handoff: entry chat=%r does not match request chat=%r",
            entry_chat,
            chat_id,
        )
        return False
    if not HANDOFF_SIGNING_KEY:
        logger.warning("Rejecting skip-rag handoff: SKIP_RAG_HANDOFF_SIGNING_KEY is not configured")
        return False
    file_id = entry.get("file_id", "")
    sha256 = entry.get("sha256", "")
    has_identity_metadata = bool(file_id or sha256)
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        logger.warning("Rejecting skip-rag handoff: invalid sha256 metadata for user=%s chat=%s", user_id, chat_id)
        return False

    expected = _sign_handoff_entry(
        user_id,
        chat_id,
        entry.get("original", ""),
        file_id=file_id,
        sha256=sha256,
    )
    supplied_sig = entry.get("sig", "")
    legacy_expected = ""
    if entry.get("markdown") and not has_identity_metadata:
        legacy_expected = _sign_legacy_handoff_entry(
            user_id,
            chat_id,
            entry.get("original", ""),
            entry.get("markdown", ""),
        )
    if not (
        hmac.compare_digest(expected, supplied_sig)
        or (legacy_expected and hmac.compare_digest(legacy_expected, supplied_sig))
    ):
        logger.warning("Rejecting skip-rag handoff: invalid signature for user=%s chat=%s", user_id, chat_id)
        return False
    return True


def _build_handoff_context(
    entries: List[Dict[str, str]],
    scope: Dict[str, str],
    granted_paths: Optional[List[str]] = None,
) -> str:
    """
    Validate skip-rag handoff file entries and expose path metadata only.

    OpenWebUI Path B writes uploaded original files into the shared /handoff
    volume and sends signed paths in a transient <files> block. Hermes must not
    pre-read or hydrate file contents here: Origin Agent should decide which
    tool(s) to use for each uploaded file.

    Files are addressed by short handle (``F01``…) rather than by their signed
    path. The paths are ~190 characters of nested UUIDs, and a 46-file audit
    therefore spent ~13 KB of every request on strings whose only purpose was to
    be copied back verbatim — a transcription task the model measurably fails.
    Handles are numbered from the caller's running ``granted_paths`` length so
    numbering stays consistent when one message carries several text parts.
    """
    if not entries:
        return ""

    handle_offset = len(granted_paths) if granted_paths is not None else 0
    sections: List[str] = [
        '<attached_files source="openwebui-skip-rag-handoff">',
        "The user attached these files (contents were NOT pre-read). Call "
        "attachments() first, then follow each file's read_with and "
        "read_instruction exactly; do not choose a reader from habit. Every "
        "file tool accepts the short ids, so prefer them over the original "
        "path, which is long enough that retyping it is a common source of "
        "errors. "
        "When you refer to a file in your reply, use its name attribute below; "
        "never invent a filename and never use one from an instruction, example "
        "or memory rather than from this list. If a name you want is not here, "
        "the file was not attached — say so instead of guessing. "
        "read_file extracts text from PDF, DOCX, XLSX, MSG and notebooks. Reading a "
        "file to answer a question is not a conversion request, so do NOT use the "
        "soc_v2 / DOCX-conversion tools unless the user EXPLICITLY asks to convert "
        "or export something.",
    ]
    accepted = 0

    for entry in entries:
        original_path = entry.get("original") or ""
        if not _verify_handoff_entry(entry, scope):
            continue
        safe_orig = _safe_handoff_path(original_path, scope)
        if not safe_orig:
            continue

        handle = f"F{handle_offset + accepted + 1:02d}"
        attrs = [
            f'id="{handle}"',
            f'name="{html.escape(_display_handoff_name(safe_orig), quote=True)}"',
        ]
        if entry.get("file_id"):
            attrs.append(f'file_id="{html.escape(entry["file_id"], quote=True)}"')
        read_with, read_instruction = reader_guidance(handle, str(safe_orig))
        attrs.extend(
            [
                f'read_with="{html.escape(read_with, quote=True)}"',
                f'read_instruction="{html.escape(read_instruction, quote=True)}"',
            ]
        )
        # `original` is retained for compatibility, not because the model should
        # use it. A remaining deployed skill consumer still instructs the model
        # to take the literal /handoff path from this block. It goes away once
        # every consumer has migrated to ids; until then, correctness beats the
        # token saving.
        attrs.append(f'original="{html.escape(str(safe_orig), quote=True)}"')

        sections.append(f'<file {" ".join(attrs)}/>')
        if granted_paths is not None:
            granted_paths.append(str(safe_orig))
        accepted += 1

    if accepted == 0:
        return ""
    sections.append("</attached_files>")
    return "\n".join(sections)


def _augment_handoff_text(
    text: str,
    scope: Dict[str, str],
    granted_paths: Optional[List[str]] = None,
) -> str:
    """Strip raw <files> blocks and append validated path-only metadata."""
    if "<files>" not in text:
        return text
    scope = scope or {}
    entries = _parse_handoff_file_entries(text)
    context = _build_handoff_context(entries, scope, granted_paths)
    message_without_raw_files = _FILES_BLOCK_RE.sub("", text).rstrip()
    if not context:
        return message_without_raw_files
    logger.info("Accepted %d skip-rag handoff file path(s) from %s", len(entries), HANDOFF_DIR)
    return f"{message_without_raw_files}\n\n{context}"


def _augment_message_with_handoff_context(
    user_message: Any,
    scope: Optional[Dict[str, str]] = None,
    granted_paths: Optional[List[str]] = None,
) -> Any:
    """Replace raw signed /handoff blocks with validated path-only metadata."""
    scope = scope or {}
    if isinstance(user_message, str):
        return _augment_handoff_text(user_message, scope, granted_paths)
    if isinstance(user_message, list):
        augmented_parts: List[Any] = []
        for part in user_message:
            if isinstance(part, dict):
                part_type = str(part.get("type") or "").strip().lower()
                text = part.get("text")
                if part_type in _TEXT_PART_TYPES and isinstance(text, str):
                    new_part = dict(part)
                    new_part["text"] = _augment_handoff_text(
                        text,
                        scope,
                        granted_paths,
                    )
                    augmented_parts.append(new_part)
                    continue
            augmented_parts.append(part)
        return augmented_parts
    return user_message


# Content part type aliases used by the OpenAI Chat Completions and Responses
# APIs.  We accept both spellings on input and emit a single canonical internal
# shape (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``) that the
# rest of the agent pipeline already understands.
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_multimodal_content(content: Any) -> Any:
    """Validate and normalize multimodal content for the API server.

    Returns a plain string when the content is text-only, or a list of
    ``{"type": "text"|"image_url", ...}`` parts when images are present.
    The output shape is the native OpenAI Chat Completions vision format,
    which the agent pipeline accepts verbatim (OpenAI-wire providers) or
    converts (``_preprocess_anthropic_content`` for Anthropic).

    Raises ``ValueError`` with an OpenAI-style code on invalid input:
      * ``unsupported_content_type`` — file/input_file/file_id parts, or
        non-image ``data:`` URLs.
      * ``invalid_image_url`` — missing URL or unsupported scheme.
      * ``invalid_content_part`` — malformed text/image objects.

    Callers translate the ValueError into a 400 response.
    """
    # Scalar passthrough mirrors ``_normalize_chat_content``.
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content
    if not isinstance(content, list):
        # Mirror the legacy text-normalizer's fallback so callers that
        # pre-existed image support still get a string back.
        return _normalize_chat_content(content)

    items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
    normalized_parts: List[Dict[str, Any]] = []
    text_accum_len = 0

    for part in items:
        if isinstance(part, str):
            if part:
                trimmed = part[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if not isinstance(part, dict):
            # Ignore unknown scalars for forward compatibility with future
            # Responses API additions (e.g. ``refusal``).  The same policy
            # the text normalizer applies.
            continue

        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()

        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is None:
                continue
            if not isinstance(text, str):
                text = str(text)
            if text:
                trimmed = text[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if part_type in _IMAGE_PART_TYPES:
            detail = part.get("detail")
            image_ref = part.get("image_url")
            # OpenAI Responses sends ``input_image`` with a top-level
            # ``image_url`` string; Chat Completions sends ``image_url`` as
            # ``{"url": "...", "detail": "..."}``.  Support both.
            if isinstance(image_ref, dict):
                url_value = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url_value = image_ref
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
            url_value = url_value.strip()
            lowered = url_value.lower()
            if lowered.startswith("data:"):
                if not lowered.startswith("data:image/") or "," not in url_value:
                    raise ValueError(
                        "unsupported_content_type:Only image data URLs are supported. "
                        "Non-image data payloads are not supported."
                    )
            elif not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError(
                    "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs."
                )
            image_part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
            if detail is not None:
                if not isinstance(detail, str) or not detail.strip():
                    raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
                image_part["image_url"]["detail"] = detail.strip()
            normalized_parts.append(image_part)
            continue

        if part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint."
            )

        # Unknown part type — reject explicitly so clients get a clear error
        # instead of a silently dropped turn.
        raise ValueError(
            f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
            "Only text and image_url/input_image parts are supported."
        )

    if not normalized_parts:
        return ""

    # Text-only: collapse to a plain string so downstream logging/trajectory
    # code sees the native shape and prompt caching on text-only turns is
    # unaffected.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))

    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """True when content has any text or image attachment.  Used to reject empty turns."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip():
                    return True
                if ptype in _IMAGE_PART_TYPES:
                    return True
    return False


def _multimodal_validation_error(exc: ValueError, *, param: str) -> "web.Response":
    """Translate a ``_normalize_multimodal_content`` ValueError into a 400 response."""
    raw = str(exc)
    code, _, message = raw.partition(":")
    if not message:
        code, message = "invalid_content_part", raw
    return web.json_response(
        _openai_error(message, code=code, param=param),
        status=400,
    )


def _session_chat_user_message(body: Dict[str, Any], *, param: str = "message") -> tuple[Any, Optional["web.Response"]]:
    """Parse and normalize session chat ``message`` / ``input`` like chat completions."""
    user_message = body.get("message") or body.get("input")
    if not _content_has_visible_payload(user_message):
        return None, web.json_response(
            _openai_error("Missing 'message' field", code="missing_message"),
            status=400,
        )
    try:
        return _normalize_multimodal_content(user_message), None
    except ValueError as exc:
        return None, _multimodal_validation_error(exc, param=param)


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        self._db_path: Optional[str] = db_path if db_path != ":memory:" else None
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._db_path = None
        # Use shared WAL-fallback helper so response_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same filesystem
        # issue addressed for state.db/kanban.db — see
        # hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="response_store.db")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                user_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                response_id TEXT NOT NULL,
                PRIMARY KEY (user_id, name)
            )"""
        )
        # Forward-compat for DBs created before user_id was part of the
        # conversations PK.  Older rows are keyed by name alone; bring the
        # column in with a DEFAULT '' so existing pointers stay reachable
        # only when the caller passes user_id="" (which fail-closed prevents
        # for OpenWebUI-driven traffic).  PRAGMA table_info is cheap.
        try:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(conversations)").fetchall()}
            if "user_id" not in cols:
                self._conn.execute("ALTER TABLE conversations ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
                # SQLite cannot redefine a PK in-place; the legacy PK stays
                # on `name` for the migrated rows.  New rows go through the
                # composite logic in get/set_conversation, so writes won't
                # collide cross-user even when the schema isn't fully
                # rebuilt.  Logging once at init for visibility.
                logger.info("[api_server] Migrated ResponseStore.conversations to include user_id column")
        except Exception as e:
            logger.warning("[api_server] conversations migration skipped: %s", e)
        self._conn.commit()
        # response_store.db contains conversation history (tool payloads,
        # prompts, results). Tighten to owner-only after creation so other
        # local users on a shared box can't read it. Run once at __init__
        # rather than after every commit — chmod-on-every-write is wasted
        # syscalls on a hot path.
        self._tighten_file_permissions()

    def _tighten_file_permissions(self) -> None:
        """Force owner-only permissions on the DB and SQLite sidecars."""
        if not self._db_path:
            return
        for candidate in (
            Path(self._db_path),
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
        ):
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                logger.debug(
                    "Failed to restrict response store permissions for %s",
                    candidate,
                    exc_info=True,
                )

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupted JSON in response store for id=%s, evicting entry",
                response_id,
            )
            self._conn.execute(
                "DELETE FROM responses WHERE response_id = ?",
                (response_id,),
            )
            self._conn.commit()
            return None

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            # Collect IDs that will be evicted
            evict_ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?",
                    (count - self._max_size,),
                ).fetchall()
            ]
            if evict_ids:
                placeholders = ",".join("?" for _ in evict_ids)
                # Clear conversation mappings pointing to evicted responses
                self._conn.execute(
                    f"DELETE FROM conversations WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
                # Delete evicted responses
                self._conn.execute(
                    f"DELETE FROM responses WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        # Clear conversation mappings pointing to this response
        self._conn.execute(
            "DELETE FROM conversations WHERE response_id = ?", (response_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str, user_id: str = "") -> Optional[str]:
        """Get the latest response_id for (user_id, conversation name).

        user_id defaults to "" only for backwards compatibility with callers
        that have not been updated.  In multi-user deployments the OpenWebUI
        path always supplies a non-empty user_id so two users can use the
        same `conversation` name without overwriting each other's pointer.
        """
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str, user_id: str = "") -> None:
        """Map (user_id, conversation name) to its latest response_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (user_id, name, response_id) VALUES (?, ?, ?)",
            (user_id, name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in {"POST", "PUT", "PATCH"}:
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        return await handler(request)
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics."""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._inflight: Dict[tuple[str, str], "asyncio.Task[Any]"] = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]

        inflight_key = (key, fingerprint)
        task = self._inflight.get(inflight_key)
        if task is None:
            async def _compute_and_store():
                resp = await compute_coro()
                import time as _t
                self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
                self._purge()
                return resp

            task = asyncio.create_task(_compute_and_store())
            self._inflight[inflight_key] = task

            def _clear_inflight(done_task: "asyncio.Task[Any]") -> None:
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)

            task.add_done_callback(_clear_inflight)

        return await asyncio.shield(task)


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


# Allowed characters in OpenWebUI identity headers.  Conservative: alnum,
# underscore, hyphen, dot.  Dots are allowed for email-style IDs but the
# helper below rejects any value that contains `..` to block path-traversal
# style misuse if the value is ever appended to a filesystem path or URL.
_OWUI_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,63}$")
_OWUI_ID_MAX = 64


def _sanitize_owui_id(raw: str) -> str:
    """Sanitise an OpenWebUI-supplied ID.  Returns ``""`` on suspicious input.

    Validation is lossless: malformed or overlong values are rejected instead
    of being rewritten onto another user's filesystem namespace. Values that
    contain ``..`` are also rejected as potential path traversal.
    """
    if not raw:
        return ""
    cleaned = raw.strip()
    if len(cleaned) > _OWUI_ID_MAX or not _OWUI_ID_RE.fullmatch(cleaned):
        return ""
    if ".." in cleaned:
        return ""
    return cleaned


# Role is a short lowercase token (e.g. "admin", "user").  Groups header is a
# comma-separated list of stable OpenWebUI group IDs; each element is sanitised
# with the same rules as a user ID and the whole header is length-capped.
_OWUI_ROLE_RE = re.compile(r"[^a-z0-9_\-]")
_OWUI_GROUPS_MAX = 2048
_OWUI_GROUPS_MAX_COUNT = 64


def _sanitize_owui_role(raw: str) -> str:
    """Sanitise the X-OpenWebUI-User-Role header to a lowercase token."""
    if not raw:
        return ""
    return _OWUI_ROLE_RE.sub("", raw.strip().lower())[:_OWUI_ID_MAX]


def _sanitize_owui_groups(raw: str) -> str:
    """Parse X-OpenWebUI-User-Groups into a clean comma-joined string of IDs.

    Splits on commas, sanitises each element as an OpenWebUI ID, drops empties
    and duplicates (order-preserving), and caps the count.  Returns ``""`` when
    no valid IDs remain.  Identity is taken from trusted headers only.
    """
    if not raw:
        return ""
    ids: List[str] = []
    for part in raw.strip()[:_OWUI_GROUPS_MAX].split(","):
        gid = _sanitize_owui_id(part)
        if gid and gid not in ids:
            ids.append(gid)
            if len(ids) >= _OWUI_GROUPS_MAX_COUNT:
                break
    return ",".join(ids)


def _extract_owui_scope(request: "web.Request") -> Dict[str, str]:
    """Extract sanitised user_id / user_name / chat_id / user_role / user_groups.

    Always returns a dict with all keys (empty string if header is missing or
    fails sanitisation).  Callers decide whether to fail-closed on missing
    user_id.  ``user_role``/``user_groups`` feed the Hermes skill ACL.
    """
    user_id = _sanitize_owui_id(request.headers.get("X-OpenWebUI-User-Id", ""))
    chat_id = _sanitize_owui_id(request.headers.get("X-OpenWebUI-Chat-Id", ""))
    raw_user_name = request.headers.get("X-OpenWebUI-User-Name", "").strip()
    user_name = raw_user_name[:128] if raw_user_name else ""
    user_role = _sanitize_owui_role(request.headers.get("X-OpenWebUI-User-Role", ""))
    raw_user_groups = request.headers.get("X-OpenWebUI-User-Groups")
    user_groups = _sanitize_owui_groups(raw_user_groups or "")
    if user_id and raw_user_groups is None:
        logger.warning("owui_acl_group_context_missing user_id=%s", user_id)
    elif user_id and raw_user_groups and not user_groups:
        # Never log the untrusted raw header: stable group identifiers are
        # authorization context and unnecessary for diagnosing sanitization.
        logger.warning("owui_acl_group_context_invalid user_id=%s", user_id)
    return {
        "user_id": user_id,
        "user_name": user_name,
        "chat_id": chat_id,
        "user_role": user_role,
        "user_groups": user_groups,
    }


_SKILL_TOOLSET_KEYS = {"skills", "skills_read", "skills_manage"}
_FILE_TOOLSET_KEYS = {"file", "file_read", "file_write"}
# Toolsets that let a caller read/mutate the protected skills dir OUTSIDE the
# skill tools (arbitrary shell). Shared-skill ACL grants never expose these on
# api_server; operator shell access is a separate out-of-band concern (#13).
_BYPASS_TOOLSET_KEYS = {"terminal"}
_ACL_MANAGED_TOOLSET_KEYS = _SKILL_TOOLSET_KEYS | _FILE_TOOLSET_KEYS | _BYPASS_TOOLSET_KEYS


def _apply_skill_acl_toolset_minimization(
    toolsets: List[str], role: str, groups: str
) -> List[str]:
    """Schema-level skill ACL minimization for the api_server platform (#12/#13).

    When ``skills_acl`` is enabled, restrict the toolset the model sees so an
    unprivileged caller cannot read/mutate protected skills:
      * ``skills`` -> ``skills_read`` (if read) and ``skills_manage`` for every
        authenticated/read-authorized Origin Agent caller, because Increment 1
        gives each caller full native CRUD on only their own user namespace.
        Runtime target resolution still applies the existing ACL to platform
        and external skills;
      * every ``file``/``file_write`` request -> read-only ``file_read``. Raw
        filesystem writes are never implied by shared-skill Reader/Editor/Admin
        authority; the trusted native manager is the only skill mutation route;
      * ``terminal`` -> always withheld: shared-skill Admin is not shell authority.

    Runtime gates (#11 read, #12 manage, #13 protected-path file guard) remain
    authoritative; this is defense-in-depth + UX. Takes ``role``/``groups`` as
    explicit args (not session context) because on /v1/runs the agent is created
    before session vars are bound. ACL disabled => unchanged. Resolution error =>
    fail safe (drop skill/write/exec toolsets; keep read-only file if a file
    toolset was present, since read-only file cannot mutate skills).

    HONEST SCOPE: this function is schema-level attack-surface reduction, not
    the skills reference monitor. The resource boundary is the gateway's
    kernel-read-only shared-skills mount plus the authenticated isolated writer.
    The live api_server config does not spawn or advertise ``opencode_runner``;
    universal MCP dispatch also denies that local code-exec server as defense in
    depth. Browser navigation accepts only HTTP(S), and image/video local-file
    ingress reuses the ownership-aware file guard. Any future local arbitrary-code,
    delegation, cron, or auxiliary tool added to api_server still requires an
    explicit same-UID bypass review; remote ``soc_v2`` remains isolated in its own
    container without skill mounts. Do not treat toolset minimization alone as the
    security boundary.
    """
    if not any(t in _ACL_MANAGED_TOOLSET_KEYS for t in toolsets):
        return toolsets
    had_file = any(t in _FILE_TOOLSET_KEYS for t in toolsets)
    try:
        from tools.skill_acl import load_skill_acl_config, resolve_skill_permissions

        cfg = load_skill_acl_config()
        if not cfg.get("enabled"):
            return toolsets
        perms = resolve_skill_permissions(role or "", groups or "", cfg)
    except Exception:
        kept = [t for t in toolsets if t not in _ACL_MANAGED_TOOLSET_KEYS]
        if had_file:
            kept.append("file_read")
        return kept
    can_read = "read" in perms
    # Reader-group callers need the native manager for full CRUD on their own
    # functional user namespace. This does not broaden platform permissions:
    # skill_manage resolves the target namespace before applying the existing
    # create/update/delete ACL to platform/external skills.
    can_skill_manage = can_read or bool(perms & {"create", "update", "delete"})
    result = [t for t in toolsets if t not in _ACL_MANAGED_TOOLSET_KEYS]
    if can_read:
        result.append("skills_read")
    if can_skill_manage:
        result.append("skills_manage")
    if had_file:
        result.append("file_read")
    return result


def _scope_session_id(base_session_id: str, scope: Dict[str, str]) -> str:
    """Append per-user and per-chat scope suffixes to a base session ID.

    Idempotent: passing an already-scoped session ID through with the same
    scope yields the same result.  Scope is applied as ``-user-<id>`` and
    ``-chat-<id>`` so two users (or two chats by the same user) cannot
    collide on the conversation fingerprint alone.
    """
    out = base_session_id
    user_id = scope.get("user_id", "")
    chat_id = scope.get("chat_id", "")
    if user_id and f"-user-{user_id}" not in out:
        out = f"{out}-user-{user_id}"
    if chat_id and f"-chat-{chat_id}" not in out:
        out = f"{out}-chat-{chat_id}"
    return out


def _missing_user_id_error() -> "web.Response":
    """Standard 400 response when the X-OpenWebUI-User-Id header is required."""
    return web.json_response(
        _openai_error(
            "Missing X-OpenWebUI-User-Id header.  This Hermes API server is "
            "configured for multi-user isolation; every request must identify "
            "the end-user via the X-OpenWebUI-User-Id header so sessions and "
            "memory stay scoped per user.  OpenWebUI sends this automatically "
            "when ENABLE_FORWARD_USER_INFO_HEADERS=true.",
            err_type="invalid_request_error",
            param="X-OpenWebUI-User-Id",
        ),
        status=400,
    )


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """Derive a stable session ID from the conversation's first user message.

    OpenAI-compatible frontends (Open WebUI, LibreChat, etc.) send the full
    conversation history with every request.  The system prompt and first user
    message are constant across all turns of the same conversation, so hashing
    them produces a deterministic session ID that lets the API server reuse
    the same Hermes session (and therefore the same Docker container sandbox
    directory) across turns.

    Note: this base ID is NOT user/chat scoped.  Callers must run the result
    through ``_scope_session_id()`` to add per-user / per-chat suffixes.
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


_CRON_AVAILABLE = False
try:
    from cron.jobs import (
        list_jobs as _cron_list,
        get_job as _cron_get,
        create_job as _cron_create,
        update_job as _cron_update,
        remove_job as _cron_remove,
        pause_job as _cron_pause,
        resume_job as _cron_resume,
        trigger_job as _cron_trigger,
    )
    _CRON_AVAILABLE = True
except ImportError:
    _cron_list = None
    _cron_get = None
    _cron_create = None
    _cron_update = None
    _cron_remove = None
    _cron_pause = None
    _cron_resume = None
    _cron_trigger = None


def _notify_cron_provider_jobs_changed() -> None:
    """Tell the active cron scheduler provider the job set changed after a REST
    mutation (no-op for the built-in). Best-effort — never breaks the handler."""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass

# Defense-in-depth: mirror the agent-facing cronjob tool, which scans the
# user-supplied prompt for exfiltration/injection payloads at create/update
# time (tools/cronjob_tools.py).  The REST cron endpoints are authenticated
# (every handler runs _check_auth, and connect() refuses to start without
# API_SERVER_KEY), so this is not the trust boundary — it's parity with the
# tool path so a malicious prompt is rejected the same way regardless of
# which surface created the job.  Imported defensively: a missing scanner
# must not disable the cron REST API.
try:
    from tools.cronjob_tools import _scan_cron_prompt as _scan_cron_prompt
except Exception:  # pragma: no cover - scanner is optional hardening
    _scan_cron_prompt = None


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        raw_port = extra.get("port")
        if raw_port is None:
            raw_port = os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))
        self._port: int = _coerce_port(raw_port, DEFAULT_PORT)
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._openwebui_bridge_key: str = extra.get(
            "openwebui_bridge_key",
            os.getenv("OPENWEBUI_BRIDGE_API_KEY", ""),
        )
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[str, float] = {}
        # Active run agent/task references for stop support
        self._active_run_agents: Dict[str, Any] = {}
        self._active_run_tasks: Dict[str, "asyncio.Task"] = {}
        # Pollable run status for dashboards and external control-plane UIs.
        self._run_statuses: Dict[str, Dict[str, Any]] = {}
        # Active approval session key for each run_id.  The approval core
        # resolves requests by session key, while API clients address the
        # in-flight run by run_id.
        self._run_approval_sessions: Dict[str, str] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity
        # Per-session idle tracking → triggers OpenViking commit + memory
        # extraction after IDLE_COMMIT_SECONDS of silence.  In api_server
        # mode the AIAgent is short-lived (one per request) and never fires
        # on_session_end on its own, so without this watcher OpenViking
        # accumulates messages.jsonl forever and never extracts memories.
        # Each entry: {"user_id", "chat_id", "last_seen": float, "committed": bool}
        # Only mutated from the event loop, so no lock needed (mutations are
        # `__setitem__` on existing keys + dict insertion, both atomic enough
        # against the single-coroutine watcher).
        self._session_activity: Dict[str, Dict[str, Any]] = {}
        self._idle_commit_task: Optional["asyncio.Task"] = None
        self._sweep_task: Optional["asyncio.Task"] = None
        # Disk-backed activity table — survives container/process restarts
        # so a user who walks away mid-conversation still gets memory
        # extraction once the watcher resumes after restart.  Path is
        # resolved lazily on first persist.
        self._session_activity_path: Optional[Path] = None
        self._openwebui_bridge_service: Optional[Any] = None
        self._dreaming_scheduler_task: Optional["asyncio.Task"] = None
        import threading as _threading
        self._session_activity_lock = _threading.Lock()

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """Derive the advertised model name for /v1/models.

        Priority:
        1. Explicit override (config extra or API_SERVER_MODEL_NAME env var)
        2. Active profile name (so each profile advertises a distinct model)
        3. Fallback: "hermes-agent"
        """
        if explicit and explicit.strip():
            return explicit.strip()
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in {"default", "custom"}:
                return profile
        except Exception:
            pass
        return "hermes-agent"

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    @staticmethod
    def _clean_log_value(value: Any, *, max_len: int = 200) -> str:
        """Sanitize request metadata before it reaches security logs."""
        if value is None:
            return ""
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        return text[:max_len]

    def _request_audit_context(self, request: "web.Request") -> Dict[str, str]:
        """Return non-secret source metadata for security/audit warnings."""
        peer_ip = ""
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                peer_ip = str(peer[0])
        except Exception:
            peer_ip = ""

        return {
            "remote": self._clean_log_value(getattr(request, "remote", "") or peer_ip),
            "peer_ip": self._clean_log_value(peer_ip),
            "forwarded_for": self._clean_log_value(request.headers.get("X-Forwarded-For", "")),
            "real_ip": self._clean_log_value(request.headers.get("X-Real-IP", "")),
            "method": self._clean_log_value(request.method, max_len=16),
            "path": self._clean_log_value(request.path_qs, max_len=500),
            "user_agent": self._clean_log_value(request.headers.get("User-Agent", ""), max_len=300),
        }

    def _request_audit_log_suffix(self, request: "web.Request") -> str:
        ctx = self._request_audit_context(request)
        fields = [f"{key}={value!r}" for key, value in ctx.items() if value]
        return " ".join(fields) if fields else "source='unknown'"

    def _cron_origin_from_request(self, request: "web.Request") -> Dict[str, str]:
        """Persist safe API source metadata on cron jobs created over HTTP."""
        ctx = self._request_audit_context(request)
        origin = {
            "platform": "api_server",
            "chat_id": "api",
        }
        if ctx.get("remote"):
            origin["source_ip"] = ctx["remote"]
        if ctx.get("peer_ip"):
            origin["peer_ip"] = ctx["peer_ip"]
        if ctx.get("forwarded_for"):
            origin["forwarded_for"] = ctx["forwarded_for"]
        if ctx.get("real_ip"):
            origin["real_ip"] = ctx["real_ip"]
        if ctx.get("user_agent"):
            origin["user_agent"] = ctx["user_agent"]
        return origin

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        connect() refuses to start the API server without API_SERVER_KEY, so
        the no-key branch only exists for tests or unsupported manual wiring.
        """
        if not self._api_key:
            return None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._api_key):
                return None  # Auth OK

        logger.warning(
            "API server rejected invalid API key: %s",
            self._request_audit_log_suffix(request),
        )
        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    def _check_strict_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """Require an API key for sensitive internal bridge routes."""
        if not self._openwebui_bridge_key:
            return web.json_response(
                {
                    "error": {
                        "message": "OPENWEBUI_BRIDGE_API_KEY required for internal OpenWebUI bridge routes",
                        "type": "invalid_request_error",
                        "code": "api_key_required",
                    }
                },
                status=401,
            )
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._openwebui_bridge_key):
                return None

        return web.json_response(
            {
                "error": {
                    "message": "Invalid OpenWebUI bridge API key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
            status=401,
        )

    # ------------------------------------------------------------------
    # OpenWebUI memory/dreaming bridge
    # ------------------------------------------------------------------

    def _openwebui_bridge(self) -> Any:
        """Lazy-load the OpenWebUI bridge service.

        Importing lazily keeps the existing OpenAI-compatible API surface
        unchanged for deployments that never call the internal bridge routes.
        """
        if self._openwebui_bridge_service is None:
            from gateway.openwebui_bridge import OpenWebUIBridgeService

            self._openwebui_bridge_service = OpenWebUIBridgeService.from_env()
        return self._openwebui_bridge_service

    def _require_owui_scope(
        self, request: "web.Request"
    ) -> tuple[Optional[Dict[str, str]], Optional["web.Response"]]:
        auth_err = self._check_strict_auth(request)
        if auth_err:
            return None, auth_err
        scope = _extract_owui_scope(request)
        if not scope.get("user_id"):
            return None, _missing_user_id_error()
        return scope, None

    def _require_owui_admin(self, request: "web.Request") -> Optional["web.Response"]:
        auth_err = self._check_strict_auth(request)
        if auth_err:
            return auth_err
        if request.headers.get("X-OpenWebUI-User-Role", "").strip().lower() != "admin":
            return web.json_response({"error": "admin role required"}, status=403)
        return None

    @staticmethod
    def _json_error(message: str, status: int = 400) -> "web.Response":
        return web.json_response({"error": message}, status=status)

    @staticmethod
    def _event_user_ids(event: Dict[str, Any]) -> set[str]:
        user_ids = set()
        for value in (event.get("user_id"),):
            if isinstance(value, str) and value.strip():
                user_ids.add(value.strip())
        feedback = event.get("feedback") if isinstance(event.get("feedback"), dict) else {}
        previous = event.get("previous_feedback") if isinstance(event.get("previous_feedback"), dict) else {}
        for payload in (feedback, previous):
            value = payload.get("user_id")
            if isinstance(value, str) and value.strip():
                user_ids.add(value.strip())
        return user_ids

    async def _handle_openwebui_feedback_events(self, request: "web.Request") -> "web.Response":
        """POST /api/openwebui/feedback-events.

        Accepts at-least-once OpenWebUI outbox deliveries and stores one raw
        signal per user/event idempotently in OpenViking.
        """
        scope, err = self._require_owui_scope(request)
        if err:
            return err
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return self._json_error("JSON object body required")
            if not body.get("event_id"):
                idem = request.headers.get("Idempotency-Key", "").strip()
                if idem:
                    body["event_id"] = idem
            mismatched_users = self._event_user_ids(body) - {scope["user_id"]}
            if mismatched_users:
                return self._json_error("feedback event user_id does not match authenticated scope", status=400)
            result = await self._openwebui_bridge().ingest_feedback(scope["user_id"], body)
            return web.json_response(result)
        except ValueError as exc:
            return self._json_error(str(exc), status=400)
        except Exception as exc:
            logger.exception("OpenWebUI feedback ingestion failed")
            return self._json_error(str(exc), status=500)

    async def _handle_openwebui_list_memories(self, request: "web.Request") -> "web.Response":
        """GET /api/openwebui/memories."""
        scope, err = self._require_owui_scope(request)
        if err:
            return err
        limit_raw = request.query.get("limit")
        limit = None
        if limit_raw:
            try:
                limit = int(limit_raw)
            except ValueError:
                return self._json_error("limit must be an integer", status=400)
            if limit <= 0:
                return self._json_error("limit must be greater than zero", status=400)
        try:
            memories = await self._openwebui_bridge().list_memories(
                scope["user_id"],
                memory_type=request.query.get("type") or None,
                query=request.query.get("q") or request.query.get("query") or None,
                limit=limit,
            )
            complete = limit is None or len(memories) < limit
            return web.json_response(
                {
                    "memories": memories,
                    "total": len(memories) if complete else None,
                    "has_more": not complete,
                    "truncated": not complete,
                }
            )
        except ValueError as exc:
            return self._json_error(str(exc), status=400)
        except Exception as exc:
            from gateway.openwebui_bridge import OpenVikingListingTruncated

            if isinstance(exc, OpenVikingListingTruncated):
                return web.json_response(
                    {
                        "memories": [],
                        "total": 0,
                        "has_more": True,
                        "truncated": True,
                        "error": str(exc),
                    }
                )
            logger.exception("OpenWebUI memory list failed")
            return self._json_error(str(exc), status=500)

    async def _handle_openwebui_upsert_memory(self, request: "web.Request") -> "web.Response":
        """POST /api/openwebui/memories."""
        scope, err = self._require_owui_scope(request)
        if err:
            return err
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return self._json_error("JSON object body required")
            memory = await self._openwebui_bridge().upsert_memory(scope["user_id"], body)
            return web.json_response({"memory": memory})
        except Exception as exc:
            error_text = str(exc).lower()
            if exc.__class__.__name__ == "OpenVikingNotFound":
                status = 404
            else:
                status = 409 if "tombstone" in error_text or "retracted" in error_text else 400
            return self._json_error(str(exc), status=status)

    async def _handle_openwebui_delete_memory(self, request: "web.Request") -> "web.Response":
        """DELETE /api/openwebui/memories/{memory_id} or ?memory_id=..."""
        scope, err = self._require_owui_scope(request)
        if err:
            return err
        memory_id = (
            request.match_info.get("memory_id")
            or request.query.get("memory_id")
            or request.query.get("id")
            or ""
        ).strip()
        if not memory_id:
            return self._json_error("memory_id is required")
        try:
            result = await self._openwebui_bridge().delete_memory(scope["user_id"], memory_id)
            return web.json_response(result)
        except ValueError as exc:
            return self._json_error(str(exc), status=400)
        except Exception as exc:
            return self._json_error(str(exc), status=500)

    async def _handle_dreaming_run_now(self, request: "web.Request") -> "web.Response":
        """POST /api/dreaming/run-now."""
        auth_err = self._check_strict_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        scope_mode = str(body.get("scope") or request.query.get("scope") or "user").lower()
        org = scope_mode in {"org", "global", "organization"}
        if org:
            admin_err = self._require_owui_admin(request)
            if admin_err:
                return admin_err
            try:
                return web.json_response(await self._openwebui_bridge().run_nightly_pipeline())
            except Exception as exc:
                logger.exception("Org dreaming run failed")
                return self._json_error(str(exc), status=500)

        scope, scope_err = self._require_owui_scope(request)
        if scope_err:
            return scope_err
        try:
            return web.json_response(await self._openwebui_bridge().run_now(user_id=scope["user_id"]))
        except ValueError as exc:
            return self._json_error(str(exc), status=400)
        except Exception as exc:
            logger.exception("User dreaming run failed")
            return self._json_error(str(exc), status=500)

    async def _handle_dreaming_status(self, request: "web.Request") -> "web.Response":
        """GET /api/dreaming/status."""
        auth_err = self._check_strict_auth(request)
        if auth_err:
            return auth_err
        scope_mode = str(request.query.get("scope") or "user").lower()
        if scope_mode in {"org", "global", "organization"}:
            admin_err = self._require_owui_admin(request)
            if admin_err:
                return admin_err
            return web.json_response(self._openwebui_bridge().status(org=True))
        scope, scope_err = self._require_owui_scope(request)
        if scope_err:
            return scope_err
        return web.json_response(self._openwebui_bridge().status(user_id=scope["user_id"]))

    # ------------------------------------------------------------------
    # Session header helpers
    # ------------------------------------------------------------------

    # Soft length cap for session identifiers.  Headers are bounded in
    # aggregate by aiohttp (``client_max_size`` / default 8 KiB per
    # header), but we impose a tighter limit on the session headers so a
    # caller can't burn memory by passing a multi-kilobyte "session key".
    # 256 chars is well above any realistic stable channel identifier
    # (e.g. ``agent:main:webui:dm:user-42``) while staying small enough
    # that the sanitized form is safe to pass into Honcho / state.db.
    _MAX_SESSION_HEADER_LEN = 256

    def _parse_session_key_header(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        """Extract and validate the ``X-Hermes-Session-Key`` header.

        The session key is a stable per-channel identifier that scopes
        long-term memory (e.g. Honcho sessions) across transcripts.  It
        is independent of ``X-Hermes-Session-Id``: callers may send
        either, both, or neither.

        Returns ``(session_key, None)`` on success (with an empty/absent
        header yielding ``None`` for the key), or ``(None, error_response)``
        on validation failure.

        Security: like session continuation, accepting a caller-supplied
        memory scope requires API-key authentication so that an
        unauthenticated client on a local-only server can't inject itself
        into another user's long-term memory scope by guessing a key.
        """
        raw = request.headers.get("X-Hermes-Session-Key", "").strip()
        if not raw:
            return None, None

        if not self._api_key:
            logger.warning(
                "X-Hermes-Session-Key rejected: no API key configured. "
                "Set API_SERVER_KEY to enable long-term memory scoping."
            )
            return None, web.json_response(
                _openai_error(
                    "X-Hermes-Session-Key requires API key authentication. "
                    "Configure API_SERVER_KEY to enable this feature."
                ),
                status=403,
            )

        # Reject control characters that could enable header injection on
        # the echo path.
        if re.search(r'[\r\n\x00]', raw):
            return None, web.json_response(
                {"error": {"message": "Invalid session key", "type": "invalid_request_error"}},
                status=400,
            )

        if len(raw) > self._MAX_SESSION_HEADER_LEN:
            return None, web.json_response(
                {"error": {"message": "Session key too long", "type": "invalid_request_error"}},
                status=400,
            )

        return raw, None

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        """Lazily initialise and return the shared SessionDB instance.

        Sessions are persisted to ``state.db`` so that ``hermes sessions list``
        shows API-server conversations alongside CLI and gateway ones.
        """
        if self._session_db is None:
            try:
                from hermes_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.debug("SessionDB unavailable for API server: %s", e)
        return self._session_db

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key: Optional[str] = None,
        reasoning_callback=None,
        user_id: Optional[str] = None,
        user_role: Optional[str] = None,
        user_groups: Optional[str] = None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.

        ``gateway_session_key`` is a stable per-channel identifier supplied
        by the client (via ``X-Hermes-Session-Key``).  Unlike ``session_id``
        which scopes the short-term transcript and rotates on /new, this
        key is meant to persist across transcripts so long-term memory
        providers (e.g. Honcho) can scope their per-chat state correctly
        — matching the semantics of the native gateway's ``session_key``.
        """
        from run_agent import AIAgent
        from gateway.run import (
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            GatewayRunner,
        )
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        reasoning_config = GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))
        # Schema-level skill ACL minimization (issue #12): hide skill toolsets the
        # caller cannot use. Runtime gates remain authoritative.
        enabled_toolsets = _apply_skill_acl_toolset_minimization(
            enabled_toolsets, user_role or "", user_groups or ""
        )

        max_iterations = _current_max_iterations()

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            user_id=user_id,
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            reasoning_callback=reasoning_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            reasoning_config=reasoning_config,
            gateway_session_key=gateway_session_key,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response(
            {"status": "ok", "platform": "hermes-agent", "version": _hermes_version()}
        )

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed — rich status for cross-container dashboard probing.

        Returns gateway state, connected platforms, PID, and uptime so the
        dashboard can display full status without needing a shared PID file or
        /proc access.  No authentication required.
        """
        from gateway.status import read_runtime_status

        runtime = read_runtime_status() or {}
        return web.json_response({
            "status": "ok",
            "platform": "hermes-agent",
            "version": _hermes_version(),
            "gateway_state": runtime.get("gateway_state"),
            "platforms": runtime.get("platforms", {}),
            "active_agents": runtime.get("active_agents", 0),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
            "pid": os.getpid(),
        })

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — return hermes-agent as an available model."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": self._model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": self._model_name,
                    "parent": None,
                }
            ],
        })

    async def _handle_capabilities(self, request: "web.Request") -> "web.Response":
        """GET /v1/capabilities — advertise the stable API surface.

        External UIs and orchestrators use this endpoint to discover the API
        server's plugin-safe contract without scraping docs or assuming that
        every Hermes version exposes the same endpoints.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "model": self._model_name,
            "auth": {
                "type": "bearer",
                "required": bool(self._api_key),
            },
            "runtime": {
                "mode": "server_agent",
                "tool_execution": "server",
                "split_runtime": False,
                "description": (
                    "The API server creates a server-side Hermes AIAgent; "
                    "tools execute on the API-server host unless a future "
                    "explicit split-runtime mode is enabled."
                ),
            },
            "features": {
                "chat_completions": True,
                "chat_completions_streaming": True,
                "responses_api": True,
                "responses_streaming": True,
                "artifact_downloads": True,
                "run_submission": True,
                "run_status": True,
                "run_events_sse": True,
                "run_stop": True,
                "run_approval_response": True,
                "tool_progress_events": True,
                "approval_events": True,
                "session_resources": True,
                "session_chat": True,
                "session_chat_streaming": True,
                "session_fork": True,
                "admin_config_rw": False,
                "jobs_admin": False,
                "memory_write_api": False,
                "skills_api": True,
                "audio_api": False,
                "realtime_voice": False,
                "session_continuity_header": "X-Hermes-Session-Id",
                "session_key_header": "X-Hermes-Session-Key",
                "cors": bool(self._cors_origins),
            },
            "endpoints": {
                "health": {"method": "GET", "path": "/health"},
                "health_detailed": {"method": "GET", "path": "/health/detailed"},
                "models": {"method": "GET", "path": "/v1/models"},
                "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
                "responses": {"method": "POST", "path": "/v1/responses"},
                "artifact_download": {"method": "GET", "path": "/v1/artifacts/{artifact_id}/{filename}"},
                "runs": {"method": "POST", "path": "/v1/runs"},
                "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
                "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
                "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
                "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
                "skills": {"method": "GET", "path": "/v1/skills"},
                "toolsets": {"method": "GET", "path": "/v1/toolsets"},
                "sessions": {"method": "GET", "path": "/api/sessions"},
                "session_create": {"method": "POST", "path": "/api/sessions"},
                "session": {"method": "GET", "path": "/api/sessions/{session_id}"},
                "session_update": {"method": "PATCH", "path": "/api/sessions/{session_id}"},
                "session_delete": {"method": "DELETE", "path": "/api/sessions/{session_id}"},
                "session_messages": {"method": "GET", "path": "/api/sessions/{session_id}/messages"},
                "session_fork": {"method": "POST", "path": "/api/sessions/{session_id}/fork"},
                "session_chat": {"method": "POST", "path": "/api/sessions/{session_id}/chat"},
                "session_chat_stream": {"method": "POST", "path": "/api/sessions/{session_id}/chat/stream"},
            },
        })

    async def _handle_skills(self, request: "web.Request") -> "web.Response":
        """GET /v1/skills — list installed skills visible to the API-server agent.

        Read-only listing intended for external clients that need to know
        which skills are available without sending a chat message and asking
        the model. Mirrors what the gateway/CLI surfaces through
        ``/skills list``, but as a deterministic JSON payload.

        Returns the same skill metadata (name, description, category) the
        skills hub uses internally. Disabled skills are excluded so the
        listing matches what the agent actually loads.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from tools.skills_tool import _find_all_skills, _sort_skills
            skills = _sort_skills(_find_all_skills(skip_disabled=False))
        except Exception:
            logger.exception("GET /v1/skills failed")
            return web.json_response(
                _openai_error("Failed to enumerate skills", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "data": skills,
        })

    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
        """GET /v1/toolsets — list toolsets and their resolved tools.

        Returns the toolset surface the api_server platform actually exposes
        to its agent: each toolset's enabled/configured state plus the
        concrete tool names it expands to. This is the deterministic
        equivalent of what a client would otherwise have to recover by
        asking the model what tools it can call.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from hermes_cli.config import load_config
            from hermes_cli.tools_config import (
                _get_effective_configurable_toolsets,
                _get_platform_tools,
                _toolset_has_keys,
            )
            from toolsets import resolve_toolset

            config = load_config()
            enabled_toolsets = _get_platform_tools(
                config,
                "api_server",
                include_default_mcp_servers=False,
            )
            data: List[Dict[str, Any]] = []
            for name, label, desc in _get_effective_configurable_toolsets():
                try:
                    tools = sorted(set(resolve_toolset(name)))
                except Exception:
                    tools = []
                is_enabled = name in enabled_toolsets
                data.append({
                    "name": name,
                    "label": label,
                    "description": desc,
                    "enabled": is_enabled,
                    "configured": _toolset_has_keys(name, config),
                    "tools": tools,
                })
        except Exception:
            logger.exception("GET /v1/toolsets failed")
            return web.json_response(
                _openai_error("Failed to enumerate toolsets", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "platform": "api_server",
            "data": data,
        })

    # ------------------------------------------------------------------
    # /api/sessions — thin client/session resource API
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nonnegative_int(value: Any, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return min(parsed, maximum)

    @staticmethod
    def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
        """Return a stable, client-safe session representation."""
        safe_keys = (
            "id", "source", "user_id", "model", "title", "started_at", "ended_at",
            "end_reason", "message_count", "tool_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
            "api_call_count", "parent_session_id", "last_active", "preview",
            "_lineage_root_id",
        )
        payload = {key: session.get(key) for key in safe_keys if key in session}
        # Avoid exposing full system prompts/model_config through the client API;
        # callers only need to know whether those snapshots exist.
        payload["has_system_prompt"] = bool(session.get("system_prompt"))
        payload["has_model_config"] = bool(session.get("model_config"))
        return payload

    @staticmethod
    def _message_response(message: Dict[str, Any]) -> Dict[str, Any]:
        safe_keys = (
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
            "reasoning_content",
        )
        return {key: message.get(key) for key in safe_keys if key in message}

    async def _read_json_body(self, request: "web.Request") -> tuple[Dict[str, Any], Optional["web.Response"]]:
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response(_openai_error("Invalid JSON in request body"), status=400)
        if not isinstance(body, dict):
            return {}, web.json_response(_openai_error("Request body must be a JSON object"), status=400)
        return body, None

    def _get_existing_session_or_404(self, session_id: str) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        db = self._ensure_session_db()
        if db is None:
            return None, web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)
        session = db.get_session(session_id)
        if not session:
            return None, web.json_response(_openai_error(f"Session not found: {session_id}", code="session_not_found"), status=404)
        return session, None

    def _conversation_history_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        db = self._ensure_session_db()
        if db is None:
            return []
        try:
            return db.get_messages_as_conversation(session_id)
        except Exception as exc:
            logger.warning("Failed to load session history for %s: %s", session_id, exc)
            return []

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions — list persisted Hermes sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        limit = self._parse_nonnegative_int(request.query.get("limit"), default=50, maximum=200)
        offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
        source = request.query.get("source") or None
        include_children = _coerce_request_bool(request.query.get("include_children"), default=False)
        sessions = db.list_sessions_rich(
            source=source,
            limit=limit,
            offset=offset,
            include_children=include_children,
            order_by_last_active=True,
        )
        return web.json_response({
            "object": "list",
            "data": [self._session_response(s) for s in sessions],
            "limit": limit,
            "offset": offset,
            "has_more": len(sessions) == limit,
        })

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions — create an empty Hermes session row."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        body, err = await self._read_json_body(request)
        if err:
            return err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        raw_id = body.get("id") or body.get("session_id")
        session_id = str(raw_id).strip() if raw_id else f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        if not session_id or re.search(r'[\r\n\x00]', session_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if len(session_id) > self._MAX_SESSION_HEADER_LEN:
            return web.json_response(_openai_error("Session ID too long", code="invalid_session_id"), status=400)
        if db.get_session(session_id):
            return web.json_response(_openai_error(f"Session already exists: {session_id}", code="session_exists"), status=409)

        model = body.get("model") or self._model_name
        system_prompt = body.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_prompt must be a string", code="invalid_system_prompt"), status=400)
        db.create_session(session_id, "api_server", model=str(model) if model else None, system_prompt=system_prompt)
        title = body.get("title")
        if title is not None:
            try:
                db.set_session_title(session_id, str(title))
            except ValueError as exc:
                db.delete_session(session_id)
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        session = db.get_session(session_id) or {"id": session_id, "source": "api_server", "model": model, "title": title}
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)}, status=201)

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session, err = self._get_existing_session_or_404(request.match_info["session_id"])
        if err:
            return err
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} — update client-safe session metadata."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        allowed = {"title", "end_reason"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            return web.json_response(_openai_error(f"Unsupported session fields: {', '.join(unknown)}", code="unsupported_session_field"), status=400)

        db = self._ensure_session_db()
        if "title" in body:
            try:
                db.set_session_title(session_id, "" if body["title"] is None else str(body["title"]))
            except ValueError as exc:
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        if body.get("end_reason"):
            db.end_session(session_id, str(body["end_reason"]))
        session = db.get_session(session_id) or session
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = self._ensure_session_db()
        deleted = db.delete_session(session_id)
        return web.json_response({"object": "hermes.session.deleted", "id": session_id, "deleted": bool(deleted)})

    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = self._ensure_session_db()
        resolved_id = db.resolve_resume_session_id(session_id)
        messages = db.get_messages(resolved_id)
        return web.json_response({
            "object": "list",
            "session_id": resolved_id,
            "data": [self._message_response(m) for m in messages],
        })

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork — branch via current SessionDB primitives."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        source, err = self._get_existing_session_or_404(source_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        db = self._ensure_session_db()
        fork_id = str(body.get("id") or body.get("session_id") or f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}").strip()
        if not fork_id or re.search(r'[\r\n\x00]', fork_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if db.get_session(fork_id):
            return web.json_response(_openai_error(f"Session already exists: {fork_id}", code="session_exists"), status=409)

        # Match the CLI /branch semantics: mark the original as branched, then
        # create a child session that carries the transcript forward. This uses
        # SessionDB's native parent_session_id/end_reason visibility model rather
        # than inventing a parallel fork store.
        db.end_session(source_id, "branched")
        db.create_session(
            fork_id,
            "api_server",
            model=source.get("model"),
            system_prompt=source.get("system_prompt"),
            parent_session_id=source_id,
        )
        messages = db.get_messages(source_id)
        db.replace_messages(fork_id, messages)
        title = body.get("title")
        if title is None:
            base = source.get("title") or "fork"
            try:
                title = db.get_next_title_in_lineage(base)
            except Exception:
                title = f"{base} fork"
        try:
            db.set_session_title(fork_id, str(title))
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        fork = db.get_session(fork_id) or {"id": fork_id, "parent_session_id": source_id}
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat — one synchronous agent turn."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        history = self._conversation_history_for_session(session_id)
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            granted_file_paths=None,
        )
        effective_session_id = result.get("session_id") if isinstance(result, dict) else session_id
        final_response = result.get("final_response", "") if isinstance(result, dict) else ""
        headers = {"X-Hermes-Session-Id": effective_session_id or session_id}
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(
            {
                "object": "hermes.session.chat.completion",
                "session_id": effective_session_id or session_id,
                "message": {"role": "assistant", "content": final_response},
                "usage": usage,
            },
            headers=headers,
        )

    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream — SSE wrapper over _run_agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[tuple[str, Dict[str, Any]]]]" = asyncio.Queue()
        message_id = f"msg_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        seq = 0

        def _event_payload(name: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            nonlocal seq
            seq += 1
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", run_id)
            payload.setdefault("seq", seq)
            payload.setdefault("ts", time.time())
            return name, payload

        def _enqueue(name: str, payload: Dict[str, Any]) -> None:
            event = _event_payload(name, payload)
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            try:
                if running_loop is loop:
                    queue.put_nowait(event)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

        def _delta(delta: str) -> None:
            if delta:
                _enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                _enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                event_name = event_type.replace("tool.", "tool.")
                _enqueue(event_name, {"message_id": message_id, "tool_name": tool_name, "preview": preview, "args": args})

        async def _run_and_signal() -> None:
            try:
                await queue.put(_event_payload("run.started", {"user_message": {"role": "user", "content": user_message}}))
                await queue.put(_event_payload("message.started", {"message": {"id": message_id, "role": "assistant"}}))
                history = self._conversation_history_for_session(session_id)
                result, usage = await self._run_agent(
                    user_message=user_message,
                    conversation_history=history,
                    ephemeral_system_prompt=system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress,
                    gateway_session_key=gateway_session_key,
                    granted_file_paths=None,
                )
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                effective_session_id = result.get("session_id", session_id) if isinstance(result, dict) else session_id
                turn_messages = self._turn_transcript_messages(history, user_message, result) if isinstance(result, dict) else []
                await queue.put(_event_payload("assistant.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "content": final_response,
                    "completed": True,
                    "partial": False,
                    "interrupted": False,
                }))
                await queue.put(_event_payload("run.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "completed": True,
                    "messages": turn_messages,
                    "usage": usage,
                }))
            except Exception as exc:
                logger.exception("[api_server] session chat stream failed")
                await queue.put(_event_payload("error", {"message": str(exc)}))
            finally:
                await queue.put(_event_payload("done", {}))
                await queue.put(None)

        task = asyncio.create_task(_run_and_signal())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": session_id,
        }
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        last_write = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    last_write = time.monotonic()
                    continue
                if item is None:
                    break
                name, payload = item
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                last_write = time.monotonic()
        except (asyncio.CancelledError, ConnectionResetError):
            task.cancel()
            raise
        except Exception as exc:
            logger.debug("[api_server] session SSE stream error: %s", exc)
        return response

    async def _handle_artifact_download(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/artifacts/{artifact_id}/{filename} via signed URL."""
        from tools.local_document_export_tool import resolve_local_export_download

        expires = request.query.get("expires", "")
        sig = request.query.get("sig", "")
        if "expires_epoch" in request.match_info:
            try:
                expires = (
                    datetime.fromtimestamp(
                        int(request.match_info.get("expires_epoch", "")),
                        timezone.utc,
                    )
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
            except (TypeError, ValueError, OSError, OverflowError):
                expires = ""
            sig = request.match_info.get("sig", "")
        elif not sig and "sig" not in request.query:
            sig = request.query.get("amp;sig", "")

        result = resolve_local_export_download(
            artifact_id=request.match_info.get("artifact_id", ""),
            filename=request.match_info.get("filename", ""),
            expires=expires,
            sig=sig,
        )
        if not result.get("ok"):
            return web.json_response(
                {"error": result.get("error", "artifact download failed")},
                status=int(result.get("status", 404)),
            )

        filename = result["filename"]
        return web.FileResponse(
            path=result["path"],
            headers={
                "Content-Type": result["mime"],
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Per-user / per-chat scope.  Extracted up-front so that EVERY code
        # path below (request body parse, session continuation, fingerprint
        # derivation) sees the scoped session ID — otherwise history loaded
        # from one user's session can leak into another's response stream.
        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = _coerce_request_bool(body.get("stream"), default=False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                # System messages don't support images (Anthropic rejects, OpenAI
                # text-model systems don't render them).  Flatten to text.
                content = _normalize_chat_content(raw_content)
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in {"user", "assistant"}:
                try:
                    content = _normalize_multimodal_content(raw_content)
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"messages[{idx}].content")
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message: Any = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        granted_file_paths: List[str] = []
        user_message = _augment_message_with_handoff_context(
            user_message,
            scope,
            granted_file_paths,
        )

        if not _content_has_visible_payload(user_message):
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to scope long-term memory (e.g. Honcho) with a
        # stable per-channel identifier via X-Hermes-Session-Key.  This
        # is independent of X-Hermes-Session-Id: the key persists across
        # transcripts while the id rotates when the caller starts a new
        # transcript (i.e. /new semantics).  See _parse_session_key_header.
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # Allow caller to continue an existing session by passing X-Hermes-Session-Id.
        # When provided, history is loaded from state.db instead of from the request body.
        #
        # Security: session continuation exposes conversation history, so it is
        # only allowed when the API key is configured and the request is
        # authenticated.  Without this gate, any unauthenticated client could
        # read arbitrary session history by guessing/enumerating session IDs.
        provided_session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        if provided_session_id:
            if not self._api_key:
                logger.warning(
                    "Session continuation via X-Hermes-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity."
                )
                return web.json_response(
                    _openai_error(
                        "Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature."
                    ),
                    status=403,
                )
            # Sanitize: reject control characters that could enable header injection.
            if re.search(r'[\r\n\x00]', provided_session_id):
                return web.json_response(
                    {"error": {"message": "Invalid session ID", "type": "invalid_request_error"}},
                    status=400,
                )
            # Apply user/chat scope BEFORE loading history so the read and the
            # subsequent writes share the same session ID.  Without this, an
            # attacker who guesses another user's base session could pass it
            # via X-Hermes-Session-Id and have the server silently load that
            # user's history into their request.
            session_id = _scope_session_id(provided_session_id, scope)
            try:
                db = self._ensure_session_db()
                if db is not None:
                    history = db.get_messages_as_conversation(session_id)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # Derive a stable session ID from the conversation fingerprint so
            # that consecutive messages from the same Open WebUI (or similar)
            # conversation map to the same Hermes session.  The first user
            # message + system prompt are constant across all turns.
            first_user = ""
            for cm in conversation_messages:
                if cm.get("role") == "user":
                    first_user = cm.get("content", "")
                    break
            session_id = _scope_session_id(
                _derive_chat_session_id(system_prompt, first_user),
                scope,
            )
            # history already set from request body above

        user_id = scope["user_id"]
        chat_id = scope.get("chat_id", "")
        user_name = scope["user_name"]
        # Register / refresh session activity so the idle-commit watcher
        # fires OpenViking memory extraction once the user stops typing.
        self._touch_session_activity(session_id, scope)

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._model_name)
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # Filter out None — the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            # Track which tool_call_ids we've emitted a "running" lifecycle
            # event for, so a "completed" event without a matching "running"
            # (e.g. internal/filtered tools) is silently dropped instead of
            # producing an orphaned event clients can't correlate.
            _started_tool_call_ids: set[str] = set()

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Emit ``hermes.tool.progress`` with ``status: running``.

                Replaces the old ``tool_progress_callback("tool.started",
                ...)`` emit so SSE consumers receive a single event per
                tool start, carrying both the legacy ``tool``/``emoji``/
                ``label`` payload (for #6972 frontends) and the new
                ``toolCallId``/``status`` correlation fields (#16588).

                Skips tools whose names start with ``_`` so internal
                events (``_thinking``, …) stay off the wire — matching
                the prior ``_on_tool_progress`` filter exactly.
                """
                if not tool_call_id or function_name.startswith("_"):
                    return
                _started_tool_call_ids.add(tool_call_id)
                from agent.display import build_tool_preview, get_tool_emoji
                label = build_tool_preview(function_name, function_args) or function_name
                payload = _bounded_tool_progress_payload({
                    "tool": function_name,
                    "emoji": get_tool_emoji(function_name),
                    "label": label,
                    "toolCallId": tool_call_id,
                    "status": "running",
                    "arguments": function_args,
                })
                _stream_q.put(("__tool_progress__", payload))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Emit the matching ``status: completed`` event.

                Dropped if the start was filtered (internal tool, missing
                id, or never seen) so clients never get an orphaned
                ``completed`` they can't correlate to a prior ``running``.
                """
                if not tool_call_id or tool_call_id not in _started_tool_call_ids:
                    return
                _started_tool_call_ids.discard(tool_call_id)
                payload = _bounded_tool_progress_payload({
                    "tool": function_name,
                    "toolCallId": tool_call_id,
                    "status": "completed",
                    "arguments": function_args,
                    "result": function_result,
                })
                _stream_q.put(("__tool_progress__", payload))

            def _on_reasoning(text):
                """Forward incremental reasoning/thinking text to the SSE stream
                so OpenWebUI can render a live ``<details type="reasoning">``
                block. OpenWebUI's middleware natively consumes
                ``delta.reasoning_content`` (open-webui middleware.py:4117) so
                we just need to inject those chunks alongside the regular
                content deltas.
                """
                if text:
                    _stream_q.put(("__reasoning_delta__", text))

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            #
            # ``tool_progress_callback`` is intentionally not wired here:
            # it would duplicate every emit because ``run_agent`` fires it
            # side-by-side with ``tool_start_callback``/``tool_complete_callback``.
            # The structured callbacks are strictly richer (they carry the
            # tool_call id), so they own the chat-completions SSE channel.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                reasoning_callback=_on_reasoning,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
                user_id=user_id,
                chat_id=chat_id,
                user_name=user_name,
                user_role=scope.get("user_role", ""),
                user_groups=scope.get("user_groups", ""),
                granted_file_paths=granted_file_paths,
            ))
            # Ensure SSE drain loops can terminate without relying on polling
            # agent_task.done(), which can race with queue timeout checks.
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                user_id=user_id,
                chat_id=chat_id,
                user_name=user_name,
                user_role=scope.get("user_role", ""),
                user_groups=scope.get("user_groups", ""),
                granted_file_paths=granted_file_paths,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            # Per-user cache namespace: without this, two users sharing the
            # API key but sending the same Idempotency-Key would receive each
            # other's cached results.  Scope the cache key by user_id so the
            # idempotency guarantee is per-user, not global.
            scoped_idem_key = f"{user_id}:{idempotency_key}" if user_id else idempotency_key
            try:
                result, usage = await _idem_cache.get_or_set(scoped_idem_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response") or ""
        is_partial = bool(result.get("partial"))
        is_failed = bool(result.get("failed"))
        completed = bool(result.get("completed", True))
        err_msg = result.get("error")

        # Decide finish_reason. OpenAI uses "length" for truncation, "stop"
        # for normal completion, and downstream SDKs accept "error" / custom
        # codes. See issue #22496.
        if is_partial and err_msg and "truncat" in err_msg.lower():
            finish_reason = "length"
        elif is_failed or (not completed and err_msg):
            finish_reason = "error"
        else:
            finish_reason = "stop"

        response_headers = {
            "X-Hermes-Session-Id": result.get("session_id", session_id),
        }
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key

        # Hard-fail path: no usable assistant text AND a real failure → 5xx
        # with OpenAI-style error envelope so SDK clients raise instead of
        # silently rendering the internal failure string as message.content.
        if not final_response and (is_failed or is_partial):
            err_body = _openai_error(
                err_msg or "Agent run did not produce a response.",
                err_type="server_error",
                code="agent_incomplete",
            )
            err_body["error"]["hermes"] = {
                "completed": completed,
                "partial": is_partial,
                "failed": is_failed,
            }
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = "true" if is_partial else "false"
            return web.json_response(err_body, status=502, headers=response_headers)

        # Soft-partial path: we have *some* text but the run did not complete
        # (e.g. truncation with partial buffered output). Still 200 but signal
        # truncation via finish_reason="length" + Hermes-specific extras.
        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        if is_partial or is_failed or not completed:
            response_data["hermes"] = {
                "completed": completed,
                "partial": is_partial,
                "failed": is_failed,
                "error": err_msg,
                "error_code": "output_truncated" if finish_reason == "length" else "agent_error",
            }
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = "true" if is_partial else "false"
            if err_msg:
                response_headers["X-Hermes-Error"] = err_msg[:200]

        return web.json_response(response_data, headers=response_headers)

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
        gateway_session_key: str = None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            last_activity = time.monotonic()

            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
            last_activity = time.monotonic()

            # OpenWebUI compatibility state.  OpenWebUI's frontend ignores
            # custom SSE event names and only reads ``data:`` chunks, so we
            # need a second channel: tool-call lifecycle is mirrored as
            # inline ``<details type="tool_calls">`` HTML inside
            # ``delta.content`` (open-webui middleware.py:497-503), and
            # reasoning is forwarded as ``delta.reasoning_content`` which
            # OpenWebUI's backend natively converts to a live "Thinking…"
            # block (open-webui middleware.py:4117-4150).
            _owui_state = {"reasoning_open": False}
            import html as _html_mod

            def _render_tool_call_html(payload: Dict[str, Any]) -> str:
                """Build a ``<details type="tool_calls">`` block matching the
                exact attribute shape OpenWebUI's marked-extension expects
                (open-webui middleware.py:497 for done=true, :502 for
                done=false).
                """
                name = payload.get("tool", "") or ""
                call_id = payload.get("toolCallId", "") or ""
                status = payload.get("status", "")
                args = payload.get("arguments")
                args_str = (
                    args if isinstance(args, str)
                    else json.dumps(
                        args or {},
                        ensure_ascii=False,
                        default=str,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
                args_attr = _html_mod.escape(
                    json.dumps(args_str, ensure_ascii=False, allow_nan=False)
                )

                if status == "running":
                    return (
                        f'\n<details type="tool_calls" done="false" '
                        f'id="{_html_mod.escape(call_id)}" '
                        f'name="{_html_mod.escape(name)}" '
                        f'arguments="{args_attr}">\n'
                        f'<summary>Executing {_html_mod.escape(name)}…</summary>\n'
                        f'</details>\n'
                    )

                # status == "completed"
                raw_result = payload.get("result", "")
                result_str = (
                    raw_result if isinstance(raw_result, str)
                    else json.dumps(
                        raw_result,
                        ensure_ascii=False,
                        default=str,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
                if payload.get("resultTruncated"):
                    original_chars = payload.get("resultOriginalChars")
                    if isinstance(original_chars, int) and not isinstance(
                        original_chars,
                        bool,
                    ):
                        result_str += (
                            "\n…[truncated, full output is "
                            f"{original_chars} chars]"
                        )
                    else:
                        result_str += "\n…[truncated output]"
                result_body = _html_mod.escape(
                    json.dumps(
                        result_str,
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )
                return (
                    f'\n<details type="tool_calls" done="true" '
                    f'id="{_html_mod.escape(call_id)}" '
                    f'name="{_html_mod.escape(name)}" '
                    f'arguments="{args_attr}">\n'
                    f'<summary>Tool Executed</summary>\n{result_body}\n</details>\n'
                )

            def _encode_content_delta(text: str) -> bytes:
                """Encode the exact OpenAI ``delta.content`` bytes to be written."""
                content_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                return (
                    f"data: {json.dumps(content_chunk, ensure_ascii=False, default=str, allow_nan=False)}\n\n".encode()
                )

            async def _write_content_delta(text: str) -> None:
                """Send ``text`` using the shared checked content encoder."""
                await response.write(_encode_content_delta(text))

            def _encode_tool_progress_event(event_data: str) -> bytes:
                return (
                    f"event: hermes.tool.progress\ndata: {event_data}\n\n".encode()
                )

            def _max_physical_line_bytes(encoded_event: bytes) -> int:
                return max(
                    (len(line) for line in encoded_event.splitlines()),
                    default=0,
                )

            # Helper — route a queue item to the correct SSE event.
            async def _emit(item):
                """Write a single queue item to the SSE stream.

                Three queue item shapes:

                * ``("__tool_progress__", payload)`` — emitted in two
                  channels: (a) the legacy custom
                  ``event: hermes.tool.progress`` for native clients
                  (TUI/ACP, see #6972/#16588); (b) an inline
                  ``<details type="tool_calls">`` HTML block in
                  ``delta.content`` so OpenWebUI users see live tool
                  activity (its frontend drops custom SSE event names).
                * ``("__reasoning_delta__", text)`` — sent as a
                  ``delta.reasoning_content`` chunk; OpenWebUI converts
                  these to a streaming ``<details type="reasoning">``
                  block automatically.
                * Plain strings — standard ``delta.content`` chunks.
                """
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__tool_progress__":
                    payload = item[1]
                    # (a) legacy custom event for native clients — fires
                    # on BOTH running and completed so native UIs (TUI,
                    # ACP) can show live tool start.
                    event_data, wire_payload = _serialize_tool_progress_payload(payload)
                    custom_event_bytes = _encode_tool_progress_event(event_data)
                    html_event_bytes = None

                    if wire_payload.get("status") == "completed":
                        html_event_bytes = _encode_content_delta(
                            _render_tool_call_html(wire_payload)
                        )
                        html_line_bytes = _max_physical_line_bytes(html_event_bytes)
                        if html_line_bytes > TOOL_PROGRESS_SSE_LINE_MAX_BYTES:
                            custom_line_bytes = _max_physical_line_bytes(
                                custom_event_bytes
                            )
                            minimal_payload = _minimal_tool_progress_payload(
                                wire_payload
                            )
                            fallback_data, fallback_wire_payload = (
                                _serialize_tool_progress_payload(minimal_payload)
                            )
                            fallback_custom_event_bytes = (
                                _encode_tool_progress_event(fallback_data)
                            )
                            fallback_html_event_bytes = _encode_content_delta(
                                _render_tool_call_html(fallback_wire_payload)
                            )
                            fallback_custom_line_bytes = (
                                _max_physical_line_bytes(
                                    fallback_custom_event_bytes
                                )
                            )
                            fallback_html_line_bytes = _max_physical_line_bytes(
                                fallback_html_event_bytes
                            )
                            if (
                                fallback_custom_line_bytes
                                > TOOL_PROGRESS_SSE_LINE_MAX_BYTES
                                or fallback_html_line_bytes
                                > TOOL_PROGRESS_SSE_LINE_MAX_BYTES
                            ):
                                raise ValueError(
                                    "minimal completed tool-progress lifecycle "
                                    "payload exceeds SSE limit"
                                )
                            logger.warning(
                                "tool_progress_html_fallback status=%s "
                                "custom_line_bytes=%d html_line_bytes=%d "
                                "fallback_custom_line_bytes=%d "
                                "fallback_html_line_bytes=%d limit_bytes=%d",
                                fallback_wire_payload.get("status", ""),
                                custom_line_bytes,
                                html_line_bytes,
                                fallback_custom_line_bytes,
                                fallback_html_line_bytes,
                                TOOL_PROGRESS_SSE_LINE_MAX_BYTES,
                            )
                            wire_payload = fallback_wire_payload
                            custom_event_bytes = fallback_custom_event_bytes
                            html_event_bytes = fallback_html_event_bytes

                    # Completed custom + HTML bytes are chosen together above;
                    # never write the normal custom event before the HTML gate.
                    await response.write(custom_event_bytes)
                    # (b) inline HTML for OpenWebUI — fires ONLY on
                    # ``completed``.  We deliberately skip the running
                    # placeholder: OpenWebUI's marked-extension snapshots
                    # the ``done="false"`` attribute when the message
                    # text stabilises, so a placeholder followed by a
                    # completed block leaves the placeholder spinning
                    # forever ("Executing… 🌀") because content is
                    # append-only and we can never rewrite the prior
                    # ``done="false"`` to ``done="true"``.  Single
                    # ``done="true"`` block per tool gives a clean
                    # checkmark + result, matching the pattern Claude
                    # Desktop and the OpenAI Responses-API path use.
                    if wire_payload.get("status") == "completed":
                        # Any non-empty content delta implicitly closes
                        # a streaming reasoning block on OpenWebUI's
                        # side (middleware.py:4153-4179).
                        _owui_state["reasoning_open"] = False
                        assert html_event_bytes is not None
                        await response.write(html_event_bytes)
                elif isinstance(item, tuple) and len(item) == 2 and item[0] == "__reasoning_delta__":
                    text = item[1]
                    chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": text},
                            "finish_reason": None,
                        }],
                    }
                    await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    _owui_state["reasoning_open"] = True
                else:
                    # Plain content delta — close any open reasoning block
                    # first so OpenWebUI flips the "Thinking…" indicator
                    # to "Thought for N seconds" before the answer text
                    # arrives.
                    if _owui_state["reasoning_open"]:
                        _owui_state["reasoning_open"] = False
                    await _write_content_delta(item)
                return time.monotonic()

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_running_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # End of stream sentinel
                    break

                last_activity = await _emit(delta)

            # Get usage from completed agent
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            result = None
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception as exc:
                logger.warning("Agent task %s failed, usage data lost: %s", completion_id, exc)

            # M-U1-D all-exit A-channel: coverage suffix BEFORE stop/[DONE].
            # Production adapter (mutation target): emit_chat_completion_coverage_suffix
            try:
                suffix = emit_chat_completion_coverage_suffix(
                    result if isinstance(result, dict) else None
                )
                if suffix:
                    cov_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": suffix},
                                "finish_reason": None,
                            }
                        ],
                    }
                    await response.write(
                        f"data: {json.dumps(cov_chunk)}\n\n".encode()
                    )
            except Exception as _cov_emit_err:
                logger.warning(
                    "coverage suffix emit failed for %s: %s",
                    completion_id,
                    _cov_emit_err,
                )

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected mid-stream.  Interrupt the agent so it
            # stops making LLM API calls at the next loop iteration, then
            # cancel the asyncio task wrapper.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)
        except Exception as _exc:
            # Agent crashed mid-stream.  Try to emit an error chunk
            # so the client gets a proper response instead of a
            # TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            logger.error("Agent crashed mid-stream for %s: %s", completion_id, _tb.format_exc()[:300])
            try:
                error_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
                await response.write(f"data: {json.dumps(error_chunk)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
            except Exception:
                pass

        return response

    async def _write_sse_responses(
        self,
        request: "web.Request",
        response_id: str,
        model: str,
        created_at: int,
        stream_q,
        agent_task,
        agent_ref,
        conversation_history: List[Dict[str, str]],
        user_message: str,
        instructions: Optional[str],
        conversation: Optional[str],
        store: bool,
        session_id: str,
        gateway_session_key: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> "web.StreamResponse":
        """Write an SSE stream for POST /v1/responses (OpenAI Responses API).

        Emits spec-compliant event types as the agent runs:

        - ``response.created`` — initial envelope (status=in_progress)
        - ``response.output_text.delta`` / ``response.output_text.done`` —
          streamed assistant text
        - ``response.output_item.added`` / ``response.output_item.done``
          with ``item.type == "function_call"`` — when the agent invokes a
          tool (both events fire; the ``done`` event carries the finalized
          ``arguments`` string)
        - ``response.output_item.added`` with
          ``item.type == "function_call_output"`` — tool result with
          ``{call_id, output, status}``
        - ``response.completed`` — terminal event carrying the full
          response object with all output items + usage (same payload
          shape as the non-streaming path for parity)
        - ``response.failed`` — terminal event on agent error

        If the client disconnects mid-stream, ``agent.interrupt()`` is
        called so the agent stops issuing upstream LLM calls, then the
        asyncio task is cancelled.  When ``store=True`` an initial
        ``in_progress`` snapshot is persisted immediately after
        ``response.created`` and disconnects update it to an
        ``incomplete`` snapshot so GET /v1/responses/{id} and
        ``previous_response_id`` chaining still have something to
        recover from.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # State accumulated during the stream
        final_text_parts: List[str] = []
        # Track open function_call items by name so we can emit a matching
        # ``done`` event when the tool completes.  Order preserved.
        pending_tool_calls: List[Dict[str, Any]] = []
        # Output items we've emitted so far (used to build the terminal
        # response.completed payload).  Kept in the order they appeared.
        emitted_items: List[Dict[str, Any]] = []
        # Monotonic counter for output_index (spec requires it).
        output_index = 0
        # Monotonic counter for call_id generation if the agent doesn't
        # provide one (it doesn't, from tool_progress_callback).
        call_counter = 0
        # Canonical Responses SSE events include a monotonically increasing
        # sequence_number. Add it server-side for every emitted event so
        # clients that validate the OpenAI event schema can parse our stream.
        sequence_number = 0
        # Track the assistant message item id + content index for text
        # delta events — the spec ties deltas to a specific item.
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_output_index: Optional[int] = None
        message_opened = False

        async def _write_event(event_type: str, data: Dict[str, Any]) -> None:
            nonlocal sequence_number
            if "sequence_number" not in data:
                data["sequence_number"] = sequence_number
            sequence_number += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(payload.encode())

        def _envelope(status: str) -> Dict[str, Any]:
            env: Dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "status": status,
                "created_at": created_at,
                "model": model,
            }
            return env

        final_response_text = ""
        agent_error: Optional[str] = None
        usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        terminal_snapshot_persisted = False

        def _persist_response_snapshot(
            response_env: Dict[str, Any],
            *,
            conversation_history_snapshot: Optional[List[Dict[str, Any]]] = None,
        ) -> None:
            if not store:
                return
            if conversation_history_snapshot is None:
                conversation_history_snapshot = list(conversation_history)
                conversation_history_snapshot.append({"role": "user", "content": user_message})
            self._response_store.put(response_id, {
                "response": response_env,
                "conversation_history": conversation_history_snapshot,
                "instructions": instructions,
                "session_id": session_id,
                "user_id": user_id,
            })
            if conversation:
                self._response_store.set_conversation(conversation, response_id, user_id=user_id)

        def _persist_incomplete_if_needed() -> None:
            """Persist an ``incomplete`` snapshot if no terminal one was written.

            Called from both the client-disconnect (``ConnectionResetError``)
            and server-cancellation (``asyncio.CancelledError``) paths so
            GET /v1/responses/{id} and ``previous_response_id`` chaining keep
            working after abrupt stream termination.
            """
            if not store or terminal_snapshot_persisted:
                return
            incomplete_text = "".join(final_text_parts) or final_response_text
            incomplete_items: List[Dict[str, Any]] = list(emitted_items)
            if incomplete_text:
                incomplete_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": incomplete_text}],
                })
            incomplete_env = _envelope("incomplete")
            incomplete_env["output"] = incomplete_items
            incomplete_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            incomplete_history = list(conversation_history)
            incomplete_history.append({"role": "user", "content": user_message})
            if incomplete_text:
                incomplete_history.append({"role": "assistant", "content": incomplete_text})
            _persist_response_snapshot(
                incomplete_env,
                conversation_history_snapshot=incomplete_history,
            )

        try:
            # response.created — initial envelope, status=in_progress
            created_env = _envelope("in_progress")
            created_env["output"] = []
            await _write_event("response.created", {
                "type": "response.created",
                "response": created_env,
            })
            _persist_response_snapshot(created_env)
            last_activity = time.monotonic()

            async def _open_message_item() -> None:
                """Emit response.output_item.added for the assistant message
                the first time any text delta arrives."""
                nonlocal message_opened, message_output_index, output_index
                if message_opened:
                    return
                message_opened = True
                message_output_index = output_index
                output_index += 1
                item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": message_output_index,
                    "item": item,
                })

            async def _emit_text_delta(delta_text: str) -> None:
                await _open_message_item()
                final_text_parts.append(delta_text)
                await _write_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "delta": delta_text,
                    "logprobs": [],
                })

            async def _emit_tool_started(payload: Dict[str, Any]) -> str:
                """Emit response.output_item.added for a function_call.

                Returns the call_id so the matching completion event can
                reference it.  Prefer the real ``tool_call_id`` from the
                agent when available; fall back to a generated call id for
                safety in tests or older code paths.
                """
                nonlocal output_index, call_counter
                call_counter += 1
                call_id = payload.get("tool_call_id") or f"call_{response_id[5:]}_{call_counter}"
                args = payload.get("arguments", {})
                if isinstance(args, dict):
                    arguments_str = json.dumps(args)
                else:
                    arguments_str = str(args)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": payload.get("name", ""),
                    "call_id": call_id,
                    "arguments": arguments_str,
                }
                idx = output_index
                output_index += 1
                pending_tool_calls.append({
                    "call_id": call_id,
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "item_id": item["id"],
                    "output_index": idx,
                })
                emitted_items.append({
                    "type": "function_call",
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "call_id": call_id,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                })
                return call_id

            async def _emit_tool_completed(payload: Dict[str, Any]) -> None:
                """Emit response.output_item.done (function_call) followed
                by response.output_item.added (function_call_output)."""
                nonlocal output_index
                call_id = payload.get("tool_call_id")
                result = payload.get("result", "")
                pending = None
                if call_id:
                    for i, p in enumerate(pending_tool_calls):
                        if p["call_id"] == call_id:
                            pending = pending_tool_calls.pop(i)
                            break
                if pending is None:
                    # Completion without a matching start — skip to avoid
                    # emitting orphaned done events.
                    return

                # function_call done
                done_item = {
                    "id": pending["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": pending["name"],
                    "call_id": pending["call_id"],
                    "arguments": pending["arguments"],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": pending["output_index"],
                    "item": done_item,
                })

                # function_call_output added (result)
                result_str = result if isinstance(result, str) else json.dumps(result)
                output_parts = [{"type": "input_text", "text": result_str}]
                output_item = {
                    "id": f"fco_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                    "status": "completed",
                }
                idx = output_index
                output_index += 1
                emitted_items.append({
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": output_item,
                })
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": output_item,
                })

            # Main drain loop — thread-safe queue fed by agent callbacks.
            async def _dispatch(it) -> None:
                """Route a queue item to the correct SSE emitter.

                Plain strings are text deltas — they are batched (50ms)
                to reduce Open WebUI re-render storms.  Tagged tuples
                with ``__tool_started__`` / ``__tool_completed__``
                prefixes are tool lifecycle events and flush the buffer
                before emitting.
                """
                nonlocal _batch_timer
                if isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str):
                    tag, payload = it
                    # Flush batched text before tool events
                    if _batch_buf:
                        await _flush_batch()
                    if tag == "__tool_started__":
                        await _emit_tool_started(payload)
                    elif tag == "__tool_completed__":
                        await _emit_tool_completed(payload)
                elif isinstance(it, str):
                    # Batch text deltas — append to buffer, flush on timer
                    _batch_buf.append(it)
                    if _batch_timer is None:
                        _batch_timer = asyncio.create_task(_batch_flush_after(0.05))
                # Other types are silently dropped.

            # ── Batching state ──
            _batch_buf: List[str] = []
            _batch_timer: Optional[asyncio.Task] = None
            _batch_lock = asyncio.Lock()

            async def _batch_flush_after(delay: float) -> None:
                """Wait delay seconds, then flush accumulated text deltas."""
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                # Clear timer reference BEFORE flush so new deltas
                # can start a fresh timer while we emit
                nonlocal _batch_buf, _batch_timer
                _batch_timer = None
                await _flush_batch()

            async def _flush_batch() -> None:
                """Emit a single SSE delta for all accumulated text."""
                nonlocal _batch_buf
                async with _batch_lock:
                    if _batch_buf:
                        combined = "".join(_batch_buf)
                        _batch_buf = []
                        await _emit_text_delta(combined)

            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain remaining
                        while True:
                            try:
                                item = stream_q.get_nowait()
                                if item is None:
                                    break
                                await _dispatch(item)
                                last_activity = time.monotonic()
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if item is None:  # EOS sentinel
                    # Cancel pending timer and flush remaining batched text
                    if _batch_timer and not _batch_timer.done():
                        _batch_timer.cancel()
                        _batch_timer = None
                    if _batch_buf:
                        await _flush_batch()
                    break

                await _dispatch(item)
                last_activity = time.monotonic()

            # Flush any final batched text before processing result
            if _batch_buf:
                await _flush_batch()

            # Pick up agent result + usage from the completed task
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                # If the agent produced a final_response but no text
                # deltas were streamed (e.g. some providers only emit
                # the full response at the end), emit a single fallback
                # delta so Responses clients still receive a live text part.
                agent_final = result.get("final_response", "") if isinstance(result, dict) else ""
                if agent_final and not final_text_parts:
                    await _emit_text_delta(agent_final)
                    final_text_parts.append(agent_final)
                if agent_final and not final_response_text:
                    final_response_text = agent_final
                if isinstance(result, dict) and result.get("error") and not final_response_text:
                    agent_error = result["error"]
                # M-U1-D all-exit A-channel: coverage after model text.
                try:
                    streamed_so_far = "".join(final_text_parts) or final_response_text or ""
                    cov_suffix = emit_responses_coverage_suffix(
                        streamed_so_far,
                        result if isinstance(result, dict) else None,
                    )
                    if cov_suffix:
                        await _emit_text_delta(cov_suffix)
                        final_text_parts.append(cov_suffix)
                except Exception as _cov_err:
                    logger.warning("responses stream coverage emit failed: %s", _cov_err)
            except Exception as e:  # noqa: BLE001
                logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
                agent_error = str(e)

            # Close the message item if it was opened
            final_response_text = "".join(final_text_parts) or final_response_text
            if message_opened:
                await _write_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": final_response_text,
                    "logprobs": [],
                })
                msg_done_item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": final_response_text}
                    ],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": message_output_index,
                    "item": msg_done_item,
                })

            # Always append a final message item in the completed
            # response envelope so clients that only parse the terminal
            # payload still see the assistant text.  This mirrors the
            # shape produced by _extract_output_items in the batch path.
            final_items: List[Dict[str, Any]] = list(emitted_items)

            # Trim large content from tool call arguments to keep the
            # response.completed event under ~100KB.  Clients already
            # received full details via incremental events.
            for _item in final_items:
                if _item.get("type") == "function_call":
                    try:
                        _args = json.loads(_item.get("arguments", "{}")) if isinstance(_item.get("arguments"), str) else _item.get("arguments", {})
                        if isinstance(_args, dict):
                            for _k in ("content", "query", "pattern", "old_string", "new_string"):
                                if isinstance(_args.get(_k), str) and len(_args[_k]) > 500:
                                    _args[_k] = "[" + str(len(_args[_k])) + " chars — truncated for response.completed]"
                            _item["arguments"] = json.dumps(_args)
                    except Exception:
                        pass
                elif _item.get("type") == "function_call_output":
                    _output = _item.get("output", [])
                    if isinstance(_output, list) and _output:
                        _first = _output[0]
                        if isinstance(_first, dict) and _first.get("type") == "input_text":
                            _text = _first.get("text", "")
                            if len(_text) > 1000:
                                _first["text"] = _text[:500] + "...[" + str(len(_text) - 500) + " more chars]"
                                _item["output"] = [_first]

            final_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": final_response_text or (agent_error or "")}
                ],
            })

            if agent_error:
                failed_env = _envelope("failed")
                failed_env["output"] = final_items
                failed_env["error"] = {"message": agent_error, "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                _failed_history = list(conversation_history)
                _failed_history.append({"role": "user", "content": user_message})
                if final_response_text or agent_error:
                    _failed_history.append({
                        "role": "assistant",
                        "content": final_response_text or agent_error,
                    })
                _persist_response_snapshot(
                    failed_env,
                    conversation_history_snapshot=_failed_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            else:
                completed_env = _envelope("completed")
                completed_env["output"] = final_items
                completed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                full_history = self._build_response_conversation_history(
                    conversation_history,
                    user_message,
                    result,
                    final_response_text,
                )
                _persist_response_snapshot(
                    completed_env,
                    conversation_history_snapshot=full_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.completed", {
                    "type": "response.completed",
                    "response": completed_env,
                })

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            _persist_incomplete_if_needed()
            # Client disconnected — interrupt the agent so it stops
            # making upstream LLM calls, then cancel the task.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)
        except asyncio.CancelledError:
            # Server-side cancellation (e.g. shutdown, request timeout) —
            # persist an incomplete snapshot so GET /v1/responses/{id} and
            # previous_response_id chaining still work, then re-raise so the
            # runtime's cancellation semantics are respected.
            _persist_incomplete_if_needed()
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE task cancelled")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
            logger.info("SSE task cancelled; persisted incomplete snapshot for %s", response_id)
            raise
        except Exception as _exc:
            # Agent crashed with an unhandled error (e.g. model API error like
            # BadRequestError, AuthenticationError).  Emit a response.failed
            # event and properly terminate the SSE stream so the client doesn't
            # get a TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            _persist_incomplete_if_needed()
            agent_error = _tb.format_exc()
            try:
                failed_env = _envelope("failed")
                failed_env["output"] = list(emitted_items)
                failed_env["error"] = {"message": str(_exc)[:500], "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            except Exception:
                pass
            logger.error("Agent crashed mid-stream for %s: %s", response_id, str(agent_error)[:300])

        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = _coerce_request_bool(body.get("store"), default=True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id (scoped per user
        # so two different end-users using the same `conversation` name
        # cannot read each other's chain).
        if conversation:
            previous_response_id = self._response_store.get_conversation(
                conversation, user_id=scope["user_id"]
            )
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    try:
                        content = _normalize_multimodal_content(item.get("content", ""))
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Accept explicit conversation_history from the request body.
        # This lets stateless clients supply their own history instead of
        # relying on server-side response chaining via previous_response_id.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, Any]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                try:
                    entry_content = _normalize_multimodal_content(entry["content"])
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                conversation_history.append({"role": str(entry["role"]), "content": entry_content})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            # Verify the stored response belongs to the requesting user.
            # Without this check, any client with the API key could chain
            # off of another user's response_id and harvest the prior
            # conversation_history.  Treat ownership mismatch as not-found
            # so we don't reveal that the response_id exists.
            stored_user = stored.get("user_id", "")
            if stored_user and stored_user != scope["user_id"]:
                logger.warning(
                    "previous_response_id %s belongs to user %r but request is from %r — denying",
                    previous_response_id, stored_user, scope["user_id"],
                )
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message: Any = input_messages[-1].get("content", "") if input_messages else ""
        granted_file_paths: List[str] = []
        user_message = _augment_message_with_handoff_context(
            user_message,
            scope,
            granted_file_paths,
        )
        if not _content_has_visible_payload(user_message):
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # Reuse session from previous_response_id chain so the dashboard
        # groups the entire conversation under one session entry.  Apply
        # user/chat scope (idempotent — _scope_session_id() doesn't double-
        # append if the suffix is already present, so chaining preserves
        # continuity for the same user).
        session_id = _scope_session_id(stored_session_id or str(uuid.uuid4()), scope)
        user_id = scope["user_id"]
        chat_id = scope.get("chat_id", "")
        user_name = scope["user_name"]
        self._touch_session_activity(session_id, scope)

        stream = _coerce_request_bool(body.get("stream"), default=False)
        if stream:
            # Streaming branch — emit OpenAI Responses SSE events as the
            # agent runs so frontends can render text deltas and tool
            # calls in real time.  See _write_sse_responses for details.
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # None from the agent is a CLI box-close signal, not EOS.
                # Forwarding would kill the SSE stream prematurely; the
                # SSE writer detects completion via agent_task.done().
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Queue non-start tool progress events if needed in future.

                The structured Responses stream uses ``tool_start_callback``
                and ``tool_complete_callback`` for exact call-id correlation,
                so progress events are currently ignored here.
                """
                return

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Queue a started tool for live function_call streaming."""
                _stream_q.put(("__tool_started__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Queue a completed tool result for live function_call_output streaming."""
                _stream_q.put(("__tool_completed__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                    "result": function_result,
                }))

            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
                user_id=user_id,
                chat_id=chat_id,
                user_name=user_name,
                user_role=scope.get("user_role", ""),
                user_groups=scope.get("user_groups", ""),
                granted_file_paths=granted_file_paths,
            ))
            # Ensure SSE drain loops can terminate without relying on polling
            # agent_task.done(), which can race with queue timeout checks.
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            response_id = f"resp_{uuid.uuid4().hex[:28]}"
            model_name = body.get("model", self._model_name)
            created_at = int(time.time())

            return await self._write_sse_responses(
                request=request,
                response_id=response_id,
                model=model_name,
                created_at=created_at,
                stream_q=_stream_q,
                agent_task=agent_task,
                agent_ref=agent_ref,
                conversation_history=conversation_history,
                user_message=user_message,
                instructions=instructions,
                conversation=conversation,
                store=store,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                user_id=user_id,
            )

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                user_id=user_id,
                chat_id=chat_id,
                user_name=user_name,
                user_role=scope.get("user_role", ""),
                user_groups=scope.get("user_groups", ""),
                granted_file_paths=granted_file_paths,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            scoped_idem_key = f"{user_id}:{idempotency_key}" if user_id else idempotency_key
            try:
                result, usage = await _idem_cache.get_or_set(scoped_idem_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = self._build_response_conversation_history(
            conversation_history,
            user_message,
            result,
            final_response,
        )

        # Build output items from the current turn only.  AIAgent returns a
        # full transcript in result["messages"], while older/mocked paths may
        # return only the current turn suffix.
        output_start_index = self._response_messages_turn_start_index(
            conversation_history,
            user_message,
            result,
        )
        output_items = self._extract_output_items(result, start_index=output_start_index)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", self._model_name),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
                "session_id": session_id,
                "user_id": user_id,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response.
            # Per-user scope: same conversation name from two end-users
            # gets two independent pointers.
            if conversation:
                self._response_store.set_conversation(conversation, response_id, user_id=user_id)

        response_headers = {"X-Hermes-Session-Id": session_id}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(response_data, headers=response_headers)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        # Owner check — return 404 (not 403) on mismatch so we don't leak the
        # existence of another user's response_id.
        stored_user = stored.get("user_id", "")
        if stored_user and stored_user != scope["user_id"]:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        response_id = request.match_info["response_id"]
        # Read-then-check-then-delete to enforce ownership.  Race window is
        # acceptable: no security impact, the worst case is a concurrent
        # delete by the same user winning twice.
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)
        stored_user = stored.get("user_id", "")
        if stored_user and stored_user != scope["user_id"]:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    @staticmethod
    def _check_jobs_available() -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not _CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            logger.warning(
                "Cron jobs API rejected invalid job_id %r: %s",
                job_id,
                self._request_audit_log_suffix(request),
            )
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs — list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = _cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs — create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if prompt and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
                "origin": self._cron_origin_from_request(request),
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = _cron_create(**kwargs)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} — get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} — update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if sanitized.get("prompt") and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(sanitized["prompt"])
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            job = _cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} — delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = _cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause — pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume — resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run — trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_cron_fire(self, request: "web.Request") -> "web.Response":
        """POST /api/cron/fire — Chronos managed-cron fire webhook (NAS → agent).

        Authenticated by a NAS-minted JWT (verified via the pluggable
        fire-verifier), NOT API_SERVER_KEY — NAS holds no API server key, and
        this is the only inbound that can trigger remote job execution, so it
        gets its own purpose-scoped token check.

        Returns 202 + runs the job in the background so a long agent turn never
        trips NAS's HTTP timeout. The store CAS claim inside fire_due guards
        against double-fire on a NAS/scheduler retry.
        """
        from hermes_cli.config import cfg_get, load_config
        from plugins.cron.chronos.verify import get_fire_verifier

        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""

        cfg = load_config()
        claims = get_fire_verifier()(
            token=token,
            expected_audience=cfg_get(cfg, "cron", "chronos", "expected_audience", default=""),
            jwks_or_key=cfg_get(cfg, "cron", "chronos", "nas_jwks_url", default="") or None,
            issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None,
        )
        if claims is None:
            logger.warning(
                "cron fire: rejected invalid token: %s",
                self._request_audit_log_suffix(request),
            )
            return web.json_response({"error": "invalid fire token"}, status=401)

        try:
            body = await request.json()
        except Exception:
            body = {}
        job_id = (body or {}).get("job_id")
        if not job_id:
            return web.json_response({"error": "missing job_id"}, status=400)

        from cron.scheduler_provider import resolve_cron_scheduler
        provider = resolve_cron_scheduler()

        loop = asyncio.get_running_loop()
        # Fire in the background (202 immediately). fire_due claims via the
        # store CAS, so a retry while this is in flight is de-duped.
        task = asyncio.create_task(
            asyncio.to_thread(provider.fire_due, job_id, adapters=None, loop=loop)
        )
        try:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except (TypeError, AttributeError):
            pass

        return web.json_response({"status": "accepted", "job_id": job_id}, status=202)


    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_response_conversation_history(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
        final_response: Any,
    ) -> List[Dict[str, Any]]:
        """Build the stored Responses transcript without duplicating history."""
        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        agent_messages = result.get("messages") if isinstance(result, dict) else None

        if isinstance(agent_messages, list) and agent_messages:
            turn_start = APIServerAdapter._response_messages_turn_start_index(
                conversation_history,
                user_message,
                result,
            )
            if turn_start:
                return list(agent_messages)

            full_history = prior
            full_history.append(current_user)
            full_history.extend(agent_messages)
            return full_history

        full_history = prior
        full_history.append(current_user)
        full_history.append({"role": "assistant", "content": final_response})
        return full_history

    @staticmethod
    def _response_messages_turn_start_index(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> int:
        """Detect transcript-shaped result["messages"] and return turn start."""
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return 0

        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        expected_prefix = prior + [current_user]
        if agent_messages[:len(expected_prefix)] == expected_prefix:
            return len(expected_prefix)
        if prior and agent_messages[:len(prior)] == prior:
            return len(prior)
        return 0

    @classmethod
    def _turn_transcript_messages(
        cls,
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return this turn's assistant/tool messages in client-safe shape.

        The streaming SSE contract delivers all assistant text as
        ``assistant.delta`` events under one ``message_id`` interleaved with
        ``tool.*`` events, and a single ``assistant.completed`` carrying only
        the final reply.  A client that accumulates deltas into one buffer
        cannot reconstruct *intermediate* assistant text segments that preceded
        tool calls — so when the page is re-opened mid/post-stream those
        segments appear lost, even though state.db persisted them correctly.

        Emitting the authoritative per-turn transcript on ``run.completed`` lets
        any SSE consumer reconcile its live view against ground truth without a
        separate ``GET /messages`` round-trip.  Purely additive: clients that
        ignore the field are unaffected.  Refs #34703.
        """
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return []
        start = cls._response_messages_turn_start_index(
            conversation_history, user_message, result
        )
        turn = agent_messages[start:]
        out: List[Dict[str, Any]] = []
        for msg in turn:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in {"assistant", "tool"}:
                continue
            out.append(cls._message_response(msg))
        return out

    @staticmethod
    def _extract_output_items(result: Dict[str, Any], start_index: int = 0) -> List[Dict[str, Any]]:
        """
        Build the output item array from the agent's messages.

        Walks *result["messages"]* starting at *start_index* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])
        if start_index > 0:
            messages = messages[start_index:]

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        reasoning_callback=None,
        agent_ref: Optional[list] = None,
        gateway_session_key: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_name: Optional[str] = None,
        user_role: Optional[str] = None,
        user_groups: Optional[str] = None,
        granted_file_paths: Optional[List[str]] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        loop = asyncio.get_running_loop()

        def _run():
            from gateway.session_context import clear_session_vars, set_session_vars
            from tools.local_document_export_tool import (
                reset_trusted_export_context,
                set_trusted_export_context,
            )
            from tools.file_grants import file_grant_scope, make_file_handles
            from tools.request_file_cache import request_file_cache_scope

            # Bind OpenWebUI identity (incl. role/groups) into concurrency-safe
            # session contextvars so skill ACL checks in tool handlers can read
            # the caller's scope.  Real values are passed (not defaults) so the
            # chat-completions path no longer falls back to stale os.environ.
            # set_session_vars is not stack-safe, so upstream's session-id bind
            # and the kg ACL identity bind are merged into ONE call (a second
            # call would reset session_id to "" — losing upstream's binding).
            tokens = set_session_vars(
                platform="api_server",
                chat_id=chat_id or session_id or "",
                user_id=user_id or "",
                user_name=user_name or "",
                session_key=gateway_session_key or session_id or "",
                session_id=session_id or "",
                user_role=user_role or "",
                user_groups=user_groups or "",
            )
            export_context_token = set_trusted_export_context(
                {
                    "platform": "api_server",
                    "user_id": user_id or "",
                    "chat_id": chat_id or "",
                    "session_id": session_id or "",
                    "gateway_session_key": gateway_session_key or "",
                }
            )
            try:
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=stream_delta_callback,
                    tool_progress_callback=tool_progress_callback,
                    tool_start_callback=tool_start_callback,
                    tool_complete_callback=tool_complete_callback,
                    gateway_session_key=gateway_session_key,
                    reasoning_callback=reasoning_callback,
                    user_id=user_id,
                    user_role=user_role,
                    user_groups=user_groups,
                )
                if agent_ref is not None:
                    agent_ref[0] = agent
                effective_task_id = session_id or str(uuid.uuid4())
                if granted_file_paths is None:
                    result = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id=effective_task_id,
                    )
                else:
                    # Handles are positional over the same list the
                    # <attached_files> block numbered, so F01 is entry 1.
                    # One request scope: the grant list authorises paths, the
                    # read memo stops identical re-reads re-inserting full text.
                    with file_grant_scope(
                        effective_task_id,
                        granted_file_paths,
                        handles=make_file_handles(granted_file_paths),
                    ), request_file_cache_scope(effective_task_id):
                        result = agent.run_conversation(
                            user_message=user_message,
                            conversation_history=conversation_history,
                            task_id=effective_task_id,
                        )
                usage = {
                    "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                    "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                }
                # Include the effective session ID in the result so callers
                # (e.g. X-Hermes-Session-Id header) can track compression-
                # triggered session rotations. (#16938)
                _eff_sid = getattr(agent, "session_id", session_id)
                if isinstance(_eff_sid, str) and _eff_sid:
                    result["session_id"] = _eff_sid
                return result, usage
            finally:
                try:
                    clear_session_vars(tokens)
                finally:
                    reset_trusted_export_context(export_context_token)

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _MAX_CONCURRENT_RUNS = 10  # Prevent unbounded resource allocation
    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept
    _RUN_STATUS_TTL = 3600  # seconds to retain terminal run status for polling

    def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        """Update pollable run status without exposing private agent objects."""
        now = time.time()
        current = self._run_statuses.get(run_id, {})
        current.update({
            "object": "hermes.run",
            "run_id": run_id,
            "status": status,
            "updated_at": now,
        })
        current.setdefault("created_at", fields.pop("created_at", now))
        current.update(fields)
        self._run_statuses[run_id] = current
        return current

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            self._set_run_status(
                run_id,
                self._run_statuses.get(run_id, {}).get("status", "running"),
                last_event=event.get("event"),
            )
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded

        return _callback

    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        # Enforce concurrency limit
        if len(self._run_streams) >= self._MAX_CONCURRENT_RUNS:
            return web.json_response(
                _openai_error(f"Too many concurrent runs (max {self._MAX_CONCURRENT_RUNS})", code="rate_limit_exceeded"),
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        granted_file_paths: List[str] = []
        user_message = _augment_message_with_handoff_context(
            user_message,
            scope,
            granted_file_paths,
        )
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                # Owner check: don't carry another user's history into this run.
                stored_user = stored.get("user_id", "")
                if stored_user and stored_user != scope["user_id"]:
                    logger.warning(
                        "previous_response_id %s belongs to user %r but request is from %r — denying",
                        previous_response_id, stored_user, scope["user_id"],
                    )
                    return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
                conversation_history = list(stored.get("conversation_history", []))
                stored_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        user_id = scope["user_id"]
        chat_id = scope.get("chat_id", "")

        # Sanitize the optional client-supplied session_id so a hostile caller
        # can't inject control characters or chase down another user's run.
        raw_body_session = body.get("session_id")
        body_session_id = ""
        if isinstance(raw_body_session, str) and raw_body_session:
            if re.search(r"[\r\n\x00]", raw_body_session):
                return web.json_response(_openai_error("Invalid session_id"), status=400)
            body_session_id = raw_body_session.strip()[:256]

        run_id = f"run_{uuid.uuid4().hex}"
        # Always run user/chat scope through _scope_session_id so that whether
        # the base came from the client, a stored response, or the auto-generated
        # run_id, it ends with -user-<id>(-chat-<id>).
        session_id = _scope_session_id(body_session_id or stored_session_id or run_id, scope)
        approval_session_key = gateway_session_key or session_id or run_id
        self._touch_session_activity(session_id, scope)
        ephemeral_system_prompt = instructions
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        created_at = time.time()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = created_at
        self._run_approval_sessions[run_id] = approval_session_key

        event_cb = self._make_run_event_callback(run_id, loop)

        # Also wire stream_delta_callback so message.delta events flow through.
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        self._set_run_status(
            run_id,
            "queued",
            created_at=created_at,
            session_id=session_id,
            model=body.get("model", self._model_name),
        )

        async def _run_and_close():
            try:
                self._set_run_status(run_id, "running")
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    gateway_session_key=gateway_session_key,
                    user_id=user_id,
                    user_role=scope.get("user_role", ""),
                    user_groups=scope.get("user_groups", ""),
                )
                self._active_run_agents[run_id] = agent

                def _approval_notify(approval_data: Dict[str, Any]) -> None:
                    event = dict(approval_data or {})
                    event.update({
                        "event": "approval.request",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "choices": ["once", "session", "always", "deny"],
                    })
                    self._set_run_status(
                        run_id,
                        "waiting_for_approval",
                        last_event="approval.request",
                    )
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass

                def _run_sync():
                    from gateway.session_context import clear_session_vars, set_session_vars
                    from tools.local_document_export_tool import (
                        reset_trusted_export_context,
                        set_trusted_export_context,
                    )
                    from tools.file_grants import file_grant_scope, make_file_handles
                    from tools.request_file_cache import request_file_cache_scope
                    from tools.approval import (
                        register_gateway_notify,
                        reset_current_session_key,
                        set_current_session_key,
                        unregister_gateway_notify,
                    )

                    effective_task_id = session_id or run_id
                    approval_token = None
                    export_context_token = None
                    session_tokens = []
                    try:
                        # Bind approval/session identity for this API run via
                        # contextvars so concurrent runs do not share process
                        # environment state.
                        approval_token = set_current_session_key(approval_session_key)
                        export_context_token = set_trusted_export_context(
                            {
                                "platform": "api_server",
                                "user_id": user_id or "",
                                "chat_id": chat_id or "",
                                "session_id": session_id or "",
                                "gateway_session_key": gateway_session_key or "",
                            }
                        )
                        session_tokens = set_session_vars(
                            platform="api_server",
                            chat_id=chat_id or "",
                            user_id=user_id or "",
                            user_name=scope.get("user_name", ""),
                            session_key=approval_session_key,
                            user_role=scope.get("user_role", ""),
                            user_groups=scope.get("user_groups", ""),
                        )
                        register_gateway_notify(approval_session_key, _approval_notify)
                        # Handles are positional over the same list the
                        # <attached_files> block numbered, so F01 is entry 1.
                        # One request scope: the grant list authorises paths, the
                        # read memo stops identical re-reads re-inserting full text.
                        with file_grant_scope(
                            effective_task_id,
                            granted_file_paths,
                            handles=make_file_handles(granted_file_paths),
                        ), request_file_cache_scope(effective_task_id):
                            r = agent.run_conversation(
                                user_message=user_message,
                                conversation_history=conversation_history,
                                task_id=effective_task_id,
                            )
                    finally:
                        try:
                            unregister_gateway_notify(approval_session_key)
                        finally:
                            if approval_token is not None:
                                try:
                                    reset_current_session_key(approval_token)
                                except Exception:
                                    pass
                            if session_tokens:
                                try:
                                    clear_session_vars(session_tokens)
                                except Exception:
                                    pass
                            if export_context_token is not None:
                                try:
                                    reset_trusted_export_context(export_context_token)
                                except Exception:
                                    pass
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                # Check for structured failure (non-retryable client errors like
                # 401/400 return failed=True instead of raising, so the except
                # block below never fires — issue #15561).
                if isinstance(result, dict) and result.get("failed"):
                    error_msg = result.get("error") or "agent run failed"
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": error_msg,
                    })
                    self._set_run_status(
                        run_id,
                        "failed",
                        error=error_msg,
                        last_event="run.failed",
                    )
                else:
                    final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                    q.put_nowait({
                        "event": "run.completed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "output": final_response,
                        "usage": usage,
                    })
                    self._set_run_status(
                        run_id,
                        "completed",
                        output=final_response,
                        usage=usage,
                        last_event="run.completed",
                    )
            except asyncio.CancelledError:
                self._set_run_status(
                    run_id,
                    "cancelled",
                    last_event="run.cancelled",
                )
                try:
                    q.put_nowait({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                self._set_run_status(
                    run_id,
                    "failed",
                    error=str(exc),
                    last_event="run.failed",
                )
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                # If the asyncio wrapper is cancelled (for example via
                # /stop), the executor thread can still be blocked waiting
                # on an approval Event.  Unregistering here releases those
                # waits immediately; the in-thread unregister is harmlessly
                # idempotent on normal completion.
                try:
                    from tools.approval import unregister_gateway_notify

                    unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                # Sentinel: signal SSE stream to close
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)

        task = asyncio.create_task(_run_and_close())
        self._active_run_tasks[run_id] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        response_headers = (
            {"X-Hermes-Session-Key": gateway_session_key} if gateway_session_key else {}
        )
        return web.json_response(
            {"run_id": run_id, "status": "started"},
            status=202,
            headers=response_headers,
        )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        """GET /v1/runs/{run_id} — return pollable run status for external UIs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )
        return web.json_response(status)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished — send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response


    async def _handle_run_approval(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/approval — resolve a pending run approval."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_choice = str(body.get("choice", "")).strip().lower()
        aliases = {"approve": "once", "approved": "once", "allow": "once"}
        choice = aliases.get(raw_choice, raw_choice)
        allowed = {"once", "session", "always", "deny"}
        if choice not in allowed:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected one of: once, session, always, deny",
                    code="invalid_approval_choice",
                ),
                status=400,
            )

        approval_session_key = self._run_approval_sessions.get(run_id)
        if not approval_session_key:
            return web.json_response(
                _openai_error(
                    f"Run has no active approval session: {run_id}",
                    code="approval_not_active",
                ),
                status=409,
            )

        resolve_all = (
            _coerce_request_bool(body.get("all"), default=False)
            or _coerce_request_bool(body.get("resolve_all"), default=False)
        )
        try:
            from tools.approval import resolve_gateway_approval

            resolved = resolve_gateway_approval(
                approval_session_key,
                choice,
                resolve_all=resolve_all,
            )
        except Exception as exc:
            logger.exception("[api_server] approval resolution failed for run %s", run_id)
            return web.json_response(_openai_error(str(exc)), status=500)

        if resolved <= 0:
            return web.json_response(
                _openai_error(
                    f"Run has no pending approval: {run_id}",
                    code="approval_not_pending",
                ),
                status=409,
            )

        self._set_run_status(run_id, "running", last_event="approval.responded")
        q = self._run_streams.get(run_id)
        if q is not None:
            try:
                q.put_nowait({
                    "event": "approval.responded",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "choice": choice,
                    "resolved": resolved,
                })
            except Exception:
                pass

        return web.json_response({
            "object": "hermes.run.approval_response",
            "run_id": run_id,
            "choice": choice,
            "resolved": resolved,
        })

    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop — interrupt a running agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        agent = self._active_run_agents.get(run_id)
        task = self._active_run_tasks.get(run_id)

        if agent is None and task is None:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        self._set_run_status(run_id, "stopping", last_event="run.stopping")

        if agent is not None:
            try:
                agent.interrupt("Stop requested via API")
            except Exception:
                pass

        if task is not None and not task.done():
            task.cancel()
            # Bounded wait: run_conversation() executes in the default
            # executor thread which task.cancel() cannot preempt — we rely on
            # agent.interrupt() above to break the loop. Cap the wait so a
            # slow/unresponsive interrupt can't hang this handler.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[api_server] stop for run %s timed out after 5s; "
                    "agent may still be finishing the current step",
                    run_id,
                )
            except (asyncio.CancelledError, Exception):
                pass

        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically clean up run streams that were never consumed."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                run_id
                for run_id, created_at in list(self._run_streams_created.items())
                if now - created_at > self._RUN_STREAM_TTL
            ]
            for run_id in stale:
                logger.debug("[api_server] sweeping orphaned run %s", run_id)
                try:
                    from tools.approval import unregister_gateway_notify

                    approval_session_key = self._run_approval_sessions.get(run_id)
                    if approval_session_key:
                        unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)

            # Also age out terminal run statuses we keep around for polling.
            stale_statuses = [
                run_id
                for run_id, status in list(self._run_statuses.items())
                if status.get("status") in {"completed", "failed", "cancelled"}
                and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL
            ]
            for run_id in stale_statuses:
                self._run_statuses.pop(run_id, None)

    # ------------------------------------------------------------------
    # Per-session idle-commit watcher.
    #
    # The api_server platform is stateless: every chat-completion request
    # spawns a fresh AIAgent that is dropped when the response returns.
    # The OpenViking memory plugin's sync_turn() runs per-turn (so messages
    # accumulate inside OpenViking), but on_session_end() — which posts
    # /sessions/{id}/commit and triggers OpenViking's auto memory-extraction
    # pipeline — only fires from AIAgent's explicit shutdown, which we
    # never call in this platform.
    #
    # Without intervention every session sits at commit_count=0 forever and
    # no memory is ever extracted.  This watcher closes the loop:
    #
    #   - Every chat handler calls _touch_session_activity() with the
    #     scoped session_id, recording the user/chat and bumping last_seen.
    #   - A background task scans every IDLE_SCAN_INTERVAL_S seconds.
    #   - Sessions idle for ≥IDLE_COMMIT_SECONDS get committed once.
    #   - When the same session becomes active again, we clear the
    #     "committed" flag so a later idle period commits incrementally
    #     (OpenViking dedupes / merges memories across multiple commits).
    #
    # Operators can also explicitly fire commit via
    # POST /v1/sessions/{session_id}/end (e.g. on chat-switch in OpenWebUI).
    # ------------------------------------------------------------------

    IDLE_COMMIT_SECONDS = 30.0
    IDLE_SCAN_INTERVAL_S = 5.0
    SESSION_ACTIVITY_TTL_S = 86400.0  # forget about sessions after a day

    def _session_activity_file(self) -> Path:
        """Resolve the disk path for the persisted session-activity table."""
        if self._session_activity_path is None:
            try:
                from hermes_constants import get_hermes_home
                base = get_hermes_home()
            except Exception:
                base = Path.home() / ".hermes"
            self._session_activity_path = base / "session_activity.json"
        return self._session_activity_path

    def _persist_session_activity(self) -> None:
        """Atomically write _session_activity to disk.

        Best-effort: any failure is logged at DEBUG and swallowed.
        Writes via tmp-file + os.replace so a crash mid-write can't leave
        a half-written JSON file.
        """
        path = self._session_activity_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._session_activity_lock:
                snapshot = dict(self._session_activity)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshot), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug("[api_server] persist session_activity failed: %s", exc)

    def _load_session_activity(self) -> None:
        """Reload _session_activity from disk on startup.

        After this call the regular idle watcher will commit anything
        whose ``last_seen`` is past ``IDLE_COMMIT_SECONDS`` on its first
        scan — the disk file is the only mechanism that survives a
        container restart, so without it a user who closed their browser
        mid-conversation never gets their preference extracted.
        """
        path = self._session_activity_file()
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[api_server] load session_activity failed: %s", exc)
            return
        if not isinstance(loaded, dict):
            return
        # Drop entries past TTL — across many restarts this dict could
        # otherwise grow unbounded.
        now = time.time()
        kept = {
            sid: info for sid, info in loaded.items()
            if isinstance(info, dict)
            and now - float(info.get("last_seen", 0) or 0) < self.SESSION_ACTIVITY_TTL_S
        }
        with self._session_activity_lock:
            for sid, info in kept.items():
                # Don't clobber any record the running process already
                # has (defensive — load is called once at startup).
                self._session_activity.setdefault(sid, info)
        logger.info(
            "[api_server] reloaded %d session activity records "
            "(of %d on disk) from %s",
            len(kept), len(loaded), path,
        )

    def _maybe_commit_sibling_sessions(self, session_id: str, user_id: str, chat_id: str) -> None:
        """When the same user opens a different chat_id, immediately
        schedule a commit on every uncommitted session for that user
        whose chat_id differs from the new one.

        OpenWebUI does not notify the gateway on chat-switch, but a new
        chat_id arriving from the same user is the natural "I'm done
        with the old one" signal — much faster than waiting 30s of
        idle for the watcher.
        """
        if not user_id or not chat_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        snapshot = list(self._session_activity.items())
        for sid, info in snapshot:
            if sid == session_id:
                continue
            if info.get("committed"):
                continue
            if info.get("user_id") != user_id:
                continue
            if info.get("chat_id") == chat_id:
                continue
            # Mark committed up-front so the periodic watcher doesn't
            # also fire its own commit before this in-flight task lands.
            info["committed"] = True
            if loop is not None:
                loop.create_task(self._commit_session_async(sid, info.get("user_id", "")))
                logger.info(
                    "[api_server] chat-switch: scheduled commit of %s "
                    "(prior chat=%s, current=%s)",
                    sid, info.get("chat_id", ""), chat_id,
                )

    def _touch_session_activity(self, session_id: str, scope: Dict[str, str]) -> None:
        """Record (or refresh) per-session activity for the idle-commit watcher.

        Called from each scoped chat handler after the session_id has been
        finalised.  Resetting "committed" lets the next idle window commit
        again — OpenViking handles incremental commits cleanly.

        Side-effects:
        - Detects chat-switch and schedules immediate commit on prior chats
          for the same user (P1).
        - Persists the activity table so a container restart doesn't lose
          any pending commits (P0).
        """
        if not session_id:
            return
        user_id = scope.get("user_id", "")
        chat_id = scope.get("chat_id", "")
        # P1: detect chat-switch BEFORE we register the new session, so
        # we look at the dict in its pre-touch state.
        self._maybe_commit_sibling_sessions(session_id, user_id, chat_id)
        info = self._session_activity.get(session_id)
        if info is None:
            self._session_activity[session_id] = {
                "user_id": user_id,
                "chat_id": chat_id,
                "last_seen": time.time(),
                "committed": False,
            }
        else:
            info["last_seen"] = time.time()
            info["committed"] = False
            # Refresh user/chat in case of re-binding (defensive)
            if user_id:
                info["user_id"] = user_id
            if chat_id:
                info["chat_id"] = chat_id
        # P0: persist so a container restart doesn't lose pending commits.
        self._persist_session_activity()

    def _commit_openviking_session_sync(self, session_id: str, user_id: str) -> bool:
        """Direct POST to OpenViking /api/v1/sessions/{id}/commit.

        Bypasses AIAgent so we don't pay the full agent-init cost just to
        run a single HTTP POST.  Returns True on a 2xx response, False on
        any failure (logged at WARN; non-fatal for the watcher loop).
        """
        endpoint = (os.environ.get("OPENVIKING_ENDPOINT", "") or "").rstrip("/")
        if not endpoint:
            return False
        try:
            import httpx
        except ImportError:
            logger.debug("[api_server] httpx unavailable; cannot commit OpenViking session")
            return False
        api_key = os.environ.get("OPENVIKING_API_KEY", "")
        account = os.environ.get("OPENVIKING_ACCOUNT", "default")
        agent = os.environ.get("OPENVIKING_AGENT", "hermes")
        viking_user = user_id or os.environ.get("OPENVIKING_USER", "default")
        headers = {
            "Content-Type": "application/json",
            "X-OpenViking-Account": account,
            "X-OpenViking-User": viking_user,
            "X-OpenViking-Agent": agent,
        }
        if api_key:
            headers["X-API-Key"] = api_key
        url = f"{endpoint}/api/v1/sessions/{session_id}/commit"
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(url, headers=headers, json={})
                if resp.status_code == 404:
                    # Session never reached OpenViking (no sync_turn fired).
                    # Not an error — just nothing to commit.
                    logger.debug(
                        "[api_server] OpenViking session %s has no messages; skipping commit",
                        session_id,
                    )
                    return False
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning(
                "[api_server] OpenViking commit failed for session %s: %s",
                session_id, exc,
            )
            return False

    async def _commit_session_async(self, session_id: str, user_id: str) -> bool:
        """Run _commit_openviking_session_sync in the default executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._commit_openviking_session_sync, session_id, user_id
        )

    async def _session_idle_watcher(self) -> None:
        """Background task: commit sessions idle ≥ IDLE_COMMIT_SECONDS."""
        while True:
            try:
                await asyncio.sleep(self.IDLE_SCAN_INTERVAL_S)
            except asyncio.CancelledError:
                break
            try:
                now = time.time()
                # Snapshot to avoid mutating during iteration.
                snapshot = list(self._session_activity.items())
                stale = [
                    (sid, info) for sid, info in snapshot
                    if not info.get("committed")
                    and now - info.get("last_seen", now) >= self.IDLE_COMMIT_SECONDS
                ]
                # GC very old sessions so the dict doesn't grow unbounded
                gc_keys = [
                    sid for sid, info in snapshot
                    if now - info.get("last_seen", now) >= self.SESSION_ACTIVITY_TTL_S
                ]
                for sid in gc_keys:
                    self._session_activity.pop(sid, None)

                for sid, info in stale:
                    ok = await self._commit_session_async(sid, info.get("user_id", ""))
                    # Mark committed even on False so we don't retry forever
                    # for sessions OpenViking doesn't know about.  A new chat
                    # turn will reset this via _touch_session_activity.
                    info["committed"] = True
                    if ok:
                        logger.info(
                            "[api_server] auto-committed idle session %s (user=%s, chat=%s)",
                            sid, info.get("user_id", ""), info.get("chat_id", ""),
                        )
                # P0: persist after every scan iteration that mutated the
                # table — saves both committed-flag flips and gc removals.
                if stale or gc_keys:
                    self._persist_session_activity()
            except Exception as exc:
                logger.warning("[api_server] idle-watcher iteration failed: %s", exc)

    async def _handle_end_session(self, request: "web.Request") -> "web.Response":
        """POST /v1/sessions/{session_id}/end — explicitly commit a session.

        Designed for clients (e.g. OpenWebUI chat-switch hook) that want
        memory extraction to happen NOW rather than after the 30s idle
        window.  The caller must own the session — we verify by checking
        that the session_id ends with the same -user-<id> suffix the
        request's X-OpenWebUI-User-Id sanitises to.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        session_id = request.match_info["session_id"]
        # Ownership: session_id must contain "-user-{caller}".  Returning 404
        # rather than 403 to avoid revealing whether the session exists.
        if f"-user-{scope['user_id']}" not in session_id:
            return web.json_response(
                _openai_error(f"Session not found: {session_id}"), status=404,
            )

        info = self._session_activity.get(session_id)
        user_id_for_commit = (info or {}).get("user_id") or scope["user_id"]

        ok = await self._commit_session_async(session_id, user_id_for_commit)
        if info is not None:
            info["committed"] = True
            # P0: persist the flip so a restart doesn't double-commit
            # this session.
            self._persist_session_activity()
        if not ok:
            return web.json_response(
                _openai_error(
                    f"Commit failed for session {session_id} "
                    "(session may not have any messages in OpenViking yet)",
                    err_type="server_error",
                ),
                status=502,
            )

        return web.json_response({
            "id": session_id,
            "object": "session.end",
            "committed": True,
        })

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            mws = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws, client_max_size=MAX_REQUEST_BYTES)
            assert self._app is not None
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/health/detailed", self._handle_health_detailed)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_get("/v1/capabilities", self._handle_capabilities)
            self._app.router.add_get("/v1/skills", self._handle_skills)
            self._app.router.add_get("/v1/toolsets", self._handle_toolsets)
            # Session/client control surface (thin wrappers over SessionDB + _run_agent)
            self._app.router.add_get("/api/sessions", self._handle_list_sessions)
            self._app.router.add_post("/api/sessions", self._handle_create_session)
            self._app.router.add_get("/api/sessions/{session_id}", self._handle_get_session)
            self._app.router.add_patch("/api/sessions/{session_id}", self._handle_patch_session)
            self._app.router.add_delete("/api/sessions/{session_id}", self._handle_delete_session)
            self._app.router.add_get("/api/sessions/{session_id}/messages", self._handle_session_messages)
            self._app.router.add_post("/api/sessions/{session_id}/fork", self._handle_fork_session)
            self._app.router.add_post("/api/sessions/{session_id}/chat", self._handle_session_chat)
            self._app.router.add_post("/api/sessions/{session_id}/chat/stream", self._handle_session_chat_stream)
            self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get(
                "/v1/artifacts/{artifact_id}/{filename}",
                self._handle_artifact_download,
            )
            self._app.router.add_get(
                "/v1/artifacts/{artifact_id}/{filename}/download/{expires_epoch}/{sig}",
                self._handle_artifact_download,
            )
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            # OpenWebUI internal bridge APIs (user-scoped memory + dreaming).
            self._app.router.add_post(
                "/api/openwebui/feedback-events",
                self._handle_openwebui_feedback_events,
            )
            self._app.router.add_get(
                "/api/openwebui/memories",
                self._handle_openwebui_list_memories,
            )
            self._app.router.add_post(
                "/api/openwebui/memories",
                self._handle_openwebui_upsert_memory,
            )
            self._app.router.add_delete(
                "/api/openwebui/memories",
                self._handle_openwebui_delete_memory,
            )
            self._app.router.add_delete(
                "/api/openwebui/memories/{memory_id}",
                self._handle_openwebui_delete_memory,
            )
            self._app.router.add_post("/api/dreaming/run-now", self._handle_dreaming_run_now)
            self._app.router.add_get("/api/dreaming/status", self._handle_dreaming_status)
            # Cron jobs management API
            self._app.router.add_get("/api/jobs", self._handle_list_jobs)
            self._app.router.add_post("/api/jobs", self._handle_create_job)
            self._app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
            self._app.router.add_patch("/api/jobs/{job_id}", self._handle_update_job)
            self._app.router.add_delete("/api/jobs/{job_id}", self._handle_delete_job)
            self._app.router.add_post("/api/jobs/{job_id}/pause", self._handle_pause_job)
            self._app.router.add_post("/api/jobs/{job_id}/resume", self._handle_resume_job)
            self._app.router.add_post("/api/jobs/{job_id}/run", self._handle_run_job)

            # Chronos managed-cron fire webhook (NAS → agent). Authenticated by a
            # NAS-minted JWT (NOT API_SERVER_KEY), so it has its own auth path.
            if _CRON_AVAILABLE:
                self._app.router.add_post("/api/cron/fire", self._handle_cron_fire)
            # Structured event streaming
            self._app.router.add_post("/v1/runs", self._handle_runs)
            self._app.router.add_get("/v1/runs/{run_id}", self._handle_get_run)
            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
            self._app.router.add_post("/v1/runs/{run_id}/approval", self._handle_run_approval)
            self._app.router.add_post("/v1/runs/{run_id}/stop", self._handle_stop_run)
            # Store the adapter after native routes are registered. Local Hermes-Relay
            # bootstrap shims use this key as a feature-detection hook; registering
            # native routes first lets those shims no-op instead of shadowing the
            # upstream session-control handlers.
            self._app["api_server_adapter"] = self

            # Explicit session-end (commit + memory extraction).  Pair with the
            # idle watcher below — clients that know they're done (e.g. on
            # chat-switch) can call this to fire commit immediately.
            self._app.router.add_post("/v1/sessions/{session_id}/end", self._handle_end_session)

            # Refuse to start without authentication. The API server can
            # dispatch terminal-capable agent work, so every deployment needs
            # an explicit API_SERVER_KEY regardless of bind address.  The
            # idle-commit watcher, orphan sweep, and dreaming scheduler are
            # started after self._site.start() below (de3806d31 moved them).
            if not self._api_key:
                logger.error(
                    "[%s] Refusing to start: API_SERVER_KEY is required for the API server, "
                    "including loopback-only binds on %s.",
                    self.name, self._host,
                )
                return False

            # Refuse to start network-accessible with a placeholder key.
            # Ported from openclaw/openclaw#64586.
            if is_network_accessible(self._host) and self._api_key:
                try:
                    from hermes_cli.auth import has_usable_secret
                    if not has_usable_secret(self._api_key, min_length=8):
                        logger.error(
                            "[%s] Refusing to start: API_SERVER_KEY is set to a "
                            "placeholder value. Generate a real secret "
                            "(e.g. `openssl rand -hex 32`) and set API_SERVER_KEY "
                            "before exposing the API server on %s.",
                            self.name, self._host,
                        )
                        return False
                except ImportError:
                    pass

            # Port conflict detection — fail fast if port is already in use
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(1)
                    _s.connect(('127.0.0.1', self._port))
                logger.error('[%s] Port %d already in use. Set a different port in config.yaml: platforms.api_server.port', self.name, self._port)
                return False
            except (ConnectionRefusedError, OSError):
                pass  # port is free

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            # P0: restore the activity table from disk so a container
            # restart doesn't lose pending commits.  Must run BEFORE the
            # watcher starts so its first scan sees the loaded entries.
            self._load_session_activity()
            # Background watcher: commits OpenViking sessions after 30s idle so
            # auto memory-extraction actually happens.  See _session_idle_watcher.
            self._idle_commit_task = asyncio.create_task(self._session_idle_watcher())
            try:
                self._background_tasks.add(self._idle_commit_task)
            except TypeError:
                pass
            if hasattr(self._idle_commit_task, "add_done_callback"):
                self._idle_commit_task.add_done_callback(self._background_tasks.discard)
            # Start background sweep to clean up orphaned (unconsumed) run streams.
            self._sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(self._sweep_task)
            except TypeError:
                pass
            if hasattr(self._sweep_task, "add_done_callback"):
                self._sweep_task.add_done_callback(self._background_tasks.discard)

            if os.getenv("HERMES_DREAMING_ENABLED", "false").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                from gateway.openwebui_bridge import dreaming_scheduler_loop

                poll_seconds = float(os.getenv("HERMES_DREAMING_POLL_SECONDS", "300"))
                self._dreaming_scheduler_task = asyncio.create_task(
                    dreaming_scheduler_loop(self._openwebui_bridge(), poll_seconds=poll_seconds)
                )
                try:
                    self._background_tasks.add(self._dreaming_scheduler_task)
                except TypeError:
                    pass
                if hasattr(self._dreaming_scheduler_task, "add_done_callback"):
                    self._dreaming_scheduler_task.add_done_callback(self._background_tasks.discard)

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d (model: %s)",
                self.name, self._host, self._port, self._model_name,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            try:
                await self.disconnect()
            except Exception:
                pass
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server and release all owned resources.

        Closes the ResponseStore SQLite connection in addition to stopping
        the aiohttp web server. Without this, every adapter instance leaks
        2 file descriptors (the database file and its WAL sidecar) — the
        reconnect loop in ``gateway.run`` constructs a fresh adapter on
        every retry, so 2 fds/retry × 300s backoff cap ≈ 12 fds/hour, which
        exhausts the default 2560 fd limit after ~12h of failed reconnects
        and turns the whole gateway into a zombie
        (OSError: [Errno 24] Too many open files, #37011).
        """
        self._mark_disconnected()
        # Stop the idle-commit watcher first so we don't fire commits at
        # OpenViking after the network is being torn down.  Best-effort —
        # the watcher swallows CancelledError.
        if self._idle_commit_task is not None:
            if not self._idle_commit_task.done():
                self._idle_commit_task.cancel()
                try:
                    await asyncio.wait_for(self._idle_commit_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass
            self._idle_commit_task = None
        if self._sweep_task is not None:
            if not self._sweep_task.done():
                self._sweep_task.cancel()
                try:
                    await asyncio.wait_for(self._sweep_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass
            self._sweep_task = None
        if self._dreaming_scheduler_task is not None:
            if not self._dreaming_scheduler_task.done():
                self._dreaming_scheduler_task.cancel()
                try:
                    await asyncio.wait_for(self._dreaming_scheduler_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass
            self._dreaming_scheduler_task = None
        if self._openwebui_bridge_service is not None:
            client = getattr(self._openwebui_bridge_service, "client", None)
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        if self._response_store is not None:
            try:
                self._response_store.close()
            except Exception:
                logger.debug(
                    "Failed to close response store for %s", self.name, exc_info=True,
                )
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
