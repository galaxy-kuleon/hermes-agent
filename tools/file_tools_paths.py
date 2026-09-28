"""Path resolution for the file tools: task-aware base dir, ``~`` expansion, workspace-divergence warning.

Core invariant: the base directory anchoring relative paths is ALWAYS absolute
and derived from the task's terminal cwd, never from the process cwd unless no
other anchor exists (a relative/sentinel ``TERMINAL_CWD`` would silently anchor
edits to the agent process cwd, e.g. the main repo during a worktree session).
"""

import logging
import re
import os
import posixpath
import sys
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# ``TERMINAL_CWD`` values that mean "not configured" ("." from a stale config;
# "auto"/"cwd" are wizard placeholders). gateway/run.py sanitizes the same set.
_TERMINAL_CWD_SENTINELS = frozenset({"", ".", "./", "auto", "cwd"})
_CONTAINER_PATH_BACKENDS_FALLBACK = frozenset({"docker", "singularity", "modal", "daytona", "vercel_sandbox"})
# Backend name inferred from the live environment's class name (first match wins).
_ENV_CLASS_NAME_HINTS = ("local", "ssh", "docker", "singularity", "modal", "daytona")


def _expand_tilde(path: str) -> str:
    """Expand ``~`` using the effective profile home (``get_subprocess_home``) so
    gateway/cron runs, whose process HOME may differ, agree with interactive CLI sessions.

    This mirrors ``hermes_constants.get_subprocess_home()`` so that ``~`` resolves consistently regardless
    of whether the tool runs interactively or inside a gateway-driven cron job (#48552).
    """
    if not path or "~" not in path:
        return path
    try:
        from hermes_constants import get_subprocess_home

        home = get_subprocess_home()
    except Exception:
        home = None
    if home and (path == "~" or path.startswith("~/")):
        return home if path == "~" else os.path.join(home, path[2:])
    return os.path.expanduser(path)


def _terminal_env_type_for_task(task_id: str = "default") -> str:
    """Best-effort terminal backend type for path-resolution decisions."""
    try:
        from tools.terminal_tool import (
            _active_environments, _env_lock, _get_env_config, _resolve_container_task_id)

        try:
            container_key = _resolve_container_task_id(task_id)
        except Exception:
            container_key = task_id
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)
        if env is not None:
            name = env.__class__.__name__.lower()
            hint = next((h for h in _ENV_CLASS_NAME_HINTS if h in name), None)
            stamped = getattr(env, "_hermes_backend_name", None)
            if hint or (isinstance(stamped, str) and stamped):
                return hint or stamped
        return str(_get_env_config().get("env_type") or os.getenv("TERMINAL_ENV") or "local").lower()
    except Exception:
        return str(os.getenv("TERMINAL_ENV") or "local").lower()


def _uses_container_paths(task_id: str = "default") -> bool:
    env_type = _terminal_env_type_for_task(task_id)
    try:
        from tools.terminal_tool import _is_container_backend

        return _is_container_backend(env_type)
    except Exception:
        return env_type in _CONTAINER_PATH_BACKENDS_FALLBACK


def container_backend_for_task(task_id: str = "default") -> str | None:
    """The task's backend name when its file paths belong to a container, else None."""
    return _terminal_env_type_for_task(task_id) if _uses_container_paths(task_id) else None


def _normalize_without_host_deref(path: str | Path | PurePosixPath) -> PurePosixPath:
    """Normalize path syntax without following host symlinks: container paths are
    meaningful inside the sandbox, and a host-side ``/workspace`` symlink must not rewrite them."""
    return PurePosixPath(posixpath.normpath(str(path)))


def _sentinel_free_abs_cwd(raw: str | None) -> str | None:
    """Return *raw* expanded when it is a non-sentinel ABSOLUTE anchor, else ``None``
    (a relative anchor is exactly the ambiguity that misroutes worktree edits)."""
    raw = str(raw or "").strip()
    if raw.lower() in _TERMINAL_CWD_SENTINELS:
        return None
    expanded = _expand_tilde(raw)
    return expanded if os.path.isabs(expanded) else None


def _configured_terminal_cwd() -> str | None:
    """Return ``$TERMINAL_CWD`` only when it names a real (absolute, non-sentinel) anchor.
    Scope-aware: under gateway multiplexing the routed profile's cwd lives in the per-turn scope."""
    # See #68559.
    from agent.runtime_cwd import scope_terminal_cwd

    return _sentinel_free_abs_cwd(scope_terminal_cwd() or None)


def _registered_task_cwd_override(task_id: str = "default") -> str | None:
    """Return a registered cwd override keyed by the RAW task id, when available.

    ``terminal_tool`` collapses CWD-only overrides to the shared ``"default"``
    env, but the cwd value stays keyed by the raw session id.
    """
    try:
        from tools.terminal_tool import resolve_task_overrides

        overrides = resolve_task_overrides(task_id)
    except Exception:
        return None

    return _sentinel_free_abs_cwd(overrides.get("cwd"))


def _authoritative_workspace_root(task_id: str = "default") -> str | None:
    """Best-effort absolute workspace root, or ``None`` when no reliable anchor exists.

    Order: (1) the session's own cwd record (per-session, so one session's
    ``cd`` never leaks into another); (2) a registered raw-keyed cwd override
    (TUI/Desktop/ACP); (3) a sentinel-free absolute ``$TERMINAL_CWD``.
    """
    try:
        from tools.terminal_tool import get_session_cwd

        recorded = get_session_cwd(task_id)
    except Exception:
        recorded = None
    return recorded or _registered_task_cwd_override(task_id) or _configured_terminal_cwd()


def _host_text(text: str, container_paths: bool) -> str:
    """Expand ``~``; on host backends also translate Git Bash ``/c/Users/...`` drive
    paths before Path sees them. Container/WSL Linux paths are never rewritten."""
    if not container_paths:
        from tools.environments.local import _msys_to_windows_path

        text = _msys_to_windows_path(text)
    return _expand_tilde(text)


def _anchor(text: str, base, container_paths: bool) -> Path | PurePosixPath:
    """Return *text* as an absolute, normalized path, joining it onto ``base()`` when
    relative. Container: pure-posix, no host deref. Host: resolve() (win32: ntpath normpath)."""
    if container_paths:
        if not posixpath.isabs(text):
            text = posixpath.join(str(base()), text)
        return _normalize_without_host_deref(text)
    if sys.platform == "win32":
        import ntpath

        if not ntpath.isabs(text):
            text = ntpath.join(str(base()), text)
        return Path(ntpath.normpath(text))
    p = Path(text)
    if not p.is_absolute():
        p = Path(base()) / p
    return p.resolve()


def _resolve_base_dir_unscoped(
    task_id: str = "default", *, container_paths: bool | None = None) -> Path | PurePosixPath:
    """Return the ABSOLUTE base directory for resolving relative paths:
    ``_authoritative_workspace_root``, else the process cwd as a last resort."""
    root = _authoritative_workspace_root(task_id)
    if container_paths is None:
        container_paths = _uses_container_paths(task_id)
    # A backend's relative cwd is anchored to the process cwd once, here.
    return _anchor(_host_text(root or os.getcwd(), container_paths), os.getcwd, container_paths)






_WORKSPACE_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_WORKSPACE_USER_ID_MAX_CHARS = 64
_WORKSPACE_USER_DIR_NAME = "user"
_PER_USER_WORKSPACE_UNAVAILABLE_ERROR = (
    "Per-user workspace is unavailable; refusing relative path access."
)
_PER_USER_WORKSPACE_BOUNDARY_ERROR = (
    "Per-user workspace boundary refused path access."
)


class _PerUserWorkspaceError(RuntimeError):
    """A caller-safe denial while enforcing the per-user workspace boundary."""


class _PerUserWorkspaceUnavailable(_PerUserWorkspaceError):
    """The request requires user isolation but its safe base is unavailable."""


class _PerUserWorkspaceBoundaryViolation(_PerUserWorkspaceError):
    """A workspace path resolved outside the current user's isolated root."""


def _workspace_user_scope() -> str:
    """Return a safe api_server user-id path segment, or ``""``.

    Read the request ContextVar at call time: api_server binds it after some
    agents have already been constructed, and concurrent requests must never
    share a cached identity.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return ""
        user_id = str(get_session_env("HERMES_SESSION_USER_ID", "") or "")
    except Exception:
        return ""
    if (
        not user_id
        or len(user_id) > _WORKSPACE_USER_ID_MAX_CHARS
        or user_id == "."
        or ".." in user_id
        or _WORKSPACE_USER_ID_RE.fullmatch(user_id) is None
    ):
        return ""
    return user_id


def _per_user_workspace_context(
    base: Path,
) -> tuple[Path, Path, Path] | None:
    """Create and return the current api_server user's workspace context.

    Scoping applies only while the legacy resolution base is still inside the
    configured workspace. An explicit live cwd outside that workspace (for
    example a git worktree) remains authoritative. Isolation setup failures
    raise :class:`_PerUserWorkspaceUnavailable`; callers must reject the
    relative path rather than fall back to the shared legacy base.

    Returns ``(effective_base, workspace_root, user_root)``. ``effective_base``
    preserves an explicit cwd already inside ``user_root``; otherwise it is the
    user's root itself. The separate ``user_root`` is the security boundary:
    ``effective_base`` may be a nested cwd and must not become the containment
    boundary.
    """
    user_scope = _workspace_user_scope()
    workspace_raw = _configured_terminal_cwd()
    if not user_scope or not workspace_raw:
        return None

    try:
        workspace = Path(workspace_raw).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        logger.error(
            "per_user_workspace_isolation_failed "
            "reason=workspace_unavailable error_type=%s",
            type(error).__name__,
        )
        raise _PerUserWorkspaceUnavailable(
            _PER_USER_WORKSPACE_UNAVAILABLE_ERROR
        ) from error
    try:
        base.relative_to(workspace)
    except ValueError:
        return None

    candidate = workspace / _WORKSPACE_USER_DIR_NAME / user_scope
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        logger.error(
            "per_user_workspace_isolation_failed "
            "reason=mkdir_failed error_type=%s",
            type(error).__name__,
        )
        raise _PerUserWorkspaceUnavailable(
            _PER_USER_WORKSPACE_UNAVAILABLE_ERROR
        ) from error
    try:
        scoped_base = candidate.resolve()
        if scoped_base != candidate:
            raise ValueError("user workspace path contains a symlink")
        scoped_base.relative_to(workspace)
    except (OSError, RuntimeError, ValueError) as error:
        logger.error(
            "per_user_workspace_isolation_failed "
            "reason=invalid_scope error_type=%s",
            type(error).__name__,
        )
        raise _PerUserWorkspaceUnavailable(
            _PER_USER_WORKSPACE_UNAVAILABLE_ERROR
        ) from error

    # Preserve an explicit cwd already inside this user's scope. Any other cwd
    # below the shared workspace is reset to the user's isolated root.
    try:
        base.relative_to(scoped_base)
        effective_base = base
    except ValueError:
        effective_base = scoped_base
    return effective_base, workspace, scoped_base


def _per_user_workspace_base(base: Path) -> Path | None:
    """Create and return the effective per-user base, when scoping applies."""
    context = _per_user_workspace_context(base)
    return context[0] if context is not None else None


def _resolve_base_dir_scope_context(
    task_id: str = "default",
) -> tuple[Path, Path | None, Path | None]:
    """Return effective base plus configured workspace/user containment roots."""
    base = _legacy_resolution_base(task_id)
    context = _per_user_workspace_context(base)
    if context is None:
        return base, None, None
    return context


def _resolve_base_dir_with_scope(
    task_id: str = "default",
) -> tuple[Path, bool]:
    """Return the absolute local base and whether user scoping applied.

    Resolution order:
      1. The task's live terminal cwd (the directory the agent is actually
         working in — e.g. a git worktree). Authoritative when known.
      2. A registered task/session cwd override (TUI/Desktop/ACP sessions
         register a raw-keyed workspace cwd before any terminal command runs).
      3. A sentinel-free, absolute ``$TERMINAL_CWD`` (the worktree path set by
         ``cli.py``/``main.py`` for ``-w`` sessions). Used even before any
         terminal command has populated the live cwd registry.
      4. The process cwd.

    The returned base is ALWAYS absolute. This is the core invariant that
    prevents the worktree-cwd divergence bug: a relative or sentinel
    ``TERMINAL_CWD`` (commonly the literal ``"."`` from a stale config) is
    meaningless as a resolution anchor — left to ``Path.resolve()`` it silently
    resolves against whatever the agent PROCESS cwd happens to be (e.g. the main
    repo while the terminal is in a worktree), routing edits to the wrong
    checkout. We therefore reject sentinel/relative ``TERMINAL_CWD`` values
    outright (rather than anchoring them to the process cwd) and fall through to
    the process cwd only as a last resort, deterministically.
    """
    base, _workspace, user_root = _resolve_base_dir_scope_context(task_id)
    return base, user_root is not None


def _resolve_base_dir(
    task_id: str = "default",
    *,
    container_paths: bool | None = None,
) -> Path | PurePosixPath:
    if container_paths is None:
        container_paths = _uses_container_paths(task_id)
    if container_paths:
        return _resolve_base_dir_unscoped(task_id, container_paths=True)
    return _resolve_base_dir_with_scope(task_id)[0]


def _resolve_path_for_task_with_scope(
    filepath: str,
    task_id: str = "default",
) -> tuple[Path | PurePosixPath, bool]:
    """Resolve a path and enforce the api_server user's workspace boundary.

    Relative paths are contained by the current user's realpath root whenever
    per-user scoping applies. Absolute paths outside the configured workspace
    retain their pre-T-M4 behavior (for example handoff files under ``/tmp``).
    Absolute paths *inside* the configured workspace may only address the
    current user's root. Resolving the complete candidate neutralizes ``..``
    and symlinks in any path component before the shell/file layer sees it.
    """
    if not _workspace_user_scope():
        container_paths = _uses_container_paths(task_id)
        return _anchor(
            _host_text(filepath, container_paths),
            lambda: _resolve_base_dir_unscoped(task_id, container_paths=container_paths),
            container_paths,
        ), False
    if _uses_container_paths(task_id):
        expanded = _expand_tilde(filepath)
        if posixpath.isabs(expanded):
            return _normalize_without_host_deref(expanded), False
        base = _resolve_base_dir_unscoped(task_id, container_paths=True)
        return _normalize_without_host_deref(base / expanded), False

    p = Path(_host_text(filepath, False))
    if p.is_absolute():
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError) as error:
            # Absolute paths historically bypassed workspace scoping. Only
            # replace their raw resolution error with a non-identifying denial
            # when this request is otherwise eligible for per-user scoping.
            try:
                base = _legacy_resolution_base(task_id)
                workspace_raw = _configured_terminal_cwd()
                workspace = (
                    Path(workspace_raw).expanduser().resolve()
                    if _workspace_user_scope() and workspace_raw
                    else None
                )
                if workspace is None:
                    raise error
                base.relative_to(workspace)
            except ValueError:
                raise error
            except (OSError, RuntimeError):
                raise error
            logger.error(
                "per_user_workspace_path_denied "
                "reason=resolution_failed error_type=%s",
                type(error).__name__,
            )
            raise _PerUserWorkspaceBoundaryViolation(
                _PER_USER_WORKSPACE_BOUNDARY_ERROR
            ) from error

        # Preserve the established ability to read absolute handoff paths
        # outside the configured workspace without creating/requiring a user
        # directory. Only absolute targets inside an applicable shared
        # workspace need the user boundary.
        user_scope = _workspace_user_scope()
        workspace_raw = _configured_terminal_cwd()
        if not user_scope or not workspace_raw:
            return resolved, False
        try:
            workspace = Path(workspace_raw).expanduser().resolve()
            legacy_base = _legacy_resolution_base(task_id)
            legacy_base.relative_to(workspace)
            resolved.relative_to(workspace)
        except (OSError, RuntimeError, ValueError):
            return resolved, False

        _base, _workspace, user_root = _resolve_base_dir_scope_context(task_id)
        if user_root is None:
            return resolved, False
        try:
            resolved.relative_to(user_root)
        except ValueError as error:
            logger.error(
                "per_user_workspace_path_denied reason=outside_user_scope"
            )
            raise _PerUserWorkspaceBoundaryViolation(
                _PER_USER_WORKSPACE_BOUNDARY_ERROR
            ) from error
        return resolved, True

    base, _workspace, user_root = _resolve_base_dir_scope_context(task_id)
    try:
        resolved = (base / p).resolve()
    except (OSError, RuntimeError) as error:
        if user_root is None:
            raise
        logger.error(
            "per_user_workspace_path_denied "
            "reason=resolution_failed error_type=%s",
            type(error).__name__,
        )
        raise _PerUserWorkspaceBoundaryViolation(
            _PER_USER_WORKSPACE_BOUNDARY_ERROR
        ) from error
    if user_root is not None:
        try:
            resolved.relative_to(user_root)
        except ValueError as error:
            logger.error(
                "per_user_workspace_path_denied reason=outside_user_scope"
            )
            raise _PerUserWorkspaceBoundaryViolation(
                _PER_USER_WORKSPACE_BOUNDARY_ERROR
            ) from error
    return resolved, user_root is not None


def _resolve_path_for_task(filepath: str, task_id: str = "default") -> Path | PurePosixPath:
    """Resolve *filepath* against the task's absolute base directory.

    See :func:`_resolve_base_dir` for how the base is chosen. Absolute input
    paths are returned resolved-but-unanchored.

    On native Windows, Git Bash / MSYS drive paths (``/c/Users/...``) are
    translated to ``C:\\Users\\...`` before resolution so file tools don't
    treat them as relative ``\\c\\Users\\...`` under the process cwd.
    """
    return _resolve_path_for_task_with_scope(filepath, task_id)[0]


def _workspace_path_access_error(
    filepath: str,
    task_id: str = "default",
) -> str | None:
    """Return a caller-safe per-user setup or containment error."""
    try:
        _resolve_path_for_task_with_scope(filepath, task_id)
    except _PerUserWorkspaceError as error:
        return str(error)
    return None


def _legacy_resolution_base(task_id: str = "default") -> Path:
    root = _authoritative_workspace_root(task_id)
    return _anchor(_host_text(root or os.getcwd(), False), os.getcwd, False)


def _path_resolution_warning(filepath: str, resolved: Path, task_id: str = "default") -> str | None:
    """Warn when a RELATIVE path resolved OUTSIDE the task's workspace root (the
    edit is about to land in a different checkout than the terminal's cwd).
    ``None`` for absolute paths, an unknown root, or a path under the root."""
    try:
        if Path(_expand_tilde(filepath)).is_absolute():
            return None
        workspace_root = _authoritative_workspace_root(task_id)
        if not workspace_root:
            return None
        if _uses_container_paths(task_id):
            root = _normalize_without_host_deref(Path(_expand_tilde(workspace_root)))
        else:
            root = Path(_expand_tilde(workspace_root)).resolve()
        if resolved.is_relative_to(root):
            return None
        return (
            f"Relative path {filepath!r} resolved to {str(resolved)!r}, which is "
            f"OUTSIDE the active workspace ({str(root)!r}). The edit will land in "
            f"a different directory than the terminal's cwd. If this is not "
            f"intended (e.g. a git-worktree session writing into the main "
            f"checkout), pass an absolute path under the workspace instead.")
    except Exception:
        return None
