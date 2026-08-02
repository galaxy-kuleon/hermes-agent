"""Transactional out-of-band writer for read-only platform skills.

Chat/agent containers receive the platform content volume read-only. The only
deployment context that should set :data:`PLATFORM_SKILL_WRITER_ENV` and mount
that volume read-write is a short-lived operator service. The environment flag
is an intent check; the mount capability remains the enforcement boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, Optional

import yaml

from hermes_constants import get_bundled_skills_dir, get_hermes_home
from tools.skill_state import (
    PLATFORM_CURATOR_ARCHIVE_PLAN_FILENAME,
    PLATFORM_CURATOR_BACKUPS_DIRNAME,
    PLATFORM_CURATOR_STATE_FILENAME,
    PLATFORM_CURATOR_SUPPRESSION_FILENAME,
    PLATFORM_GENERATION_FILENAME,
    PLATFORM_HUB_DIRNAME,
    PLATFORM_MANIFEST_FILENAME,
    PLATFORM_SKILL_WRITER_ENV,
    PLATFORM_TERMUX_SYNC_STAMP_FILENAME,
    PLATFORM_USAGE_FILENAME,
    platform_skill_state_dir,
    platform_skills_dir,
)
from utils import atomic_json_write


PLATFORM_TRANSACTIONS_DIRNAME = "platform-skill-transactions"
TRANSACTION_RECEIPT_FILENAME = "receipt.json"
PLATFORM_SNAPSHOT_FILENAME = "platform-before.tar.gz"
STATE_SNAPSHOT_FILENAME = "state-before.tar.gz"
POST_STATE_ARCHIVE_FILENAME = "governance-after.tar.gz"
ROLLBACK_POST_STATE_ARCHIVE_FILENAME = "governance-rollback-after.tar.gz"
ROLLBACK_PLATFORM_SNAPSHOT_FILENAME = "platform-before-rollback.tar.gz"
ROLLBACK_STATE_SNAPSHOT_FILENAME = "state-before-rollback.tar.gz"
TRANSACTION_LOCK_FILENAME = ".writer.lock"
TRANSACTION_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
_OPERATOR_STATE_NAMES = frozenset(
    {
        PLATFORM_GENERATION_FILENAME,
        PLATFORM_HUB_DIRNAME,
        PLATFORM_MANIFEST_FILENAME,
    }
)

_LEGACY_GENERATED_STATE = {
    ".usage.json": PLATFORM_USAGE_FILENAME,
    ".bundled_manifest": PLATFORM_MANIFEST_FILENAME,
    ".curator_state": PLATFORM_CURATOR_STATE_FILENAME,
    ".curator_suppressed": PLATFORM_CURATOR_SUPPRESSION_FILENAME,
    ".termux_bundled_sync_stamp": PLATFORM_TERMUX_SYNC_STAMP_FILENAME,
    ".hub": PLATFORM_HUB_DIRNAME,
    ".curator_backups": PLATFORM_CURATOR_BACKUPS_DIRNAME,
    ".restore-backups": "legacy-restore-backups",
}


class PlatformSkillStoreError(RuntimeError):
    """A platform writer precondition, validation, or transaction failed."""


def default_transactions_dir(home: Optional[Path] = None) -> Path:
    return Path(home or get_hermes_home()) / PLATFORM_TRANSACTIONS_DIRNAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        rel = item.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(_sha256(item).encode("ascii"))
    return digest.hexdigest()


def skill_tree_hash(path: Path) -> Optional[str]:
    """Return a stable SHA-256 for one skill tree, or ``None`` if absent.

    The digest covers relative file names and file content, never absolute paths.
    Symlinks are ignored here because :func:`verify_store` rejects them before a
    transaction can commit.
    """

    path = Path(path)
    if not path.exists() or not path.is_dir():
        return None
    return _dir_digest(path)


def store_manifest(root: Path) -> Dict[str, Dict[str, Any]]:
    """Return a stable relative file manifest without following redirects."""

    root = Path(root)
    if not root.exists():
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        info = path.stat()
        result[rel] = {
            "sha256": _sha256(path),
            "size": info.st_size,
            "mode": stat.S_IMODE(info.st_mode),
        }
    return result


def _read_frontmatter(skill_md: Path) -> Dict[str, Any]:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("unclosed YAML frontmatter")
    parsed = yaml.safe_load(text[4:end])
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter is not a mapping")
    name = parsed.get("name")
    description = parsed.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing name")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("missing description")
    return parsed


def verify_store(root: Path) -> Dict[str, Any]:
    """Validate platform content and return errors plus a content manifest."""

    root = Path(root)
    errors: list[str] = []
    names: Dict[str, str] = {}
    if not root.exists() or not root.is_dir():
        errors.append(f"platform root is not a directory: {root}")
        return {"ok": False, "errors": errors, "manifest": {}}

    for legacy_name in sorted(_LEGACY_GENERATED_STATE):
        if (root / legacy_name).exists() or (root / legacy_name).is_symlink():
            errors.append(f"generated state remains in platform content: {legacy_name}")

    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"symlink is not allowed in platform content: {rel}")
            continue
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            errors.append(f"cannot stat platform path {rel}: {exc}")
            continue
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            errors.append(f"special file is not allowed in platform content: {rel}")

    for skill_md in sorted(root.rglob("SKILL.md")):
        if skill_md.is_symlink():
            continue
        rel = skill_md.relative_to(root).as_posix()
        try:
            frontmatter = _read_frontmatter(skill_md)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"invalid frontmatter in {rel}: {exc}")
            continue
        name = str(frontmatter["name"]).strip()
        prior = names.get(name)
        if prior is not None:
            errors.append(f"duplicate skill name {name!r}: {prior} and {rel}")
        else:
            names[name] = rel

    return {
        "ok": not errors,
        "errors": errors,
        "skills": sorted(names),
        "manifest": store_manifest(root),
    }


def _validate_tree_safety(root: Path) -> None:
    for path in root.rglob("*") if root.exists() else []:
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PlatformSkillStoreError(
                f"symlink is not allowed in platform content: {rel}"
            )
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise PlatformSkillStoreError(
                f"special file is not allowed in platform content: {rel}"
            )


def build_plan(source_root: Path, target_root: Path) -> Dict[str, Any]:
    """Compare source skill packages with target without mutating either tree."""

    source_root = Path(source_root)
    target_root = Path(target_root)
    source: Dict[str, str] = {}
    target: Dict[str, str] = {}
    for skill_md in sorted(source_root.rglob("SKILL.md")) if source_root.exists() else []:
        rel = skill_md.parent.relative_to(source_root).as_posix()
        source[rel] = _dir_digest(skill_md.parent)
    for skill_md in sorted(target_root.rglob("SKILL.md")) if target_root.exists() else []:
        rel = skill_md.parent.relative_to(target_root).as_posix()
        target[rel] = _dir_digest(skill_md.parent)
    return {
        "source_root": str(source_root),
        "target_root": str(target_root),
        "add": sorted(set(source) - set(target)),
        "update": sorted(
            name for name in set(source) & set(target) if source[name] != target[name]
        ),
        "unchanged": sorted(
            name for name in set(source) & set(target) if source[name] == target[name]
        ),
        "target_only": sorted(set(target) - set(source)),
    }


def read_generation(state_dir: Optional[Path] = None) -> int:
    path = Path(state_dir or platform_skill_state_dir()) / PLATFORM_GENERATION_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = int(data.get("generation", 0)) if isinstance(data, dict) else 0
        return max(0, value)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _write_generation(state_dir: Path, generation: int, transaction_id: str) -> None:
    atomic_json_write(
        state_dir / PLATFORM_GENERATION_FILENAME,
        {
            "generation": generation,
            "transaction_id": transaction_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
        sort_keys=True,
    )


def _session_is_api_server() -> bool:
    try:
        from gateway.session_context import get_session_env

        return get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server"
    except Exception:
        return False


def _path_writable(path: Path) -> bool:
    probe = path if path.exists() else path.parent
    try:
        mode = probe.stat().st_mode
    except OSError:
        return False
    if not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        return False
    try:
        readonly_flag = getattr(os, "ST_RDONLY", 1)
        if os.statvfs(probe).f_flag & readonly_flag:
            return False
    except (AttributeError, OSError):
        pass
    return os.access(probe, os.W_OK)


def _require_operator(target_root: Path) -> None:
    if _session_is_api_server():
        raise PlatformSkillStoreError(
            "platform writer is out-of-band only; api_server contexts are denied"
        )
    if os.getenv(PLATFORM_SKILL_WRITER_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise PlatformSkillStoreError(
            f"out-of-band writer intent missing: set {PLATFORM_SKILL_WRITER_ENV}=1 "
            "only in the isolated admin service"
        )
    if not _path_writable(target_root):
        raise PlatformSkillStoreError(
            f"platform root is not writable in this context: {target_root}"
        )


@contextmanager
def _writer_lock(transactions_dir: Path) -> Iterator[None]:
    transactions_dir.mkdir(parents=True, exist_ok=True)
    lock_path = transactions_dir / TRANSACTION_LOCK_FILENAME
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows fallback is serialized by service
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover
            pass
        handle.close()


def _snapshot_tree(root: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        if not root.exists():
            return
        for entry in sorted(root.iterdir()):
            tar.add(entry, arcname=entry.name, recursive=True)


def _snapshot_operator_state(state_dir: Path, archive: Path) -> None:
    """Snapshot only writer-owned state; never capture mutable usage telemetry."""

    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for name in sorted(_OPERATOR_STATE_NAMES):
            entry = state_dir / name
            if entry.exists() or entry.is_symlink():
                tar.add(entry, arcname=name, recursive=True)


def _clear_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for entry in list(root.iterdir()):
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)


def _restore_tree(root: Path, archive: Path) -> None:
    _clear_tree(root)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
                raise PlatformSkillStoreError(
                    f"unsafe path in transaction snapshot: {member.name}"
                )
        try:
            tar.extractall(root, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12
            tar.extractall(root)


def _restore_operator_state(state_dir: Path, archive: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    for name in _OPERATOR_STATE_NAMES:
        entry = state_dir / name
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
        elif entry.exists():
            shutil.rmtree(entry)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] not in _OPERATOR_STATE_NAMES
                or member.issym()
                or member.islnk()
            ):
                raise PlatformSkillStoreError(
                    f"unsafe path in operator-state snapshot: {member.name}"
                )
        try:
            tar.extractall(state_dir, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12
            tar.extractall(state_dir)


def migrate_legacy_generated_state(
    target_root: Path,
    state_dir: Path,
    *,
    preserve_existing: bool = False,
) -> list[str]:
    """Move generated state out of platform content during an operator transaction."""

    moved: list[str] = []
    state_dir.mkdir(parents=True, exist_ok=True)
    pending = [
        (legacy_name, state_name)
        for legacy_name, state_name in _LEGACY_GENERATED_STATE.items()
        if (target_root / legacy_name).exists()
        or (target_root / legacy_name).is_symlink()
    ]
    if not preserve_existing:
        for legacy_name, state_name in pending:
            if (state_dir / state_name).exists():
                raise PlatformSkillStoreError(
                    f"cannot migrate {legacy_name}: state destination already exists"
                )

    for legacy_name, state_name in pending:
        source = target_root / legacy_name
        destination = state_dir / state_name
        if destination.exists():
            if source.is_dir() and not source.is_symlink():
                shutil.rmtree(source)
            else:
                source.unlink()
        else:
            shutil.move(str(source), str(destination))
        moved.append(legacy_name)
    return moved


def _recover_incomplete_locked(
    target_root: Path, state_dir: Path, transactions_dir: Path
) -> list[str]:
    recovered: list[str] = []
    if not transactions_dir.exists():
        return recovered
    for transaction_dir in sorted(transactions_dir.iterdir(), reverse=True):
        if not transaction_dir.is_dir():
            continue
        receipt_path = transaction_dir / TRANSACTION_RECEIPT_FILENAME
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if receipt.get("status") != "applying":
            continue
        platform_snapshot = transaction_dir / str(receipt["platform_snapshot"])
        state_snapshot = transaction_dir / str(receipt["state_snapshot"])
        observed_generation = read_generation(state_dir)
        _restore_tree(target_root, platform_snapshot)
        _restore_operator_state(state_dir, state_snapshot)
        migrate_legacy_generated_state(
            target_root, state_dir, preserve_existing=True
        )
        recovery_generation = max(
            observed_generation, int(receipt.get("before_generation", 0))
        ) + 1
        _write_generation(
            state_dir, recovery_generation, f"recover:{receipt['transaction_id']}"
        )
        verified = verify_store(target_root)
        if not verified["ok"]:
            raise PlatformSkillStoreError(
                "incomplete transaction recovery validation failed: "
                + "; ".join(verified["errors"])
            )
        receipt.update(
            {
                "status": "rolled_back_recovered",
                "recovered_at": datetime.now(timezone.utc).isoformat(),
                "recovery_generation": recovery_generation,
            }
        )
        atomic_json_write(receipt_path, receipt, indent=2, sort_keys=True)
        recovered.append(str(receipt["transaction_id"]))
    return recovered


def recover_incomplete_transactions(
    *,
    target_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    root = Path(target_root or platform_skills_dir())
    state = Path(state_dir or platform_skill_state_dir())
    transactions = Path(transactions_dir or default_transactions_dir())
    _require_operator(root)
    with _writer_lock(transactions):
        recovered = _recover_incomplete_locked(root, state, transactions)
    return {"ok": True, "recovered": recovered, "generation": read_generation(state)}


def _new_transaction_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def _validate_transaction_id(transaction_id: str) -> str:
    """Validate the opaque transaction directory name before path joining."""

    normalized = str(transaction_id or "").strip()
    if not TRANSACTION_ID_PATTERN.fullmatch(normalized):
        raise PlatformSkillStoreError("invalid transaction id")
    return normalized


def apply_transaction(
    operation: str,
    mutation: Callable[[str], Dict[str, Any]],
    *,
    target_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
    expected_generation: Optional[int] = None,
    receipt_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply one operator mutation with snapshots, validation, receipt and rollback."""

    target_root = Path(target_root or platform_skills_dir())
    state_dir = Path(state_dir or platform_skill_state_dir())
    transactions_dir = Path(transactions_dir or default_transactions_dir())
    _require_operator(target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    _validate_tree_safety(target_root)

    with _writer_lock(transactions_dir):
        recovered = _recover_incomplete_locked(
            target_root, state_dir, transactions_dir
        )
        current = read_generation(state_dir)
        if expected_generation is not None and current != expected_generation:
            raise PlatformSkillStoreError(
                f"generation precondition failed: expected {expected_generation}, current {current}"
            )

        transaction_id = _new_transaction_id()
        transaction_dir = transactions_dir / transaction_id
        transaction_dir.mkdir(parents=True, exist_ok=False)
        platform_snapshot = transaction_dir / PLATFORM_SNAPSHOT_FILENAME
        state_snapshot = transaction_dir / STATE_SNAPSHOT_FILENAME
        _snapshot_tree(target_root, platform_snapshot)
        _snapshot_operator_state(state_dir, state_snapshot)
        before_manifest = store_manifest(target_root)
        receipt: Dict[str, Any] = {
            "version": 1,
            "transaction_id": transaction_id,
            "operation": operation,
            "status": "applying",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "before_generation": current,
            "before_manifest": before_manifest,
            "platform_snapshot": platform_snapshot.name,
            "state_snapshot": state_snapshot.name,
        }
        if receipt_metadata:
            # Metadata is nested so callers cannot overwrite transaction state.
            # Round-trip through JSON now, before the first receipt write, to
            # reject opaque objects and keep the journal independently readable.
            receipt["request"] = json.loads(json.dumps(receipt_metadata))
        receipt_path = transaction_dir / TRANSACTION_RECEIPT_FILENAME
        atomic_json_write(receipt_path, receipt, indent=2, sort_keys=True)
        try:
            receipt["migrated_state"] = migrate_legacy_generated_state(
                target_root, state_dir
            )
            baseline = verify_store(target_root)
            if not baseline["ok"]:
                raise PlatformSkillStoreError(
                    "platform baseline validation failed: "
                    + "; ".join(baseline["errors"])
                )
            receipt["result"] = mutation(transaction_id)
            verified = verify_store(target_root)
            if not verified["ok"]:
                raise PlatformSkillStoreError(
                    "transaction validation failed: " + "; ".join(verified["errors"])
                )
            after_generation = current + 1
            _write_generation(state_dir, after_generation, transaction_id)
            receipt.update(
                {
                    "status": "committed",
                    "committed_at": datetime.now(timezone.utc).isoformat(),
                    "after_generation": after_generation,
                    "after_manifest": verified["manifest"],
                }
            )
            atomic_json_write(receipt_path, receipt, indent=2, sort_keys=True)
        except Exception as exc:
            _restore_tree(target_root, platform_snapshot)
            _restore_operator_state(state_dir, state_snapshot)
            migrate_legacy_generated_state(
                target_root, state_dir, preserve_existing=True
            )
            receipt.update(
                {
                    "status": "rolled_back_on_failure",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(exc),
                }
            )
            atomic_json_write(receipt_path, receipt, indent=2, sort_keys=True)
            if isinstance(exc, PlatformSkillStoreError):
                raise
            raise PlatformSkillStoreError(f"platform transaction failed: {exc}") from exc

        return {
            "ok": True,
            "transaction_id": transaction_id,
            "receipt_path": str(receipt_path),
            "before_generation": current,
            "after_generation": receipt["after_generation"],
            "result": receipt.get("result") or {},
            "recovered_transactions": recovered,
        }


def _safe_destination(destination: str) -> Path:
    pure = PurePosixPath(str(destination).replace("\\", "/"))
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if pure.is_absolute() or not parts or ".." in parts or parts[0].startswith("."):
        raise PlatformSkillStoreError(f"unsafe platform destination: {destination}")
    return Path(*parts)


def capture_transaction_post_state(
    transaction_id: str,
    destination: str,
    *,
    target_root: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
    archive_filename: str = POST_STATE_ARCHIVE_FILENAME,
    accepted_receipt_statuses: tuple[str, ...] = ("applying",),
    allow_existing_archive: bool = False,
) -> Dict[str, Any]:
    """Freeze one skill's exact post-state inside its transaction journal.

    The journal volume is the immutable handoff boundary between the isolated
    writer and a host-side governance reconciler.  Missing targets are explicit
    tombstones.  Present targets are archived under their platform-relative
    destination so a consumer never has to infer where the bytes belong.

    This function is called while the platform writer lock is held.  Any
    failure propagates to :func:`apply_transaction`, which restores the
    platform snapshot instead of committing a mutation with no governance
    evidence.
    """

    transaction_id = _validate_transaction_id(transaction_id)
    root = Path(target_root or platform_skills_dir())
    transactions = Path(transactions_dir or default_transactions_dir())
    safe_destination = _safe_destination(destination)
    transaction_dir = transactions / transaction_id
    receipt_path = transaction_dir / TRANSACTION_RECEIPT_FILENAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformSkillStoreError(
            f"transaction receipt not found: {transaction_id}"
        ) from exc
    if (
        receipt.get("transaction_id") != transaction_id
        or receipt.get("status") not in accepted_receipt_statuses
    ):
        raise PlatformSkillStoreError(
            f"transaction {transaction_id} is not accepting post-state evidence"
        )

    target = root / safe_destination
    metadata: Dict[str, Any] = {
        "version": 1,
        "destination": safe_destination.as_posix(),
    }
    if not target.exists():
        metadata.update(
            {
                "state": "deleted",
                "tree_hash": None,
                "archive": None,
                "archive_sha256": None,
            }
        )
        return metadata
    if target.is_symlink() or not target.is_dir():
        raise PlatformSkillStoreError(
            "governance post-state target is not a safe skill directory"
        )
    _validate_tree_safety(target)

    archive = transaction_dir / archive_filename
    if archive.exists() or archive.is_symlink():
        if not allow_existing_archive or archive.is_symlink() or not archive.is_file():
            raise PlatformSkillStoreError(
                f"transaction post-state archive already exists: {archive_filename}"
            )
        metadata.update(
            {
                "state": "present",
                "tree_hash": skill_tree_hash(target),
                "archive": archive.name,
                "archive_sha256": _sha256(archive),
            }
        )
        return metadata
    fd, temporary_name = tempfile.mkstemp(
        dir=transaction_dir,
        prefix=f".{archive_filename}.",
        suffix=".tmp",
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz") as tar:
            tar.add(
                target,
                arcname=safe_destination.as_posix(),
                recursive=True,
            )
        os.replace(temporary, archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    metadata.update(
        {
            "state": "present",
            "tree_hash": skill_tree_hash(target),
            "archive": archive.name,
            "archive_sha256": _sha256(archive),
        }
    )
    return metadata


def put_skill(
    source: Path,
    *,
    destination: Optional[str] = None,
    state_update: Optional[Callable[[Path], Optional[Dict[str, Any]]]] = None,
    target_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
    expected_generation: Optional[int] = None,
) -> Dict[str, Any]:
    source = Path(source)
    if source.is_symlink() or not source.is_dir() or not (source / "SKILL.md").is_file():
        raise PlatformSkillStoreError("put source must be a skill directory with SKILL.md")
    _validate_tree_safety(source)
    try:
        _read_frontmatter(source / "SKILL.md")
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise PlatformSkillStoreError(f"invalid put source: {exc}") from exc
    relative = _safe_destination(destination or source.name)
    root = Path(target_root or platform_skills_dir())

    def _put(transaction_id: str) -> Dict[str, Any]:
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".platform-txn-{transaction_id}-", dir=dest.parent))
        staged_skill = stage / source.name
        backup = dest.parent / f".platform-backup-{transaction_id}-{dest.name}"
        try:
            shutil.copytree(source, staged_skill)
            if dest.exists():
                os.replace(dest, backup)
            os.replace(staged_skill, dest)
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if backup.exists() and not dest.exists():
                os.replace(backup, dest)
        result: Dict[str, Any] = {
            "destination": relative.as_posix(),
            "source": str(source),
        }
        if state_update is not None:
            result["state_update"] = state_update(dest) or {}
        return result

    return apply_transaction(
        "put",
        _put,
        target_root=root,
        state_dir=state_dir,
        transactions_dir=transactions_dir,
        expected_generation=expected_generation,
    )


def delete_skill(
    name: str,
    *,
    state_update: Optional[Callable[[Path], Optional[Dict[str, Any]]]] = None,
    target_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
    expected_generation: Optional[int] = None,
) -> Dict[str, Any]:
    root = Path(target_root or platform_skills_dir())
    def _delete(transaction_id: str) -> Dict[str, Any]:
        matches: list[Path] = []
        for skill_md in root.rglob("SKILL.md") if root.exists() else []:
            try:
                if str(_read_frontmatter(skill_md).get("name")) == name:
                    matches.append(skill_md.parent)
            except Exception:
                continue
        if len(matches) != 1:
            raise PlatformSkillStoreError(
                f"delete requires exactly one platform skill named {name!r}; "
                f"found {len(matches)}"
            )
        target = matches[0]
        quarantine = target.parent / f".platform-delete-{transaction_id}-{target.name}"
        os.replace(target, quarantine)
        shutil.rmtree(quarantine)
        result: Dict[str, Any] = {
            "deleted": target.relative_to(root).as_posix(),
            "name": name,
        }
        if state_update is not None:
            result["state_update"] = state_update(target) or {}
        return result

    return apply_transaction(
        "delete",
        _delete,
        target_root=root,
        state_dir=state_dir,
        transactions_dir=transactions_dir,
        expected_generation=expected_generation,
    )


def sync_bundled(
    *,
    target_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
    expected_generation: Optional[int] = None,
) -> Dict[str, Any]:
    root = Path(target_root or platform_skills_dir())
    state = Path(state_dir or platform_skill_state_dir())

    def _sync(_transaction_id: str) -> Dict[str, Any]:
        from tools.skills_sync import sync_skills

        return sync_skills(
            quiet=True,
            target_root=root,
            state_dir=state,
            strict=True,
            operator_transaction=True,
        )

    return apply_transaction(
        "sync-bundled",
        _sync,
        target_root=root,
        state_dir=state,
        transactions_dir=transactions_dir,
        expected_generation=expected_generation,
    )


def rollback_transaction(
    transaction_id: str,
    *,
    target_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    transactions_dir: Optional[Path] = None,
    rollback_metadata: Optional[Dict[str, Any]] = None,
    expected_request_target: Optional[str] = None,
) -> Dict[str, Any]:
    transaction_id = _validate_transaction_id(transaction_id)
    root = Path(target_root or platform_skills_dir())
    state = Path(state_dir or platform_skill_state_dir())
    transactions = Path(transactions_dir or default_transactions_dir())
    _require_operator(root)
    transaction_dir = transactions / transaction_id
    receipt_path = transaction_dir / TRANSACTION_RECEIPT_FILENAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformSkillStoreError(f"transaction receipt not found: {transaction_id}") from exc
    if receipt.get("status") == "rolled_back":
        raise PlatformSkillStoreError(f"transaction {transaction_id} was already rolled back")
    if receipt.get("status") != "committed":
        raise PlatformSkillStoreError(
            f"transaction {transaction_id} is not rollback-eligible: {receipt.get('status')}"
        )
    if (
        receipt.get("transaction_id") != transaction_id
        or receipt.get("platform_snapshot") != PLATFORM_SNAPSHOT_FILENAME
        or receipt.get("state_snapshot") != STATE_SNAPSHOT_FILENAME
    ):
        raise PlatformSkillStoreError(
            f"transaction {transaction_id} receipt integrity check failed"
        )
    if expected_request_target is not None:
        request_metadata = receipt.get("request")
        receipt_target = (
            str(request_metadata.get("target") or "")
            if isinstance(request_metadata, dict)
            else ""
        )
        if receipt_target != expected_request_target:
            raise PlatformSkillStoreError(
                f"transaction {transaction_id} target does not match rollback request"
            )

    with _writer_lock(transactions):
        current = read_generation(state)
        committed_generation = int(receipt.get("after_generation", -1))
        if current != committed_generation:
            raise PlatformSkillStoreError(
                f"rollback base generation mismatch: transaction ended at "
                f"{committed_generation}, current is {current}"
            )
        platform_snapshot = transaction_dir / PLATFORM_SNAPSHOT_FILENAME
        state_snapshot = transaction_dir / STATE_SNAPSHOT_FILENAME
        rollback_platform_snapshot = (
            transaction_dir / ROLLBACK_PLATFORM_SNAPSHOT_FILENAME
        )
        rollback_state_snapshot = transaction_dir / ROLLBACK_STATE_SNAPSHOT_FILENAME
        if not rollback_platform_snapshot.exists():
            _snapshot_tree(root, rollback_platform_snapshot)
        if not rollback_state_snapshot.exists():
            _snapshot_operator_state(state, rollback_state_snapshot)
        try:
            _restore_tree(root, platform_snapshot)
            _restore_operator_state(state, state_snapshot)
            migrate_legacy_generated_state(root, state, preserve_existing=True)
            verified = verify_store(root)
            if not verified["ok"]:
                raise PlatformSkillStoreError(
                    "rollback validation failed: " + "; ".join(verified["errors"])
                )
            original_outbox = (receipt.get("result") or {}).get(
                "governance_outbox"
            )
            if isinstance(original_outbox, dict) and original_outbox.get(
                "destination"
            ):
                receipt["rollback_governance_outbox"] = (
                    capture_transaction_post_state(
                        transaction_id,
                        str(original_outbox["destination"]),
                        target_root=root,
                        transactions_dir=transactions,
                        archive_filename=ROLLBACK_POST_STATE_ARCHIVE_FILENAME,
                        accepted_receipt_statuses=("committed",),
                        allow_existing_archive=True,
                    )
                )
            rollback_generation = current + 1
            _write_generation(state, rollback_generation, f"rollback:{transaction_id}")
            receipt.update(
                {
                    "status": "rolled_back",
                    "rolled_back_at": datetime.now(timezone.utc).isoformat(),
                    "rollback_generation": rollback_generation,
                }
            )
            if rollback_metadata:
                receipt["rollback_request"] = json.loads(json.dumps(rollback_metadata))
            atomic_json_write(receipt_path, receipt, indent=2, sort_keys=True)
        except Exception:
            _restore_tree(root, rollback_platform_snapshot)
            _restore_operator_state(state, rollback_state_snapshot)
            raise
    return {
        "ok": True,
        "transaction_id": transaction_id,
        "after_generation": rollback_generation,
        "manifest": verified["manifest"],
    }


def bundled_source_dir() -> Path:
    return get_bundled_skills_dir(Path(__file__).parent.parent / "skills")
