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
from tools.attachment_ledger import OUTCOME_READ, get_outcome, record_outcome  # noqa: E402
from tools.attachments_tool import attachments_tool  # noqa: E402
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

    def test_reading_the_rest_clears_the_truncation_gap(self):
        """Finishing the document must stop the report calling it partial.

        Observed live on 2026-08-05: a 443-line judgment was read in full across
        two calls (1-334, then 335-443) and the coverage report still said
        "not fully included ... extent: ranges=[[1, 443]] total=443 -- gaps:
        oversize_truncated". The ledger knew the whole document had been read
        and told the reader otherwise -- the honesty defect inverted, landing
        exactly when the model did the right thing and continued.
        """
        paths = [str(self.path)]
        handles = make_file_handles(paths)
        with file_grant_scope("oversize-doc", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("oversize-doc")
        ):
            with patch("tools.file_tools._get_max_read_chars", return_value=_BUDGET):
                first = json.loads(read_file_tool("F01", task_id="oversize-doc"))
                mid = json.loads(attachments_tool("oversize-doc"))
                self.assertEqual(mid["files"][0]["status"], "partial",
                                 "a half-read document is honestly partial")
                self.assertEqual(
                    get_outcome(str(self.path), task_id="oversize-doc")["ranges"],
                    [[1, first["next_offset"] - 1]],
                    "ledger must record only bytes shown to the model, not the "
                    "larger pre-truncation request range",
                )
                offset = first["next_offset"]
                while True:
                    nxt = json.loads(read_file_tool("F01", task_id="oversize-doc",
                                                    offset=offset))
                    if not nxt.get("truncated"):
                        break
                    offset = nxt["next_offset"]
            final = json.loads(attachments_tool("oversize-doc"))
        entry = final["files"][0]
        self.assertEqual(entry["status"], OUTCOME_READ,
                         "a fully read document must not stay partial: %s" % entry)
        self.assertNotIn("oversize_truncated", entry.get("gaps") or [])

    def test_a_format_gap_survives_full_coverage(self):
        """Covering every line must NOT clear a gap the lines cannot answer.

        The truncation fix upgrades a document to `read` once the ranges cover
        the total. That is only correct for gaps the ranges actually answer.
        A format gap -- body-only MSG extraction, a missing sheet -- is still
        real when every line has been read, and silently upgrading it would
        turn this honesty fix into a new way to overclaim coverage.
        """
        paths = [str(self.path)]
        handles = make_file_handles(paths)
        with file_grant_scope("fmt-gap", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("fmt-gap")
        ):
            record_outcome(
                str(self.path), task_id="fmt-gap", status="partial",
                reason="body_only", gaps=["body_only_extraction"],
                extent={"kind": "lines", "start": 1, "end": 30, "total": 60},
            )
            record_outcome(
                str(self.path), task_id="fmt-gap", status=OUTCOME_READ,
                extent={"kind": "lines", "start": 31, "end": 60, "total": 60},
            )
            entry = json.loads(attachments_tool("fmt-gap"))["files"][0]
        self.assertEqual(entry["status"], "partial",
                         "a format gap is not answered by reading more lines: %s" % entry)
        self.assertIn("body_only_extraction", entry.get("gaps") or [])

    def test_a_successful_read_that_does_not_reach_the_end_stays_partial(self):
        """Progress is not completion.

        Guards the status decision directly: a truncated read followed by a
        SUCCESSFUL read that still stops short of the last line must leave the
        document partial. Without the coverage check, "the last read went fine"
        would be enough to claim full coverage of a judgment.
        """
        paths = [str(self.path)]
        handles = make_file_handles(paths)
        with file_grant_scope("partial-2", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("partial-2")
        ):
            record_outcome(
                str(self.path), task_id="partial-2", status="partial",
                reason="char_limit", gaps=["oversize_truncated"],
                extent={"kind": "lines", "start": 1, "end": 30, "total": 60},
            )
            record_outcome(  # clean read, but only up to line 45 of 60
                str(self.path), task_id="partial-2", status=OUTCOME_READ,
                extent={"kind": "lines", "start": 31, "end": 45, "total": 60},
            )
            entry = json.loads(attachments_tool("partial-2"))["files"][0]
        self.assertEqual(entry["status"], "partial",
                         "lines 46-60 were never read: %s" % entry)


if __name__ == "__main__":
    unittest.main()
