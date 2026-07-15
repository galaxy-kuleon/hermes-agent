"""Paths for mutable skill state that must not live in platform content.

The platform content root may be mounted read-only in chat/agent containers.
Operational state therefore belongs under ``HERMES_HOME/skill-state``. User
skills keep their state in their own writable root for Increment 1 compatibility.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


SKILL_STATE_DIRNAME = "skill-state"
PLATFORM_STATE_NAMESPACE = "platform"
PLATFORM_USAGE_FILENAME = "usage.json"
PLATFORM_MANIFEST_FILENAME = "bundled_manifest"
PLATFORM_HUB_DIRNAME = "hub"
PLATFORM_GENERATION_FILENAME = "generation.json"
PLATFORM_CURATOR_STATE_FILENAME = "curator_state.json"
PLATFORM_CURATOR_BACKUPS_DIRNAME = "curator-backups"
PLATFORM_CURATOR_SUPPRESSION_FILENAME = "curator_suppressed"
PLATFORM_CURATOR_ARCHIVE_PLAN_FILENAME = "curator-archive-plan.jsonl"
PLATFORM_TERMUX_SYNC_STAMP_FILENAME = "termux_bundled_sync_stamp"
PLATFORM_PROMPT_SNAPSHOT_FILENAME = "prompt_snapshot.json"
PLATFORM_SKILL_WRITER_ENV = "HERMES_PLATFORM_SKILL_WRITER"
PLATFORM_SKILLS_IMMUTABLE_ENV = "HERMES_PLATFORM_SKILLS_IMMUTABLE"


def platform_skills_dir(home: Optional[Path] = None) -> Path:
    return Path(home or get_hermes_home()) / "skills"


def platform_skill_state_dir(home: Optional[Path] = None) -> Path:
    return Path(home or get_hermes_home()) / SKILL_STATE_DIRNAME / PLATFORM_STATE_NAMESPACE


def platform_manifest_file(home: Optional[Path] = None) -> Path:
    return platform_skill_state_dir(home) / PLATFORM_MANIFEST_FILENAME


def platform_hub_dir(home: Optional[Path] = None) -> Path:
    return platform_skill_state_dir(home) / PLATFORM_HUB_DIRNAME


def platform_generation_file(home: Optional[Path] = None) -> Path:
    return platform_skill_state_dir(home) / PLATFORM_GENERATION_FILENAME


def is_platform_skills_root(root: Path, home: Optional[Path] = None) -> bool:
    """Return whether *root* is the configured profile's platform content root."""

    candidate = Path(root)
    expected = platform_skills_dir(home)
    try:
        return candidate.resolve() == expected.resolve()
    except OSError:
        return candidate.absolute() == expected.absolute()


def state_dir_for_skills_root(root: Path, home: Optional[Path] = None) -> Path:
    """Return the writable state location for a resolved skill content root."""

    root = Path(root)
    if is_platform_skills_root(root, home):
        return platform_skill_state_dir(home)
    return root
