"""Request-scoped authorization for local files supplied by trusted adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterable, Iterator, Mapping


_CAPABILITY_VERSION = 1
_CAPABILITY_TTL_SECONDS = 60
_CAPABILITY_META_KEY = "hermes_file_capability"
_GRANTS: ContextVar[dict[str, frozenset[str]] | None] = ContextVar(
    "local_file_grants",
    default=None,
)
# Short handle -> canonical path, per task. Attached-file paths are ~190
# characters of nested UUIDs; asking a model to retype one verbatim for every
# file is a transcription task it measurably fails (observed live: spliced
# UUIDs, interpolated dates, and filenames copied out of a skill's own
# examples). Handles let the model address a file by `F07` instead.
_ALIASES: ContextVar[dict[str, dict[str, str]] | None] = ContextVar(
    "local_file_grant_aliases",
    default=None,
)

# A handle is a bare `F` + digits token, optionally `#`-prefixed. Real paths
# always contain a separator, so the two namespaces cannot collide.
_HANDLE_RE = re.compile(r"^#?(F\d{1,4})$", re.IGNORECASE)
_MAX_LISTED_HANDLES = 12


def _canonical_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def make_file_handles(paths: Iterable[str | Path]) -> dict[str, str]:
    """Assign stable `F01`-style handles to *paths* in the given order."""
    handles: dict[str, str] = {}
    for index, path in enumerate(paths, start=1):
        handles[f"F{index:02d}"] = str(path)
    return handles


@contextmanager
def file_grant_scope(
    task_id: str,
    paths: Iterable[str | Path],
    *,
    handles: Mapping[str, str | Path] | None = None,
) -> Iterator[None]:
    """Bind exact canonical paths to one task for the lifetime of a request.

    ``handles`` optionally binds short aliases to those same paths. An alias
    grants nothing on its own: it is rewritten to a path and then subjected to
    the identical membership check, so an alias pointing outside ``paths`` is
    denied exactly like any other ungranted path.
    """
    task_key = str(task_id or "default")
    current = _GRANTS.get() or {}
    updated = dict(current)
    updated[task_key] = frozenset(_canonical_path(path) for path in paths)
    grants_token = _GRANTS.set(updated)

    current_aliases = _ALIASES.get() or {}
    updated_aliases = dict(current_aliases)
    updated_aliases[task_key] = {
        str(name).upper(): _canonical_path(target)
        for name, target in (handles or {}).items()
    }
    alias_token = _ALIASES.set(updated_aliases)
    try:
        yield
    finally:
        _ALIASES.reset(alias_token)
        _GRANTS.reset(grants_token)


def resolve_grant_alias(value: str | Path, *, task_id: str) -> str:
    """Rewrite a short handle to its canonical path; pass anything else through.

    Callers use this to obtain the path they will actually open. It is a pure
    lookup in the current request's alias table — an unknown handle is returned
    unchanged so it fails the normal grant check rather than being special-cased.
    """
    raw = str(value)
    match = _HANDLE_RE.match(raw.strip())
    if not match:
        return raw
    aliases = (_ALIASES.get() or {}).get(str(task_id or "default")) or {}
    return aliases.get(match.group(1).upper(), raw)


def _known_handles_hint(task_id: str) -> str:
    aliases = (_ALIASES.get() or {}).get(str(task_id or "default")) or {}
    if not aliases:
        return ""
    names = sorted(aliases)
    shown = names[:_MAX_LISTED_HANDLES]
    listed = ", ".join(shown)
    if len(names) > len(shown):
        listed += f", … ({len(names)} total)"
    return f" Valid handles this request: {listed}."


def resolve_file_grant(
    path: str | Path,
    *,
    task_id: str,
    operation: str,
) -> tuple[str | None, str | None]:
    """Resolve *path* once and return its authorized canonical identity."""
    resolved = resolve_grant_alias(path, task_id=task_id)
    canonical_path = _canonical_path(resolved)
    scopes = _GRANTS.get()
    task_key = str(task_id or "default")
    if scopes is None or canonical_path in (scopes.get(task_key) or ()):
        return canonical_path, None
    return None, (
        f"Local file access not granted for {operation}: {path!s}. "
        "Use a handle or an exact path supplied in this request's validated "
        "attached files." + _known_handles_hint(task_key)
    )


def file_grant_error(path: str | Path, *, task_id: str, operation: str) -> str | None:
    """Return a denial message when an active request did not grant *path*."""
    if _GRANTS.get() is None:
        return None
    _, denial = resolve_file_grant(
        path,
        task_id=task_id,
        operation=operation,
    )
    return denial


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
    canonical_path: str,
    *,
    operation: str,
    now: int | None = None,
) -> str:
    """Create a token bound to an already-authorized canonical path."""
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": _CAPABILITY_VERSION,
        "op": str(operation),
        "path": canonical_path,
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
    "make_file_handles",
    "resolve_file_grant",
    "resolve_grant_alias",
]
