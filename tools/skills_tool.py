#!/usr/bin/env python3
"""Skills Tool — list and view skill documents (progressive disclosure). A skill is a directory
holding SKILL.md (YAML frontmatter + instructions) plus optional references/, templates/, assets/,
scripts/. `skills_list` returns name/description only; `skill_view` returns full content and
linked files. Sibling modules (skills_tool_setup / _plugin / _dedup) re-export here."""

import hashlib
import json
import logging
import os
import re
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS as _EXCLUDED_SKILL_DIRS, is_skill_support_path as _is_skill_support_path)
from tools.skills_tool_setup import (  # noqa: F401
    SkillReadinessStatus, _build_setup_note, _capture_required_environment_variables,
    _get_required_environment_variables, _is_env_var_persisted, _is_remote_env_backend)
from tools.skills_tool_plugin import (  # noqa: F401
    MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH, _INJECTION_PATTERNS, _fail, _json,
    _mark_background_review_read, _preprocess_skill, _read_skill_text, _safe_frontmatter,
    _serve_plugin_skill, _serve_skill_file, _truncate_description)
from tools.skills_tool_dedup import (  # noqa: F401
    _check_skill_view_dedup, _record_skill_view, reset_skill_view_dedup)
from tools.skill_provenance import is_background_review

logger = logging.getLogger(__name__)

# Per-session discovery cache: {cache_key: (signature, timestamp, skills_list)}. Signature =
# per-dir max mtime of the dir and its immediate children (add/remove inside a category does
# NOT bump the root mtime) + the disabled set (config-only change, no mtime) + platform; the
# TTL bounds staleness from in-place SKILL.md edits, which no directory signature can see.
_SKILLS_CACHE: dict = {}
_SKILLS_CACHE_TTL_SECONDS = 30.0


_MAX_DECLARED_SKILL_VIEW_CALL_CHARS = 512


_MAX_DECLARED_SKILL_VIEW_EXAMPLES = 3


_DECLARED_SKILL_VIEW_CALL_RE = re.compile(
    rf"skill_view\s*\(([^)\n]{{1,{_MAX_DECLARED_SKILL_VIEW_CALL_CHARS}}})\)"
)


_DECLARED_SKILL_VIEW_NAME_RE = re.compile(
    r"\bname\s*=\s*['\"]([^'\"]+)['\"]"
)


_DECLARED_SKILL_VIEW_FILE_RE = re.compile(
    r"\bfile_path\s*=\s*['\"]([^'\"]+)['\"]"
)


def _declared_skill_view_examples(content: str) -> list[dict[str, str]]:
    """Extract bounded, non-traversing ``skill_view`` examples from a skill.

    These are recovery guidance only. They are never executed or used to
    bypass namespace or file ACL checks.
    """
    examples: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for call in _DECLARED_SKILL_VIEW_CALL_RE.findall(content or ""):
        name_match = _DECLARED_SKILL_VIEW_NAME_RE.search(call)
        file_match = _DECLARED_SKILL_VIEW_FILE_RE.search(call)
        if not name_match or not file_match:
            continue
        name = name_match.group(1).strip()
        file_path = file_match.group(1).strip()
        path = PurePosixPath(file_path)
        if (
            not name
            or any(char.isspace() for char in name)
            or not file_path
            or file_path.startswith("/")
            or "\\" in file_path
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            continue
        key = (name, file_path)
        if key in seen:
            continue
        seen.add(key)
        examples.append({"name": name, "file_path": file_path})
        if len(examples) >= _MAX_DECLARED_SKILL_VIEW_EXAMPLES:
            break
    return examples


def _declared_answer_contract(content: str) -> dict[str, Any] | None:
    """Extract only explicit, mechanically checkable answer rules.

    The contract is advisory metadata carried beside the skill body. It does
    not invent policy: every emitted field must be backed by literal wording
    in the loaded skill.
    """
    text = content or ""
    contract: dict[str, Any] = {}
    patterns = (
        (
            "class_1_34_max_items",
            r"For\s+\*\*Classes\s+1[–-]34\*\*,\s*choose\s+\*\*(\d+)\s+items\s+at\s+most\*\*",
        ),
        (
            "class_35_wholesale_retail_max_items",
            r"For\s+\*\*Class\s+35\*\*[^\n]{0,180}?choose\s+\*\*(\d+)\s+items\s+at\s+most\*\*",
        ),
        (
            "class_1_34_relevant_max_items",
            r"for\s+\*\*classes\s+1[–-]34\*\*\s+pick\s+\*\*(\d+)\s+items\s+at\s+most\*\*",
        ),
        (
            "class_1_34_coverage_items",
            r"however\s+pick\s+\*\*(\d+)\s+items\*\*[^\n]{0,180}?cover\s+more\s+subgroups",
        ),
        (
            "class_1_34_coverage_distinct_subgroups",
            r"three\s+items\s+preferably\s+cover\s+\*\*(\d+|three)\s+different\s+subgroups\*\*",
        ),
    )
    for key, pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).lower()
            contract[key] = 3 if value == "three" else int(value)

    if re.search(r"\*\*Always\s+list\s+every\s+chosen\s+item\*\*", text, re.IGNORECASE):
        contract["list_every_chosen_item"] = True
    if re.search(r"Total:\s*\[X\]\s+items", text, re.IGNORECASE):
        contract["require_total"] = True
    if re.search(
        r"Relevant\s+items:\s*.*Coverage\s+items:",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        contract["require_relevant_and_coverage_sections"] = True
    if re.search(
        r"however\s+pick\s+\*\*\d+\s+items\*\*[\s\S]{0,240}?"
        r"cover\s+more\s+subgroups",
        text,
        re.IGNORECASE,
    ):
        contract["coverage_must_add_new_subgroups"] = True
    if re.search(
        r"Items\s+must\s+be\s+chosen\s+ONLY\s+from[\s\S]{0,300}?"
        r"NEVER\s+invent\s+items?,\s*wording,\s*or\s*codes",
        text,
        re.IGNORECASE,
    ):
        contract["require_authoritative_item_wording"] = True

    return contract or None


_ACL_READ_DENY = (
    "Hermes skills ACL: read access could not be verified for the current "
    "OpenWebUI role/group scope; denied."
)


def _acl_read_block(action: str) -> Optional[str]:
    """Fail closed for skill reads on the multi-user OpenWebUI surface."""
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        return None
    if platform != "api_server":
        return None
    try:
        from tools.skill_acl import load_skill_acl_config, require_skill_permission

        if not load_skill_acl_config().get("enabled"):
            return None
        allowed, reason = require_skill_permission(action)
        return None if allowed else reason
    except Exception:
        return _ACL_READ_DENY


def _owned_user_skill_read(name: str) -> bool:
    """Whether this API caller explicitly addressed its own private skill."""
    try:
        from agent.skill_namespaces import (
            current_skill_namespace_user_id,
            split_builtin_qualified_name,
        )

        namespace, _ = split_builtin_qualified_name(name)
        return namespace == "user" and current_skill_namespace_user_id() is not None
    except (ImportError, ValueError):
        return False


def _skills_scan_signature(dirs_to_scan, disabled) -> tuple:
    """O(#dirs + #categories) stat-based change signature; platform is read via
    ``agent.skill_utils.sys`` so test patches are honored."""
    from agent import skill_utils as _skill_utils
    platform = getattr(getattr(_skill_utils, "sys", None), "platform", "")
    sig = []
    for d in dirs_to_scan:
        try:
            m = d.stat().st_mtime
        except OSError:
            continue
        with suppress(OSError), os.scandir(d) as it:
            for entry in it:
                with suppress(OSError):
                    if entry.is_dir(follow_symlinks=False):
                        m = max(m, entry.stat(follow_symlinks=False).st_mtime)
        sig.append((str(d), m))
    from tools.platform_skill_store import read_generation
    return (tuple(sig), frozenset(disabled), platform, read_generation())


HERMES_HOME = get_hermes_home()  # all skills live in ~/.hermes/skills/ (seeded from bundled)
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Active profile's skills dir at call time: the patched ``SKILLS_DIR`` when a patcher changed
    it, else live profile-scoped HERMES_HOME (long-lived runtimes may import before profile set)."""
    configured = Path(SKILLS_DIR)
    return configured if configured != _SKILLS_DIR_AT_IMPORT else get_hermes_home() / "skills"


_secret_capture_callback = None
_LOOKUP_HINT = "Use a skill name or relative path within the skills directory."


def _skill_lookup_path_error(name: str) -> Optional[str]:
    """Error if lookup *name* could escape the search roots it is joined onto. Windows drive
    paths are rejected too: their ``:`` would be misread as a plugin namespace separator."""
    from tools.path_security import has_traversal_component
    if not isinstance(name, str):
        return "Skill name must be a string."
    win = PureWindowsPath(candidate := name.strip())
    if PurePosixPath(candidate).is_absolute() or win.is_absolute() or win.drive:
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def load_env() -> Dict[str, str]:
    """Snapshot of HERMES_HOME/.env for the post-skill secret-capture diff (same tokenizer that
    installs the profile scope, so a captured value never differs from the served one)."""
    from agent.secret_scope import load_env_file

    return load_env_file(get_hermes_home() / ".env")


def set_secret_capture_callback(callback) -> None:
    global _secret_capture_callback
    _secret_capture_callback = callback


def _skill_utils_delegate(attr: str):
    """Lazy call-time delegate to ``agent.skill_utils.<attr>`` (re-export; patches honored)."""
    def _delegate(*args):
        from agent import skill_utils
        return getattr(skill_utils, attr)(*args)
    _delegate.__name__ = _delegate.__qualname__ = attr
    return _delegate


skill_matches_platform = _skill_utils_delegate("skill_matches_platform")
# Offer-time relevance gate (kanban/docker/s6), NOT hard compatibility; explicit loads bypass it.
skill_matches_environment = _skill_utils_delegate("skill_matches_environment")
skill_matches_apps = _skill_utils_delegate("skill_matches_apps")
_parse_frontmatter = _skill_utils_delegate("parse_frontmatter")
_get_disabled_skill_names = _skill_utils_delegate("get_disabled_skill_names")


def check_skills_requirements() -> bool:
    return True  # always available: the directory is created on first use


def _get_category_from_path(skill_path: Path) -> Optional[str]:
    """``~/.hermes/skills/mlops/axolotl/SKILL.md`` -> ``"mlops"``; active profile dir first
    (respects test monkeypatching), then skills.external_dirs."""
    dirs_to_check = [_skills_dir()]
    with suppress(Exception):
        from agent.skill_utils import get_external_skills_dirs
        dirs_to_check.extend(get_external_skills_dirs())
    for skills_dir in dirs_to_check:
        with suppress(ValueError):
            if len(parts := skill_path.relative_to(skills_dir).parts) >= 3:
                return parts[0]
    return None


def _parse_tags(tags_value) -> List[str]:
    """Tags from frontmatter: a parsed list, "[a, b]", or "a, b"."""
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]
    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


def _is_skill_disabled(name: str, platform: str = None) -> bool:
    """Disabled in config? Platform precedence: explicit arg, ``HERMES_PLATFORM``, session
    ``HERMES_SESSION_PLATFORM``. A globally-disabled skill stays disabled on every platform
    (keep in sync with agent.skill_utils.get_disabled_skill_names)."""
    try:
        from hermes_cli.config import load_config
        skills_cfg = load_config().get("skills", {})
        resolved_platform = platform or os.getenv("HERMES_PLATFORM")
        if not resolved_platform:
            with suppress(Exception):
                from gateway.session_context import get_session_env
                resolved_platform = get_session_env("HERMES_SESSION_PLATFORM") or ""
        platform_disabled = None
        if resolved_platform:
            platform_disabled = cfg_get(skills_cfg, "platform_disabled", resolved_platform)
        in_platform = platform_disabled is not None and name in platform_disabled
        return in_platform or name in skills_cfg.get("disabled", [])
    except Exception:
        return False


def _skill_search_dirs(namespace_filter=None) -> Tuple[list, list, Path]:
    from agent.skill_utils import get_skill_roots, get_project_skills_dirs
    project_dirs = list(get_project_skills_dirs()) if namespace_filter is None else []
    active_skills_dir = _skills_dir()
    roots = get_skill_roots(platform_dir=active_skills_dir)
    all_dirs = list(project_dirs)
    all_dirs.extend(
        root.path for root in roots
        if (namespace_filter is None or root.namespace == namespace_filter)
        and root.path.exists() and root.path not in all_dirs
        and (root.namespace != "user" or not (root.path.is_symlink() or root.path.parent.is_symlink()))
    )
    return project_dirs, all_dirs, active_skills_dir


def _find_all_skills(*, skip_disabled: bool = False) -> List[Dict[str, Any]]:
    """All skills (name, description, category) across project/local/external dirs, first-wins
    by name; cached per session. ``skip_disabled=True`` ignores disabled state (config UI)."""
    from agent.skill_utils import iter_project_skill_files, iter_skill_index_files
    cache_key = "with_disabled" if skip_disabled else "filtered"
    disabled = set() if skip_disabled else _get_disabled_skill_names()
    project_dirs, dirs_to_scan, _ = _skill_search_dirs()
    signature = _skills_scan_signature(dirs_to_scan, disabled)
    now = time.monotonic()
    cached = _SKILLS_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature and (now - cached[1]) < _SKILLS_CACHE_TTL_SECONDS:
        # Shallow copies: callers mutate the returned dicts (web_server annotates
        # s["enabled"]/s["usage"]); handing out cached objects would poison the cache.
        return [dict(s) for s in cached[2]]
    from agent.skill_utils import get_skill_roots
    from agent.skill_namespaces import qualify_skill_name
    roots_to_scan = get_skill_roots(platform_dir=_skills_dir())
    skills = []
    seen_names: set = set()
    for scan_dir in dirs_to_scan:  # project dirs go through the quarantine chokepoint
        _iter = iter_project_skill_files if scan_dir in project_dirs else lambda d: iter_skill_index_files(d, "SKILL.md")
        for skill_md in _iter(scan_dir):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            try:
                source_root = next((root for root in roots_to_scan if root.path == scan_dir), None)
                if source_root is not None and source_root.namespace == "user" and not skill_md.resolve().is_relative_to(source_root.path.resolve()):
                    continue
                frontmatter, body = _parse_frontmatter(_read_skill_text(skill_md)[:4000])
                if not skill_matches_platform(frontmatter) or not skill_matches_environment(frontmatter) or not skill_matches_apps(frontmatter):
                    continue
                name = frontmatter.get("name", skill_md.parent.name)[:MAX_NAME_LENGTH]
                if name in seen_names or name in disabled:
                    continue
                description = frontmatter.get("description", "")
                if not description:  # first non-heading body line (a null value stays null)
                    description = next((ln for ln in map(str.strip, body.strip().split("\n"))
                                        if ln and not ln.startswith("#")), description)
                seen_names.add(name)
                namespace = next((r.namespace for r in roots_to_scan if r.path == scan_dir), "project")
                declared_category = frontmatter.get("category")
                category = declared_category.strip() if isinstance(declared_category, str) and declared_category.strip() else _get_category_from_path(skill_md)
                skills.append({"name": name, "description": _truncate_description(description),
                               "category": category, "namespace": namespace,
                               "qualified_name": qualify_skill_name(namespace, name)})
            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug("Failed to read skill file %s: %s", skill_md, e)
            except Exception as e:
                logger.debug("Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True)
    # Keyed by the signature computed BEFORE the scan: a write racing the scan changes the
    # signature, so the next call re-scans instead of serving a torn result.
    _SKILLS_CACHE[cache_key] = (signature, now, skills)
    return [dict(s) for s in skills]


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep every skill listing path ordered the same way."""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(category: str = None, task_id: str = None) -> str:
    """Tier 1 listing: name + description (+ category) only; ``task_id`` is handler parity."""
    blocked = _acl_read_block("skills_list")
    if blocked:
        return tool_error(blocked, success=False)
    try:
        all_skills = _find_all_skills()
        try:
            from hermes_cli.plugins import discover_plugins, get_plugin_manager
            discover_plugins()
            for plugin_skill in get_plugin_manager().list_plugin_skill_metadata():
                frontmatter = plugin_skill.pop("frontmatter", {})
                if not skill_matches_platform(frontmatter) or _is_skill_disabled(plugin_skill["name"]):
                    continue
                all_skills.append(plugin_skill)
        except Exception:
            logger.debug("Plugin skill listing failed", exc_info=True)
        if not all_skills:
            return _json({"success": True, "skills": [], "categories": [],
                          "message": "No skills found in skills/ directory."})
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]
        all_skills = _sort_skills(all_skills)
        categories = sorted({s.get("category") for s in all_skills if s.get("category")})
        return _json({
            "success": True, "skills": all_skills, "categories": categories,
            "count": len(all_skills),
            "hint": "Use skill_view(name) to see full content, tags, and linked files"})
    except Exception as e:
        return tool_error(str(e), success=False)


def _resolve_plugin_skill(name, file_path, task_id, preprocess):
    """``plugin:skill`` dispatch: ``(result_json, None)`` when answered, else ``(None,
    local_category_name)`` to fall through to the flat-tree scan — categorized local skills also use
    ``category:skill`` in config/gateway prompts, so the on-disk ``category/skill`` form returns."""
    from agent.skill_utils import is_valid_namespace, parse_qualified_name
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    namespace, bare = parse_qualified_name(name)
    if not is_valid_namespace(namespace):
        return _fail(f"Invalid namespace '{namespace}' in '{name}'. Namespaces must match [a-zA-Z0-9_-]+."), None
    discover_plugins()  # idempotent
    pm = get_plugin_manager()
    active_memory_provider = None
    try:
        from plugins.memory import _get_active_memory_provider, _prune_inactive_memory_provider_skills
        active_memory_provider = _get_active_memory_provider()
        _prune_inactive_memory_provider_skills(active_memory_provider)
    except Exception as exc:
        logger.debug("Failed pruning inactive memory-provider skills: %s", exc)
    plugin_skill_md = pm.find_plugin_skill(name)
    # Memory providers load through plugins.memory, not the general PluginManager: load the
    # namespaced provider once so its collector can forward its skills into the registry.
    if plugin_skill_md is None and namespace == active_memory_provider:
        try:
            from plugins.memory import load_memory_provider
            load_memory_provider(namespace)
            plugin_skill_md = pm.find_plugin_skill(name)
        except Exception as exc:
            logger.debug("Failed lazy memory-provider skill load for %s: %s", namespace, exc)
    if plugin_skill_md is not None and not plugin_skill_md.exists():
        pm.remove_plugin_skill(name)  # stale registry entry — file deleted out of band
        return _fail(
            f"Skill '{name}' file no longer exists at {plugin_skill_md}. The registry entry "
            f"has been cleaned up — try again after the plugin is reloaded."), None
    if plugin_skill_md is not None:
        return _serve_plugin_skill(
            plugin_skill_md, namespace, bare, file_path=file_path, preprocess=preprocess, session_id=task_id), None
    if available := pm.list_plugin_skills(namespace):  # plugin exists but this specific skill is missing
        return _fail(
            f"Skill '{bare}' not found in plugin '{namespace}'.",
            available_skills=[f"{namespace}:{s}" for s in available],
            hint=f"The '{namespace}' plugin provides {len(available)} skill(s)."), None
    return None, (f"{namespace}/{bare}" if bare else None)  # plugin not found → local scan


def _under_any(path: Path, dirs) -> bool:
    """True when ``path`` (resolved where possible) lives under one of ``dirs``."""
    resolved = path
    with suppress(Exception):
        resolved = path.resolve()
    return any(resolved.is_relative_to(d) for d in dirs)


def _is_package_owned_markdown(path: Path, search_root: Path) -> bool:
    """True when a legacy Markdown candidate belongs to an ancestor directory skill."""
    try:
        relative = path.relative_to(search_root)
    except ValueError:
        return False
    return any(
        (search_root.joinpath(*relative.parts[:depth]) / "SKILL.md").is_file()
        for depth in range(1, len(relative.parts))
    )


def _collect_skill_candidates(name, local_category_name, all_dirs):
    """ALL (skill_dir, skill_md) candidates across every dir and lookup strategy (direct path,
    recursive by dir / frontmatter name, legacy flat <name>.md), deduped by resolved path.
    Collision detection is the point: silent shadowing of a local skill by a same-named
    external one is a real bug class, so the caller refuses >1."""
    from agent.skill_utils import iter_skill_index_files
    candidates: List[Tuple[Optional[Path], Path]] = []
    seen_md: set = set()

    def _record(sd: Optional[Path], smd: Path) -> None:
        key = smd
        with suppress(Exception):
            key = smd.resolve()
        if key not in seen_md:
            seen_md.add(key)
            candidates.append((sd, smd))

    def _record_direct(direct_path: Path, search_root: Path) -> None:  # "mlops/axolotl" / "axolotl" or its flat .md sibling
        flat = direct_path.with_suffix(".md")
        if not _is_skill_support_path(direct_path) and direct_path.is_dir() and (direct_path / "SKILL.md").exists():
            _record(direct_path, direct_path / "SKILL.md")
        elif (flat.exists() and not _is_skill_support_path(flat)
              and not _is_package_owned_markdown(flat, search_root)):
            _record(None, flat)

    for search_dir in all_dirs:
        for direct in filter(None, (name, local_category_name)):  # "p:x" with no plugin p → "p/x"
            _record_direct(search_dir / direct, search_dir)
        # Recursive by directory name plus frontmatter `name:` — skills_list()
        # exposes the frontmatter name, so skill_view(name) must accept it too.
        for found_skill_md in iter_skill_index_files(search_dir, "SKILL.md"):
            if (found_skill_md.parent.name == name
                    or _safe_frontmatter(found_skill_md).get("name") == name):
                _record(found_skill_md.parent, found_skill_md)
        # Legacy flat <name>.md anywhere under the dir. Markdown owned by an ancestor
        # directory skill loads through file_path and must not shadow a real skill.
        for found_md in search_dir.rglob(f"{name}.md"):
            if (found_md.name != "SKILL.md" and not _is_skill_support_path(found_md)
                    and not _is_package_owned_markdown(found_md, search_dir)):
                _record(None, found_md)
    return candidates


# (support dir, globs, recursive, files only) — order is the linked_files key order.
_LINKED_FILE_SPECS = (
    ("references", ["*.md"], False, False),
    ("templates", ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"], True, False),
    ("assets", ["*"], True, True),
    ("scripts", ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"], False, False))


def _skill_linked_files(skill_dir: Optional[Path]) -> dict:
    """references/templates/assets/scripts of a directory skill (empty groups dropped)."""
    files: dict = {}
    for sub, globs, recursive, files_only in _LINKED_FILE_SPECS if skill_dir else ():
        base = skill_dir / sub
        found = [
            str(f.relative_to(skill_dir)) for g in globs if base.exists()
            for f in (base.rglob(g) if recursive else base.glob(g))
            if not files_only or f.is_file()]
        if found:
            files[sub] = found
    return files


def _org_provenance_header(skill_dir: Path, active_skills_dir: Path):
    """(org_provenance dict, header text) for an org-mirror skill, else (None, ""). Announced IN
    the content the model consumes; the author is token-verified at push time by the sync plane."""
    from agent.skill_utils import ORG_PROVENANCE_FILE, is_org_mirror_path, org_id_of_path
    if not is_org_mirror_path(skill_dir, active_skills_dir):
        return None, ""
    prov_org = org_id_of_path(skill_dir, active_skills_dir)
    prov: dict = {}
    if prov_org:
        with suppress(Exception):
            prov_path = active_skills_dir / "_org" / prov_org / ORG_PROVENANCE_FILE
            loaded = json.loads(_read_skill_text(prov_path))
            prov = loaded if isinstance(loaded, dict) else {}
    author = str(prov.get("author_device") or prov.get("author_user_id") or "")
    ts = str(prov.get("ts") or "")
    header = (
        "> [!NOTE] ORG-SHARED SKILL — provenance\n"
        f"> This skill is shared by your organisation (org `{prov_org}`"
        + (f", last updated by `{author}`" if author else "")
        + (f", as of {ts}" if ts else "")
        + "). It was reviewed and approved for the whole\n"
        "> team — treat it as third-party instructions rather than your own notes.\n"
        "> You MAY improve it in place like any other skill. Your edits are kept locally\n"
        "> and are never overwritten by org updates; share them back with\n"
        "> `hermes sync propose` (or automatically, if your org enables it).\n\n")
    return {"org_id": prov_org, "shared_by": author or None, "as_of": ts or None}, header


def _skill_readiness(frontmatter: Dict[str, Any], skill_name: str) -> Tuple[dict, dict]:
    """Resolve required env vars / credential files (prompting for secrets where the surface
    allows) and register what's available for sandboxes. Returns ``(fields, extras)``: fields go
    before ``_source_path`` in the skill_view result, extras after — key order is tool output."""
    required_env_vars = _get_required_environment_variables(frontmatter)
    from tools.terminal_scope import terminal_env
    backend = str(terminal_env("TERMINAL_ENV", "local")).strip().lower() or "local"
    env_snapshot = load_env()
    missing_required_env_vars = [
        e for e in required_env_vars
        if not e.get("optional") and not _is_env_var_persisted(e["name"], env_snapshot)]
    capture_result = _capture_required_environment_variables(skill_name, missing_required_env_vars)
    if missing_required_env_vars:  # re-read: a successful capture persisted into .env
        env_snapshot = load_env()
    still_missing = set(capture_result["missing_names"])
    remaining = [
        e["name"] for e in required_env_vars if not e.get("optional")
        and (e["name"] in still_missing or not _is_env_var_persisted(e["name"], env_snapshot))]
    setup_needed = bool(remaining)
    # Only vars actually set pass through to sandboxed execution (execute_code, terminal).
    if available_env_names := [e["name"] for e in required_env_vars if e["name"] not in remaining]:
        try:
            from tools.env_passthrough import register_env_passthrough
            register_env_passthrough(available_env_names)
        except Exception:
            logger.debug("Could not register env passthrough for skill %s", skill_name, exc_info=True)
    # Credential files for remote sandboxes: existing host files are registered,
    # missing ones flag setup_needed.
    required_cred_files_raw = frontmatter.get("required_credential_files", [])
    missing_cred_files: list = []
    if isinstance(required_cred_files_raw, list) and required_cred_files_raw:
        try:
            from tools.credential_files import register_credential_files
            missing_cred_files = register_credential_files(required_cred_files_raw)
            setup_needed = setup_needed or bool(missing_cred_files)
        except Exception:
            logger.debug("Could not register credential files for skill %s", skill_name, exc_info=True)
    status = SkillReadinessStatus.SETUP_NEEDED if setup_needed else SkillReadinessStatus.AVAILABLE
    fields = {
        "required_environment_variables": required_env_vars, "required_commands": [],
        "missing_required_environment_variables": remaining,
        "missing_credential_files": missing_cred_files, "missing_required_commands": [],
        "setup_needed": setup_needed, "setup_skipped": capture_result["setup_skipped"],
        "readiness_status": status.value}
    extras: dict = {}
    if setup_help := next((e["help"] for e in required_env_vars if e.get("help")), None):
        extras["setup_help"] = setup_help
    if capture_result["gateway_setup_hint"]:
        extras["gateway_setup_hint"] = capture_result["gateway_setup_hint"]
    missing_items = [f"env ${n}" for n in remaining] + [f"file {p}" for p in missing_cred_files]
    if setup_needed and (setup_note := _build_setup_note(status, missing_items, setup_help)):
        if _is_remote_env_backend(backend):
            setup_note = f"{setup_note} {backend.upper()}-backed skills need these requirements available inside the remote environment as well."
        extras["setup_note"] = setup_note
    return fields, extras


def _owning_search_dir(skill_md: Path, all_dirs) -> Optional[Path]:
    """Most specific search dir containing *skill_md*, compared lexically: a symlinked entry
    belongs to the root that exposes it, not to the root its target lives in."""
    owners = [Path(d) for d in all_dirs if skill_md.is_relative_to(d)]
    return max(owners, key=lambda d: len(d.parts), default=None)


def _rank_same_root_candidate(candidate, root: Path) -> tuple:
    """Real SKILL.md beats a legacy flat ``<name>.md``, then the shallower path wins."""
    _skill_dir, skill_md = candidate
    return (skill_md.name != "SKILL.md", len(skill_md.relative_to(root).parts))


def _provably_same_skill(candidates) -> bool:
    """True only when every candidate is the SAME skill: one resolved SKILL.md (symlink view)
    or byte-identical content (copy). Anything else is two different skills sharing a name,
    and picking one by depth would let ``<root>/evil`` (``name: github``) shadow the real one."""
    try:
        if len({os.path.realpath(smd) for _sd, smd in candidates}) == 1:
            return True
        return len({hashlib.sha256(smd.read_bytes()).hexdigest() for _sd, smd in candidates}) == 1
    except OSError:
        return False


def _locate_skill(name: str, local_category_name: Optional[str], project_dirs: list, all_dirs, *, roots=()):
    """Unique on-disk skill for *name*: collision refusal, project-tier precedence, same-root
    precedence, quarantine gate, not-found listing. ``(error_json, skill_dir, skill_md)``;
    skill_md set iff no error."""
    if not all_dirs:
        return _fail(
            "Skills directory does not exist yet. It will be created on first install."), None, None
    candidates = _collect_skill_candidates(name, local_category_name, all_dirs)
    if len(candidates) > 1 and project_dirs:
        # A project skill intentionally overrides a same-named local/external skill;
        # ambiguity WITHIN the project tier (two different skills) still refuses.
        candidates = [c for c in candidates if _under_any(c[1], project_dirs)] or candidates
    if len(candidates) > 1:
        rooted = []
        for candidate in candidates:
            owner = next((root for root in roots if candidate[1].is_relative_to(root.path)), None)
            rooted.append((owner, candidate))
        users = [candidate for root, candidate in rooted if root is not None and root.namespace == "user"]
        if len(users) == 1 and all(root is not None and root.namespace in {"user", "platform"} for root, _ in rooted):
            candidates = users
    if len(candidates) > 1:
        # The refusal below guards against one skill silently shadowing another. Copies of ONE
        # skill inside a single search dir (``<root>/x`` symlink view + ``<root>/cat/x`` copy)
        # shadow nothing, so rank them instead; different content, an equal-rank tie or a
        # cross-tier spread still refuses.
        roots = {_owning_search_dir(smd, all_dirs) for _sd, smd in candidates}
        if len(roots) == 1 and None not in roots and _provably_same_skill(candidates):
            root = roots.pop()
            ranked = sorted(candidates, key=lambda c: _rank_same_root_candidate(c, root))
            if _rank_same_root_candidate(ranked[0], root) != _rank_same_root_candidate(ranked[1], root):
                logger.info("Skill '%s': %d identical same-root copies, resolved to %s (duplicates: %s)",
                            name, len(candidates), ranked[0][1],
                            "; ".join(str(smd) for _sd, smd in ranked[1:]))
                candidates = [ranked[0]]
    if len(candidates) > 1:
        paths = [str(smd) for _, smd in candidates]
        logger.warning("Skill name collision for '%s': %d candidates — %s", name, len(candidates), "; ".join(paths))
        return _fail(
            f"Ambiguous skill name '{name}': {len(candidates)} skills match across your local skills dir "
            "and external_dirs. Refusing to guess — load one explicitly by its categorized path.",
            matches=paths,
            hint="Pass the full relative path instead of the bare name (e.g., 'category/skill-name'), "
            "or rename one of the colliding skills so each name is unique."), None, None
    skill_dir, skill_md = candidates[0] if candidates else (None, None)
    # Quarantine gate: a project-tier skill with a dangerous scan verdict must not
    # load even by explicit name (same chokepoint the index and skills_list use).
    if skill_md is not None and project_dirs:
        from agent.skill_utils import is_quarantined_project_skill
        if _under_any(skill_md, project_dirs) and is_quarantined_project_skill(skill_md):
            return _fail(
                f"Project skill '{name}' is quarantined: the security scan flagged its content as "
                "dangerous. It will not load until the repo's skill content changes and passes a re-scan.",
                hint="Inspect the skill in the repo checkout, or untrust the repo with "
                "`hermes skills untrust`."), None, None
    if not skill_md or not skill_md.exists():
        available = [s["name"] for s in _sort_skills(_find_all_skills())[:20]]
        return _fail(f"Skill '{name}' not found.", available_skills=available,
                     hint="Use skills_list to see all available skills"), None, None
    return None, skill_dir, skill_md


def _log_security_warnings(name: str, skill_md: Path, content: str, all_dirs, active_skills_dir):
    """Warn (never block) when loaded from outside the trusted dirs (project + local + external)
    and/or when common prompt-injection patterns appear. The check is on the RESOLVED path:
    every candidate is built as ``<search_dir>/...`` so a lexical test can never fire, and a
    SKILL.md symlinked to a file outside every root is exactly what this guards against."""
    trusted_dirs = [active_skills_dir.resolve()]
    with suppress(Exception):
        trusted_dirs.extend(d.resolve() for d in all_dirs)
    warnings = []
    if not _under_any(skill_md, trusted_dirs):
        warnings.append(f"skill file is outside the trusted skills directory (~/.hermes/skills/): {skill_md}")
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        warnings.append("skill content contains patterns that may indicate prompt injection")
    if warnings:
        logger.warning("Skill security warning for '%s': %s", name, "; ".join(warnings))


def skill_view(
    name: str, file_path: str = None, task_id: str = None, preprocess: bool = True) -> str:
    """View a skill (SKILL.md) or a file within its directory, as JSON. ``name`` is a skill name
    or path ("axolotl", "03-fine-tuning/axolotl"); "plugin:skill" resolves plugin-provided
    skills. ``preprocess`` applies the configured SKILL.md template / inline shell rendering;
    slash/preload callers render the message themselves."""
    blocked = _acl_read_block("skill_view")
    if blocked and not _owned_user_skill_read(name):
        return tool_error(blocked, success=False)
    try:
        # Validate before the ':' dispatch so a Windows drive path (C:\skills\foo) can't be
        # reinterpreted as a plugin namespace.
        if lookup_error := _skill_lookup_path_error(name):
            return _fail(lookup_error, hint=_LOOKUP_HINT)
        from agent.skill_namespaces import split_builtin_qualified_name, qualify_skill_name
        from agent.skill_utils import get_skill_roots
        root_namespace_filter, name = split_builtin_qualified_name(name)
        if lookup_error := _skill_lookup_path_error(name):
            return _fail(lookup_error, hint=_LOOKUP_HINT)
        local_category_name: str | None = None
        if ":" in name:  # plugin registry; bare names use the flat-tree scan below
            served, local_category_name = _resolve_plugin_skill(name, file_path, task_id, preprocess)
            if served is not None:
                return served
        # The fall-through form (namespace/bare) joins onto each search dir too; re-validate it
        # since `bare` is not namespace-checked.
        if local_category_name and (lookup_error := _skill_lookup_path_error(local_category_name)):
            return _fail(lookup_error, hint=_LOOKUP_HINT)
        project_dirs, all_dirs, active_skills_dir = _skill_search_dirs(root_namespace_filter)
        all_roots = [root for root in get_skill_roots(platform_dir=active_skills_dir)
                     if root_namespace_filter is None or root.namespace == root_namespace_filter]
        error, skill_dir, skill_md = _locate_skill(
            name, local_category_name, project_dirs, all_dirs, roots=all_roots)
        if error is not None:
            return error
        selected_root = next((root for root in all_roots if skill_md.is_relative_to(root.path)), None)
        if selected_root is not None and selected_root.namespace == "user":
            if selected_root.path.is_symlink() or selected_root.path.parent.is_symlink() or not skill_md.resolve().is_relative_to(selected_root.path.resolve()):
                return _fail("Refusing a user-skill path outside the authenticated namespace.")
        try:  # read once — reused for platform check and main content
            content = _read_skill_text(skill_md)
        except Exception as e:
            return _fail(f"Failed to read skill '{name}': {e}")
        _log_security_warnings(name, skill_md, content, all_dirs, active_skills_dir)
        frontmatter = _safe_frontmatter(content=content)
        if not skill_matches_platform(frontmatter):
            return _fail(f"Skill '{name}' is not supported on this platform.", readiness_status=SkillReadinessStatus.UNSUPPORTED.value)
        resolved_name = frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return _fail(f"Skill '{resolved_name}' is disabled. Enable it with `hermes skills` or inspect the files directly on disk.")
        if file_path and skill_dir:
            return _serve_skill_file(
                skill_dir, file_path, name, list_available=True, mark_read=True,
                hint="Use a relative path within the skill directory",
                declared_examples=_declared_skill_view_examples(content))
        # tags/related_skills: metadata.hermes.* (agentskills.io) first, then top-level.
        metadata = frontmatter.get("metadata")
        hermes_meta = (metadata.get("hermes", {}) or {}) if isinstance(metadata, dict) else {}
        tags, related_skills = (
            _parse_tags(hermes_meta.get(k) or frontmatter.get(k, "")) for k in ("tags", "related_skills"))
        linked_files = _skill_linked_files(skill_dir)
        try:
            rel_path = str(skill_md.relative_to(active_skills_dir))
        except ValueError:  # external skill — relative to its own parent dir
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
        skill_name = frontmatter.get("name", skill_md.stem if not skill_dir else skill_dir.name)
        readiness, readiness_extras = _skill_readiness(frontmatter, skill_name)
        rendered_content = content if not preprocess else _preprocess_skill(
            content, skill_dir, task_id, "Could not preprocess skill content for %s", skill_name)
        org_provenance, header = None, ""
        if skill_dir:
            try:
                org_provenance, header = _org_provenance_header(skill_dir, active_skills_dir)
            except Exception:
                logger.debug("Could not resolve org provenance for %s", skill_name, exc_info=True)
        declared_examples = _declared_skill_view_examples(rendered_content)
        answer_contract = _declared_answer_contract(rendered_content)
        source_contract = None
        if declared_examples:
            source_contract = {
                "required_before_answer": True,
                "instruction": (
                    "Load every relevant declared skill_view source before the "
                    "final answer. Replace placeholders from the user's request. "
                    "Do not substitute memory, local-path guesses, attachments, "
                    "terminal access, or a prior run for these sources."
                ),
                "declared_skill_view_examples": declared_examples,
                "answer_contract": answer_contract,
            }

        result = {
            "success": True, "name": skill_name, "description": frontmatter.get("description", ""),
            "namespace": selected_root.namespace if selected_root else "project",
            "qualified_name": qualify_skill_name(selected_root.namespace if selected_root else "project", skill_name),
            "tags": tags, "related_skills": related_skills, "source_contract": source_contract,
            "content": header + rendered_content,
            "path": rel_path, "skill_dir": str(skill_dir) if skill_dir else None,
            "org_provenance": org_provenance,
            "linked_files": linked_files if linked_files else None,
            "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'" if linked_files else None,
            **readiness,
            # Internal: absolute source path for the repeat-view dedup fingerprint.
            "_source_path": str(skill_md),
            **readiness_extras}
        _mark_background_review_read(skill_md)
        if frontmatter.get("compatibility"):  # agentskills.io optional fields
            result["compatibility"] = frontmatter["compatibility"]
        if isinstance(metadata, dict):
            result["metadata"] = metadata
        return _json(result)
    except Exception as e:
        return tool_error(str(e), success=False)


SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "The only tool for reading SKILL.md or anything under a skills directory; never use read_file/search_files for skill paths. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus an exact 'linked_files' inventory. To access one, call again with that exact file_path; never guess an unlisted path.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills). For plugin-provided skills, use the qualified form 'plugin:skill' (e.g. 'superpowers:writing-plans').",
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="skills_list", toolset="skills", schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(category=args.get("category"), task_id=kw.get("task_id")),
    check_fn=check_skills_requirements, emoji="📚")


def _skill_view_with_bump(args, **kw):
    """Invoke skill_view, then bump view_count on success. Best-effort: a
    telemetry failure never breaks the tool call."""
    name = args.get("name", "")
    task_id = kw.get("task_id")
    # ── Repeat-view dedup ────────────────────────────────────────────
    # Mirrors read_file's unchanged-stub: when this session already
    # loaded the SAME skill file and it hasn't changed on disk, return a
    # short stub instead of re-sending the full content (production
    # mining: ~286k tokens of verbatim repeat skill_view content in one
    # 400k-message window). The stub only ever replaces content that is
    # already fully present earlier in this conversation, so the
    # "skills must be loaded fully" rule is preserved — and the cache is
    # cleared on context compression (same hook as read_file's dedup)
    # so a post-compression re-view returns full content again.
    # Explicit file-tool routing is a recovery path: the model is asking for
    # the bytes again because the earlier copy may no longer be usable after
    # context pruning.  In that path an ``unchanged`` stub is actively
    # misleading, so callers may require the authoritative content.  This is
    # deliberately a private handler kwarg; it is not exposed in the model's
    # skill_view schema and ordinary repeated skill_view calls still dedup.
    dedup_task_id = None if is_background_review() else task_id
    force_content = bool(kw.get("force_content", False))
    if not force_content:
        stub = _check_skill_view_dedup(dedup_task_id, name, args.get("file_path"))
        if stub is not None:
            return stub
    result = skill_view(
        name, file_path=args.get("file_path"), task_id=task_id
    )
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            _record_skill_view(dedup_task_id, name, args.get("file_path"), parsed)
            # Use the resolved skill name from the payload when present —
            # qualified forms ("plugin:skill") return with the canonical name.
            resolved = parsed.get("name") or name
            if resolved:
                from tools.skill_usage import bump_use, bump_view, skill_usage_scope

                skills_root = None
                skill_dir = parsed.get("skill_dir")
                if skill_dir:
                    from agent.skill_utils import get_skill_roots

                    resolved_dir = Path(str(skill_dir)).resolve()
                    for root in get_skill_roots(platform_dir=_skills_dir()):
                        try:
                            resolved_dir.relative_to(root.path.resolve())
                            skills_root = root.path
                            break
                        except (OSError, ValueError):
                            continue
                with skill_usage_scope(skills_root):
                    bump_view(str(resolved))
                    bump_use(
                        str(resolved),
                        task_id=kw.get("task_id"),
                        session_id=kw.get("session_id"),
                    )
    except Exception:
        pass
    return result


registry.register(
    name="skill_view", toolset="skills", schema=SKILL_VIEW_SCHEMA, handler=_skill_view_with_bump,
    check_fn=check_skills_requirements, emoji="📚")


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from enum import Enum  # noqa: F401,E402
from typing import Set  # noqa: F401,E402
import re  # noqa: F401,E402
import threading  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'display_hermes_home': ('hermes_constants', 'display_hermes_home'),
    'env_var_enabled': ('utils', 'env_var_enabled'),
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
