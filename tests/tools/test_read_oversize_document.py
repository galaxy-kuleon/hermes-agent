#!/usr/bin/env python3
"""
An oversize EXTRACTED document (.docx and friends) must come back truncated,
never as a bare error.

This is a different code path from the plain-text oversize guard in
test_file_read_guards.py, and it is the one a real user hit. On 2026-08-04 a
lawyer attached a 245-line judgment (a 64 KB .docx) with an elaborate
"exhaustive structured executive summary" prompt. Extraction rendered 102,688
characters -- 2.7% over the 100,000 budget -- and read_file returned only:

    "Read produced 102,688 characters which exceeds the safety limit
     (100,000 chars). Use offset and limit to read a smaller range."

The model made exactly one read_file call, received that, and ended the turn.
No second call with an offset was ever made and the user got no answer.

Run with:  python -m pytest tests/tools/test_read_oversize_document.py -v
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools import request_file_cache  # noqa: E402
from tools.file_grants import file_grant_scope, make_file_handles  # noqa: E402
from tools.file_tools import read_file_tool  # noqa: E402

_BUDGET = 2_000  # small so the fixture stays fast; the behaviour is the same


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
        ))


class OversizeExtractedDocumentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.path = self.root / "001-deadbeef-judgment.docx"
        # Comfortably over _BUDGET once line numbers are prefixed.
        self.paragraphs = [f"Paragraph {i} of the judgment. " + "w" * 60 for i in range(1, 61)]
        _write_docx(self.path, self.paragraphs)

    def _read(self, **kwargs):
        paths = [str(self.path)]
        handles = make_file_handles(paths)
        with file_grant_scope("oversize-doc", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("oversize-doc")
        ):
            with patch("tools.file_tools._get_max_read_chars", return_value=_BUDGET):
                return json.loads(read_file_tool("F01", task_id="oversize-doc", **kwargs))

    def test_the_reader_gets_the_document_not_an_error(self):
        result = self._read()
        self.assertNotIn("error", result)
        self.assertTrue(result.get("content"), "an oversize document must still be readable")
        self.assertLessEqual(len(result["content"]), _BUDGET)
        self.assertTrue(result.get("truncated"))
        self.assertEqual(result.get("truncated_reason"), "char_limit")
        self.assertEqual(result.get("report_as"), "partial")
        # The first paragraph is what a summary prompt needs first.
        self.assertIn("Paragraph 1 of the judgment", result["content"])

    def test_it_says_exactly_where_to_resume(self):
        result = self._read()
        kept_lines = len(result["content"].splitlines())
        self.assertEqual(result["next_offset"], 1 + kept_lines)
        self.assertIn(f"offset={result['next_offset']}", result["hint"])
        self.assertIn("Do not claim full coverage", result["hint"])

    def test_resuming_at_next_offset_continues_with_no_gap_and_no_repeat(self):
        first = self._read()
        nxt = first["next_offset"]
        second = self._read(offset=nxt)
        last_of_first = first["content"].splitlines()[-1]
        first_of_second = second["content"].splitlines()[0]
        self.assertNotEqual(last_of_first, first_of_second, "no repeated line")
        # Line-numbered content: the numbers must be consecutive across the seam.
        self.assertEqual(
            int(first_of_second.split("|", 1)[0]),
            int(last_of_first.split("|", 1)[0]) + 1,
            "no skipped line between the two reads",
        )


if __name__ == "__main__":
    unittest.main()
