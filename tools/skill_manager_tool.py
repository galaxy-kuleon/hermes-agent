#!/usr/bin/env python3
"""Skill Manager Tool — agent-managed skill creation & editing.

Skills are the agent's procedural memory (narrow "how to do X"; MEMORY.md/USER.md are
broad, declarative). New skills land in ~/.hermes/skills/ (or ``skills.create_dir``);
existing skills (bundled, hub, user) are modified in place. Layout:
``<skills>/[category/]<skill>/SKILL.md`` + optional ``references/ templates/ scripts/ assets/``.
"""

import contextvars as _ctxvars
import hashlib
import json
from contextlib import ExitStack, suppress, nullcontext
import logging
import os
import tempfile
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from hermes_constants import get_hermes_home, display_hermes_home
from utils import atomic_write_text, is_truthy_value
from hermes_cli.config import cfg_get
from agent.skill_utils import (
    extract_skill_description,
    is_skill_description_truncated_for_prompt,
    parse_frontmatter as _parse_frontmatter,
    SKILL_PROMPT_DESC_LIMIT)
from tools.skill_manager_guards import (
    _background_review_preflight, _background_review_read_before_write_guard, _background_review_write_guard,
    _containing_skills_root, _curator_consolidation_delete_guard, _maybe_auto_propose_org_edit,
    _org_mirror_write_guard, _pinned_guard, _validate_delete_target, _is_background_review, _refusal as _err)
from tools.skill_manager_batch import (
    _PATCH_EITHER_OR, _PATCH_NEEDS_NEW_STRING, _PATCH_NEEDS_OLD_STRING, _op_shape_error, _skill_manage_batch)
from tools.skills_guard import scan_skill, should_allow_install, format_scan_report

logger = logging.getLogger(__name__)


def _guard_agent_created_enabled() -> bool:
    """skills.guard_agent_created (default False): opt-in — terminal() runs the same code ungated."""
    try:
        from hermes_cli.config import load_config
        return is_truthy_value(cfg_get(load_config(), "skills", "guard_agent_created"), default=False)
    except Exception:
        return False


def _security_scan_skill(skill_dir: Path) -> Optional[str]:
    """Post-write scan (opt-in); error string if blocked, else None. An "ask" verdict
    (dangerous findings) is surfaced as an error so the agent can retry without them."""
    if not _guard_agent_created_enabled():
        return None
    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is None:
            logger.warning("Agent-created skill blocked (dangerous findings): %s", reason)
        if allowed is not True:
            return f"Security scan blocked this skill ({reason}):\n{format_scan_report(result)}"
    except Exception as e:
        logger.warning("Security scan failed for %s: %s", skill_dir, e, exc_info=True)
    return None


# All skills live in ~/.hermes/skills/ (single source of truth)
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Active profile's skills dir at call time (multi-profile runtimes rebind per session).
    An explicitly patched module-level ``SKILLS_DIR`` (tests) wins over the live HERMES_HOME.

    Long-lived multi-profile runtimes (Dashboard/TUI/Desktop backend, cron, kanban workers) import this
    module once under the launch HERMES_HOME and later bind a different profile per session (#40677).
    """
    configured = Path(SKILLS_DIR)
    return configured if configured != _SKILLS_DIR_AT_IMPORT else get_hermes_home() / "skills"


def _skill_lock_path(name: str) -> Path:
    namespace, bare_name, error = _normalize_namespace_and_name(name)
    if error or _validate_name(Path(bare_name).name):
        raise ValueError(error or "Invalid skill name for mutation lock")
    existing = _find_managed_skill(bare_name, namespace)
    _, root, root_error = _resolve_skill_dir(bare_name, namespace=namespace)
    if existing is None and root is None:
        raise ValueError(root_error or "A verified subject and available namespace are required for a skill mutation lock")
    skills_root = Path(existing["root"]) if existing else root.path
    from tools.skill_state import is_platform_skills_root, platform_skill_state_dir
    lock_root = platform_skill_state_dir() if is_platform_skills_root(skills_root) else skills_root
    digest = hashlib.sha256(Path(bare_name).name.encode("utf-8", "surrogatepass")).hexdigest()
    return lock_root / ".locks" / f"{digest}.lock"


def _skill_mutation_lock(name: str):
    """Exclusive lock held across one skill's whole read-modify-write; thread-re-entrant."""
    from tools.skill_usage import skill_file_lock
    return skill_file_lock(_skill_lock_path(name))


def _skill_mutation_locks(names):
    """Every lock of an atomic batch, acquired in one stable path order (deadlock-free across batches)."""
    from tools.skill_usage import skill_file_lock
    stack = ExitStack()
    for lock_path in sorted({_skill_lock_path(n) for n in names}):
        stack.enter_context(skill_file_lock(lock_path))
    return stack


MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')  # filesystem-safe, URL-friendly
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}  # for write_file/remove_file
_FRONTMATTER_END_RE = re.compile(r'\n---\s*\n')
_NAME_RULE = "Use lowercase letters, numbers, hyphens, dots, and underscores."


def _display_create_dir() -> str:
    """Skill-creation dir for schema/instruction text; follows ``skills.create_dir``."""
    try:
        from agent.skill_utils import display_skill_create_dir
        return display_skill_create_dir()
    except Exception:
        return f"{display_hermes_home()}/skills/"


# --- Validation helpers -------------------------------------------------------

def _check_identifier(value: str, label: str, invalid: str) -> Optional[str]:
    if len(value) > MAX_NAME_LENGTH:
        return f"{label} exceeds {MAX_NAME_LENGTH} characters."
    return None if VALID_NAME_RE.match(value) else invalid


def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "Skill name is required."
    return _check_identifier(
        name, "Skill name", f"Invalid skill name '{name}'. {_NAME_RULE} Must start with a letter or digit.")


def _validate_category(category: Optional[str]) -> Optional[str]:
    if category is None or (isinstance(category, str) and not category.strip()):
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    category = category.strip()
    invalid = (f"Invalid category '{category}'. {_NAME_RULE} "
               "Categories must be a single directory name.")
    if "/" in category or "\\" in category:
        return invalid
    return _check_identifier(category, "Category", invalid)


def _validate_frontmatter(content: str, *, new_skill: bool = False) -> Optional[str]:
    """Validate frontmatter (name + description) and a non-empty body. ``new_skill`` (create
    only) also enforces SKILL_PROMPT_DESC_LIMIT so new skills never lose routing signal to
    index truncation; edit/patch skip it so existing over-limit skills stay maintainable."""
    if not content.strip():
        return "Content cannot be empty."
    content = content.lstrip("\ufeff")  # tolerate a Windows UTF-8 BOM
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."
    end_match = _FRONTMATTER_END_RE.search(content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."
    try:
        parsed = yaml.safe_load(content[3:end_match.start() + 3])
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"
    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    for field in ("name", "description"):
        if field not in parsed:
            return f"Frontmatter must include '{field}' field."
    desc = str(parsed["description"])
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if new_skill and len(desc.strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT:
        return (
            f"Description is {len(desc.strip())} chars — new skills must fit the "
            f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget (one sentence, trigger first, "
            f"ends with a period). The skill index truncates longer descriptions to "
            f"{SKILL_PROMPT_DESC_LIMIT - 3} chars + '...', destroying the routing signal. "
            f"Move detail into the skill body.")
    if not content[end_match.end() + 3:].strip():
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."
    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters (limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files in references/ "
            f"or templates/.")
    return None


def _description_preview(content: str) -> str:
    """First 120 chars of the frontmatter description; '' on any failure."""
    with suppress(Exception):
        fm_end = _FRONTMATTER_END_RE.search(content[3:])
        if fm_end:
            return str(yaml.safe_load(content[3:fm_end.start() + 3]).get("description", ""))[:120]
    return ""


MAX_SKILL_IMPORT_FILES = 256


MAX_SKILL_IMPORT_CHARS = 48_000_000


SKILL_IMPORT_CHUNK_CHARS = 240_000


DEFAULT_SKILL_IMPORT_SUBDIR = "references/source-library"


def _skill_roots():
    from agent.skill_namespaces import EXTERNAL_NAMESPACE_PREFIX, SkillRoot
    from agent.skill_utils import get_all_skills_dirs, get_skill_roots

    roots = list(get_skill_roots(platform_dir=_skills_dir()))
    seen = {
        root.path.resolve() if root.path.exists() else root.path.absolute()
        for root in roots
    }
    # Preserve the long-standing extension seam used by embedders and tests:
    # callers may override get_all_skills_dirs() with extra writable roots.
    for candidate in get_all_skills_dirs():
        path = Path(candidate)
        identity = path.resolve() if path.exists() else path.absolute()
        if identity in seen:
            continue
        seen.add(identity)
        roots.append(SkillRoot(f"{EXTERNAL_NAMESPACE_PREFIX}-{len(roots)}", path))
    return roots


def _normalize_namespace_and_name(
    name: str, namespace: Optional[str] = None
) -> Tuple[Optional[str], str, Optional[str]]:
    from agent.skill_namespaces import split_builtin_qualified_name

    try:
        resolved_namespace, bare_name = split_builtin_qualified_name(name, namespace)
    except ValueError as exc:
        return None, name, str(exc)
    if resolved_namespace is not None and not bare_name:
        return None, bare_name, "Skill name is required after the namespace qualifier."
    return resolved_namespace, bare_name, None


def _root_for_namespace(namespace: str):
    return next((root for root in _skill_roots() if root.namespace == namespace), None)


def _default_create_namespace() -> str:
    return "user" if _root_for_namespace("user") is not None else "platform"


def _namespace_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except (ValueError, TypeError):
        return path.name


def _find_skill_draft(
    name: str, namespace: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Find an inactive skill draft without exposing it to skill discovery."""
    from agent.skill_namespaces import qualify_skill_name

    resolved_namespace, bare_name, error = _normalize_namespace_and_name(
        name, namespace
    )
    if error:
        return None
    for root in _skill_roots():
        if resolved_namespace is not None and root.namespace != resolved_namespace:
            continue
        draft_dir = root.path / ".drafts" / bare_name
        skill_md = draft_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            draft_dir.resolve().relative_to(root.path.resolve())
        except (OSError, ValueError):
            continue
        if draft_dir.is_symlink() or root.path.is_symlink():
            continue
        return {
            "path": draft_dir,
            "root": root.path,
            "namespace": root.namespace,
            "owner_user_id": root.owner_user_id,
            "qualified_name": qualify_skill_name(root.namespace, bare_name),
            "draft": True,
        }
    return None


def _find_managed_skill(
    name: str, namespace: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Resolve an active skill first, then an inactive draft for mutation."""
    return _find_skill(name, namespace) or _find_skill_draft(name, namespace)


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a supporting-file path and ensure it stays within the skill directory."""
    from tools.path_security import validate_within_dir

    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


def _create_retry_frontmatter(name: str) -> str:
    """Return a valid, bounded frontmatter block a model can copy verbatim."""
    prefix = "Use for "
    suffix = " tasks."
    budget = SKILL_PROMPT_DESC_LIMIT - len(prefix) - len(suffix)
    label = str(name).replace("-", " ").replace("_", " ")[:budget].rstrip()
    description = f"{prefix}{label}{suffix}"
    return f"---\nname: {name}\ndescription: {description}\n---"


def _publish_skill(
    name: str,
    category: str = None,
    namespace: Optional[str] = None,
    requirements_confirmed: bool = False,
) -> Dict[str, Any]:
    """Atomically promote one complete, lint-valid draft into discovery."""
    if not requirements_confirmed:
        return {
            "success": False,
            "draft": True,
            "published": False,
            "error": (
                "requirements_confirmed=true is required to publish. Set it "
                "only after the user supplied the complete rules."
            ),
        }
    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}
    if _find_skill(name, namespace):
        return {"success": False, "error": f"Skill '{name}' is already active."}
    draft = _find_skill_draft(name, namespace)
    if not draft:
        return {
            "success": False,
            "error": f"No inactive draft named '{name}' was found.",
        }
    skill_md = draft["path"] / "SKILL.md"
    try:
        from tools.skill_linter import lint_skill
        findings = lint_skill(skill_md)
    except Exception as exc:
        return {
            "success": False,
            "draft": True,
            "published": False,
            "error": f"Could not validate draft before publish: {exc}",
        }
    blockers = [
        finding for finding in findings
        if finding.severity == "error" or finding.rule == "dangling-reference"
    ]
    if blockers:
        return {
            "success": False,
            "draft": True,
            "published": False,
            "error": "Draft failed publish validation.",
            "publish_blockers": [
                {"severity": f.severity, "rule": f.rule, "message": f.message}
                for f in blockers
            ],
        }
    scan_error = _security_scan_skill(draft["path"])
    if scan_error:
        return {
            "success": False,
            "draft": True,
            "published": False,
            "error": scan_error,
        }
    target, root, resolve_error = _resolve_skill_dir(name, category, namespace)
    if resolve_error or target is None or root is None:
        return {"success": False, "error": resolve_error or "Skill root unavailable."}
    target.parent.mkdir(parents=True, exist_ok=True)
    draft["path"].replace(target)
    return {
        "success": True,
        "message": f"Skill '{name}' published and active.",
        "path": str(target.relative_to(root.path)),
        "skill_md": _namespace_relative(target / "SKILL.md", root.path),
        "namespace": root.namespace,
        "qualified_name": draft["qualified_name"],
        "_skills_root": str(root.path),
        "draft": False,
        "published": True,
    }


def _safe_import_stem(display_name: str, ordinal: int) -> str:
    """Return a stable, collision-free support-file stem."""
    stem = Path(display_name).stem.strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "document"
    return f"{ordinal:03d}-{stem[:120]}"


def _split_import_text(text: str) -> list[str]:
    """Split large extracted text without base64 or model-authored summaries."""
    return [
        text[offset : offset + SKILL_IMPORT_CHUNK_CHARS]
        for offset in range(0, len(text), SKILL_IMPORT_CHUNK_CHARS)
    ]


def _import_source_files(
    name: str,
    source_paths: list[str],
    reference_subdir: str | None,
    namespace: Optional[str],
    task_id: str | None,
) -> Dict[str, Any]:
    """Atomically turn granted attachments into complete skill references.

    Extraction happens outside model context, reuses the content-addressed
    document cache, and writes plain UTF-8 Markdown only. Large documents are
    divided into bounded parts. One incomplete extraction aborts the complete
    batch before the active skill tree changes.
    """
    if not isinstance(source_paths, list) or not source_paths:
        return {"success": False, "error": "source_paths must contain at least one attached-file handle."}
    if len(source_paths) > MAX_SKILL_IMPORT_FILES:
        return {
            "success": False,
            "error": (
                f"source_paths contains {len(source_paths)} items; "
                f"the batch limit is {MAX_SKILL_IMPORT_FILES}."
            ),
        }
    if any(not isinstance(value, str) or not value.strip() for value in source_paths):
        return {"success": False, "error": "Every source_paths item must be a non-empty attached-file handle."}

    subdir = (reference_subdir or DEFAULT_SKILL_IMPORT_SUBDIR).strip().replace("\\", "/").rstrip("/")
    path_error = _validate_file_path(f"{subdir}/index.md")
    if path_error:
        return {"success": False, "error": path_error}

    existing = _find_managed_skill(name, namespace)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name, " Create it first with action='create'.")}
    if existing.get("namespace") != "user":
        return {
            "success": False,
            "error": "Import attachments into the caller's private skill first, then publish that complete tree to the platform namespace.",
        }
    skill_dir = Path(existing["path"])
    guard = _background_review_write_guard(name, skill_dir, "import_files")
    if guard:
        return guard

    from tools import document_extract_cache
    from tools.file_grants import file_grant_error, resolve_grant_alias
    from tools.read_extract import (
        MAX_DOCUMENT_BYTES,
        ExtractionError,
        extract_document_text,
        is_extractable_document,
    )

    task = task_id or "default"
    stage_root = Path(tempfile.mkdtemp(prefix=f".{name}-import-", dir=skill_dir.parent))
    staged_library = stage_root / "library"
    staged_library.mkdir(parents=True)
    receipts: list[dict[str, Any]] = []
    coverage_sources: list[tuple[str, str, str]] = []
    aggregate_chars = 0
    try:
        for ordinal, requested in enumerate(source_paths, start=1):
            resolved = Path(resolve_grant_alias(requested.strip(), task_id=task)).resolve(strict=True)
            grant_error = file_grant_error(str(resolved), task_id=task, operation="read")
            if grant_error:
                raise ValueError(grant_error)
            if not resolved.is_file():
                raise ValueError(f"Attached source is not a regular file: {requested}")
            if not is_extractable_document(str(resolved)):
                raise ValueError(f"Attached source is not a supported extractable document: {requested}")
            file_size = resolved.stat().st_size
            if file_size > MAX_DOCUMENT_BYTES:
                raise ValueError(
                    f"Attached source exceeds the document limit: {requested} "
                    f"({file_size:,} > {MAX_DOCUMENT_BYTES:,} bytes)"
                )
            with resolved.open("rb") as source_handle:
                source_sha256 = hashlib.file_digest(
                    source_handle, "sha256"
                ).hexdigest()
            suffix = resolved.suffix.lower()
            cached = document_extract_cache.lookup(source_sha256, suffix)
            gaps: list[str] = []
            if cached is not None:
                text = str(cached.get("text") or "")
                gaps = list(cached.get("gaps") or [])
                cache_status = "hit"
            else:
                try:
                    with document_extract_cache.extraction_lock(source_sha256, suffix):
                        cached = document_extract_cache.lookup(source_sha256, suffix)
                        if cached is not None:
                            text = str(cached.get("text") or "")
                            gaps = list(cached.get("gaps") or [])
                            cache_status = "hit_after_wait"
                        else:
                            text = extract_document_text(str(resolved), gaps_out=gaps)
                            document_extract_cache.remember(
                                source_sha256,
                                suffix,
                                text=text,
                                file_size=file_size,
                                gaps=gaps,
                            )
                            cache_status = "miss"
                except (ExtractionError, OSError, ValueError) as exc:
                    raise ValueError(f"Cannot extract {requested}: {exc}") from exc
            if not text.strip():
                raise ValueError(f"Extraction returned no text for {requested}")
            if gaps:
                raise ValueError(
                    f"Extraction for {requested} is incomplete: {', '.join(gaps)}"
                )
            aggregate_chars += len(text)
            if aggregate_chars > MAX_SKILL_IMPORT_CHARS:
                raise ValueError(
                    f"Extracted batch exceeds {MAX_SKILL_IMPORT_CHARS:,} characters."
                )
            display_name = re.sub(r"^\d{3}-[0-9a-f]{8}-", "", resolved.name)
            stem = _safe_import_stem(display_name, ordinal)
            parts = _split_import_text(text)
            part_paths: list[str] = []
            for part_index, part in enumerate(parts, start=1):
                suffix_label = "" if len(parts) == 1 else f".part-{part_index:03d}"
                relative = f"{stem}{suffix_label}.md"
                atomic_write_text(staged_library / relative, part, preserve_mode=True)
                part_paths.append(f"{subdir}/{relative}")
            receipts.append(
                {
                    "request_handle": requested,
                    "name": display_name,
                    "source_sha256": source_sha256,
                    "source_bytes": file_size,
                    "text_chars": len(text),
                    "parts": part_paths,
                    "cache": cache_status,
                    "gaps": [],
                }
            )
            coverage_sources.append((str(resolved), requested, display_name))

        index_lines = [
            "# Imported source library",
            "",
            "Every entry below was extracted from the exact attached bytes. ",
            "A document appears in multiple bounded parts only when necessary; read all listed parts before claiming complete coverage.",
            "",
        ]
        for receipt in receipts:
            index_lines.append(
                f"- `{receipt['name']}` — SHA-256 `{receipt['source_sha256']}` — "
                + ", ".join(f"[{Path(path).name}]({Path(path).name})" for path in receipt["parts"])
            )
        atomic_write_text(staged_library / "index.md", "\n".join(index_lines) + "\n", preserve_mode=True)
        atomic_write_text(
            staged_library / "manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "documents": receipts,
                    "document_count": len(receipts),
                    "text_chars": aggregate_chars,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            preserve_mode=True,
        )

        target = skill_dir / subdir
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = skill_dir.parent / f".{name}-import-backup"
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged_library, target)
            scan_error = _security_scan_skill(skill_dir)
            if scan_error:
                raise ValueError(scan_error)
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        # A successful atomic import is a complete reader outcome for each
        # attached source: exact bytes were extracted without gaps and the
        # resulting text is now durably present in the skill tree.  Record it
        # only after the whole batch commits so a failed import cannot claim
        # coverage.  This also prevents the terminal coverage footer from
        # contradicting a successful import by labelling every source pending.
        try:
            from tools.attachment_ledger import OUTCOME_READ, record_outcome

            for source_path, requested, display_name in coverage_sources:
                record_outcome(
                    source_path,
                    task_id=task,
                    status=OUTCOME_READ,
                    reason="complete extraction imported into skill",
                    display_name=display_name,
                    handle=requested if requested.upper().startswith("F") else "",
                    reader="skill_manage.import_files",
                )
        except Exception:
            logger.debug("skill import attachment ledger update skipped", exc_info=True)
        return {
            "success": True,
            "message": (
                f"Imported {len(receipts)} complete attachment(s) into skill "
                f"'{name}' as {sum(len(row['parts']) for row in receipts)} bounded Markdown part(s)."
            ),
            "namespace": existing["namespace"],
            "qualified_name": existing["qualified_name"],
            "reference_subdir": subdir,
            "documents": len(receipts),
            "parts": sum(len(row["parts"]) for row in receipts),
            "text_chars": aggregate_chars,
            "manifest_sha256": hashlib.sha256(
                (target / "manifest.json").read_bytes()
            ).hexdigest(),
            "receipts": receipts,
            "_skills_root": str(existing["root"]),
        }
    except (OSError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


_ACL_MANAGE_DENY = (
    "Hermes skills ACL: management permission could not be verified for the "
    "current OpenWebUI role/group scope; denied."
)


def _acl_manage_block(action: str, target_namespace: Optional[str]) -> Optional[str]:
    """Fail closed for OpenWebUI writes; shared writes are rechecked by broker."""
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        platform = ""
    if platform != "api_server":
        return None
    if target_namespace == "user":
        try:
            from agent.skill_namespaces import current_skill_namespace_user_id

            return None if current_skill_namespace_user_id() else _ACL_MANAGE_DENY
        except Exception:
            return _ACL_MANAGE_DENY
    try:
        from tools.skill_acl import load_skill_acl_config, require_skill_permission

        config = load_skill_acl_config()
        if not config.get("enabled"):
            return _ACL_MANAGE_DENY if target_namespace == "platform" else None
        if target_namespace == "platform":
            return None
        allowed, reason = require_skill_permission(action)
        return None if allowed else reason
    except Exception:
        return _ACL_MANAGE_DENY


def _resolve_skill_dir(
    name: str, category: str = None, namespace: Optional[str] = None
) -> Tuple[Optional[Path], Optional[Any], Optional[str]]:
    resolved_namespace, bare_name, error = _normalize_namespace_and_name(name, namespace)
    if error:
        return None, None, error
    target_namespace = resolved_namespace or _default_create_namespace()
    root = _root_for_namespace(target_namespace)
    if root is None:
        if target_namespace == "user":
            return None, None, "A valid OpenWebUI user identity is required for the user skill namespace."
        return None, None, f"Skill namespace '{target_namespace}' is not available."
    if target_namespace == "platform" and Path(SKILLS_DIR) == _SKILLS_DIR_AT_IMPORT:
        from gateway.session_context import get_session_env
        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            from agent.skill_utils import get_skill_create_dir
            from agent.skill_namespaces import SkillRoot
            if create_dir := get_skill_create_dir():
                root = SkillRoot(root.namespace, Path(create_dir), root.owner_user_id)
    relative = Path(category) / bare_name if category else Path(bare_name)
    return root.path / relative, root, None


def _iter_skill_dirs(root: Path):
    from agent.skill_utils import is_excluded_skill_path
    for skill_md in root.rglob("SKILL.md"):
        if not is_excluded_skill_path(skill_md):
            yield skill_md.parent


def _find_skill(name: str, namespace: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Find a skill by name across all skill directories.

    Searches the local skills dir (~/.hermes/skills/) first, then any
    external dirs configured via skills.external_dirs.  Returns
    {"path": Path} or None.
    """
    from agent.skill_namespaces import qualify_skill_name
    from agent.skill_utils import is_excluded_skill_path, parse_frontmatter

    resolved_namespace, bare_name, error = _normalize_namespace_and_name(name, namespace)
    if error:
        return None
    relative_name = Path(bare_name)
    if relative_name.is_absolute() or ".." in relative_name.parts:
        return None

    def _found(root, skill_dir: Path, declared_name: Optional[str] = None):
        if root.namespace == "user":
            try:
                if root.path.is_symlink() or root.path.parent.is_symlink():
                    return None
                skill_dir.resolve().relative_to(root.path.resolve())
                cursor = root.path
                for part in skill_dir.relative_to(root.path).parts:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        return None
            except (OSError, ValueError):
                return None
        canonical_name = declared_name or skill_dir.name
        return {
            "path": skill_dir,
            "root": root.path,
            "namespace": root.namespace,
            "owner_user_id": root.owner_user_id,
            "qualified_name": qualify_skill_name(root.namespace, canonical_name),
        }

    for root in _skill_roots():
        if resolved_namespace is not None and root.namespace != resolved_namespace:
            continue
        skills_dir = root.path
        if not skills_dir.exists():
            continue
        direct = skills_dir / relative_name
        if direct.is_dir() and (direct / "SKILL.md").is_file():
            declared_name = None
            try:
                frontmatter, _ = parse_frontmatter(
                    (direct / "SKILL.md").read_text(encoding="utf-8")[:4000]
                )
                declared_name = str(frontmatter.get("name") or "") or None
            except (OSError, UnicodeDecodeError):
                pass
            found = _found(root, direct, declared_name)
            if found:
                return found
        for skill_md in skills_dir.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_md):
                continue
            declared_name = None
            if skill_md.parent.name != bare_name:
                try:
                    frontmatter, _ = parse_frontmatter(
                        skill_md.read_text(encoding="utf-8")[:4000]
                    )
                    declared_name = str(frontmatter.get("name") or "") or None
                except (OSError, UnicodeDecodeError):
                    pass
                if declared_name != bare_name:
                    continue
            found = _found(root, skill_md.parent, declared_name)
            if found:
                return found
    return None


def _find_skill_in_other_profiles(name: str) -> List[Tuple[str, Path]]:
    """``(profile, skill_dir)`` pairs for OTHER profiles holding ``name`` (so the not-found
    error can explain a wrong-profile mistake). Fail-quiet."""
    matches: List[Tuple[str, Path]] = []
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except Exception:
        return matches
    _active = _skills_dir()
    active_dir = _active.resolve() if _active.exists() else _active
    # Every profile's skills dir EXCEPT the active one (already searched). A candidate whose
    # path cannot be resolved is skipped (not a fatal error); is_dir() checks stay unguarded.
    candidates: List[Tuple[str, Path]] = []
    with suppress(OSError, RuntimeError):
        if (root / "skills").resolve() != active_dir:
            candidates.append(("default", root / "skills"))
    if (root / "profiles").is_dir():
        with suppress(OSError):
            for entry in (root / "profiles").iterdir():
                if not entry.is_dir():
                    continue
                try:
                    if (entry / "skills").resolve() == active_dir:
                        continue
                except (OSError, RuntimeError):
                    continue
                candidates.append((entry.name, entry / "skills"))
    for profile_name, skills_dir in candidates:
        if not skills_dir.is_dir():
            continue
        with suppress(OSError):
            hit = next((d for d in _iter_skill_dirs(skills_dir) if d.name == name), None)
            if hit is not None:
                matches.append((profile_name, hit))  # one match per profile is enough
    return matches


def _skill_not_found_error(name: str, suffix: str = "") -> str:
    """Not-found error naming other profiles that hold the skill, plus ``suffix``."""
    from agent.file_safety import _resolve_active_profile_name
    base = f"Skill '{name}' not found in active profile '{_resolve_active_profile_name()}'."
    others = _find_skill_in_other_profiles(name)
    if len(others) == 1:
        other_profile, other_path = others[0]
        base += (
            f" A skill by that name exists in profile '{other_profile}' ({other_path}). To edit "
            f"it, switch profiles (`hermes -p {other_profile}`) or edit the file directly "
            f"(file tools / terminal).")
    elif others:
        names = ", ".join(f"'{p}'" for p, _ in others)
        base += (
            f" Skills by that name exist in other profiles: {names}. Switch profiles (`hermes -p "
            f"<name>`) to edit there, or edit the files directly (file tools / terminal).")
    else:
        base += " Use skills_list() to see available skills."
    return base + suffix


def _validate_file_path(file_path: str) -> Optional[str]:
    """Validate a write_file/remove_file path: under an allowed subdir, no escape."""
    from tools.path_security import has_traversal_component
    if not file_path:
        return "file_path is required."
    parts = Path(file_path).parts
    # Traversal first, so the SKILL.md exception is unreachable by a traversal-laden path.
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."
    # SKILL.md lives at the skill root; accept 'SKILL.md' and '<skill>/SKILL.md'.
    if parts and parts[-1] == "SKILL.md" and len(parts) in (1, 2):
        return None
    if not parts or parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"
    if len(parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{parts[0]}/myfile.md'"
    return None


def _resolve_supporting_file(skill_dir: Path, file_path: str):
    """Validate ``file_path`` and resolve it inside ``skill_dir``
    -> ``(target, None)`` | ``(None, error_dict)``."""
    from tools.path_security import validate_within_dir
    target = skill_dir / (file_path or "")
    err = _validate_file_path(file_path) or validate_within_dir(target, skill_dir)
    return (None, _err(err)) if err else (target, None)


def _locate_for_write(name: str, action: str, not_found_suffix: str = "", *,
                      org_guard: bool = True):
    """Find the skill; run the org-mirror (unless ``org_guard=False``) and background-review
    write guards -> ``(skill_dir, None)`` | ``(None, error_dict)``."""
    existing = _find_skill(name)
    if not existing:
        return None, _err(_skill_not_found_error(name, not_found_suffix))
    skill_dir = existing["path"]
    guard = ((org_guard and _org_mirror_write_guard(name, skill_dir, action))
             or _background_review_write_guard(name, skill_dir, action))
    return (None, guard) if guard else (skill_dir, None)


def _guarded_write(name: str, skill_dir: Path, target: Path, action: str, label: str,
                   content: str) -> Optional[Dict[str, Any]]:
    """Read-before-write guard (existing targets only), atomic write, then the security scan;
    a blocked scan restores the original (or unlinks a new file). Error dict or None."""
    original = None
    if target.exists():
        if read_guard := _background_review_read_before_write_guard(name, target, action, label):
            return read_guard
        original = target.read_text(encoding="utf-8")
    from hermes_constants import mkdir_under_hermes_home
    mkdir_under_hermes_home(target.parent)
    atomic_write_text(target, content, preserve_mode=True, create_mode=0o644)
    scan_error = _security_scan_skill(skill_dir)
    if not scan_error:
        return None
    if original is not None:
        atomic_write_text(target, original, preserve_mode=True)
    else:
        target.unlink(missing_ok=True)
    return _err(scan_error)


def _attach_org_note(result: Dict[str, Any], name: str, skill_dir: Path) -> Dict[str, Any]:
    if org_note := _maybe_auto_propose_org_edit(name, skill_dir):
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    return result


def _add_description_prompt_preview(result: Dict[str, Any], content: str) -> Dict[str, Any]:
    fm, _ = _parse_frontmatter(content)
    if is_skill_description_truncated_for_prompt(fm):
        result["system_prompt_preview"] = (
            f"System prompt will show: \"{extract_skill_description(fm)}\" — keep the trigger "
            f"self-contained in the first {SKILL_PROMPT_DESC_LIMIT - 3} chars.")
    return result


def _attach_lint_findings(result: Dict[str, Any], skill_md: Path, before: Optional[str] = None) -> None:
    """Attach ADVISORY authoring findings (hard rejects already ran in _validate_frontmatter).
    With ``before`` (the pre-write content) only rules the write INTRODUCED are attached, so a
    patch reports the line it crossed rather than re-listing the skill's standing findings."""
    try:
        from tools.skill_linter import lint_content, lint_skill  # local import: optional path
        findings = lint_skill(skill_md)
        if before is not None:
            standing = {f.rule for f in lint_content(before, skill_dir=skill_md.parent)}
            findings = [f for f in findings if f.rule not in standing]
    except Exception:
        findings = None
    if not findings:
        return
    result["lint_warnings"] = [
        {"severity": f.severity, "rule": f.rule, "message": f.message} for f in findings]
    result["lint_hint"] = (
        "The write succeeded. These are advisory authoring-convention findings (not blockers) "
        "— fix them with skill_manage(action='patch') to match Hermes skill standards.")


def _clip(text: str, n: int, ellipsis: str) -> str:
    return text[:n] + (ellipsis if len(text) > n else "")


# --- Core actions -------------------------------------------------------------

def _create_skill(
    name: str,
    content: str,
    category: str = None,
    namespace: Optional[str] = None,
    requirements_confirmed: bool = True,
    enforce_publish_lint: bool = False,
) -> Dict[str, Any]:
    """Draft a new skill and publish only after requirements/lint gates."""
    resolved_namespace, bare_name, namespace_error = _normalize_namespace_and_name(
        name, namespace
    )
    if namespace_error:
        return {"success": False, "error": namespace_error}
    # Validate name
    err = _validate_name(bare_name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    # Validate content
    err = _validate_frontmatter(content, new_skill=True)
    if err:
        return {
            "success": False,
            "error": err,
            "retry_frontmatter": _create_retry_frontmatter(bare_name),
            "retry_instruction": (
                "Copy retry_frontmatter verbatim at the top, preserve the detailed "
                "instructions in the body below it, and retry create once."
            ),
        }

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # Check for name collisions across all directories
    existing = _find_skill(bare_name)
    if existing:
        return {
            "success": False,
            "error": (
                f"A skill named '{bare_name}' already exists in the "
                f"'{existing['namespace']}' namespace. User skills cannot shadow "
                "a caller-visible platform or external skill."
            ),
        }

    # Create the skill directory
    skill_dir, root, resolve_error = _resolve_skill_dir(
        bare_name, category, resolved_namespace
    )
    if resolve_error or skill_dir is None or root is None:
        return {"success": False, "error": resolve_error or "Skill root unavailable."}
    if root.path.is_symlink() or root.path.parent.is_symlink():
        return {"success": False, "error": "Refusing to use a symlinked skill namespace root."}
    if root.namespace == "user":
        root_was_missing = not root.path.exists()
        root.path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if root_was_missing:
            root.path.chmod(0o700)
    # Drafts are deliberately outside discovery/system-prompt roots. This
    # keeps incomplete model-authored procedure from becoming active while
    # preserving the proposed bytes for later patch/write_file/publish.
    draft_dir = root.path / ".drafts" / bare_name
    draft_dir.mkdir(parents=True, exist_ok=True)
    skill_md = draft_dir / "SKILL.md"
    atomic_write_text(skill_md, content, preserve_mode=True)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(draft_dir)
    if scan_error:
        return {"success": False, "error": scan_error}

    try:
        from tools.skill_linter import lint_skill
        lint_findings = lint_skill(skill_md)
    except Exception:
        lint_findings = []
    blocking_lint = [
        finding for finding in lint_findings
        if finding.severity == "error" or finding.rule == "dangling-reference"
    ]

    # Extract description from frontmatter for verbose notifications
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{bare_name}' draft saved; it is not active.",
        "path": str(draft_dir.relative_to(root.path)),
        "skill_md": _namespace_relative(skill_md, root.path),
        "namespace": root.namespace,
        "_skills_root": str(root.path),
        "_change": {"description": _desc},
        "draft": True,
        "published": False,
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "Use patch/write_file to complete this inactive draft, then call "
        "skill_manage(action='publish', name='{}', requirements_confirmed=true) "
        "only after the user supplied the complete rules.".format(bare_name)
    )
    from agent.skill_namespaces import qualify_skill_name
    result["qualified_name"] = qualify_skill_name(root.namespace, bare_name)
    _add_description_prompt_preview(result, content)
    _attach_lint_findings(result, skill_md)
    if not requirements_confirmed:
        result["publish_blockers"] = [
            "requirements_confirmed is false; the user's complete rules are not confirmed"
        ]
        return result
    if enforce_publish_lint and blocking_lint:
        result["publish_blockers"] = [
            {"severity": f.severity, "rule": f.rule, "message": f.message}
            for f in blocking_lint
        ]
        return result

    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    draft_dir.replace(skill_dir)
    result.update({
        "message": f"Skill '{bare_name}' published and active.",
        "path": str(skill_dir.relative_to(root.path)),
        "skill_md": _namespace_relative(skill_dir / "SKILL.md", root.path),
        "draft": False,
        "published": True,
    })
    return result


def _edit_skill(
    name: str, content: str, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_managed_skill(name, namespace)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}
    org_guard = _org_mirror_write_guard(name, existing["path"], "edit")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "edit")
    if guard:
        return guard

    skill_md = existing["path"] / "SKILL.md"
    read_guard = _background_review_read_before_write_guard(
        name, skill_md, "edit", "SKILL.md"
    )
    if read_guard:
        return read_guard

    # Back up original content for rollback
    original_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
    atomic_write_text(skill_md, content, preserve_mode=True)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            atomic_write_text(skill_md, original_content, preserve_mode=True)
        return {"success": False, "error": scan_error}

    # Extract description from new content for verbose notifications
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{name}' updated (full rewrite).",
        "path": str(existing["path"]),
        "namespace": existing["namespace"],
        "qualified_name": existing["qualified_name"],
        "_skills_root": str(existing["root"]),
        "_change": {"description": _desc},
    }
    if existing.get("draft"):
        result.update({"draft": True, "published": False})
        result["message"] = f"Inactive draft '{name}' updated (full rewrite)."
    org_note = _maybe_auto_propose_org_edit(name, existing["path"])
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    _add_description_prompt_preview(result, content)
    return result


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md. Use file_path to patch a supporting file instead.
    Requires a unique match unless replace_all is True.
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    existing = _find_managed_skill(name, namespace)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]
    org_guard = _org_mirror_write_guard(name, skill_dir, "patch")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, skill_dir, "patch")
    if guard:
        return guard

    if file_path:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
        assert target is not None
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    read_guard = _background_review_read_before_write_guard(
        name,
        target,
        "patch",
        "SKILL.md" if not file_path else file_path,
    )
    if read_guard:
        return read_guard

    content = target.read_text(encoding="utf-8")

    # Use the same fuzzy matching engine as the file patch tool.
    # This handles whitespace normalization, indentation differences,
    # escape sequences, and block-anchor matching — saving the agent
    # from exact-match failures on minor formatting mismatches.
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        # Show a short preview of the file so the model can self-correct
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            from tools.fuzzy_match import format_no_match_hint
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }

    # Check size limit on the result
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # If patching SKILL.md, validate frontmatter is still intact
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    original_content = content  # for rollback
    atomic_write_text(target, new_content, preserve_mode=True)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        atomic_write_text(target, original_content, preserve_mode=True)
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
        "namespace": existing["namespace"],
        "qualified_name": existing["qualified_name"],
        "_skills_root": str(existing["root"]),
    }
    if existing.get("draft"):
        result.update({"draft": True, "published": False})
        result["message"] = (
            f"Patched inactive draft '{name}' ({match_count} "
            f"replacement{'s' if match_count > 1 else ''})."
        )
    # Include change previews for verbose notifications
    result["_change"] = {
        "old": old_string[:200] + ("…" if len(old_string) > 200 else ""),
        "new": new_string[:200] + ("…" if len(new_string) > 200 else ""),
    }
    org_note = _maybe_auto_propose_org_edit(name, skill_dir)
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    if not file_path:
        _attach_lint_findings(result, target, before=content)
    return result


def _delete_skill(
    name: str,
    absorbed_into: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning because the curator
        classification pipeline can't tell consolidation from pruning without it.
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    existing = _find_managed_skill(name, namespace)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}
    org_guard = _org_mirror_write_guard(name, existing["path"], "delete")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "delete")
    if guard:
        return guard

    # Fail closed on unverified deletes during the curator consolidation pass.
    # A bare prune (no absorbed_into) from the LLM umbrella pass is the
    # fail-open behavior reported in #29912 — refuse it; keep the skill active.
    fail_closed = _curator_consolidation_delete_guard(name, absorbed_into)
    if fail_closed:
        return fail_closed

    pinned_err = _pinned_guard(name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    # Validate absorbed_into target when declared non-empty
    absorbed_target = (
        absorbed_into.strip()
        if absorbed_into is not None and isinstance(absorbed_into, str)
        else ""
    )
    is_consolidation = bool(absorbed_target)
    if is_consolidation:
        target_name = absorbed_target
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skill_dir = existing["path"]
    skills_root = _containing_skills_root(skill_dir)

    # Defense-in-depth before the recursive delete (port of Kilo Code #11240).
    unsafe = _validate_delete_target(skill_dir)
    if unsafe:
        return {"success": False, "error": unsafe}

    # During the curator consolidation pass, a verified consolidation must be
    # RECOVERABLE: archival into ~/.hermes/skills/.archive/ is documented as
    # the maximum destructive action the curator may take, and
    # `hermes curator restore` promises the skill can be brought back. Route
    # through the recoverable archive primitive instead of permanent rmtree so
    # a misjudged consolidation can be undone (#29912). Foreground,
    # user-directed deletes keep their existing hard-delete semantics.
    try:
        from tools.skill_provenance import is_background_review
        curator_pass = is_background_review()
    except Exception:
        curator_pass = False

    if curator_pass:
        try:
            from tools.skill_usage import archive_skill
            ok, archive_msg = archive_skill(name)
        except Exception as e:
            return {"success": False, "error": f"failed to archive '{name}': {e}"}
        if not ok:
            return {"success": False, "error": archive_msg}
        message = f"Skill '{name}' archived ({archive_msg})."
        if is_consolidation:
            message += f" Content absorbed into '{absorbed_target}'."
        return {"success": True, "message": message, "_archived": True}

    shutil.rmtree(skill_dir)

    # Clean up empty category directories (don't remove the skills root itself)
    parent = skill_dir.parent
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    message = f"Skill '{name}' deleted."
    if is_consolidation:
        message += f" Content absorbed into '{absorbed_target}'."

    return {
        "success": True,
        "message": message,
        "namespace": existing["namespace"],
        "qualified_name": existing["qualified_name"],
        "_skills_root": str(existing["root"]),
    }


def _rmdir_if_empty(parent: Path, stop: Path) -> None:
    if parent != stop and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


def _write_file(
    name: str,
    file_path: str,
    file_content: str,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Add or overwrite a supporting file within any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Check size limits
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_managed_skill(name, namespace)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name, " Create it first with action='create'.")}
    org_guard = _org_mirror_write_guard(name, existing["path"], "write_file")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "write_file")
    if guard:
        return guard

    target, err = _resolve_skill_target(existing["path"], file_path)
    if err:
        return {"success": False, "error": err}
    assert target is not None
    if target.exists():
        read_guard = _background_review_read_before_write_guard(
            name, target, "write_file", file_path
        )
        if read_guard:
            return read_guard
    target.parent.mkdir(parents=True, exist_ok=True)
    # Back up for rollback
    original_content = target.read_text(encoding="utf-8") if target.exists() else None
    atomic_write_text(target, file_content, preserve_mode=True)

    # Security scan — roll back on block
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            atomic_write_text(target, original_content, preserve_mode=True)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
        "namespace": existing["namespace"],
        "qualified_name": existing["qualified_name"],
        "_skills_root": str(existing["root"]),
    }
    if existing.get("draft"):
        result.update({"draft": True, "published": False})
        result["message"] = f"File '{file_path}' written to inactive draft '{name}'."
    org_note = _maybe_auto_propose_org_edit(name, existing["path"])
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    if file_path.startswith("references/") and (existing["path"] / "SKILL.md").exists():
        _attach_lint_findings(result, existing["path"] / "SKILL.md")
    return result


def _remove_file(
    name: str, file_path: str, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """Remove a supporting file from any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_managed_skill(name, namespace)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]
    guard = _background_review_write_guard(name, skill_dir, "remove_file")
    if guard:
        return guard

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    assert target is not None
    if not target.exists():
        # List what's actually there for the model to see
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    read_guard = _background_review_read_before_write_guard(
        name, target, "remove_file", file_path
    )
    if read_guard:
        return read_guard

    target.unlink()

    # Clean up empty subdirectories
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
        "namespace": existing["namespace"],
        "qualified_name": existing["qualified_name"],
        "_skills_root": str(existing["root"]),
    }


# --- Main entry point ---------------------------------------------------------

# Set while replaying an approved staged skill write so skill_manage() does not re-gate it.
_skill_gate_bypass: "_ctxvars.ContextVar[bool]" = _ctxvars.ContextVar(
    "skill_gate_bypass", default=False)


def _run_write_gate(build_staging):
    """Shared write gate: None to proceed, else a JSON tool result (blocked/staged).
    ``build_staging(wa) -> (payload, gist)`` runs only when staging. Fails open if
    write_approval cannot be imported."""
    try:
        from tools import write_approval as wa
    except Exception:
        return None  # fail open
    decision = wa.evaluate_gate(wa.SKILLS)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)
    payload, gist = build_staging(wa)
    record = wa.stage_write(wa.SKILLS, payload, summary=gist, origin=wa.current_origin())
    return json.dumps({"success": True, "staged": True, "pending_id": record["id"],
                       "gist": gist, "message": decision.message}, ensure_ascii=False)


def _apply_skill_write_gate(action, name, **payload_kwargs):
    """Evaluate the skill write gate. Returns a JSON tool-result string when the
    write should NOT proceed (blocked or staged), or None to perform the real
    write. Bypassed during approved-pending replay.
    """
    if action not in {"create", "publish", "edit", "patch", "delete", "write_file", "remove_file", "import_files"}:
        return None
    if _skill_gate_bypass.get():
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        return None  # fail open

    decision = wa.evaluate_gate(wa.SKILLS)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage — record the full skill_manage kwargs so approval can replay it.
    payload = {"action": action, "name": name}
    payload.update({k: v for k, v in payload_kwargs.items() if v is not None})
    if payload.get("namespace") == "user":
        try:
            from agent.skill_namespaces import current_skill_namespace_user_id

            subject_user_id = current_skill_namespace_user_id()
        except Exception:
            subject_user_id = None
        if not subject_user_id:
            return tool_error(
                "A valid original subject is required for a staged user-skill write.",
                success=False,
            )
        payload["subject_user_id"] = subject_user_id
    gist = wa.skill_gist(
        action, name,
        content=payload_kwargs.get("content") or "",
        file_path=payload_kwargs.get("file_path") or "",
        old_string=payload_kwargs.get("old_string") or "",
        new_string=payload_kwargs.get("new_string") or "",
    )
    record = wa.stage_write(wa.SKILLS, payload, summary=gist, origin=wa.current_origin())
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "gist": gist, "message": decision.message},
        ensure_ascii=False,
    )


_FLAT_OP_KEYS = ('content', 'category', 'file_path', 'file_content', 'old_string', 'new_string', 'absorbed_into', 'operations', 'namespace', 'requirements_confirmed', 'source_paths', 'reference_subdir')


def _skill_manage_from(payload: Dict[str, Any], **extra) -> str:
    """Call ``skill_manage`` with the flat-shape fields (and absorbed_into/operations) of ``payload``."""
    return skill_manage(
        action=payload.get("action", ""), name=payload.get("name", ""),
        replace_all=payload.get("replace_all", False),
        **{k: payload.get(k) for k in _FLAT_OP_KEYS}, **extra)


def apply_skill_pending(payload: Dict[str, Any]) -> str:
    """Replay a staged skill write, bypassing the gate. Returns the tool result
    JSON string. Called by the /skills approve handler.
    """
    if payload.get("operations") is not None:
        contains_user_op = any(isinstance(op, dict) and (op.get("namespace") == "user" or str(op.get("name") or "").startswith("user:")) for op in payload.get("operations", []))
        if payload.get("subject_user_id") or payload.get("namespace") == "user" or contains_user_op:
            return tool_error("Staged user skill batches require an explicit original-subject transaction; replay refused.", success=False)
        token = _skill_gate_bypass.set(True)
        try:
            return _skill_manage_from(payload)
        finally:
            _skill_gate_bypass.reset(token)
    namespace = str(payload.get("namespace") or "") or None
    subject_user_id = str(payload.get("subject_user_id") or "")
    if namespace == "user" and not subject_user_id:
        return tool_error(
            "Approved user-skill write is missing its original subject; denied.",
            success=False,
        )
    from agent.skill_namespaces import bind_skill_namespace_user

    replay_scope = (
        bind_skill_namespace_user(subject_user_id)
        if namespace == "user"
        else nullcontext()
    )
    token = _skill_gate_bypass.set(True)
    try:
        with replay_scope:
            return skill_manage(
                action=payload.get("action", ""),
                name=payload.get("name", ""),
                namespace=namespace,
                content=payload.get("content"),
                category=payload.get("category"),
                file_path=payload.get("file_path"),
                file_content=payload.get("file_content"),
                old_string=payload.get("old_string"),
                new_string=payload.get("new_string"),
                replace_all=payload.get("replace_all", False),
                absorbed_into=payload.get("absorbed_into"),
                requirements_confirmed=payload.get("requirements_confirmed", False),
                source_paths=payload.get("source_paths"),
                reference_subdir=payload.get("reference_subdir"),
            )
    finally:
        _skill_gate_bypass.reset(token)


# Sync push debounce: a burst of skill_manage writes collapses into one push on a daemon timer.
# One timer per profile home: in a multiplexed process B's write must not cancel A's pending push.
_sync_push_timers: Dict[str, threading.Timer] = {}
_sync_push_lock = threading.Lock()
_SYNC_PUSH_DEBOUNCE_S = 5.0


def _maybe_debounced_sync_push(skill_name: str) -> None:
    """Debounced best-effort sync push after a skill write; never blocks the caller. Skills not
    opted into sync do nothing (no auth/network); ``maybe_push_skills`` enforces the access gate."""
    try:
        from tools.skill_usage import is_sync_enabled
        if not is_sync_enabled(skill_name):
            return
    except Exception:
        return
    from hermes_constants import hermes_home_key
    from tools.skill_usage import current_skills_dir
    home_key = f"{hermes_home_key()}::{current_skills_dir()}"
    # Timer threads start with empty ContextVars; without the scheduling turn's context the push would
    # resolve the launch profile's home and credentials instead of the writing profile's.
    ctx = _ctxvars.copy_context()
    def _fire():
        with suppress(Exception):
            from tools.skills_sync_client import maybe_push_skills
            maybe_push_skills(message=f"sync: {skill_name}")
    with _sync_push_lock:
        pending = _sync_push_timers.get(home_key)
        if pending is not None:
            pending.cancel()  # only sets an Event; never raises
        timer = threading.Timer(_SYNC_PUSH_DEBOUNCE_S, ctx.run, args=(_fire,))
        timer.daemon = True
        _sync_push_timers[home_key] = timer
        timer.start()


def _act_patch(a):
    """Two shapes: old_string/new_string = targeted replacement (validated in _patch_skill so the
    tool and the helper give the same guidance); content alone = full rewrite (the old 'edit')."""
    if a["content"] and (a["old_string"] or a["new_string"] is not None):
        return tool_error(_PATCH_EITHER_OR, success=False)
    if a["content"]:
        return _edit_skill(a["name"], a["content"])
    return _patch_skill(a["name"], a["old_string"], a["new_string"], a["file_path"], a["replace_all"])


# action -> handler(args dict) returning a result dict, or a tool_error JSON string for
# argument-shape errors. "edit" is a legacy alias for a full rewrite (not in the schema).
_ACTION_HANDLERS = {
    "create": lambda a: _create_skill(a["name"], a["content"], a["category"]),
    "edit": lambda a: _edit_skill(a["name"], a["content"]),
    "patch": _act_patch,
    "delete": lambda a: _delete_skill(a["name"], absorbed_into=a["absorbed_into"]),
    "write_file": lambda a: _write_file(a["name"], a["file_path"], a["file_content"]),
    "remove_file": lambda a: _remove_file(a["name"], a["file_path"])}


def _record_success(action, name, result, *, file_path, absorbed_into, task_id,
                    session_id, ledger_before) -> None:
    """Best-effort post-mutation side effects (never break the tool): ledger, prompt-cache
    clear, curator telemetry, debounced sync push."""
    with suppress(Exception):
        from tools import skill_ledger as _ledger
        _post = _find_skill(name)
        # delete: consolidation vs prune, and whether the recoverable archive handled it
        _evidence = ({"absorbed_into": absorbed_into, "archived": bool(result.get("_archived"))}
                     if action == "delete" else {})
        _evidence.update({k: v for k, v in (("session_id", session_id), ("file_path", file_path)) if v})
        _ledger.record_mutation(
            action, name, before=ledger_before if ledger_before is not None else [],
            after_root=_post["path"] if _post else None, evidence=_evidence)
    with suppress(Exception):
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    # Curator telemetry: only the background review fork marks a skill agent-created
    # (foreground creates belong to the user). A recoverable curator archive keeps its
    # record as STATE_ARCHIVED (`hermes curator status`/`restore`); only a hard delete forgets.
    with suppress(Exception):
        from tools.skill_usage import bump_patch, forget, record_created
        # During the curator consolidation pass, a verified consolidation must be RECOVERABLE: archival into
        # ~/.hermes/skills/.archive/ is documented as the maximum destructive action the curator may take,
        # and `hermes curator restore` promises the skill can be brought back. Route through the recoverable
        # archive primitive instead of permanent rmtree so a misjudged consolidation can be undone (#29912).
        # Foreground, user-directed deletes keep their existing hard-delete semantics.
        from tools.skill_provenance import is_background_review
        if action == "create":
            record_created(name, agent_created=is_background_review(),
                           task_id=task_id, session_id=session_id)
        elif action in {"patch", "edit", "write_file", "remove_file"}:
            bump_patch(name, action=action, task_id=task_id, session_id=session_id)
        elif action == "delete" and not result.get("_archived"):
            forget(name)
    # Only AFTER the write gate passed (staged writes returned early): never push un-reviewed content.
    with suppress(Exception):
        _maybe_debounced_sync_push(name)


def _skill_manage_single(
    action: str,
    name: str,
    namespace: str = None,
    content: str = None,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    absorbed_into: str = None,
    requirements_confirmed: Optional[bool] = None,
    task_id: str = None,
    session_id: str = None,
    source_paths: list[str] = None,
    reference_subdir: str = None,
) -> str:
    """
    Manage user-created skills. Dispatches to the appropriate action handler.

    Returns JSON string with results.
    """
    resolved_namespace, bare_name, namespace_error = _normalize_namespace_and_name(
        name, namespace
    )
    if namespace_error:
        return tool_error(namespace_error, success=False)
    try:
        from gateway.session_context import get_session_env

        api_server_session = get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server"
    except Exception:
        api_server_session = False
    requirements_are_confirmed = (
        bool(requirements_confirmed)
        if requirements_confirmed is not None
        else not api_server_session
    )
    if resolved_namespace is None and api_server_session:
        resolved_namespace = "user"
    target = (
        None
        if action == "create"
        else _find_managed_skill(bare_name, resolved_namespace)
    )
    target_namespace = (
        resolved_namespace
        or (target.get("namespace") if target else None)
        or _default_create_namespace()
    )

    blocked = _acl_manage_block(action, target_namespace)
    if blocked:
        return tool_error(blocked, success=False)

    preflight = _background_review_preflight(action, bare_name, namespace=target_namespace)
    if preflight is not None:
        return json.dumps(preflight, ensure_ascii=False)

    # Approval gate: when on, stages the write for review (skills are too large
    # to review inline, so they always stage regardless of origin); when off
    # (default) passes straight through. The gate is bypassed when this call is
    # itself replaying an already-approved staged write (_skill_apply_pending).
    gate_result = _apply_skill_write_gate(
        action, bare_name, namespace=target_namespace,
        content=content, category=category,
        file_path=file_path, file_content=file_content,
        old_string=old_string, new_string=new_string,
        replace_all=replace_all, absorbed_into=absorbed_into,
        requirements_confirmed=requirements_are_confirmed,
        source_paths=source_paths, reference_subdir=reference_subdir,
    )
    if gate_result is not None:
        return gate_result

    # Audit ledger (tracker #79686 P3): capture the pre-mutation state of the
    # skill directory so every mutation — any actor — lands in the append-only
    # JSONL ledger with before/after blobs. Telemetry, not a gate: failures
    # here must NEVER block the mutation (capture_before returns None on
    # error, and record_mutation below swallows everything).
    lock_scope = nullcontext() if target_namespace == "platform" and api_server_session else _skill_mutation_lock(f"{target_namespace}:{bare_name}" if target_namespace in {"user", "platform"} else bare_name)
    target_root = _root_for_namespace(target_namespace)
    from tools.skill_usage import skill_usage_scope
    with lock_scope, skill_usage_scope(target_root.path if target_root else None):
        _ledger_before = None
        _ledger_before_dir = None
        _ledger_root = None
        try:
            from tools import skill_ledger as _ledger
            _pre = _find_managed_skill(bare_name, target_namespace)
            _ledger_before_dir = _pre["path"] if _pre else None
            target_root = _root_for_namespace(target_namespace)
            # Preserve the long-standing platform ledger/blob locations so
            # existing rollback entries stay readable. Only private user skills
            # need a namespace-local ledger: the platform root is deliberately
            # read-only for ordinary API users.
            _ledger_root = (
                target_root.path
                if target_namespace == "user" and target_root is not None
                else None
            )
            with _ledger.ledger_scope(_ledger_root):
                _ledger_before = _ledger.capture_before(_ledger_before_dir, complete_package=(action == "delete"), skill=bare_name)
        except Exception:
            pass

        if target_namespace == "platform" and api_server_session:
            from tools.shared_skill_writer import (
                SharedSkillWriterError,
                request_shared_skill_mutation,
                serialize_skill_tree,
            )

            arguments = {
                "content": content,
                "category": category,
                "file_path": file_path,
                "file_content": file_content,
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": replace_all,
                "absorbed_into": absorbed_into,
                "requirements_confirmed": requirements_are_confirmed,
            }
            import_result = None
            if action == "publish":
                personal = _find_managed_skill(bare_name, "user")
                if not personal:
                    return tool_error(
                        f"No private user skill named '{bare_name}' exists to publish.",
                        success=False,
                    )
                if source_paths:
                    import_result = json.loads(
                        skill_manage(
                            action="import_files",
                            name=bare_name,
                            namespace="user",
                            source_paths=source_paths,
                            reference_subdir=reference_subdir,
                            task_id=task_id,
                            session_id=session_id,
                        )
                    )
                    if not import_result.get("success"):
                        return json.dumps(import_result, ensure_ascii=False)
                    personal = _find_managed_skill(bare_name, "user")
            try:
                if action == "publish":
                    arguments["files"] = serialize_skill_tree(personal["path"])

                result = request_shared_skill_mutation(
                    action, bare_name, arguments=arguments
                )
                if import_result and result.get("success"):
                    result["source_import"] = {
                        key: import_result.get(key)
                        for key in (
                            "documents",
                            "parts",
                            "text_chars",
                            "reference_subdir",
                            "manifest_sha256",
                        )
                    }
            except SharedSkillWriterError as exc:
                result = {
                    "success": False,
                    "error": str(exc),
                    "error_code": exc.code,
                }

        elif action == "create":
            if not content:
                return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
            result = _create_skill(
                bare_name,
                content,
                category,
                target_namespace,
                requirements_confirmed=requirements_are_confirmed,
                enforce_publish_lint=True,
            )

        elif action == "publish":
            result = _publish_skill(
                bare_name,
                category,
                target_namespace,
                requirements_confirmed=requirements_are_confirmed,
            )

        elif action == "edit":
            if not content:
                return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
            result = _edit_skill(bare_name, content, target_namespace)

        elif action == "patch":
            if content and old_string is None and new_string is None:
                return skill_manage(action="edit", name=bare_name, namespace=target_namespace, content=content, task_id=task_id, session_id=session_id)
            if content:
                return tool_error(_PATCH_EITHER_OR, success=False)
            if not old_string:
                return tool_error(_PATCH_NEEDS_OLD_STRING, success=False)
            if new_string is None:
                return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
            result = _patch_skill(
                bare_name, old_string, new_string, file_path, replace_all, target_namespace
            )

        elif action == "delete":
            result = _delete_skill(
                bare_name, absorbed_into=absorbed_into, namespace=target_namespace
            )

        elif action == "write_file":
            if not file_path:
                return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
            if file_content is None:
                return tool_error("file_content is required for 'write_file'.", success=False)
            result = _write_file(bare_name, file_path, file_content, target_namespace)

        elif action == "import_files":
            result = _import_source_files(
                bare_name,
                source_paths,
                reference_subdir,
                target_namespace,
                task_id,
            )

        elif action == "remove_file":
            if not file_path:
                return tool_error("file_path is required for 'remove_file'.", success=False)
            result = _remove_file(bare_name, file_path, target_namespace)

        else:
            result = {"success": False, "error": f"Unknown action '{action}'. Use: create, publish, edit, patch, delete, write_file, import_files, remove_file"}

        if result.get("success"):
            # Audit ledger append (best-effort; never blocks the mutation).
            try:
                from tools import skill_ledger as _ledger
                _post = _find_managed_skill(bare_name, target_namespace)
                _after_dir = _post["path"] if _post else None
                _evidence = {}
                if action == "delete":
                    # Record delete intent: consolidation vs prune, and whether
                    # the recoverable-archive path handled it (curator pass).
                    _evidence["absorbed_into"] = absorbed_into
                    _evidence["archived"] = bool(result.get("_archived"))
                if session_id:
                    _evidence["session_id"] = session_id
                if file_path:
                    _evidence["file_path"] = file_path
                if action == "import_files":
                    _evidence["reference_subdir"] = (
                        reference_subdir or DEFAULT_SKILL_IMPORT_SUBDIR
                    )
                    _evidence["source_count"] = len(source_paths or [])
                with _ledger.ledger_scope(_ledger_root):
                    _ledger.record_mutation(
                        action,
                        bare_name,
                        before=_ledger_before if _ledger_before is not None else [],
                        after_root=_after_dir,
                        evidence=_evidence,
                    )
            except Exception:
                pass
            try:
                from agent.prompt_builder import clear_skills_system_prompt_cache
                clear_skills_system_prompt_cache(clear_snapshot=True)
            except Exception:
                pass
            # Curator telemetry: bump patch_count on edit/patch/write_file (the actions
            # that mutate an existing skill's guidance), drop the record on delete.
            # Only mark a skill as agent-created when the background self-improvement
            # review fork creates it — foreground `skill_manage(create)` calls are
            # user-directed, and those skills belong to the user (the curator must
            # not touch them). Best-effort; telemetry failures never break the tool.
            try:
                from tools.skill_usage import bump_patch, forget, record_created
                from tools.skill_provenance import is_background_review
                if action in {"create", "publish"} and result.get("published"):
                    record_created(
                        bare_name,
                        agent_created=is_background_review(),
                        task_id=task_id,
                        session_id=session_id,
                    )
                elif action in {"patch", "edit", "write_file", "remove_file"}:
                    bump_patch(
                        bare_name,
                        action=action,
                        task_id=task_id,
                        session_id=session_id,
                    )
                elif action == "delete":
                    # A recoverable curator archive (routed through archive_skill)
                    # keeps its usage record as STATE_ARCHIVED so `hermes curator
                    # status`/`restore` still see it. Only a hard delete forgets.
                    if not result.get("_archived"):
                        forget(bare_name)
            except Exception:
                pass

            # Sync push hook (debounced, best-effort). Fires only AFTER the
            # write gate passed (staged/unapproved writes never reach here -- the
            # gate returns early above), so we never push un-reviewed content.
            # Inert unless the access gate is open (the user is a Nous admin on the
            # token), a sync base URL is configured, and the skill is opted into
            # sync. Debounced so a burst of edits collapses to one push. Never
            # raises -- an agent write must never block on sync (M1-C invariant).
            if not result.get("draft"):
                try:
                    _maybe_debounced_sync_push(bare_name)
                except Exception:
                    pass

        return json.dumps(result, ensure_ascii=False)


def skill_manage(
    action: str = "", name: str = "", namespace: str = None, content: str = None,
    category: str = None, file_path: str = None, file_content: str = None,
    old_string: str = None, new_string: str = None, replace_all: bool = False,
    absorbed_into: str = None, requirements_confirmed: Optional[bool] = None,
    task_id: str = None, session_id: str = None, source_paths: list[str] = None,
    reference_subdir: str = None, operations=None,
) -> str:
    if operations is not None:
        from gateway.session_context import get_session_env
        if get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server":
            if not isinstance(operations, list) or len(operations) != 1 or not isinstance(operations[0], dict):
                return tool_error(
                    "Multi-operation skill batches are unavailable on the multi-user API surface. Use one explicit skill operation per call; platform publication still imports and publishes its complete tree through the shared writer.",
                    success=False, error_code="api_skill_batch_unsupported", effects_applied=False,
                )
            op = dict(operations[0])
            if "operations" in op:
                return tool_error("Nested skill batches are invalid.", success=False)
            op.setdefault("name", name)
            if namespace is not None:
                op.setdefault("namespace", namespace)
            if requirements_confirmed is not None:
                op.setdefault("requirements_confirmed", requirements_confirmed)
            return _skill_manage_from(op, task_id=task_id, session_id=session_id)
        return _skill_manage_batch(operations, default_name=name or None, task_id=task_id, session_id=session_id)
    return _skill_manage_single(
        action=action, name=name, namespace=namespace, content=content, category=category,
        file_path=file_path, file_content=file_content, old_string=old_string,
        new_string=new_string, replace_all=replace_all, absorbed_into=absorbed_into,
        requirements_confirmed=requirements_confirmed, task_id=task_id, session_id=session_id,
        source_paths=source_paths, reference_subdir=reference_subdir,
    )


# --- OpenAI Function-Calling Schema -------------------------------------------

def _skill_manage_description(create_dir: str) -> str:
    return (
        "Create, update, or delete skills — your procedural memory for "
        "recurring task types. The call is an operations array (a single "
        "edit is a list of one); it applies atomically — any failure rolls "
        "every touched skill back. Ops: create (full SKILL.md; lands in "
        f"{create_dir}; must precede that skill's other "
        "ops), patch (targeted old_string/new_string fix — preferred; "
        "content alone REPLACES the whole file, read it via skill_view() "
        "first), write_file/remove_file (supporting files), delete (sole "
        "op only). Existing skills are modified wherever they live. Keep "
        "the description's first 57 chars a self-contained trigger: 'Use "
        "when <trigger>. <one-line behavior>.' Write lessons, not logs: "
        "imperative rule + why, no PR numbers/dates/incident narration, one "
        "rule per lesson, references/ named by topic (extend before adding). "
        "skill_view() shows format conventions."
    )


def _skill_manage_schema_overrides() -> dict:
    """Rebuild the create-dir hint from the ACTIVE profile at every get_definitions(): the
    multiplexed gateway serves every profile from one process, so a path baked in at import
    would name the launch profile's skills dir for everyone else (#95685)."""
    return {"description": _skill_manage_description(_display_create_dir())}


_NAME = {"type": "string"}
_OLD_STRING = {"type": "string",
               "description": "Text to find (same matching semantics as the patch tool)."}
_NEW_STRING = {"type": "string", "description": "Replacement; empty string deletes the match."}
_FILE_PATH = {
    "type": "string",
    "description": (
        "Path RELATIVE to the skill's own directory, e.g. 'references/api.md' — no leading "
        "slash, never absolute; first segment references/, templates/, scripts/, or assets/."
    ),
}  # stated once (write_file); patch/remove_file point at it


def _op_schema(action: str, props: dict, required: tuple) -> dict:
    """One self-contained per-action op shape. ``additionalProperties: false`` is what lets a
    grammar-constrained backend refuse another action's text slot outright."""
    return {"type": "object", "additionalProperties": False,
            "properties": {"name": _NAME, "action": {"type": "string", "enum": [action]}, **props},
            "required": ["name", "action", *required]}


_UPSTREAM_SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    # ONE advertised call shape (memory-tool pattern): the call IS an operations
    # array. The legacy flat shape (top-level action/name/content/...) is still
    # ACCEPTED for old transcripts and staged-write replay, but not advertised.
    "description": _skill_manage_description("the profile's skills.create_dir"),
    "parameters": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": (
                    "Ordered ops; each names its target skill (lowercase, hyphens/underscores, "
                    "max 64 chars). Each action is its own shape; another action's text slot is invalid."
                ),
                # Per-action branches, not one flat union: with one object holding content /
                # new_string / file_content side by side, a 27B model that just used write_file
                # kept emitting file_content on create and the whole batch rolled back (#112677).
                "items": {"anyOf": [
                    _op_schema("create", {
                        "content": {"type": "string",
                                    "description": "Full SKILL.md text (YAML frontmatter + markdown body)."},
                        "category": {"type": "string",
                                     "description": "Optional category subdir (e.g. 'devops')."},
                    }, ("content",)),
                    _op_schema("patch", {
                        "old_string": _OLD_STRING, "new_string": _NEW_STRING,
                        "replace_all": {"type": "boolean",
                                        "description": "Replace all occurrences (default false)."},
                        "file_path": {"type": "string",
                                      "description": "Optional supporting file (write_file's shape); default SKILL.md."},
                    }, ("old_string", "new_string")),
                    _op_schema("patch", {
                        "content": {"type": "string",
                                    "description": "Full SKILL.md rewrite (REPLACES the whole file; last resort)."},
                    }, ("content",)),
                    _op_schema("write_file", {
                        "file_path": _FILE_PATH,
                        "file_content": {"type": "string", "description": "Full text of the supporting file."},
                    }, ("file_path", "file_content")),
                    _op_schema("remove_file", {
                        "file_path": {"type": "string", "description": "Supporting file (write_file's shape)."},
                    }, ("file_path",)),
                    # `absorbed_into` stays in the delete shape: with additionalProperties:false a
                    # grammar-constrained backend would otherwise strip it and the curator's
                    # consolidation delete guard would fail-close every consolidation.
                    _op_schema("delete", {
                        "absorbed_into": {"type": "string",
                                          "description": "Curator consolidation only: umbrella skill "
                                                         "that absorbed this one (must exist)."},
                    }, ()),
                ]},
            },
            # Also accepted, never advertised: the legacy flat single-op fields.
        },
        "required": ["operations"],
    },
}


SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        f"New skills go to {display_hermes_home()}/skills/; existing skills can be modified wherever they live.\n\n"
        "Actions: create (save a draft; publish immediately only when "
        "requirements_confirmed=true and validation passes), publish (promote "
        "a completed inactive draft), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, import_files, remove_file. import_files turns a "
        "whole granted attachment batch into plain Markdown references in one "
        "atomic, divide-and-conquer operation; it never embeds base64. To share "
        "a private skill with colleagues, use publish with namespace='platform'. "
        "publish may receive source_paths and will import those attachments "
        "before publishing the complete personal tree. Portability fast path: "
        "when the user asks for current attachments to travel with a skill across "
        "sessions or to colleagues, make exactly one skill_manage call with "
        "action='publish', namespace='platform', and every attachment handle in "
        "source_paths. That one atomic call imports, indexes, and publishes the "
        "complete tree. Do not call import_files separately, create another index, "
        "or patch SKILL.md unless the user separately asked to change its "
        "instructions. After success, answer from source_import in the receipt.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. This lets the curator tell "
        "consolidation from pruning without guessing, so downstream consumers "
        "(cron jobs that reference the old skill name, etc.) get updated "
        "correctly. The target you name in `absorbed_into` must already "
        "exist — create/patch the umbrella first, then delete.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "Never mutate a skill merely because applying it exposed a problem. "
        "Report the issue; create, patch, edit, publish, delete, or write files "
        "only when the current user explicitly asks to mutate skill state. A "
        "referential confirmation such as 'yes', 'do it', or 'make it the "
        "governing rule' counts when the recent conversation explicitly "
        "identified the skill mutation being confirmed; do not make the user "
        "repeat the full edit command.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples.\n\n"
        f"Description: create requires one sentence of at most {SKILL_PROMPT_DESC_LIMIT} "
        "characters, with the trigger first and ending in a period. Move all "
        "detail into the skill body. Example: 'Use when reviewing HK marks.'\n\n"
        "Pinned skills are protected from deletion only — skill_manage(action='delete') "
        "will refuse with a message pointing the user to `hermes curator unpin <name>`. "
        "Patches and edits go through on pinned skills so you can still improve them as "
        "pitfalls come up; pin only guards against irrecoverable loss."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "publish", "patch", "edit", "delete", "write_file", "import_files", "remove_file"],
                "description": "The action to perform."
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                )
            },
            "namespace": {
                "type": "string",
                "enum": ["user", "platform"],
                "description": (
                    "Destination skill namespace. OpenWebUI sessions default to "
                    "the authenticated user's private namespace; shared platform "
                    "writes require explicit namespace='platform' and ACL approval."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. For 'edit', read the skill "
                    "first with skill_view() and provide the complete updated text."
                )
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                )
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'); must differ from "
                    "old_string. Can be empty string to delete the matched text."
                )
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false)."
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Used with 'create' or 'publish'."
                )
            },
            "requirements_confirmed": {
                "type": "boolean",
                "description": (
                    "For create/publish: true only when the user has supplied "
                    "and confirmed the complete rules. False or omitted keeps "
                    "create as an inactive draft and blocks publish. Never "
                    "infer missing requirements or set this merely to finish."
                ),
                "default": False,
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                )
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'."
            },
            "source_paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_SKILL_IMPORT_FILES,
                "description": (
                    "For import_files, or publish to namespace=platform: the complete "
                    "list of current attachment handles such as F01, F02. Every source "
                    "is extracted outside model context, completeness-checked, split "
                    "into bounded Markdown parts, and indexed. One incomplete source "
                    "aborts the batch."
                ),
            },
            "reference_subdir": {
                "type": "string",
                "description": (
                    "Optional destination below references/. Defaults to "
                    "references/source-library."
                ),
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "For 'delete' only — declares intent so the curator can "
                    "tell consolidation from pruning without guessing. "
                    "Pass the umbrella skill name when this skill's content "
                    "was merged into another (the target must already exist). "
                    "Pass an empty string when the skill is truly stale and "
                    "being pruned with no forwarding target. Omitting the arg "
                    "on delete is supported for backward compatibility but "
                    "downstream tooling (e.g. cron-job skill reference "
                    "rewriting) will have to guess at intent."
                )
            },
        },
        "required": ["action", "name"],
    },
}

SKILL_MANAGE_SCHEMA["parameters"]["properties"]["operations"] = _UPSTREAM_SKILL_MANAGE_SCHEMA["parameters"]["properties"]["operations"]
SKILL_MANAGE_SCHEMA["parameters"]["properties"]["operations"]["description"] += " Multi-user API sessions accept one operation per call; atomic multi-operation batches are available only in standalone contexts."
SKILL_MANAGE_SCHEMA["parameters"].pop("required", None)
SKILL_MANAGE_SCHEMA["parameters"]["anyOf"] = [{"required": ["action", "name"]}, {"required": ["operations"]}]


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="skill_manage", toolset="skills", schema=SKILL_MANAGE_SCHEMA, emoji="📝",
    handler=lambda args, **kw: _skill_manage_from(
        args, task_id=kw.get("task_id"), session_id=kw.get("session_id")),
    )


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.


_PLUGIN_COMPAT_LAZY = {
    'mark_background_review_skill_read': ('tools.skill_manager_guards', 'mark_background_review_skill_read'),
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
