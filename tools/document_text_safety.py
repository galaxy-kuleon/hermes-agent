"""Fail-safe cleanup for text produced by document converters.

Document text is allowed into an LLM prompt; embedded binary payloads are not.
Converters must never turn page images into multi-megabyte ``data:`` URLs in
Markdown or HTML. This module removes those URLs while preserving surrounding
text and emits content-free observability about what was removed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

INLINE_IMAGE_DATA_URI_RE = re.compile(
    r"data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=\r\n]+",
    re.IGNORECASE,
)
INLINE_IMAGE_PLACEHOLDER = "data:image/omitted"


def strip_inline_base64_images(text: str, *, source: str = "document") -> str:
    """Remove every inline base64 image URI from converter-produced text.

    The payload is never logged. The warning contains only the source suffix,
    occurrence count, and encoded character count so a request remains fully
    observable without reproducing the context-exhaustion incident in logs.
    """
    if not isinstance(text, str) or "data:image/" not in text.lower():
        return text

    matches = list(INLINE_IMAGE_DATA_URI_RE.finditer(text))
    if not matches:
        return text
    encoded_chars = sum(len(match.group(0)) for match in matches)
    logger.warning(
        "document_inline_base64_removed source=%s occurrences=%d encoded_chars=%d",
        Path(str(source)).suffix.lower() or "document",
        len(matches),
        encoded_chars,
    )
    return INLINE_IMAGE_DATA_URI_RE.sub(INLINE_IMAGE_PLACEHOLDER, text)


__all__ = [
    "INLINE_IMAGE_DATA_URI_RE",
    "INLINE_IMAGE_PLACEHOLDER",
    "strip_inline_base64_images",
]
