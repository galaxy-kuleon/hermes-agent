#!/usr/bin/env python3
"""File Tools Module - LLM agent file manipulation tools.

Companions: ``file_tools_paths`` (task-aware resolution), ``file_tools_write_guards``
(write-side guards), ``file_tools_read_tracking`` (per-task dedup / loop-detection /
staleness state).
"""

import base64
import errno
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from agent.file_safety import get_nt_namespace_error, get_read_block_error
from agent.tool_result_classification import GUARDRAIL_REFUSAL_KEY
from tools.binary_extensions import has_binary_extension
from tools.skill_provenance import is_background_review
from tools.file_operations import (
    ShellFileOperations, normalize_read_pagination, normalize_search_pagination)
from tools.file_operations_common import DEFAULT_READ_LIMIT, count_conflict_blocks
from tools import file_state
from agent.redact import _is_secret_file_arg, redact_sensitive_text
from tools.file_tools_paths import (
    _expand_tilde, _path_resolution_warning, _resolve_base_dir, _resolve_path_for_task,
    _PerUserWorkspaceError, _resolve_path_for_task_with_scope, _workspace_path_access_error)
from tools.file_tools_write_guards import (
    _READ_DEDUP_STATUS_MESSAGE, _check_approval_required_write, _check_binary_document_write,
    _check_cross_profile_path, _check_protected_instruction_write, _check_sensitive_path,
    _is_internal_file_tool_content, _stale_overwrite_blocker, _stale_write_refusal)
from tools.file_tools_read_tracking import (
    forget_task_reads, _bump_consecutive, _cap_read_tracker_data, _check_file_staleness, _check_not_found_cache,
    _file_metadata, _file_version,
    _mark_full_write_baseline, _mark_verification_stale, _note_read_coverage, _patch_failure_lock,
    _patch_failure_tracker, _read_tracker, _read_tracker_lock, _record_not_found,
    _record_patch_failure, _reset_patch_failures, _task_data, _update_read_timestamp)

logger = logging.getLogger(__name__)


_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}

# Read-size guard. Model-agnostic, so characters proxy tokens: 100K chars is
# ~25-35K tokens across typical tokenisers. Configurable: file_read_max_chars.
_DEFAULT_MAX_READ_CHARS = 100_000
def _get_max_read_chars() -> int:
    """Return ``file_read_max_chars`` from config.yaml (default on missing/invalid). No module
    cache: ``load_config_readonly`` is already mtime+path cached, and a process-lifetime slot
    would pin the launch profile's value under the multiplexed gateway."""
    try:
        from hermes_cli.config import load_config_readonly
        val = load_config_readonly().get("file_read_max_chars")
    except Exception:
        val = None
    valid = isinstance(val, (int, float)) and val > 0
    return int(val) if valid else _DEFAULT_MAX_READ_CHARS


def _truncate_to_char_budget(content: str, max_chars: int) -> tuple[str, int, bool]:
    """Trim line-numbered ``read_file`` content to fit a char budget.

    Ported in spirit from nearai/ironclaw#5029 (dual line/byte cap on
    ``read_file``). Where hermes previously hard-rejected an oversized read
    (forcing the model to guess a smaller ``limit`` and burn a round-trip
    returning nothing), this trims the content to the last *complete line*
    that fits within ``max_chars`` and reports how many lines were kept so
    the caller can offer a ``next_offset`` continuation.

    ``content`` is the gutter-rendered text (``LINE_NUM|CONTENT`` joined by
    ``\\n``). Individual lines are already clamped to ``get_max_line_length()``
    upstream, so a single line never blows the whole budget on its own; the
    overflow this handles is the *accumulation* of many lines under the
    line-count limit (logs, wide CSV rows, minified data).

    Returns ``(kept_text, lines_kept, clamped_mid_line)``. If not even the
    first line fits, that single line is clamped on a code-point boundary
    (Python ``str`` slicing never splits a code point). The third value lets
    callers report the resulting non-recoverable line tail honestly.
    """
    if len(content) <= max_chars:
        return content, (content.count("\n") + 1 if content else 0), False

    lines = content.split("\n")
    kept: list[str] = []
    running = 0
    for line in lines:
        # +1 for the "\n" that rejoins this line to the previous one.
        addition = len(line) + (1 if kept else 0)
        if running + addition > max_chars:
            break
        kept.append(line)
        running += addition

    if not kept:
        # First line alone exceeds the budget. Clamp on a code-point
        # boundary rather than emitting nothing.
        kept.append(lines[0][:max_chars])
        return "\n".join(kept), len(kept), True

    return "\n".join(kept), len(kept), False


def _apply_char_budget(result_dict: dict, content: str, offset: int, total_lines, max_chars: int) -> str:
    """Trim *content* to the char budget, annotate *result_dict* with the
    continuation hint, and return the trimmed text."""
    trimmed, lines_kept, clamped_mid_line = _truncate_to_char_budget(content, max_chars)
    next_offset = offset + lines_kept
    result_dict["content"] = trimmed
    result_dict["truncated"] = True
    result_dict["truncated_by"] = "bytes"
    result_dict["truncated_reason"] = "char_limit"
    result_dict["next_offset"] = next_offset
    result_dict["hint"] = (
        f"Output truncated at the {max_chars:,}-char read budget after "
        f"{lines_kept} line(s) (showing lines {offset}-{next_offset - 1} of "
        f"{total_lines}). Use offset={next_offset} to continue.")
    if clamped_mid_line:
        result_dict["truncated_lines"] = True
        result_dict["hint"] += (
            " Note: the first line alone exceeded the budget and was "
            "clamped mid-line; its remainder is not retrievable via offset.")
    return trimmed


# Above this size, a wide read (limit > 200) gets a hint toward targeted reads.
_LARGE_FILE_HINT_BYTES = 512_000

# Device/fd paths whose reads hang the process. Checked by path only — no I/O.
_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",     # never reach EOF
    "/dev/stdin", "/dev/tty", "/dev/console",                    # block on input
    "/dev/stdout", "/dev/stderr",                                # nonsensical to read
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",                       # fd aliases
})
# /proc/<pid>/... (and /proc/<pid>/task/<tid>/...) files that leak secrets,
# argv, memory layout (ASLR oracle: maps family, auxv, pagemap) or raw memory.
_BLOCKED_PROC_SUFFIXES = (
    "/fd/0", "/fd/1", "/fd/2",  # stdio aliases
    "/environ", "/cmdline", "/maps", "/smaps", "/smaps_rollup", "/numa_maps",
    "/mem", "/auxv", "/pagemap")


def _file_ops_uses_host_paths(file_ops) -> bool:
    """True when *file_ops* targets the host filesystem (only then may we stat paths
    or rewrite V4A headers to host-absolute paths; sandboxes have their own namespace)."""
    env = getattr(file_ops, "env", None)
    if env is None:
        return True
    try:
        from tools.environments.local import LocalEnvironment
    except ImportError:
        return True
    return isinstance(env, LocalEnvironment)


# V4A file headers: group 1 = header prefix, 2 = op, 3 = path. ``\s*`` after
# ``***`` mirrors patch_parser's leniency (``***Update File:`` applies, so it
# must be checked).
_V4A_SINGLE_HEADER_RE = re.compile(r'^(\*\*\*\s*(Update|Add|Delete)\s+File:\s*)(.+)$', re.MULTILINE)
_V4A_MOVE_HEADER_RE = re.compile(r'^(\*\*\*\s*Move\s+File:\s*)(.+?)\s*->\s*(.+)$', re.MULTILINE)


def _rewrite_v4a_patch_paths_for_host(patch: str, path_to_resolved: dict, file_ops) -> str:
    """Rewrite V4A file headers to the resolved host paths (host backends only).

    The shell layer must patch the SAME files ``patch_tool`` resolved for
    locking/staleness, not re-resolve a relative header against its own cwd
    (which can differ — the git-worktree cwd bug).
    """
    if not _file_ops_uses_host_paths(file_ops):
        return patch

    def _res(raw: str) -> str:
        raw = raw.strip()
        return path_to_resolved.get(raw) or raw

    patch = _V4A_SINGLE_HEADER_RE.sub(lambda m: f"{m.group(1)}{_res(m.group(3))}", patch)
    return _V4A_MOVE_HEADER_RE.sub(lambda m: f"{m.group(1)}{_res(m.group(2))} -> {_res(m.group(3))}", patch)


def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd/proc paths that can hang reads or leak process state."""
    normalized = os.path.normpath(_expand_tilde(path))
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    return normalized.startswith("/proc/") and normalized.endswith(_BLOCKED_PROC_SUFFIXES)


def _is_blocked_device(filepath: str, base_dir: str | Path | None = None) -> bool:
    """True if the path (literal, any symlink hop, or final realpath) is a blocked device.

    Literal first so /dev/stdin is caught before resolving to a terminal path;
    every symlink hop is checked so an alias cannot bypass the guard.
    """
    expanded = _expand_tilde(filepath)
    if base_dir is not None and not os.path.isabs(expanded):
        expanded = os.path.join(os.fspath(base_dir), expanded)
    normalized = os.path.normpath(expanded)
    if _is_blocked_device_path(normalized):
        return True

    seen: set[str] = set()
    current = normalized
    for _ in range(20):
        try:
            target = os.readlink(current)
        except OSError:
            break
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        target = os.path.normpath(target)
        if _is_blocked_device_path(target):
            return True
        if target in seen:
            break
        seen.add(target)
        current = target

    try:
        resolved = os.path.normpath(os.path.realpath(normalized))
    except (OSError, ValueError):
        return False
    return _is_blocked_device_path(resolved)


def _resolved_match_path(path: str, task_id: str) -> str:
    """Best-effort task-cwd resolution of a search hit's path.

    Search backends may return cwd-relative paths while the process cwd differs, so both the
    read-block filter and the redaction classifier must resolve against the task cwd. An
    unresolvable path is used as-is (the raw path is still worth classifying).
    """
    try:
        return str(_resolve_path_for_task(path, task_id))
    except (OSError, ValueError, RuntimeError):
        return path


def _filter_read_blocked_search_results(result, task_id: str = "default") -> int:
    """Remove credential/cache/env paths from a SearchResult in-place; return the omitted count.

    Each path is resolved against the task cwd first (search backends may
    return cwd-relative paths; the process cwd can differ).
    """
    omitted = 0

    def _allowed(path: str) -> bool:
        nonlocal omitted
        if get_read_block_error(_resolved_match_path(path, task_id)):
            omitted += 1
            return False
        return True

    if getattr(result, "matches", None):
        result.matches = [m for m in result.matches if _allowed(m.path)]
    if getattr(result, "files", None):
        result.files = [f for f in result.files if _allowed(f)]
    if getattr(result, "counts", None):
        result.counts = {f: c for f, c in result.counts.items() if _allowed(f)}
    return omitted


def _is_expected_write_exception(exc: Exception) -> bool:
    """Return True for expected write denials that should not hit error logs."""
    return isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS)


# ── ShellFileOperations per terminal environment ─────────────────────────
_file_ops_lock = threading.Lock()
_file_ops_cache: dict = {}


def _create_terminal_env_for_file_ops(raw_task_id: str, task_id: str):
    """Build the terminal environment for *task_id* via the shared ``_create_configured_env``,
    so a file tool that runs before any terminal command still gets the configured backend."""
    from tools.terminal_tool_config import _is_container_backend
    from tools.terminal_tool import (
        _create_configured_env, _get_env_config, _is_unusable_container_cwd,
        _resolve_task_host_cwd, _select_image, get_session_cwd, resolve_task_overrides)

    config = _get_env_config()
    env_type = config["env_type"]
    overrides = resolve_task_overrides(raw_task_id)
    try:
        recorded_cwd = get_session_cwd(raw_task_id)
    except Exception:
        recorded_cwd = None
    cwd = overrides.get("cwd") or recorded_cwd or config["cwd"]
    # Re-apply the container cwd guard: a gateway/TUI/ACP override is a raw HOST
    # path and ``docker run -w <host-path>`` makes search_files & co silently
    # return nothing. Valid in-container overrides (/workspace, /root) pass.
    # Re-apply the container cwd guard that _get_env_config() already ran on config["cwd"] (see #50636). A
    # per-task cwd override registered by the gateway/TUI/ACP for workspace tracking is a raw host path
    # (e.g. a Desktop session's /Users/<me>/workspace or C:\\Users\\<me>). On a container backend that
    # reaches ``docker run -w <host-path>`` and the container starts in a directory that doesn't exist
    # inside the sandbox, so search_files and friends silently return empty results (#54447). Sanitize it
    # back to the already-validated config["cwd"] so the override can't bypass the guard.
    if _is_container_backend(env_type) and _is_unusable_container_cwd(cwd):
        if cwd != config["cwd"]:
            logger.info(
                "Ignoring host/relative cwd override %r for %s backend "
                "(won't exist in sandbox). Using %r instead.",
                cwd, env_type, config["cwd"])
        cwd = config["cwd"]
    logger.info("Creating new %s environment for task %s...", env_type, task_id[:8])
    terminal_env = _create_configured_env(
        config, env_type, image=_select_image(env_type, overrides, config), cwd=cwd,
        timeout=config["timeout"], task_id=task_id,
        host_cwd=_resolve_task_host_cwd(config, raw_task_id),
        local_config={"persistent": config.get("local_persistent", False)} if env_type == "local" else None,
    )
    return env_type, terminal_env


def _get_file_ops(task_id: str = "default") -> ShellFileOperations:
    """Get or create ShellFileOperations for the task's terminal environment.

    Uses terminal_tool's per-task creation locks (no duplicate sandboxes).
    Subagent task_ids collapse to "default" (``_resolve_container_task_id``) so
    delegate_task children share the parent's container; RL/benchmark task_ids
    with a registered env override keep their isolation.
    """
    from tools.terminal_tool import (
        _active_environments, _env_lock, _last_activity, _start_cleanup_thread,
        _creation_locks, _creation_locks_lock, _resolve_container_task_id,
        get_session_cwd, record_session_cwd)

    raw_task_id = task_id or "default"
    task_id = _resolve_container_task_id(raw_task_id)

    # Fast path: cached AND the environment is still alive (cleanup thread may have killed it).
    with _file_ops_lock:
        cached = _file_ops_cache.get(task_id)
    if cached is not None:
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                return cached
            # Env was cleaned up: rescue its cwd into the session record FILL-ONLY
            # (``cached.cwd`` is the SHARED env's cwd, not this session's own).
            # Environment was cleaned up -- preserve the old cwd in the session record before invalidating
            # the stale cache entry (fixes #26211: silent file-creation failures in long-running
            # conversations). Usually a no-op: every completed command already recorded its cwd. Fill-only:
            # ``cached.cwd`` is a snapshot of the SHARED env's cwd at cache-build time, so it is not
            # attributable to this session (same class as the interrupted-command bug, #85658). Rescue a
            # session that has no record, but never overwrite a record the session wrote for itself.
            old_cwd = getattr(cached, "cwd", None)
            if old_cwd:
                try:
                    if get_session_cwd(raw_task_id) is None:
                        record_session_cwd(raw_task_id, old_cwd)
                except Exception:
                    pass
            with _file_ops_lock:
                _file_ops_cache.pop(task_id, None)

    with _creation_locks_lock:
        task_lock = _creation_locks.setdefault(task_id, threading.Lock())

    with task_lock:
        # Double-check: another thread may have created it while we waited.
        with _env_lock:
            terminal_env = _active_environments.get(task_id)
            if terminal_env is not None:
                _last_activity[task_id] = time.time()
        if terminal_env is None:
            env_type, terminal_env = _create_terminal_env_for_file_ops(raw_task_id, task_id)
            with _env_lock:
                _active_environments[task_id] = terminal_env
                _last_activity[task_id] = time.time()
            _start_cleanup_thread()
            logger.info("%s environment ready for task %s", env_type, task_id[:8])

    file_ops = ShellFileOperations(terminal_env)
    with _file_ops_lock:
        _file_ops_cache[task_id] = file_ops
    return file_ops


def clear_file_ops_cache(task_id: str = None):
    """Clear file-operation state for a finished task, or all tasks."""
    with _file_ops_lock:
        if task_id:
            _file_ops_cache.pop(task_id, None)
        else:
            _file_ops_cache.clear()

    with _read_tracker_lock:
        if task_id:
            _read_tracker.pop(task_id, None)
        else:
            _read_tracker.clear()

    with _patch_failure_lock:
        if task_id:
            _patch_failure_tracker.pop(task_id, None)
        else:
            _patch_failure_tracker.clear()

    if task_id:
        file_state.get_registry().forget_task(task_id)
    else:
        file_state.get_registry().clear()


_SPECIAL_FILE_KINDS = (
    (stat.S_ISFIFO, "a FIFO (named pipe)"),
    (stat.S_ISSOCK, "a socket"),
    (stat.S_ISCHR, "a character device"),
    (stat.S_ISBLK, "a block device"))


_HANDOFF_NAME_PREFIX_RE = re.compile(r"^\d{3}-[0-9a-f]{8}-")
_SOURCE_HASH_CHUNK_BYTES = 1024 * 1024
_ACL_PROTECTED_READ_DENY = (
    "Hermes skills ACL: reading under a protected skills path requires skill "
    "read permission for the current OpenWebUI role/group scope; denied."
)
_ACL_PROTECTED_WRITE_DENY = (
    "Hermes skills ACL: raw writes under protected skills paths are denied; "
    "use trusted native skill_manage with an explicit namespace."
)
_ACL_RAW_FILE_WRITE_DENY = (
    "Hermes skills ACL: raw file writes are unavailable on the multi-user "
    "api_server; use trusted native skill_manage for skill mutations or a "
    "purpose-built tool for generated artifacts."
)
_ACL_GUARDED_CODE_EXEC_MCP_SERVERS = frozenset({"opencode_runner"})


def _sha256_file(path: str) -> str:
    """Return the full server-observed source digest without loading it twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(_SOURCE_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _acl_raw_file_write_block() -> str | None:
    """Deny every raw file mutation on the ACL-enabled multi-user surface.

    Skill Editor/Admin authority governs the shared library only through the
    native ``skill_manage`` contract; it is not shell or generic filesystem
    authority. This runtime gate backs up schema minimization so an
    unadvertised/direct dispatch of ``write_file`` or ``patch`` cannot persist
    same-UID code and bypass the isolated writer's authenticated boundary.

    Local CLI/cron surfaces and ACL-disabled deployments retain their existing
    file-write behavior. Once the surface is known to be ``api_server``, ACL
    configuration errors fail closed.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return None
    except Exception:
        return None
    try:
        from tools.skill_acl import load_skill_acl_config

        if not load_skill_acl_config().get("enabled"):
            return None
    except Exception:
        logger.warning(
            "skills_acl_raw_file_write_denied platform=api_server "
            "reason=acl_config_unavailable"
        )
        return _ACL_RAW_FILE_WRITE_DENY
    logger.warning(
        "skills_acl_raw_file_write_denied platform=api_server reason=acl_enabled"
    )
    return _ACL_RAW_FILE_WRITE_DENY


def _path_is_at_or_under(target: Path, root: Path) -> bool:
    """Return True when *target* is *root* or a descendant of *root*."""
    return target == root or root in target.parents


def _acl_builtin_skill_path_scope(target: Path) -> str | None:
    """Classify built-in skill roots for api_server ACL decisions.

    Returns one of:
    - ``platform``: shared platform skill library
    - ``own_user``: current OpenWebUI caller's own user namespace
    - ``other_user``: the user-skills base itself or a sibling user's namespace
    - ``writer_control``: isolated-writer socket or authentication material
    - ``None``: not a built-in skill root path

    The classification is intentionally ownership-aware for ``user-skills``.
    Generic ``read``/``update`` permission must never reveal or mutate sibling
    user roots.
    """
    from agent.skill_namespaces import (
        get_current_user_skills_dir,
        get_user_skills_base_dir,
    )
    from tools.shared_skill_writer import writer_secret_path, writer_socket_path
    from tools.skill_state import platform_skills_dir

    writer_socket_root = writer_socket_path().resolve().parent
    writer_secret = writer_secret_path().resolve()
    if _path_is_at_or_under(target, writer_socket_root) or target == writer_secret:
        return "writer_control"

    platform_root = platform_skills_dir().resolve()
    if _path_is_at_or_under(target, platform_root):
        return "platform"

    user_base = get_user_skills_base_dir().resolve()
    if not _path_is_at_or_under(target, user_base):
        return None

    current_user_dir = get_current_user_skills_dir()
    if current_user_dir is not None:
        try:
            own_root = current_user_dir.resolve()
            if _path_is_at_or_under(target, own_root):
                return "own_user"
        except OSError:
            pass
    return "other_user"


def _acl_protected_path_block(
    path,
    mode: str = "write",
    task_id: str = "default",
    permission: str | None = None,
):
    """Return a non-leaky denial if a file *mode* op on *path* would bypass the
    skill ACL by touching a protected skills directory, else ``None`` (allow).

    *mode* is ``"read"`` (requires skill read permission for shared/protected
    roots) or ``"write"``. Raw writes to protected skill roots are never
    authorized by shared create/update/delete grants; callers must use the
    trusted native ``skill_manage`` API, which preserves action-level ACLs and
    delete independence. ``permission`` remains a compatibility argument for
    existing file-tool callers but cannot authorize a protected raw write.
    **api_server-platform only** — CLI/cron/chat are the trusted owner / gated
    elsewhere and exempt. The concrete platform root remains write-protected even
    when ACL is disabled. User namespaces are always ownership-aware: own user
    reads are allowed, sibling user roots are hidden, and raw writes are denied.
    Other configured protected roots use legacy behavior when ACL is disabled.
    When ACL is ENABLED on api_server, any resolution error **fails CLOSED**.
    ``..``/symlink traversal is neutralised via ``Path.resolve()``. The reason
    reveals no skill names/contents.
    """
    if not path:
        return None
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return None
    except Exception:
        return None
    deny = _ACL_PROTECTED_READ_DENY if mode == "read" else _ACL_PROTECTED_WRITE_DENY
    try:
        from tools.skill_acl import load_skill_acl_config, resolve_skill_permissions
        from gateway.session_context import get_session_env

        try:
            target = Path(_resolve_path_for_task(path, task_id))
        except Exception:
            target = Path(os.path.expanduser(str(path)))
        target = target.resolve()

        builtin_scope = _acl_builtin_skill_path_scope(target)
        if builtin_scope in {"other_user", "writer_control"}:
            return deny
        if builtin_scope == "own_user":
            return None if mode == "read" else _ACL_PROTECTED_WRITE_DENY
        if builtin_scope == "platform" and mode != "read":
            return _ACL_PROTECTED_WRITE_DENY

        cfg = load_skill_acl_config()
        if not cfg.get("enabled"):
            return None
        protect = list(cfg.get("protect_paths") or [])
        if not protect:
            from hermes_constants import get_hermes_home

            protect = [str(Path(get_hermes_home()) / "skills")]
        under = False
        for root in protect:
            try:
                root_p = Path(os.path.expanduser(str(root))).resolve()
            except Exception:
                continue
            if _path_is_at_or_under(target, root_p):
                under = True
                break
        if not under:
            return None  # not a protected path -> normal file op
        if mode != "read":
            return _ACL_PROTECTED_WRITE_DENY
        role = get_session_env("HERMES_SESSION_USER_ROLE", "")
        groups = get_session_env("HERMES_SESSION_USER_GROUPS", "")
        perms = resolve_skill_permissions(role, groups, cfg)
        if "read" in perms:
            return None  # caller is permitted for this protected-path op
        return deny
    except Exception:
        return deny  # fail closed


def _acl_guard_code_exec_mcp_call(server_name, args, task_id: str = "default"):
    """Deny local arbitrary-code MCP on the multi-user ACL surface.

    A cwd-only check cannot constrain absolute-path access by a subprocess. When
    the skills ACL is enabled for ``api_server``, every locally launched server in
    :data:`_ACL_GUARDED_CODE_EXEC_MCP_SERVERS` is therefore denied regardless of
    its cwd. Remote isolated MCP servers (notably ``soc_v2``) remain unaffected.
    ACL-disabled and non-api_server surfaces preserve legacy behavior.
    """
    if server_name not in _ACL_GUARDED_CODE_EXEC_MCP_SERVERS:
        return None
    try:
        from gateway.session_context import get_session_env
        from tools.skill_acl import load_skill_acl_config

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return None
        if not load_skill_acl_config().get("enabled"):
            return None
    except Exception:
        return _ACL_PROTECTED_WRITE_DENY
    if not isinstance(args, dict):
        return _ACL_PROTECTED_WRITE_DENY
    return (
        "Hermes skills ACL: local arbitrary-code MCP is unavailable on the "
        "multi-user api_server surface; use trusted native tools."
    )


def _acl_filter_search_result(result, task_id: str = "default"):
    """Exclude search matches/files/counts under protected or sibling skill roots.

    Per-RESULT filtering (not just the search root) so legitimate non-protected
    matches still return while hidden paths AND their content/snippets are
    withheld; ``total_count`` is recomputed so hidden matches are not implied.
    api_server-platform only; ACL disabled leaves shared/configured roots
    unchanged, but sibling ``user-skills`` roots remain hidden. Resolution errors
    while api_server+enabled fail CLOSED by dropping all results.
    """
    if result is None:
        return result
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return result
    except Exception:
        return result

    def _blank(r):
        try:
            r.matches = []
            r.files = []
            r.counts = {}
            r.total_count = 0
            r.truncated = False
        except Exception:
            pass
        return r

    try:
        from tools.skill_acl import load_skill_acl_config, resolve_skill_permissions
        from gateway.session_context import get_session_env

        cfg = load_skill_acl_config()
        acl_enabled = bool(cfg.get("enabled"))
        shared_read_allowed = True
        if acl_enabled:
            role = get_session_env("HERMES_SESSION_USER_ROLE", "")
            groups = get_session_env("HERMES_SESSION_USER_GROUPS", "")
            shared_read_allowed = "read" in resolve_skill_permissions(role, groups, cfg)
        protect = list(cfg.get("protect_paths") or [])
        if not protect:
            from hermes_constants import get_hermes_home

            protect = [str(Path(get_hermes_home()) / "skills")]
        roots = []
        for _r in protect:
            try:
                roots.append(Path(os.path.expanduser(str(_r))).resolve())
            except Exception:
                continue
    except Exception:
        return _blank(result)  # api_server but unresolvable -> fail closed

    def _hidden(p) -> bool:
        try:
            tp = Path(_resolve_path_for_task(p, task_id)).resolve()
        except Exception:
            try:
                tp = Path(os.path.expanduser(str(p))).resolve()
            except Exception:
                return True  # unresolvable -> treat as hidden (fail closed)
        scope = _acl_builtin_skill_path_scope(tp)
        if scope in {"other_user", "writer_control"}:
            return True
        if scope == "own_user":
            return False
        if scope == "platform":
            return not shared_read_allowed
        for root in roots:
            if _path_is_at_or_under(tp, root):
                return not shared_read_allowed
        return False

    try:
        if getattr(result, "matches", None):
            result.matches = [m for m in result.matches if not _hidden(m.path)]
        if getattr(result, "files", None):
            result.files = [f for f in result.files if not _hidden(f)]
        if getattr(result, "counts", None):
            result.counts = {k: v for k, v in result.counts.items() if not _hidden(k)}
        result.total_count = (
            len(getattr(result, "matches", []) or [])
            + len(getattr(result, "files", []) or [])
            + len(getattr(result, "counts", {}) or {})
        )
        result.truncated = False
    except Exception:
        return _blank(result)
    return result


def _vision_auto_read_image(*, target: str, task_id: str) -> dict | None:
    """Best-effort vision analysis when ``read_file`` is pointed at an image.

    Returns a success dict from vision_analyze, or None when vision is
    unavailable / fails. Callers fall back to the explicit recovery instruction.

    Handles (``F01``) must be resolved to a real path before calling
    ``vision_analyze_tool``: that coroutine opens ``image_url`` directly and
    does not re-run grant alias resolution (only the tool registry wrapper
    does). Passing a bare handle is a silent no-op — vision fails and we
    fall back to the recovery error (WWTP RELH 2026-08-02).
    """
    try:
        from model_tools import _run_async
        from tools.file_grants import resolve_grant_alias
        from tools.vision_tools import vision_analyze_tool

        image_url = resolve_grant_alias(str(target), task_id=task_id)
        vision_raw = _run_async(
            vision_analyze_tool(
                image_url=image_url,
                user_prompt=(
                    "Describe this image in detail for document "
                    "and file analysis. Include any visible text."
                ),
                task_id=task_id,
            )
        )
        vision_parsed = json.loads(vision_raw) if isinstance(vision_raw, str) else vision_raw
        if isinstance(vision_parsed, dict) and vision_parsed.get("success"):
            return vision_parsed
    except Exception:
        return None
    return None


def _advance_past_covered_lines(
    path: str,
    *,
    offset: int,
    task_id: str,
) -> tuple[int, dict | None]:
    """Move a stale document offset to the first uncovered line.

    Context compression can preserve the attachment coverage ledger while a
    model forgets the last ``next_offset``.  Replaying a covered 100K-character
    slice is pure context churn.  The ledger is mechanism-level state, so use
    it to resume at the first gap; callers that truly need an earlier passage
    can still request a narrower range that begins outside the covered prefix.
    """
    from tools import request_file_cache
    from tools.attachment_ledger import get_outcome, merge_ranges

    outcome = get_outcome(path, task_id=task_id)
    if not outcome or str(outcome.get("extent_unit") or "lines") != "lines":
        return offset, outcome
    # Durable coverage proves what the service read in an older request; it
    # does not prove that the older tool payload is still present in this
    # request's model context.  Allow one content-bearing refill before using
    # the old ranges to auto-advance.  The request memo then makes every later
    # repeat cheap, while a new user turn can always recover the source text.
    if (
        str(outcome.get("reader") or "") == "read_file_history"
        and request_file_cache.lookup_latest(path, task_id=task_id) is None
    ):
        return offset, outcome
    candidate = offset
    for start, end in merge_ranges(outcome.get("ranges") or []):
        if start <= candidate <= end:
            candidate = end + 1
        elif start > candidate:
            break
    return candidate, outcome


def _route_exact_skill_file(
    path: str,
    *,
    task_id: str,
    source_tool: str,
    requested_pattern: str | None = None,
) -> str | None:
    """Safely redirect an exact skill-file read to the skill API.

    This does not grant file access. ``skill_view`` performs its normal ACL
    check, path containment validation, linked-file validation, and output
    handling. Directory searches and non-skill paths deliberately fall through
    to the ordinary file-grant boundary.
    """
    from tools.file_grants import skill_view_target_for_path

    target = skill_view_target_for_path(path)
    if target is None:
        return None

    from tools.skills_tool import _skill_view_with_bump

    result = _skill_view_with_bump(
        target,
        task_id=task_id,
        force_content=True,
    )
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict):
        return result
    payload["routing"] = {
        "from_tool": source_tool,
        "to_tool": "skill_view",
        "reason": "exact_skill_file_path",
        "requested_path": str(path),
        "skill_name": target["name"],
        "file_path": target.get("file_path"),
    }
    if requested_pattern is not None:
        payload["routing"]["requested_pattern"] = requested_pattern
        payload["routing"]["note"] = (
            "The complete authoritative skill file is returned; search it in "
            "the supplied content without retrying file tools."
        )
    content_returned = isinstance(payload.get("content"), str)
    payload["content_returned"] = content_returned
    if payload.get("success") and content_returned:
        # Make the completeness contract machine-readable and unambiguous.
        # These fields describe the skill API payload, not a best-effort grep
        # snippet: the caller has the complete authoritative file bytes.
        payload["complete"] = True
        payload["truncated"] = False
        payload["source_available"] = True
        payload["authoritative"] = True
    return json.dumps(payload, ensure_ascii=False)


def _targeted_skill_search_result(
    routed: str,
    *,
    pattern: str,
    context: int,
    limit: int,
    offset: int,
    output_mode: str,
) -> str:
    """Search authoritative skill bytes without reinserting the whole file.

    ``skill_view`` remains the ACL and source-of-truth boundary. Once it has
    returned the exact requested file, a search tool should do search work:
    return the matching lines and requested neighbourhood, not a multi-KB file
    body that forces the model to search it again in its context.
    """
    try:
        source_payload = json.loads(routed)
    except (TypeError, ValueError):
        return routed
    if not isinstance(source_payload, dict):
        return routed
    source = source_payload.get("content")
    if not source_payload.get("success") or not isinstance(source, str):
        return routed

    try:
        expression = re.compile(pattern)
    except re.error as exc:
        return tool_error(f"Invalid search regex: {exc}")

    offset, limit = normalize_search_pagination(offset, limit)
    context = max(0, int(context or 0))
    lines = source.splitlines()
    all_hits = [
        (line_number, line)
        for line_number, line in enumerate(lines, start=1)
        if expression.search(line)
    ]
    selected = all_hits[offset : offset + limit]
    requested_path = str(
        (source_payload.get("routing") or {}).get("requested_path") or ""
    )
    truncated = offset + len(selected) < len(all_hits)

    result = {
        "success": True,
        "name": source_payload.get("name"),
        "file": source_payload.get("file")
        or (source_payload.get("routing") or {}).get("file_path"),
        "total_count": len(all_hits),
        "offset": offset,
        "limit": limit,
        "context": context,
        "matches": [
            {"path": requested_path, "line": line_number, "content": line}
            for line_number, line in selected
        ],
        "routing": dict(source_payload.get("routing") or {}),
        "complete": not truncated,
        "truncated": truncated,
        "source_available": True,
        "authoritative": True,
    }
    result["routing"]["note"] = (
        "Search was evaluated locally against the complete authoritative skill "
        "file; only exact matches and requested context are returned."
    )

    if output_mode == "count":
        result["counts"] = {requested_path: len(all_hits)}
        result["content_returned"] = False
        return json.dumps(result, ensure_ascii=False)
    if output_mode == "files_only":
        result["files"] = [requested_path] if all_hits else []
        result["content_returned"] = False
        return json.dumps(result, ensure_ascii=False)

    windows = []
    for line_number, _ in selected:
        start = max(1, line_number - context)
        end = min(len(lines), line_number + context)
        if windows and start <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    blocks = [
        "\n".join(f"{number}:{lines[number - 1]}" for number in range(start, end + 1))
        for start, end in windows
    ]
    result["content"] = "\n--\n".join(blocks)
    result["content_returned"] = bool(blocks)
    return json.dumps(result, ensure_ascii=False)


def _literal_relative_search_file(file_glob: str | None) -> str | None:
    """Return one safe literal relative file path, or ``None``.

    ``search_files`` calls generated by models sometimes put an exact linked
    skill support file (for example ``references/class-35.md``) in the
    historical ``file_glob`` field.  Treat that value as a file only when it
    cannot expand or escape the supplied directory.  The skill-path resolver
    remains responsible for deciding whether the joined path is an
    authoritative linked skill file.
    """
    if not file_glob or any(char in file_glob for char in "*?["):
        return None
    if "\\" in file_glob or file_glob.startswith("/"):
        return None
    parts = PurePosixPath(file_glob).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _special_file_kind(path) -> str | None:
    """Human name for a non-regular file type that would hang a read, else None.

    Stat-based sibling of ``_is_blocked_device``: a FIFO/socket in a workspace
    hangs like ``/dev/zero`` but has no recognizable name. Host filesystems
    only; unstattable paths return None and flow to the normal read path.
    """
    try:
        mode = os.stat(os.fspath(path)).st_mode  # follows symlinks, matching a real read
    except OSError:
        return None
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
        return None
    return next((label for predicate, label in _SPECIAL_FILE_KINDS if predicate(mode)),
                "a special (non-regular) file")


def _read_extracted_document(path: str, _resolved, offset: int, limit: int, task_id: str) -> str | None:
    from tools.attachment_ledger import (
        OUTCOME_PARTIAL,
        OUTCOME_UNREADABLE,
        record_outcome,
        record_read_extent,
    )
    from tools.file_magic import magic_conflicts_with_suffix
    from tools.file_reader_routing import UNREADABLE_REPORT_INSTRUCTION
    from tools.read_extract import (
        ANYDOC_EXTENSIONS,
        EXTRACTABLE_EXTENSIONS,
        MAX_DOCUMENT_BYTES,
        ExtractionError,
        extract_document_bytes,
        extract_document_text,
        is_extractable_document,
    )

    resolved_path = str(_resolved)
    display = _HANDOFF_NAME_PREFIX_RE.sub(
        "", Path(resolved_path).name, count=1
    ) or Path(resolved_path).name
    ext = Path(resolved_path).suffix.lower() or "document"
    from tools.file_grants import file_handle_for_path
    handle_for_ledger = file_handle_for_path(resolved_path, task_id=task_id) or ""

    def _ledger(status: str, reason: str = "", gaps: list | None = None) -> None:
        record_outcome(
            resolved_path,
            task_id=task_id,
            status=status,
            reason=reason,
            gaps=gaps,
            display_name=display,
            handle=handle_for_ledger,
        )

    conflict = False
    sniffed = None
    if _file_ops_uses_host_paths(_get_file_ops(task_id)):
        conflict, sniffed, _ = magic_conflicts_with_suffix(resolved_path)
    if conflict:
        reason = f"declared suffix {ext} conflicts with sniffed type {sniffed}"
        _ledger(OUTCOME_UNREADABLE, reason=reason)
        return json.dumps(
            {
                "error": (
                    f"Cannot safely read '{display}': {reason}. "
                    f"{UNREADABLE_REPORT_INSTRUCTION}"
                ),
                "readable": False,
                "extraction_failed": True,
                "report_as": "unreadable",
                "name": display,
                "sniffed_kind": sniffed,
            },
            ensure_ascii=False,
        )

    if is_extractable_document(resolved_path):
        file_ops = _get_file_ops(task_id)
        binary = None
        document_bytes = b""
        document_gaps: list[str] = []
        extraction_cache_kind = "request"
        from tools import document_extract_cache, request_file_cache

        cached_document = request_file_cache.lookup_document(
            resolved_path, task_id=task_id
        )
        try:
            if cached_document is not None:
                extracted_text = str(cached_document.get("text") or "")
                file_size = int(cached_document.get("file_size") or 0)
                document_gaps = list(cached_document.get("gaps") or [])
                source_sha256 = str(
                    cached_document.get("source_sha256") or ""
                )
            elif (
                _file_ops_uses_host_paths(file_ops)
                and os.path.isfile(resolved_path)
            ):
                file_size = os.path.getsize(resolved_path)
                if file_size > MAX_DOCUMENT_BYTES:
                    raise ExtractionError(
                        "Document too large to convert "
                        f"({file_size:,} bytes, limit is "
                        f"{MAX_DOCUMENT_BYTES:,})"
                    )
                source_sha256 = _sha256_file(resolved_path)
                ext = Path(resolved_path).suffix.lower()
                with document_extract_cache.extraction_lock(source_sha256, ext):
                    persistent = document_extract_cache.lookup(source_sha256, ext)
                    if persistent is not None:
                        extracted_text = str(persistent["text"])
                        document_gaps = list(persistent.get("gaps") or [])
                        file_size = int(persistent.get("file_size") or file_size)
                        extraction_cache_kind = "persistent"
                    else:
                        # Avoid the old 4/3 base64 transport expansion for
                        # local 80+ MB PDFs. anydoc reads the path directly.
                        extracted_text = extract_document_text(
                            resolved_path, gaps_out=document_gaps
                        )
                        document_extract_cache.remember(
                            source_sha256,
                            ext,
                            text=extracted_text,
                            file_size=file_size,
                            gaps=document_gaps,
                        )
                        extraction_cache_kind = "miss"
                request_file_cache.remember_document(
                    resolved_path,
                    task_id=task_id,
                    text=extracted_text,
                    file_size=file_size,
                    gaps=document_gaps,
                    source_sha256=source_sha256,
                )
            else:
                binary = file_ops.read_file_bytes(
                    resolved_path, max_bytes=MAX_DOCUMENT_BYTES
                )
                if binary.error or binary.base64_content is None:
                    raise ExtractionError(
                        binary.error or "Document bytes unavailable"
                    )
                document_bytes = base64.b64decode(
                    binary.base64_content, validate=True
                )
                file_size = getattr(binary, "file_size", len(document_bytes))
                source_sha256 = hashlib.sha256(document_bytes).hexdigest()
                ext = Path(resolved_path).suffix.lower()
                with document_extract_cache.extraction_lock(source_sha256, ext):
                    persistent = document_extract_cache.lookup(source_sha256, ext)
                    if persistent is not None:
                        extracted_text = str(persistent["text"])
                        document_gaps = list(persistent.get("gaps") or [])
                        file_size = int(persistent.get("file_size") or file_size)
                        extraction_cache_kind = "persistent"
                    else:
                        extracted_text = extract_document_bytes(
                            document_bytes, resolved_path, gaps_out=document_gaps
                        )
                        document_extract_cache.remember(
                            source_sha256,
                            ext,
                            text=extracted_text,
                            file_size=file_size,
                            gaps=document_gaps,
                        )
                        extraction_cache_kind = "miss"
                request_file_cache.remember_document(
                    resolved_path,
                    task_id=task_id,
                    text=extracted_text,
                    file_size=file_size,
                    gaps=document_gaps,
                    source_sha256=source_sha256,
                )
        except (
            ExtractionError,
            OSError,
            ValueError,
            base64.binascii.Error,
        ) as exc:
            reason = str(exc).strip() or type(exc).__name__
            logger.warning(
                "document_extraction_failed task=%s ext=%s kind=%s reason=%s",
                task_id,
                ext,
                type(exc).__name__,
                reason[:240],
            )
            binary_doc = ext in ANYDOC_EXTENSIONS or (
                ext in EXTRACTABLE_EXTENSIONS and ext != ".ipynb"
            )
            if binary_doc:
                _ledger(OUTCOME_UNREADABLE, reason=reason)
                return json.dumps(
                    {
                        "error": (
                            f"Cannot extract readable text from '{display}' "
                            f"({ext}): document extraction failed — {reason}. "
                            f"{UNREADABLE_REPORT_INSTRUCTION}"
                        ),
                        "readable": False,
                        "extraction_failed": True,
                        "failure_kind": type(exc).__name__,
                        "report_as": "unreadable",
                        "name": display,
                    },
                    ensure_ascii=False,
                )
        else:
            if ext == ".msg":
                from tools.msg_extract import inspect_msg_capability_gaps

                document_gaps = inspect_msg_capability_gaps(resolved_path)
            elif ext == ".eml" and not document_gaps:
                document_gaps = ["body_only_extraction"]
            lines = extracted_text.splitlines()
            total_lines = len(lines)
            end_line = offset + limit - 1
            page_end = min(end_line, total_lines) if total_lines else end_line
            page_text = "\n".join(lines[offset - 1:end_line])
            from tools.tool_output_limits import get_max_line_length
            line_clamped = any(len(line) > get_max_line_length() for line in page_text.splitlines())
            rendered = (
                file_ops._add_line_numbers(page_text, offset)
                if page_text else ""
            )
            max_chars = _get_max_read_chars()
            char_limited = len(rendered) > max_chars
            clamped_mid_line = False
            lines_kept = len(page_text.splitlines()) if page_text else 0
            if char_limited:
                rendered, lines_kept, clamped_mid_line = (
                    _truncate_to_char_budget(rendered, max_chars)
                )
            consumed_end = (
                offset + lines_kept - 1 if lines_kept else offset - 1
            )
            has_more = bool(total_lines and consumed_end < total_lines)
            format_gaps = list(document_gaps)
            if line_clamped:
                format_gaps.append("line_exceeded_display_limit")
            if clamped_mid_line:
                format_gaps.append("single_line_exceeded_char_budget")
            settled = record_read_extent(
                resolved_path,
                task_id=task_id,
                start=offset,
                end=consumed_end,
                total=total_lines if total_lines else None,
                format_gaps=format_gaps or None,
                reason=(
                    "extraction incomplete for this format"
                    if format_gaps
                    else "document_extracted"
                ),
                display_name=display,
                handle=handle_for_ledger,
                reader="read_file",
            )
            report_as = settled if settled in {"read", "partial"} else (
                "partial" if has_more or format_gaps else "read"
            )
            result_dict = {
                "content": rendered,
                "total_lines": total_lines,
                "file_size": file_size,
                "truncated": has_more,
                "extracted_document": True,
                "readable": report_as == "read",
                "report_as": report_as,
                "coverage": report_as,
                "name": display,
                "extraction_cache": extraction_cache_kind,
                "consumed": {
                    "unit": "lines",
                    "start": offset,
                    "end": consumed_end,
                    "total": total_lines,
                },
                "source": {
                    "request_handle": handle_for_ledger or None,
                    "sha256": source_sha256,
                    "bytes": file_size,
                    "representation": (
                        "docling_markdown_or_text_with_ocr"
                        if ext == ".pdf"
                        else "converter_extracted_text"
                    ),
                    "extent": {
                        "unit": "lines",
                        "start": offset,
                        "end": consumed_end,
                        "total": total_lines,
                    },
                },
            }
            if ext == ".pdf":
                from tools.pdf_extract import PDF_PAGE_BREAK_PLACEHOLDER

                result_dict["source"]["document_extent"] = {
                    "unit": "pages",
                    "total": extracted_text.count(PDF_PAGE_BREAK_PLACEHOLDER) + 1,
                    "boundary_marker": PDF_PAGE_BREAK_PLACEHOLDER,
                }
            result_gaps = list(format_gaps)
            if has_more:
                result_gaps.append(
                    f"uncovered_lines={consumed_end + 1}-{total_lines}"
                )
            if result_gaps:
                result_dict["gaps"] = result_gaps
                result_dict["coverage_note"] = (
                    "Partial extraction only; continue every uncovered line "
                    "range and preserve any named format gaps."
                )
            if has_more:
                next_offset = consumed_end + 1
                result_dict["next_offset"] = next_offset
                result_dict["hint"] = (
                    f"Use offset={next_offset} to continue reading "
                    f"(showing {offset}-{consumed_end} of {total_lines} lines). "
                    "Do not claim full coverage yet."
                )
            if line_clamped or clamped_mid_line:
                result_dict["truncated_lines"] = True
            if char_limited:
                result_dict["truncated_by"] = "bytes"
                result_dict["truncated_reason"] = "char_limit"
                result_dict["hint"] = (
                    f"Output truncated at the {max_chars:,}-char read budget "
                    f"(lines {offset}-{consumed_end} of {total_lines}). Use "
                    f"offset={consumed_end + 1} to continue; Do not claim full "
                    "coverage yet."
                )
            unredacted = result_dict["content"]
            if unredacted:
                result_dict["content"] = redact_sensitive_text(
                    unredacted, file_read=True, secret_file=_is_secret_file_arg(resolved_path)
                )
            if (offset == 1 and report_as == "read" and not result_dict.get("truncated_lines")
                    and result_dict["content"] == unredacted):
                _mark_full_write_baseline(resolved_path, task_id)
                _update_read_timestamp(resolved_path, task_id)
                file_state.record_read(task_id, resolved_path)
            return json.dumps(result_dict, ensure_ascii=False)


def _dedup_stub_or_block(task_data: dict, dedup_key: tuple, path: str) -> str:
    """Return the "unchanged" stub for a repeated identical read, escalating to a
    hard BLOCK after 2 stubs so weak tool-followers don't loop forever."""
    with _read_tracker_lock:
        hits = task_data["dedup_hits"].get(dedup_key, 0) + 1
        task_data["dedup_hits"][dedup_key] = hits
        _cap_read_tracker_data(task_data)

    if hits >= 2:
        return tool_error(
            f"BLOCKED: You have called read_file on this "
            f"exact region {hits + 1} times and the file "
            "has NOT changed. STOP calling read_file for "
            "this path — the content from your earlier "
            "read_file result in this conversation is "
            "still current. Proceed with your task using "
            "the information you already have.",
            path=path,
            already_read=hits + 1,
            # A REFUSAL the harness chose, not a failure the tool hit: without the
            # marker the failure classifiers count the block and a repeated read
            # escalates to `repeated_exact_failure_block` over calls that never failed.
            **{GUARDRAIL_REFUSAL_KEY: True})

    return json.dumps({
        "status": "unchanged",
        "message": _READ_DEDUP_STATUS_MESSAGE,
        "path": path,
        "dedup": True,
        "content_returned": False,
    }, ensure_ascii=False)


def _record_successful_read(task_data: dict, task_id: str, path: str, resolved_str: str,
                            offset: int, limit: int, dedup_key: tuple, *, partial: bool,
                            redacted: bool = False, end_line: int | None = None,
                            total_lines=None, version_before=None, snapshot=None) -> int:
    """Bookkeeping after a real (non-stub) read; returns the consecutive-read count.

    Per-task tracker under the lock (stub counter, history, consecutive count,
    mtime for dedup + staleness, page coverage, and the write_file baseline once
    the task has seen every line UNREDACTED — in one page or by paging
    contiguously through a file too big for one; a redacted page returned a
    non-round-trippable ``«redacted:…»`` sentinel, so it must not bless an
    overwrite that would persist the sentinel into a credential file). Then
    OUTSIDE our lock (no nested locking): the cross-agent registry, and the
    background-review read-mark (a FULL read of a skill file counts like
    skill_view so a follow-up skill_manage(patch) is accepted).
    """
    version = (snapshot or _file_version(resolved_str)) if version_before is not None else None
    stable = version is not None and version[:-1] == version_before == _file_metadata(resolved_str)
    complete = False
    with _read_tracker_lock:
        task_data["dedup_hits"].pop(dedup_key, None)
        task_data["dedup_generation_reads"].add(dedup_key)
        task_data["read_history"].add((path, offset, limit))
        count = _bump_consecutive(task_data, ("read", path, offset, limit))
        try:
            _mtime_now = os.path.getmtime(resolved_str)
            task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
        except OSError:
            pass
        baselines = task_data["full_write_baselines"]
        if stable and version is not None and count < 4:
            task_data["dedup"][dedup_key] = version_before
            # A narrower view does not undo knowledge of these same bytes. Do
            # not revive a baseline after a partial read of a different version.
            complete = baselines.get(resolved_str) == version
            if not complete:
                complete = not partial
                if partial and end_line is not None:
                    complete, redacted = _note_read_coverage(
                        task_data, resolved_str, version, offset, end_line, total_lines, redacted)
                complete = complete and not redacted
            if complete:
                baselines[resolved_str] = version
        if not complete:
            baselines.pop(resolved_str, None)
        if not stable or count >= 4:
            task_data["dedup"].pop(dedup_key, None)
            task_data["dedup_generation_reads"].discard(dedup_key)
        _cap_read_tracker_data(task_data)

    try:
        file_state.record_read(task_id, resolved_str, partial=not complete)
    except Exception:
        logger.debug("file_state.record_read failed", exc_info=True)

    if complete:
        try:
            # Background-review read-before-write guard integration (#61521): when the self-improvement
            # review fork reads a skill file with read_file (now whitelisted dispatch-side), register the
            # read the same way skill_view does, so a follow-up skill_manage(action='patch') on the loaded
            # file is accepted. A partial read doesn't count — the guard requires the CURRENT full content
            # to have been seen. No-op outside review forks (mark_background_review_skill_read gates on
            # is_background_review).
            from tools.skill_manager_guards import mark_background_review_skill_read
            mark_background_review_skill_read(Path(resolved_str))
        except Exception:
            logger.debug("background-review read-mark failed", exc_info=True)
    return count


def read_file_tool(path: str, offset: int = 1, limit: int = 2000, task_id: str = "default") -> str:
    """Read a file with pagination and line numbers.

    Repeated identical reads inside one request are answered from a memo
    instead of re-reading the file and re-inserting its full text into the
    prompt. See ``tools/request_file_cache.py`` for why advisory loop warnings
    were not enough on their own.
    """
    offset, limit = normalize_read_pagination(offset, limit)
    nt_err = get_nt_namespace_error(path, verb="Read")
    if nt_err:
        return tool_error(nt_err)
    from tools import request_file_cache
    from tools.file_grants import file_grant_error, resolve_grant_alias

    routed = _route_exact_skill_file(
        path,
        task_id=task_id,
        source_tool="read_file",
    )
    if routed is not None:
        return routed

    resolved_arg = resolve_grant_alias(path, task_id=task_id)
    nt_err = get_nt_namespace_error(resolved_arg, verb="Read")
    if nt_err:
        return tool_error(nt_err)
    denial = file_grant_error(resolved_arg, task_id=task_id, operation="read")
    if denial:
        return json.dumps({"error": denial, "success": False}, ensure_ascii=False)
    denial = _acl_protected_path_block(resolved_arg, mode="read", task_id=task_id)
    if denial:
        return json.dumps({"error": denial, "success": False}, ensure_ascii=False)
    handle = str(path).strip() if resolved_arg != str(path) else ""
    requested_offset = offset
    refill_after_compression = request_file_cache.consume_invalidation(
        resolved_arg, task_id=task_id
    )
    if not refill_after_compression:
        offset, prior_coverage = _advance_past_covered_lines(
            resolved_arg,
            offset=offset,
            task_id=task_id,
        )
        if offset != requested_offset:
            total = prior_coverage.get("extent_total") if prior_coverage else None
            if isinstance(total, int) and offset > total:
                return json.dumps(
                    {
                        "status": "already_covered",
                        "content_returned": False,
                        "path": handle or str(path),
                        "requested_offset": requested_offset,
                        "coverage": prior_coverage.get("status"),
                        "covered_ranges": prior_coverage.get("ranges") or [],
                        "total_lines": total,
                        "gaps": prior_coverage.get("gaps") or [],
                        "hint": (
                            "Every line is already covered in this request. Use the "
                            "earlier result or continue the task; do not reread the "
                            "document from line 1."
                        ),
                    },
                    ensure_ascii=False,
                )

    if request_file_cache.is_active(task_id):
        memo = request_file_cache.lookup(resolved_arg, offset, limit, task_id=task_id)
        if memo is not None:
            return json.dumps(
                request_file_cache.repeat_notice(memo, handle=handle),
                ensure_ascii=False,
            )

    result = _read_file_tool_impl(resolved_arg, offset, limit, task_id)

    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        parsed = None

    if isinstance(parsed, dict) and handle:
        source = parsed.get("source")
        if isinstance(source, dict):
            source["request_handle"] = handle
            result = json.dumps(parsed, ensure_ascii=False)

    if isinstance(parsed, dict) and offset != requested_offset:
        parsed["requested_offset"] = requested_offset
        parsed["auto_advanced_offset"] = offset
        parsed["auto_advanced_reason"] = "requested range already covered"
        result = json.dumps(parsed, ensure_ascii=False)

    if request_file_cache.is_active(task_id):
        # Memoise only a real content read (full or partial). Errors and
        # unreadable outcomes stay repeatable and are recorded on the ledger
        # by the impl — not as successful content memos.
        if (
            isinstance(parsed, dict)
            and not parsed.get("error")
            and isinstance(parsed.get("content"), str)
            and parsed.get("report_as") != "unreadable"
        ):
            request_file_cache.remember(
                resolved_arg,
                offset,
                limit,
                task_id=task_id,
                content=parsed["content"],
                display_name=_HANDOFF_NAME_PREFIX_RE.sub(
                    "", str(resolved_arg).rsplit("/", 1)[-1], count=1
                ),
            )

    # Authoritative ledger for plain-text success / binary-guard failure when
    # the extract branch did not already settle the outcome.
    # BLOCKING-2: truncated/paginated text reads are partial with extent.
    try:
        from tools.attachment_ledger import (
            OUTCOME_UNREADABLE,
            get_outcome,
            record_outcome,
            record_read_extent,
        )

        display = _HANDOFF_NAME_PREFIX_RE.sub(
            "", str(resolved_arg).rsplit("/", 1)[-1], count=1
        )
        existing = get_outcome(resolved_arg, task_id=task_id)
        if isinstance(parsed, dict):
            already_settled = bool(
                existing
                and existing.get("status")
                in {"partial", "read", "unreadable"}
                and (parsed.get("extracted_document") or parsed.get("read_with") == "vision_analyze")
            )
            if (
                not already_settled
                and not parsed.get("error")
                and isinstance(parsed.get("content"), str)
                and parsed.get("report_as") not in {"unreadable"}
            ):
                total_lines = parsed.get("total_lines")
                try:
                    total_i = int(total_lines) if total_lines is not None else None
                except (TypeError, ValueError):
                    total_i = None
                start = int(offset) if offset else 1
                consumed = parsed.get("consumed") or {}
                if isinstance(consumed.get("end"), int):
                    end = consumed["end"]
                elif isinstance(parsed.get("next_offset"), int):
                    end = parsed["next_offset"] - 1
                else:
                    end = start + len(parsed["content"].splitlines()) - 1
                if total_i is not None:
                    end = min(end, total_i)
                format_gaps = list(parsed.get("gaps") or [])
                if parsed.get("truncated_lines") and "line_exceeded_display_limit" not in format_gaps:
                    format_gaps.append("line_exceeded_display_limit")
                if parsed.get("report_as") == "partial" and not format_gaps:
                    format_gaps = ["partial_read"]
                settled = record_read_extent(
                    resolved_arg,
                    task_id=task_id,
                    start=start,
                    end=end,
                    total=total_i,
                    format_gaps=format_gaps or None,
                    reason=str(parsed.get("report_as") or "text_read"),
                    display_name=display,
                    handle=handle if handle else "",
                    reader="read_file",
                )
                # Reflect settled status back onto the tool result for the model.
                if isinstance(parsed, dict) and settled == "partial":
                    if parsed.get("report_as") != "partial":
                        try:
                            parsed["report_as"] = "partial"
                            parsed["coverage"] = "partial"
                            if not parsed.get("gaps"):
                                out = get_outcome(resolved_arg, task_id=task_id) or {}
                                parsed["gaps"] = list(out.get("gaps") or ["extent_incomplete"])
                            result = json.dumps(parsed, ensure_ascii=False)
                        except Exception:
                            pass
            elif parsed.get("error") and (
                "binary" in str(parsed.get("error", "")).lower()
                or parsed.get("report_as") == "unreadable"
            ):
                if not existing or existing.get("status") != "unreadable":
                    record_outcome(
                        resolved_arg,
                        task_id=task_id,
                        status=OUTCOME_UNREADABLE,
                        reason=str(parsed.get("error") or "unreadable")[:200],
                        display_name=display,
                    )
    except Exception:
        logger.debug("attachment ledger update skipped", exc_info=True)

    return result


def _read_file_tool_impl(path: str, offset: int = 1, limit: int = DEFAULT_READ_LIMIT, task_id: str = "default") -> str:
    """Read a file with pagination and line numbers.

    Guard order: NT/device-namespace prefix (raw string, no resolution) →
    device-path blocklist (no I/O) → stat-based special-file guard (host only)
    → Hermes internal denylist → document extraction → binary-extension guard
    → negative-result cache → dedup stub → real read.
    """
    try:
        offset, limit = normalize_read_pagination(offset, limit)

        # On the RAW model-supplied string, before any expanduser()/resolve():
        # on Windows resolving \??\UNC\host\share already sends SMB auth (NTLM
        # leak); on POSIX the task-base join would anchor the prefix as a
        # relative segment and hide it from every resolved-path check below.
        nt_err = get_nt_namespace_error(path, verb="Read")
        if nt_err:
            return tool_error(nt_err)

        from tools.file_grants import file_grant_error, resolve_grant_alias
        path = resolve_grant_alias(path, task_id=task_id)
        nt_err = get_nt_namespace_error(path, verb="Read")
        if nt_err:
            return tool_error(nt_err)
        denial = file_grant_error(path, task_id=task_id, operation="read")
        if denial:
            return json.dumps({"error": denial, "success": False}, ensure_ascii=False)
        denial = _acl_protected_path_block(path, mode="read", task_id=task_id)
        if denial:
            return json.dumps({"error": denial, "success": False}, ensure_ascii=False)
        device_base = None if Path(path).expanduser().is_absolute() else _resolve_base_dir(task_id)
        if _is_blocked_device(path, base_dir=device_base):
            return tool_error(
                f"Cannot read '{path}': this is a device file that would "
                "block or produce infinite output.")

        _resolved = _resolve_path_for_task(path, task_id)

        # A read on a FIFO/socket blocks until the exec timeout: a self-shipped DoS.
        if _file_ops_uses_host_paths(_get_file_ops(task_id)):
            kind = _special_file_kind(_resolved)
            if kind is not None:
                return json.dumps({
                    "success": False,
                    "note": (
                        f"'{path}' is {kind}, not a regular file — reading "
                        "it would block indefinitely, so no read was "
                        "attempted. Use terminal utilities if you need to "
                        "interact with it.")})

        # Hermes internal denylist (prompt injection via catalog metadata,
        # credential stores). Runs BEFORE document extraction so a
        # protected SQLite store (state.db) cannot be read through the extractor. Pass the RESOLVED path: the denylist's own
        # resolve() uses the process cwd and would miss a relative "auth.json".
        block_error = get_read_block_error(str(_resolved))
        if block_error:
            return tool_error(block_error)

        extracted = _read_extracted_document(path, _resolved, offset, limit, task_id)
        if extracted is not None:
            return extracted

        # The extension is a claim, so this message names only the extension;
        # the content-sniffing path names the actual magic-byte type.
        if has_binary_extension(str(_resolved)):
            _ext = _resolved.suffix.lower()
            from tools.file_grants import file_handle_for_path
            from tools.file_reader_routing import (
                READ_WITH_VISION,
                reader_call,
                reader_route,
            )

            read_with = reader_route(str(_resolved))
            if read_with == READ_WITH_VISION:
                target = (
                    file_handle_for_path(_resolved, task_id=task_id)
                    or str(_resolved)
                )
                recovery_call = reader_call(read_with, target)
                # Prefer delivering vision content in-process (issue #50).
                vision_parsed = _vision_auto_read_image(
                    target=str(target), task_id=task_id
                )
                if vision_parsed is not None:
                    analysis = vision_parsed.get("analysis") or ""
                    return json.dumps(
                        {
                            "success": True,
                            "routed_from": "read_file",
                            "read_with": READ_WITH_VISION,
                            "path": path,
                            "target": target,
                            "content": analysis,
                            "note": (
                                "Image bytes cannot be decoded as text; "
                                "vision_analyze ran automatically so this "
                                "turn is not wasted. Prefer calling "
                                f"{recovery_call} directly next time."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return json.dumps({
                    "error": (
                        f"Cannot read binary file '{path}' ({_ext}). "
                        f"Call {recovery_call} instead."
                    ),
                })
            return tool_error(
                f"Cannot read binary file '{path}' ({_ext}). "
                "Use vision_analyze for images, or terminal to inspect binary files."
            )

        resolved_str = str(_resolved)
        cached_not_found = _check_not_found_cache("read", resolved_str, task_id)
        if cached_not_found is not None:
            return cached_not_found

        # Dedup: identical (path, offset, limit) on an unchanged file returns a
        # lightweight stub instead of re-sending the content.
        dedup_key = (resolved_str, offset, limit)
        with _read_tracker_lock:
            task_data = _task_data(task_id)
            cached_version = task_data["dedup"].get(dedup_key)
            # First unchanged read after a compaction boundary serves full content
            # (the summary may have dropped exact bytes); later ones get the stub.
            content_served_in_generation = dedup_key in task_data["dedup_generation_reads"]
        # Same rule as skill_view: the review fork shares the parent's task_id and its
        # read-before-write guard needs a real read, which the stub path never records (#95976).
        file_ops = _get_file_ops(task_id)
        version_before = _file_metadata(resolved_str) if _file_ops_uses_host_paths(file_ops) else None
        if (cached_version is not None and not is_background_review()
                and version_before == cached_version and content_served_in_generation):
            return _dedup_stub_or_block(task_data, dedup_key, path)

        result = file_ops.read_file(resolved_str if _file_ops_uses_host_paths(file_ops) else path, offset, limit)
        result_dict = result.to_dict()

        # Failed reads cannot establish whole-file knowledge.
        _err = result_dict.get("error") or ""
        if isinstance(_err, str) and _err.startswith("File not found:"):
            _record_not_found("read", resolved_str, task_id, json.dumps(result_dict, ensure_ascii=False))
        if _err or result_dict.get("is_binary"):
            return json.dumps(result_dict, ensure_ascii=False)

        # Char budget on the FORMATTED content (what enters context), BEFORE
        # redaction (skip the regex pass on huge content); truncate gracefully
        # with a next_offset instead of rejecting.
        file_size = result_dict.get("file_size", 0)
        max_chars = _get_max_read_chars()
        if len(result.content or "") > max_chars:
            result.content = _apply_char_budget(
                result_dict, result.content or "", offset,
                result_dict.get("total_lines", "unknown"), max_chars)
        redacted = False
        if result.content:
            unredacted = result.content
            result.content = redact_sensitive_text(
                unredacted, file_read=True, secret_file=_is_secret_file_arg(resolved_str))
            redacted = result.content != unredacted
            result_dict["content"] = result.content

        if result.content:
            conflicts = count_conflict_blocks(result.content)
            if conflicts:
                result_dict["conflict_blocks"] = conflicts
                result_dict["_hint"] = (
                    f"{conflicts} unresolved git merge-conflict block(s) (<<<<<<< / ======= / >>>>>>>) in this "
                    "range. Resolve them (keep one side or combine, delete the markers) before editing around them.")

        if (file_size and file_size > _LARGE_FILE_HINT_BYTES
                and limit > 200 and result_dict.get("truncated")):
            result_dict.setdefault("_hint", (
                f"This file is large ({file_size:,} bytes). "
                "Consider reading only the section you need with offset and limit "
                "to keep context usage efficient."))

        total_lines = result_dict.get("total_lines")
        if result_dict.get("truncated_by") == "bytes":
            end_line = int(result_dict.get("next_offset", offset)) - 1
        else:
            end_line = offset + limit - 1
            if isinstance(total_lines, int) and total_lines > 0:
                end_line = min(end_line, total_lines)
        result_dict["consumed"] = {"unit": "lines", "start": offset, "end": end_line, "total": total_lines}
        if result_dict.get("truncated_lines"):
            result_dict["gaps"] = list(result_dict.get("gaps") or []) + ["line_exceeded_display_limit"]
        count = _record_successful_read(task_data, task_id, path, resolved_str, offset, limit,
                                        dedup_key, partial=(offset > 1) or bool(result_dict.get("truncated")),
                                        redacted=redacted or bool(result_dict.get("truncated_lines")),
                                        end_line=end_line, total_lines=total_lines,
                                        version_before=version_before,
                                        snapshot=getattr(result, "_snapshot", None))
        if count >= 4:
            return tool_error(
                f"BLOCKED: You have read this exact file region {count} times in a row. "
                "The content has NOT changed. You already have this information. "
                "STOP re-reading and proceed with your task.",
                path=path,
                already_read=count,
                **{GUARDRAIL_REFUSAL_KEY: True})
        if count >= 3:
            result_dict["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding.")
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


# ── Shared write/patch plumbing ──────────────────────────────────────────

def _resolve_or_none(filepath: str, task_id: str) -> str | None:
    """Task-resolved path string, or None when resolution fails for any reason."""
    try:
        return str(_resolve_path_for_task(filepath, task_id))
    except _PerUserWorkspaceError:
        raise
    except Exception:
        return None


def _write_precheck_error(paths: list[str], content_paths: list[str], task_id: str,
                          cross_profile: bool) -> str | None:
    """Run the shared write/patch guards in order; return the first error string.

    Order matters: hard denies (sensitive path, mirror) and the corruption
    guard run before anything that could prompt the user, and ONE approval
    prompt covers every path of a multi-file patch.
    """
    denial = _acl_raw_file_write_block()
    if denial:
        return denial
    for p in paths:
        denial = _workspace_path_access_error(p, task_id) or _acl_protected_path_block(p, mode="write", task_id=task_id)
        if denial:
            return denial
        err = _check_sensitive_path(p, task_id) or (
            None if cross_profile else _check_cross_profile_path(p, task_id))
        if err:
            return err
    for p in content_paths:
        err = _check_binary_document_write(p, task_id)
        if err:
            return err
    return (_check_protected_instruction_write(paths, task_id)
            or _check_approval_required_write(paths, task_id))


def _edit_warnings(paths: list[str], path_to_resolved: dict, task_id: str) -> list[str]:
    """One pre-edit warning per path, in priority order: cross-agent registry
    (names the sibling subagent) > per-task staleness > workspace divergence
    (relative path resolving outside the terminal's cwd — the worktree-cwd bug)."""
    warnings: list[str] = []
    for p in paths:
        r = path_to_resolved.get(p)
        w = (file_state.check_stale(task_id, r) if r else None) or _check_file_staleness(p, task_id)
        if not w and r:
            w = _path_resolution_warning(p, Path(r), task_id)
        if w:
            warnings.append(w)
    return warnings


def _note_edited(task_id: str, paths: list[str], path_to_resolved: dict, session_id: str | None) -> None:
    """Post-success bookkeeping: verification-stale marker, then per path refresh
    the read stamp (no false staleness on the next edit) and record the write."""
    _mark_verification_stale(task_id, [path_to_resolved.get(p) or p for p in paths], session_id=session_id)
    for p in paths:
        _update_read_timestamp(p, task_id)
        if path_to_resolved.get(p):
            file_state.note_write(task_id, path_to_resolved[p])


# Whole-file rewrite hint: an overwrite of an existing file this large whose new content keeps at least
# this fraction of the old lines is a patch written the expensive way. In one 1,393-agent run 661 such
# rewrites of >20k-char files cost ~25M output chars (~$155) where `patch` averaged 1.3k chars/call.
_REWRITE_HINT_MIN_CHARS = 20_000
_REWRITE_HINT_MIN_UNCHANGED = 0.80


# Above this the line diff is skipped: SequenceMatcher on pathological repeated-line files is
# quadratic (a 460 KB same-line file took ~22 s under the write lock).
_REWRITE_HINT_MAX_CHARS = 400_000


def _whole_file_rewrite_hint(task_id: str, resolved: str | None, new_content: str) -> str | None:
    """Return a hint when ``new_content`` mostly re-sends what is already at ``resolved``.

    Reads the OLD content through the task's own file ops (``read_file_raw``, the sandbox/remote
    backend the write targets), never the host path: on a remote backend the host file is a
    different file, and a host FIFO at that path would block the write lock forever. Bounded size
    and a line multiset comparison (linear) instead of a sequence diff (quadratic on repeated lines)."""
    if not resolved or not (_REWRITE_HINT_MIN_CHARS <= len(new_content) <= _REWRITE_HINT_MAX_CHARS):
        return None
    try:
        result = _get_file_ops(task_id).read_file_raw(resolved)
        old = getattr(result, "content", None)
        if getattr(result, "error", None) or not isinstance(old, str):
            return None
    except Exception:
        return None
    if not (_REWRITE_HINT_MIN_CHARS <= len(old) <= _REWRITE_HINT_MAX_CHARS):
        return None
    old_lines, new_lines = old.splitlines(), new_content.splitlines()
    if not old_lines:
        return None
    from collections import Counter
    unchanged = sum((Counter(old_lines) & Counter(new_lines)).values())
    ratio = unchanged / max(len(old_lines), len(new_lines))
    if ratio < _REWRITE_HINT_MIN_UNCHANGED:
        return None
    changed = max(len(old_lines), len(new_lines)) - unchanged
    return (
        f"{unchanged:,} of {len(new_lines):,} lines were already on disk ({ratio:.0%} unchanged); ~{changed:,} "
        f"line(s) actually changed. Re-sending a {len(new_content):,}-char file costs output tokens for every "
        "unchanged line; for edits like this use patch (old_string/new_string), which sends only the changed region."
    )


def write_file_tool(path: str, content: str, task_id: str = "default",
                    cross_profile: bool = False,
                    session_id: str | None = None) -> str:
    """Write content to a file.

    ``cross_profile`` bypasses the sandbox-mirror lost-write guards only
    (unadvertised in the schema; the mirror rejection error teaches it — the
    cross-PROFILE guard it was named for no longer exists).
    """
    # write_file checks the binary-document guard before the mirror guard.
    err = (_acl_raw_file_write_block()
           or _workspace_path_access_error(path, task_id)
           or _acl_protected_path_block(path, mode="write", task_id=task_id)
           or _check_sensitive_path(path, task_id)
           or _check_binary_document_write(path, task_id)
           or _check_protected_instruction_write([path], task_id)
           or _check_approval_required_write([path], task_id)
           or (None if cross_profile else _check_cross_profile_path(path, task_id)))
    if not err and _is_internal_file_tool_content(content):
        err = ("Refusing to write internal read_file display text as file content. "
               "Strip read_file line-number prefixes or reconstruct the intended "
               "file contents before writing.")
    if err:
        return tool_error(err)
    try:
        # Resolution failure falls back to the legacy unlocked path (the write
        # still proceeds; the per-task staleness check still runs).
        _resolved = _resolve_or_none(path, task_id)
        path_to_resolved = {path: _resolved}
        with ExitStack() as _lock:
            if _resolved:
                # Per-path lock serializes read→modify→write across concurrent
                # subagents; different paths stay fully parallel.
                _lock.enter_context(file_state.lock_path(_resolved))
            # A whole-file overwrite of content this task never saw, or that
            # changed since, is refused HERE — before the write — instead of
            # warning after the clobber (#65604). Nothing below runs.
            blocker = _stale_overwrite_blocker(path, _resolved, task_id)
            if blocker:
                return json.dumps(_stale_write_refusal(path, blocker, _resolved), ensure_ascii=False)
            warnings = _edit_warnings([path], path_to_resolved, task_id)
            rewrite_hint = _whole_file_rewrite_hint(task_id, _resolved, content)
            result = _get_file_ops(task_id).write_file(_resolved or path, content)
            result_dict = result.to_dict()
            if warnings:
                result_dict["_warning"] = warnings[0]
            if rewrite_hint and not result_dict.get("error"):
                result_dict["hint"] = rewrite_hint
            if _resolved:
                # Always report the ABSOLUTE path written so a wrong-cwd mismatch
                # is visible in the response instead of silently landing elsewhere.
                result_dict["resolved_path"] = _resolved
            if result_dict.get("error"):
                _update_read_timestamp(path, task_id)
            else:
                if _resolved:
                    result_dict["files_modified"] = [_resolved]
                    # Own write = current whole-file content: consecutive
                    # same-task writes stay unblocked. patch never does this.
                    _mark_full_write_baseline(_resolved, task_id, getattr(result, "_content_sha256", None))
                _note_edited(task_id, [path], path_to_resolved, session_id)
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        if _is_expected_write_exception(e):
            logger.debug("write_file expected denial: %s: %s", type(e).__name__, e)
        else:
            logger.error("write_file error: %s: %s", type(e).__name__, e, exc_info=True)
        return tool_error(str(e))


def _collect_v4a_header_paths(patch: str, task_id: str = "default") -> tuple[list[str], list[str]] | str:
    """Extract every path named in V4A headers, rejecting ``..`` traversal.

    Returns ``(all_paths, content_write_paths)`` or a tool_error string. Header
    paths come from patch CONTENT (more attacker-influenceable than ``path=``,
    which keeps its legitimate ``..`` use). Move headers check BOTH endpoints;
    only Update/Add write text and feed the binary-document guard.
    """
    from tools.path_security import has_traversal_component

    headers = [(m.group(3), m.group(2) in ("Update", "Add")) for m in _V4A_SINGLE_HEADER_RE.finditer(patch)]
    headers += [(g, False) for m in _V4A_MOVE_HEADER_RE.finditer(patch) for g in (m.group(2), m.group(3))]
    paths: list[str] = []
    content_paths: list[str] = []
    for raw, writes_text in headers:
        v4a_path = raw.strip()
        denial = _workspace_path_access_error(v4a_path, task_id)
        if denial:
            return tool_error(denial)
        if has_traversal_component(v4a_path):
            return tool_error(
                f"V4A patch header contains '..' traversal: {v4a_path!r}. "
                "Use the agent's cwd-relative path (no '..') or an absolute "
                "path in '*** Update File:' / '*** Add File:' / "
                "'*** Delete File:' / '*** Move File:' headers.")
        paths.append(v4a_path)
        if writes_text:
            content_paths.append(v4a_path)
    return paths, content_paths


def patch_tool(mode: str = "replace", path: str = None, old_string: str = None,
               new_string: str = None, replace_all: bool = False, patch: str = None,
               task_id: str = "default", cross_profile: bool = False,
               session_id: str | None = None) -> str:
    """Patch a file using replace mode or V4A patch format.

    ``cross_profile``: same semantics as ``write_file``'s flag (mirror-guard
    bypass only; unadvertised).
    """
    _paths_to_check = [path] if path else []
    _content_write_paths = list(_paths_to_check)
    if mode == "patch" and patch:
        collected = _collect_v4a_header_paths(patch, task_id)
        if isinstance(collected, str):
            return collected
        _paths_to_check += collected[0]
        _content_write_paths += collected[1]
    precheck_err = _write_precheck_error(_paths_to_check, _content_write_paths, task_id, cross_profile)
    if precheck_err:
        return tool_error(precheck_err)
    try:
        # Lock paths in sorted, deduplicated order so concurrent callers with
        # overlapping multi-file patches can't deadlock (every caller locks in
        # the same order). An unresolvable path is simply not locked.
        _path_to_resolved: dict[str, str] = {_p: _resolve_or_none(_p, task_id) for _p in _paths_to_check}
        with ExitStack() as _locks:
            for _r in sorted({_r for _r in _path_to_resolved.values() if _r}):
                _locks.enter_context(file_state.lock_path(_r))
            stale_warnings = _edit_warnings(_paths_to_check, _path_to_resolved, task_id)
            file_ops = _get_file_ops(task_id)

            # Hand the shell layer the RESOLVED targets so both layers agree on
            # which file is edited even when the shell's cwd differs.
            if mode == "replace":
                if not path:
                    return tool_error("path required")
                if old_string is None or new_string is None:
                    return tool_error("old_string and new_string required")
                _replace_target = _path_to_resolved.get(path) or path
                result = file_ops.patch_replace(_replace_target, old_string, new_string, replace_all)
            elif mode == "patch":
                if not patch:
                    return tool_error("patch content required")
                result = file_ops.patch_v4a(_rewrite_v4a_patch_paths_for_host(patch, _path_to_resolved, file_ops))
            else:
                return tool_error(f"Unknown mode: {mode}")

            result_dict = result.to_dict()
            if stale_warnings:
                result_dict["_warning"] = " | ".join(stale_warnings)
            if not result_dict.get("error"):
                # Report the ABSOLUTE path(s) actually patched so a wrong-cwd
                # mismatch is visible instead of silently landing elsewhere.
                _resolved_modified = [_path_to_resolved.get(_p) or _p for _p in _paths_to_check]
                result_dict["files_modified"] = _resolved_modified
                if len(_resolved_modified) == 1:
                    result_dict["resolved_path"] = _resolved_modified[0]
                _note_edited(task_id, _paths_to_check, _path_to_resolved, session_id)
                # Clear failure counters so a future miss starts a fresh count.
                _reset_patch_failures(task_id, [_r for _r in _path_to_resolved.values() if _r])
        # old_string-not-found hint. Failure escalation is tracked for replace
        # mode only (V4A misses are rare); the generic hint is suppressed when
        # patch_replace already attached a richer "Did you mean?" snippet.
        if result_dict.get("error") and "Could not find" in str(result_dict["error"]):
            failure_count = 0
            if mode == "replace" and path:
                failure_count = _record_patch_failure(task_id, _path_to_resolved.get(path) or path)
            if failure_count >= 3:
                result_dict["_hint"] = (
                    f"This is failure #{failure_count} patching {path!r}. "
                    "Stop retrying with variations of the same old_string. "
                    "Either: (1) re-read the file fresh to verify current "
                    "content, (2) use a longer / more unique old_string with "
                    "surrounding context lines, or (3) use write_file to "
                    "replace the entire file if the targeted region is hard "
                    "to anchor.")
            elif "Did you mean one of these sections?" not in str(result_dict["error"]):
                result_dict["_hint"] = (
                    "old_string not found. Use read_file to verify the current "
                    "content, or search_files to locate the text.")
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


def search_tool(pattern: str, target: str = "content", path: str = ".",
                file_glob: str = None, limit: int = 50, offset: int = 0,
                output_mode: str = "content", context: int = 0,
                order: str = "discovery",
                task_id: str = "default") -> str:
    """Search for content or files."""
    nt_err = get_nt_namespace_error(path, verb="Search")
    if nt_err:
        return tool_error(nt_err)
    if target == "content":
        # ``search_files`` commonly supplies a directory in ``path`` and one
        # literal filename in ``file_glob``.  Route that exact joined file,
        # not the directory: the skill-path resolver intentionally maps a
        # directory back to the skill root, which would otherwise return the
        # unrelated SKILL.md and invite the model to retry different grep
        # patterns forever.
        routed_path = path
        literal_relative_file = _literal_relative_search_file(file_glob)
        if literal_relative_file is not None:
            routed_path = (
                str(path).rstrip("/\\") + "/" + literal_relative_file
            )
        routed = _route_exact_skill_file(
            routed_path,
            task_id=task_id,
            source_tool="search_files",
            requested_pattern=pattern,
        )
        if routed is not None:
            try:
                routed_payload = json.loads(routed)
            except (TypeError, ValueError):
                routed_payload = None
            routed_file = (
                routed_payload.get("routing", {}).get("file_path")
                if isinstance(routed_payload, dict)
                else None
            )
            # A search path without a concrete linked file is a directory,
            # not an exact source file.  Do not silently substitute SKILL.md.
            if routed_file is None and PurePosixPath(str(routed_path)).name != "SKILL.md":
                routed = None
        if routed is not None:
            return _targeted_skill_search_result(
                routed,
                pattern=pattern,
                context=context,
                limit=limit,
                offset=offset,
                output_mode=output_mode,
            )

    try:
        offset, limit = normalize_search_pagination(offset, limit)
        from tools.file_grants import file_grant_error, resolve_grant_alias
        path = resolve_grant_alias(path, task_id=task_id)
        nt_err = get_nt_namespace_error(path, verb="Search")
        if nt_err:
            return tool_error(nt_err)
        denial = file_grant_error(path, task_id=task_id, operation="search")
        if denial:
            return json.dumps({"error": denial, "success": False}, ensure_ascii=False)
        denial = _workspace_path_access_error(path, task_id)
        if denial:
            return tool_error(denial)

        # Pagination args (and order) are part of the key so paging through truncated
        # results doesn't trip the repeated-search guard.
        search_key = ("search", pattern, target, str(path), file_glob or "", limit, offset, order)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0, "read_history": set()})
            count = _bump_consecutive(task_data, search_key)

        if count >= 4:
            return tool_error(
                f"BLOCKED: You have run this exact search {count} times in a row. "
                "The results have NOT changed. You already have this information. "
                "STOP re-searching and proceed with your task.",
                pattern=pattern,
                already_searched=count,
                **{GUARDRAIL_REFUSAL_KEY: True})

        # Raw string before _resolve_path_for_task: resolving is the NTLM-leak
        # trigger and the task-base join would hide the prefix (see read_file_tool).
        nt_err = get_nt_namespace_error(path, verb="Search")
        if nt_err:
            return tool_error(nt_err)
        try:
            resolved_search_path = str(_resolve_path_for_task(path, task_id))
        except (OSError, ValueError, RuntimeError) as exc:
            resolved_search_path = path
            # A RuntimeError still surfaces as the tool error unless the raw
            # path is itself denylisted (that error wins).
            if isinstance(exc, RuntimeError) and not get_read_block_error(path):
                raise
        block_error = get_read_block_error(resolved_search_path)
        if block_error:
            return tool_error(block_error)

        # A missing search root costs two shells (search + parent listing for
        # "Similar paths"); cache the miss so a retry skips both.
        cached_search_nf = _check_not_found_cache("search", resolved_search_path, task_id)
        if cached_search_nf is not None:
            return cached_search_nf

        file_ops = _get_file_ops(task_id)
        backend_path = resolved_search_path if _file_ops_uses_host_paths(file_ops) else path
        result = file_ops.search(
            pattern=pattern, path=backend_path, target=target, file_glob=file_glob,
            limit=limit, offset=offset, output_mode=output_mode, context=context, order=order)
        omitted = _filter_read_blocked_search_results(result, task_id)
        for m in getattr(result, "matches", None) or ():
            if getattr(m, "content", None):
                m.content = redact_sensitive_text(
                    m.content, file_read=True,
                    secret_file=_is_secret_file_arg(_resolved_match_path(m.path, task_id)))
        result = _acl_filter_search_result(result, task_id)
        result_dict = result.to_dict(densify=True)

        if omitted:
            result_dict["_omitted"] = (
                f"{omitted} result(s) omitted because they target credential, "
                "token, cache, or secret-bearing environment files.")

        # No early return on a cached miss — same rationale as the read path.
        _search_err = result_dict.get("error") or ""
        if isinstance(_search_err, str) and _search_err.startswith("Path not found:"):
            _record_not_found("search", resolved_search_path, task_id, json.dumps(result_dict, ensure_ascii=False))

        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have.")

        # Structured like ``_warning`` above: text appended after the JSON
        # breaks every json.loads consumer (execute_code RPC, strict tool-message
        # providers) — #90322.
        if result_dict.get("truncated"):
            result_dict["_hint"] = (
                f"Results truncated. Use offset={offset + limit} to see more, "
                "or narrow with a more specific pattern or file_glob."
            )
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


# ---------------------------------------------------------------------------
# Schemas + Registry
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error


def _check_file_reqs():
    """Lazy wrapper to avoid circular import with tools/__init__.py."""
    from tools import check_file_requirements
    return check_file_requirements()

READ_FILE_SCHEMA = {
    "name": "read_file",
    # Document formats are stated unconditionally: firecrawl-anydoc is a
    # core dependency (bundled), so its absence is a broken install, not a
    # configuration — the teaching error in read_extract handles that rare
    # case with the pip-install fix. The ONE dynamic word: "PDF (text
    # layer)" upgrades to "PDF (scanned or text)" when hosted OCR has a
    # route we trust (_read_file_schema_overrides). Scanned-page coverage
    # teaching lives in the response-time NEEDS-OCR warning
    # (read_extract.py); the schema doesn't pre-teach it.
    "description": "Read a text file with line numbers and pagination. Use skill_view for SKILL.md and exact linked skill files. Continue uncovered ranges and preserve named format gaps before claiming complete coverage. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are truncated on a line boundary and return a next_offset; continue with offset to read the rest. Documents auto-extract to readable text: .ipynb, Office (.docx/.xlsx/.pptx and legacy .doc/.ppt/.xls), PDF (Docling text/OCR), MSG and EML, OpenDocument, RTF, EPUB, SQLite (.db/.sqlite: partial schema and row preview only). Cannot read images/binary — use vision_analyze for images.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 2000, max: 2000). Reads are additionally capped at a ~100K-character budget with a next_offset continuation.", "default": DEFAULT_READ_LIMIT, "maximum": 2000}
        },
        "required": ["path"]
    }
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. For an EXISTING file, call read_file first: write_file refuses (file untouched) when this task has no current full read/write of the file or the file changed on disk since; on refusal, read_file, merge, then retry. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out). The result's verified:true means the on-disk content hash was confirmed — do NOT re-read the file to check the write landed.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (will be created if it doesn't exist, overwritten if it does)"},
            "content": {"type": "string", "description": "Complete content to write to the file"},
            # NOTE: the handler still accepts `cross_profile` (bool) — it now
            # bypasses only the #32049 sandbox-mirror lost-write guards, whose
            # rejection error teaches it. Unadvertised: the cross-PROFILE
            # guard it was named for was removed (profiles are not isolated,
            # maintainer decision), and mirror hits are rare + self-teaching.
        },
        "required": ["path", "content"]
    }
}

PATCH_SCHEMA = {
    "name": "patch",
    # BASE = replace-only (what nearly every model family was trained on).
    # The V4A patch mode (mode + patch params, dual-mode description) is
    # LAYERED ON dynamically for OpenAI-family mains only — V4A is the
    # OpenAI apply_patch dialect their models emit natively; advertising
    # it to everyone cost every other session ~148 tok/call
    # (_patch_schema_overrides below). The handler accepts BOTH shapes
    # from any model regardless (replay compat + strong models that know
    # V4A anyway): mode defaults to 'replace' when omitted.
    "description": (
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
        "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
        "Returns a unified diff. Auto-runs syntax checks after editing. "
        "Finds a unique string and replaces it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness.",
            },
            "new_string": {
                "type": "string",
                "description": "Changed replacement text; it must differ from old_string. Pass empty string '' to delete the matched text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring a unique match (default: false)",
                "default": False,
            },
            # NOTE: handler still accepts `cross_profile` — see write_file's
            # NOTE (mirror-guard bypass only; unadvertised by design).
            # NOTE: handler still accepts `mode` + `patch` (V4A) from ANY
            # model — the schema just doesn't advertise them off-family.
        },
        "required": ["path", "old_string", "new_string"],
    },
}


# V4A layer, rendered only for OpenAI-family main models (see PATCH_SCHEMA
# comment). Kept as data so the override composes it deterministically.
_PATCH_V4A_DESCRIPTION = (
    "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
    "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
    "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
    "REPLACE MODE (mode='replace', default): find a unique string and replace it. "
    "REQUIRED PARAMETERS: mode, path, old_string, new_string.\n"
    "PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. "
    "REQUIRED PARAMETERS: mode, patch."
)

_PATCH_V4A_PARAMS = {
    "mode": {
        "type": "string",
        "enum": ["replace", "patch"],
        "description": "Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.",
        "default": "replace",
    },
    "patch": {
        "type": "string",
        "description": "REQUIRED when mode='patch'. V4A format patch content. Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch",
    },
}


def _is_openai_family_main() -> bool:
    """Whether the active main provider/model is the OpenAI/codex family —
    the population trained on the V4A apply_patch dialect.

    Provider-family-coarse on purpose (no per-model training-diet table to
    go stale): direct OpenAI providers always qualify; on aggregators
    (openrouter/nous/azure...) the MODEL slug decides (gpt-*/o-series/
    codex). Fail-closed to the universal replace-only schema.
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider

        provider = (_read_main_provider() or "").strip().lower()
        model = (_read_main_model() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if provider in {"openai", "openai-chat", "openai-codex", "azure-openai", "codex"}:
        return True
    # Aggregators: the model slug carries the family.
    slug = model.split("/", 1)[-1]
    if slug.startswith(("gpt-", "gpt.", "chatgpt", "codex", "o1", "o3", "o4", "o5")):
        return True
    return "openai/" in model


SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search file contents or find files by name. Use skills_list for skill discovery and skill_view for SKILL.md or exact linked skill files; do not search a skills directory. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents. On macOS, broad searches above the user home automatically skip TCC-protected folders (Desktop, Documents, Downloads, Library, Movies, Music, Pictures); target one directly when access is intentional.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls. Discovery order is the fast bounded default; exact global newest-first order is an explicit opt-in and may scan the full tree.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"},
            "target": {"type": "string", "enum": ["content", "files"], "description": "'content' searches inside file contents, 'files' searches for files by name", "default": "content"},
            "path": {"type": "string", "description": "Directory or file to search in (default: current working directory)", "default": "."},
            "file_glob": {"type": "string", "description": "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)"},
            "limit": {"type": "integer", "description": "Maximum number of results to return (default: 50)", "default": 50},
            "offset": {"type": "integer", "description": "Skip first N results for pagination (default: 0)", "default": 0},
            "order": {"type": "string", "enum": ["discovery", "modified"], "description": "File-search order: 'discovery' is fast bounded traversal order; 'modified' is exact global newest-first and may scan the full tree; ignored for content", "default": "discovery"},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file", "default": "content"},
            "context": {"type": "integer", "description": "Number of context lines before and after each match (grep mode only)", "default": 0}
        },
        "required": ["pattern"]
    }
}


def _handle_read_file(args, **kw):
    tid = kw.get("task_id") or "default"
    return read_file_tool(path=args.get("path", ""), offset=args.get("offset", 1), limit=args.get("limit", DEFAULT_READ_LIMIT), task_id=tid)


def _handle_write_file(args, **kw):
    tid = kw.get("task_id") or "default"
    if not args.get("path") or not isinstance(args.get("path"), str):
        return tool_error(
            "write_file: missing required field 'path'. Re-emit the tool call with "
            "both 'path' and 'content' set."
        )
    if "content" not in args:
        return tool_error(
            "write_file: missing required field 'content'. The tool call included a "
            "path but no content argument — this is almost always a dropped-arg bug "
            "under context pressure. Re-emit the tool call with the full content "
            "payload, or use execute_code with hermes_tools.write_file() for very "
            "large files."
        )
    if not isinstance(args["content"], str):
        return tool_error(
            f"write_file: 'content' must be a string, got "
            f"{type(args['content']).__name__}."
        )
    return write_file_tool(
        path=args["path"], content=args["content"], task_id=tid,
        cross_profile=bool(args.get("cross_profile", False)),
        session_id=kw.get("session_id"),
    )


def _handle_patch(args, **kw):
    tid = kw.get("task_id") or "default"
    return patch_tool(
        mode=args.get("mode", "replace"), path=args.get("path"),
        old_string=args.get("old_string"), new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False), patch=args.get("patch"), task_id=tid,
        cross_profile=bool(args.get("cross_profile", False)),
        session_id=kw.get("session_id"),
    )


def _handle_search_files(args, **kw):
    tid = kw.get("task_id") or "default"
    target_map = {"grep": "content", "find": "files"}
    raw_target = args.get("target", "content")
    target = target_map.get(raw_target, raw_target)
    # The schema documents path='.'; a present-but-blank (or JSON null) value
    # is not a missing key for dict.get, so apply the default here (#112424).
    path = args.get("path", ".")
    if path is None or (isinstance(path, str) and not path.strip()):
        path = "."
    return search_tool(
        pattern=args.get("pattern", ""), target=target, path=path,
        file_glob=args.get("file_glob"), limit=args.get("limit", 50), offset=args.get("offset", 0),
        output_mode=args.get("output_mode", "content"), context=args.get("context", 0),
        order=args.get("order", "discovery"), task_id=tid)





registry.register(name="read_file", toolset="file_read", schema=READ_FILE_SCHEMA, handler=_handle_read_file, check_fn=_check_file_reqs, emoji="📖", max_result_size_chars=100_000)
registry.register(name="write_file", toolset="file_write", schema=WRITE_FILE_SCHEMA, handler=_handle_write_file, check_fn=_check_file_reqs, emoji="✍️", max_result_size_chars=100_000)
def _patch_schema_overrides():
    """Layer the V4A patch mode onto the base replace-only schema for
    OpenAI-family mains (see PATCH_SCHEMA comment). Config/context probe
    only — no I/O at schema-build time; compaction's tool refresh
    (#97073) re-evaluates on model switches."""
    try:
        if not _is_openai_family_main():
            return {}
        params = {
            "type": "object",
            "properties": {
                "mode": _PATCH_V4A_PARAMS["mode"],
                **PATCH_SCHEMA["parameters"]["properties"],
                "patch": _PATCH_V4A_PARAMS["patch"],
            },
            "required": ["mode"],
        }
        return {"description": _PATCH_V4A_DESCRIPTION, "parameters": params}
    except Exception:  # noqa: BLE001
        return {}


registry.register(name="patch", toolset="file_write", schema=PATCH_SCHEMA, handler=_handle_patch, check_fn=_check_file_reqs, emoji="🔧", max_result_size_chars=100_000, dynamic_schema_overrides=_patch_schema_overrides)
registry.register(name="search_files", toolset="file_read", schema=SEARCH_FILES_SCHEMA, handler=_handle_search_files, check_fn=_check_file_reqs, emoji="🔎", max_result_size_chars=100_000)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from pathlib import PurePosixPath  # noqa: F401,E402
import posixpath  # noqa: F401,E402
import sys  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'has_opaque_document_extension': ('tools.binary_extensions', 'has_opaque_document_extension'),
    'is_pdf_path': ('tools.binary_extensions', 'is_pdf_path'),
    'notify_other_tool_call': ('tools.file_tools_read_tracking', 'notify_other_tool_call'),
    'reset_file_dedup': ('tools.file_tools_read_tracking', 'reset_file_dedup'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----


from tools.attachments_tool import ATTACHMENTS_SCHEMA, _handle_attachments

registry.register(name="attachments", toolset="file_read", schema=ATTACHMENTS_SCHEMA, handler=_handle_attachments, check_fn=_check_file_reqs, emoji="📎", max_result_size_chars=100_000)
