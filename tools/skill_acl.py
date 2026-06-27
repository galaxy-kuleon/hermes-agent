"""Centralized Hermes skill ACL policy resolver (issue #10).

RESOLVER-ONLY: this module *decides* whether the current OpenWebUI caller may
perform a skill action. It does NOT enforce anything at call sites — wiring it
into ``skills_list``/``skill_view``/``skill_manage`` and closing file/terminal
bypasses are issues #11-#13.

Identity (role + stable OpenWebUI group IDs) is read from the concurrency-safe
session context populated at api_server ingress (issue #9):
``HERMES_SESSION_USER_ROLE`` and ``HERMES_SESSION_USER_GROUPS`` (a comma-joined
string of stable OpenWebUI group IDs).

Config schema — ``skills_acl`` section of ``config/hermes/config.yaml``::

    skills_acl:
      enabled: true
      roles:
        admin: [read, create, update, delete]   # admin is implicitly full; listing is optional
        user: []
      groups:                                    # keys are STABLE OpenWebUI group IDs, not names
        <group-id-readers>: [read]
        <group-id-editors>: [read, create, update]
        <group-id-admins>: [read, create, update, delete]
      protect_paths:
        - /home/hermes/skills

Semantics:
  * ACL section absent or ``enabled: false`` => legacy behavior (full access, no gating).
  * ``admin`` role => all permissions by default.
  * Otherwise permissions are the UNION of the role grant and every matching
    group grant (any role OR group grant grants a permission).
  * Fail-safe DENY: when ACL is enabled and the caller is non-admin with no
    granting role/group, the resolved permission set is empty.
  * ``delete`` is independent from ``update``.
  * A present-but-structurally-malformed section fails safe to enabled+deny for
    non-admin callers, with a populated ``error`` reason.

NOTE: ``config/hermes/config.yaml`` is intentionally NOT modified by this issue
(it carries unrelated in-flight changes). With no ``skills_acl`` section present,
the resolver reports disabled => current behavior is preserved until the section
is added.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

# The four skill permissions.
READ = "read"
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
ALL_PERMISSIONS: Set[str] = {READ, CREATE, UPDATE, DELETE}

# Map a concrete tool action to the permission it requires.
ACTION_TO_PERMISSION: Dict[str, str] = {
    # read
    "skills_list": READ,
    "skill_view": READ,
    "read": READ,
    # create
    "create": CREATE,
    # update
    "edit": UPDATE,
    "patch": UPDATE,
    "write_file": UPDATE,
    "remove_file": UPDATE,
    "update": UPDATE,
    # delete
    "delete": DELETE,
}

ADMIN_ROLE = "admin"


def _normalize_grant_map(raw: Any) -> Tuple[Dict[str, Set[str]], Optional[str]]:
    """Normalize a ``roles``/``groups`` grant mapping.

    Returns ``(grants, error)``. ``grants`` maps each key to the subset of valid
    permissions it grants (unknown permission strings are silently dropped).
    ``error`` is a human-readable string only for *structural* problems (not a
    mapping, or a grant value that is not a list) — the caller fails safe on it.
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "must be a mapping of name -> [permissions]"
    grants: Dict[str, Set[str]] = {}
    error: Optional[str] = None
    for key, value in raw.items():
        if not isinstance(value, (list, tuple, set)):
            error = f"grant for '{key}' must be a list of permissions"
            grants[str(key)] = set()
            continue
        grants[str(key)] = {str(p).strip().lower() for p in value} & ALL_PERMISSIONS
    return grants, error


def _raw_skills_acl(config: Optional[Dict[str, Any]]) -> Any:
    """Extract the raw ``skills_acl`` section from *config* or the live config."""
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config()
        except Exception:
            return None
    if not isinstance(cfg, dict):
        return None
    return cfg.get("skills_acl")


def load_skill_acl_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load and normalize the skills ACL config.

    Always returns a dict with keys ``enabled`` (bool), ``roles`` / ``groups``
    (dict[str, set[str]]), ``protect_paths`` (list[str]) and ``error``
    (Optional[str]). ``config`` (the full Hermes config dict) may be injected for
    testing; when omitted the live Hermes config is loaded. An absent
    ``skills_acl`` section => disabled (legacy). A present-but-malformed section
    fails safe to enabled+deny with ``error`` populated.
    """
    raw = _raw_skills_acl(config)
    if raw is None:
        return {
            "enabled": False,
            "roles": {},
            "groups": {},
            "protect_paths": [],
            "error": None,
        }
    if not isinstance(raw, dict):
        return {
            "enabled": True,  # present but unusable => fail safe to deny
            "roles": {},
            "groups": {},
            "protect_paths": [],
            "error": "skills_acl must be a mapping",
        }
    roles, role_err = _normalize_grant_map(raw.get("roles"))
    groups, group_err = _normalize_grant_map(raw.get("groups"))
    protect = raw.get("protect_paths")
    if isinstance(protect, str):
        protect = [protect]
    elif not isinstance(protect, list):
        protect = []
    error = role_err or group_err
    enabled = bool(raw.get("enabled", False))
    if error:
        enabled = True  # structurally malformed present config => fail safe enable+deny
    return {
        "enabled": enabled,
        "roles": roles,
        "groups": groups,
        "protect_paths": [str(p) for p in protect],
        "error": error,
    }


def _as_group_list(groups: Any) -> List[str]:
    """Coerce a groups value (comma-string or iterable) to a clean ID list."""
    if groups is None:
        return []
    if isinstance(groups, str):
        return [g.strip() for g in groups.split(",") if g.strip()]
    if isinstance(groups, (list, tuple, set)):
        return [str(g).strip() for g in groups if str(g).strip()]
    return []


def resolve_skill_permissions(
    role: Optional[str],
    groups: Any,
    cfg: Optional[Dict[str, Any]] = None,
) -> Set[str]:
    """Resolve the set of skill permissions for *role* + *groups*.

    *cfg* is a normalized config from :func:`load_skill_acl_config`; when None it
    is loaded. ``groups`` may be a comma-separated string or an iterable of
    stable OpenWebUI group IDs. Returns a subset of :data:`ALL_PERMISSIONS`.
    """
    if cfg is None:
        cfg = load_skill_acl_config()
    if not cfg.get("enabled"):
        return set(ALL_PERMISSIONS)  # ACL disabled => legacy full access
    role_norm = (role or "").strip().lower()
    if role_norm == ADMIN_ROLE:
        return set(ALL_PERMISSIONS)  # admin manages by default
    if cfg.get("error"):
        # Structurally malformed config: do NOT honor any partial role/group
        # grants — fail safe to deny all non-admin callers.
        return set()
    perms: Set[str] = set()
    roles_map = cfg.get("roles") or {}
    if role_norm and role_norm in roles_map:
        perms |= set(roles_map[role_norm])
    groups_map = cfg.get("groups") or {}
    for gid in _as_group_list(groups):
        if gid in groups_map:
            perms |= set(groups_map[gid])
    return perms & ALL_PERMISSIONS


def require_skill_permission(
    action: str,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` for a concrete skill *action*.

    Reads the caller's role/groups from the session context (issue #9). When ACL
    is disabled, always allows (legacy). *config* may be injected for tests.
    RESOLVER-ONLY: callers (#11-#13) decide how to act on the result; ``reason``
    is a user-safe denial message that does not leak policy internals.
    """
    cfg = load_skill_acl_config(config)
    if not cfg.get("enabled"):
        return True, ""
    perm = ACTION_TO_PERMISSION.get(action)
    if perm is None:
        return False, f"Hermes skills ACL: unknown action '{action}'; denied."
    try:
        from gateway.session_context import get_session_env

        role = get_session_env("HERMES_SESSION_USER_ROLE", "")
        groups = get_session_env("HERMES_SESSION_USER_GROUPS", "")
    except Exception:
        role, groups = "", ""
    perms = resolve_skill_permissions(role, groups, cfg)
    if perm in perms:
        return True, ""
    reason = (
        f"Hermes skills ACL denied action '{action}' (requires '{perm}') "
        f"for the current OpenWebUI role/group scope."
    )
    if cfg.get("error"):
        reason += " [skills_acl config error; failing safe to deny]"
    return False, reason
