"""Functional per-user skill namespace primitives.

This module deliberately provides *routing*, not a filesystem security boundary.
The Hermes gateway still runs as one process with a shared home directory during
Increment 1.  Kernel-enforced exec isolation belongs to a later increment.
"""

from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from hermes_constants import get_hermes_home


PLATFORM_NAMESPACE = "platform"
USER_NAMESPACE = "user"
EXTERNAL_NAMESPACE_PREFIX = "external"
USER_SKILLS_DIRNAME = "user-skills"
API_SERVER_PLATFORM = "api_server"

_OWUI_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_BOUND_USER_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "HERMES_SKILL_NAMESPACE_USER_ID", default=None
)


@dataclass(frozen=True)
class SkillRoot:
    """One caller-visible skill root and its functional namespace metadata."""

    namespace: str
    path: Path
    owner_user_id: Optional[str] = None

    @property
    def qualified(self) -> bool:
        return self.namespace in {PLATFORM_NAMESPACE, USER_NAMESPACE}


def validate_owui_user_id(raw: str) -> str:
    """Return a safe stable OpenWebUI ID, or raise ``ValueError``.

    Validation is intentionally lossless: two distinct incoming IDs must never
    be normalized onto the same directory name.
    """

    value = str(raw or "").strip()
    if value in {"", ".", ".."} or not _OWUI_USER_ID_RE.fullmatch(value):
        raise ValueError("invalid OpenWebUI user id for skill namespace")
    return value


def current_skill_namespace_user_id() -> Optional[str]:
    """Return the caller whose user-skill root is active, if any."""

    bound = _BOUND_USER_ID.get()
    if bound is not None:
        return bound

    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != API_SERVER_PLATFORM:
            return None
        raw = get_session_env("HERMES_SESSION_USER_ID", "")
    except Exception:
        return None

    try:
        return validate_owui_user_id(raw)
    except ValueError:
        return None


def get_user_skills_base_dir() -> Path:
    """Return the profile-aware parent of all functional user skill roots."""

    return get_hermes_home() / USER_SKILLS_DIRNAME


def get_current_user_skills_dir() -> Optional[Path]:
    """Return only the current caller's user skill root; never scan siblings."""

    user_id = current_skill_namespace_user_id()
    if user_id is None:
        return None
    return get_user_skills_base_dir() / user_id


@contextmanager
def bind_skill_namespace_user(user_id: str) -> Iterator[None]:
    """Bind a trusted original subject while replaying an approved write."""

    validated = validate_owui_user_id(user_id)
    token = _BOUND_USER_ID.set(validated)
    try:
        yield
    finally:
        _BOUND_USER_ID.reset(token)


def qualify_skill_name(namespace: str, name: str) -> str:
    """Return a stable caller-facing identifier for built-in namespaces."""

    if namespace in {PLATFORM_NAMESPACE, USER_NAMESPACE}:
        return f"{namespace}:{name}"
    return name


def split_builtin_qualified_name(
    name: str, namespace: Optional[str] = None
) -> tuple[Optional[str], str]:
    """Parse only the reserved platform/user qualifiers.

    Other ``prefix:name`` forms remain available to plugin skill resolution.
    """

    explicit = str(namespace or "").strip().lower() or None
    raw_name = str(name or "")
    if explicit is not None:
        if explicit not in {PLATFORM_NAMESPACE, USER_NAMESPACE}:
            raise ValueError("namespace must be 'platform' or 'user'")
        return explicit, raw_name
    if ":" not in raw_name:
        return None, raw_name
    prefix, bare = raw_name.split(":", 1)
    if prefix in {PLATFORM_NAMESPACE, USER_NAMESPACE}:
        return prefix, bare
    return None, raw_name
