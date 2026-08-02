"""Single source of truth for model-facing file reader routing."""

from __future__ import annotations

import json

from tools.binary_extensions import has_binary_extension, has_image_extension
from tools.read_extract import is_extractable_document


READ_WITH_FILE = "read_file"
READ_WITH_VISION = "vision_analyze"
READ_WITH_UNSUPPORTED = "unsupported"

# Shared with read_file extraction failures and the attachments ledger so the
# model always sees the same obligation: name unreadable materials in the
# final report instead of inventing content (MSG/PDF false-success disease).
UNREADABLE_REPORT_INSTRUCTION = (
    "Treat it as unreadable. Name it under unreadable materials in any "
    "final report; do not invent its content."
)


def reader_route(path: str) -> str:
    """Return the reader route that mirrors ``read_file`` extension gates."""
    if has_image_extension(path):
        return READ_WITH_VISION
    # read_file attempts extractable documents before its binary guard.
    if is_extractable_document(path) or not has_binary_extension(path):
        return READ_WITH_FILE
    return READ_WITH_UNSUPPORTED


def reader_call(read_with: str, target: str) -> str | None:
    """Return a directly callable tool expression for a supported route."""
    argument = json.dumps(str(target), ensure_ascii=False)
    if read_with == READ_WITH_VISION:
        return f"vision_analyze(image_url={argument})"
    if read_with == READ_WITH_FILE:
        return f"read_file({argument})"
    return None


def reader_guidance(target: str, path: str) -> tuple[str, str]:
    """Return the model-facing route and instruction for one file."""
    read_with = reader_route(path)
    call = reader_call(read_with, target)
    if read_with == READ_WITH_VISION:
        return read_with, f"Call {call}. Do not call read_file first."
    if read_with == READ_WITH_FILE:
        return read_with, f"Call {call}."
    return (
        read_with,
        "No direct reader is available for this binary attachment. "
        + UNREADABLE_REPORT_INSTRUCTION,
    )


__all__ = [
    "READ_WITH_FILE",
    "READ_WITH_UNSUPPORTED",
    "READ_WITH_VISION",
    "UNREADABLE_REPORT_INSTRUCTION",
    "reader_call",
    "reader_guidance",
    "reader_route",
]
