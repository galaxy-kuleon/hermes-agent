"""Request-scoped authorization for local files supplied by trusted adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterable, Iterator


_CAPABILITY_VERSION = 1
_CAPABILITY_TTL_SECONDS = 60
_CAPABILITY_META_KEY = "hermes_file_capability"
_GRANTS: ContextVar[dict[str, frozenset[str]] | None] = ContextVar(
    "local_file_grants",
    default=None,
)


def _canonical_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


@contextmanager
def file_grant_scope(task_id: str, paths: Iterable[str | Path]) -> Iterator[None]:
    """Bind exact canonical paths to one task for the lifetime of a request."""
    current = _GRANTS.get() or {}
    updated = dict(current)
    updated[str(task_id or "default")] = frozenset(_canonical_path(path) for path in paths)
    token = _GRANTS.set(updated)
    try:
        yield
    finally:
        _GRANTS.reset(token)


def file_grant_error(path: str | Path, *, task_id: str, operation: str) -> str | None:
    """Return a denial message when an active request did not grant *path*."""
    scopes = _GRANTS.get()
    if scopes is None:
        return None
    task_key = str(task_id or "default")
    granted = scopes.get(task_key)
    if granted is not None and _canonical_path(path) in granted:
        return None
    return (
        f"Local file access not granted for {operation}: {path!s}. "
        "Use an exact path supplied in this request's validated attached files."
    )


def _capability_key() -> bytes:
    value = os.environ.get("HERMES_FILE_CAPABILITY_KEY", "")
    if not value:
        raise ValueError("HERMES_FILE_CAPABILITY_KEY is not configured")
    return hmac.new(
        value.encode("utf-8"),
        b"hermes-file-capability-v1",
        hashlib.sha256,
    ).digest()


def make_file_capability(
    path: str | Path,
    *,
    operation: str,
    now: int | None = None,
) -> str:
    """Create a short-lived, operation- and path-bound cross-process token."""
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": _CAPABILITY_VERSION,
        "op": str(operation),
        "path": _canonical_path(path),
        "exp": issued_at + _CAPABILITY_TTL_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(_capability_key(), encoded, hashlib.sha256).hexdigest()
    return f"{encoded.decode('ascii')}.{signature}"


__all__ = [
    "_CAPABILITY_META_KEY",
    "file_grant_error",
    "file_grant_scope",
    "make_file_capability",
]
