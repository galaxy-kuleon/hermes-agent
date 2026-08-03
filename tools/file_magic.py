"""Bounded magic/MIME sniff for attachment routing honesty.

Extension policy is a routing *hint*, not a readability fact. A ``.crdownload``
whose bytes are PDF must not fall through to a generic text open that memos
binary garbage as a successful read.
"""

from __future__ import annotations

from pathlib import Path

# Max bytes read for sniff (named constant, not magic).
SNIFF_BYTES = 16

# High-confidence signatures only — keep this list short and fail-closed.
_PDF = b"%PDF"
_CFBF = bytes.fromhex("d0cf11e0a1b11ae1")  # MSG / legacy Office
_ZIP = b"PK\x03\x04"
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIF = b"GIF8"


def sniff_kind(path: str) -> str | None:
    """Return a coarse kind for *path*, or None when unknown/unreadable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return None
    if head.startswith(_PDF):
        return "pdf"
    if head.startswith(_CFBF):
        return "cfbf"
    if head.startswith(_ZIP):
        return "zip"
    if head.startswith(_PNG):
        return "png"
    if head.startswith(_JPEG):
        return "jpeg"
    if head.startswith(_GIF):
        return "gif"
    # Heuristic: many NUL bytes in the first block → treat as binary.
    if head and head.count(0) >= max(2, len(head) // 4):
        return "binary"
    return None


def declared_suffix(path: str) -> str:
    return Path(path).suffix.lower()


def magic_conflicts_with_suffix(path: str) -> tuple[bool, str | None, str]:
    """Return ``(conflict, sniffed_kind, suffix)`` for policy decisions."""
    suffix = declared_suffix(path)
    kind = sniff_kind(path)
    if kind is None:
        return False, None, suffix
    if kind == "pdf" and suffix not in {".pdf"}:
        return True, kind, suffix
    if kind == "cfbf" and suffix not in {".msg", ".doc", ".xls", ".ppt"}:
        return True, kind, suffix
    if kind == "zip" and suffix not in {
        ".zip",
        ".docx",
        ".xlsx",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".jar",
    }:
        # .crdownload of a docx still conflicts; treat as binary/zip mismatch.
        if suffix in {".crdownload", ".part", ".tmp", ""}:
            return True, kind, suffix
    if kind in {"png", "jpeg", "gif"} and suffix not in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    }:
        return True, kind, suffix
    if kind == "binary" and suffix in {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".html",
        ".eml",
        ".log",
    }:
        return True, kind, suffix
    return False, kind, suffix


__all__ = [
    "SNIFF_BYTES",
    "declared_suffix",
    "magic_conflicts_with_suffix",
    "sniff_kind",
]
