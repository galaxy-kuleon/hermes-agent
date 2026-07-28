"""The `attachments` ledger.

Chat f248f12e was given 46 files, read 18, re-read five of them 10-14 times,
never touched the other 28, and reported none of that. The model had no way to
enumerate its own attachments, so coverage was something it had to remember
rather than something it could look up.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools import request_file_cache
from tools.attachments_tool import attachments_tool
from tools.file_grants import file_grant_scope, make_file_handles
from tools.file_tools import read_file_tool


class AttachmentsLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.paths = []
        for index in (1, 2, 3):
            path = root / f"00{index}-da36574d-letter{index}.txt"
            path.write_text(f"body {index}\n", encoding="utf-8")
            self.paths.append(str(path))
        self.handles = make_file_handles(self.paths)
        self.addCleanup(self._tmp.cleanup)

    def _ledger(self, task_id="task-1"):
        return json.loads(attachments_tool(task_id))

    def test_reports_every_attachment_before_anything_is_read(self):
        with file_grant_scope("task-1", self.paths, handles=self.handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            ledger = self._ledger()
        self.assertEqual(ledger["total"], 3)
        self.assertEqual(ledger["read"], 0)
        self.assertEqual(ledger["unread_ids"], ["F01", "F02", "F03"])
        self.assertEqual(
            [f["name"] for f in ledger["files"]],
            ["letter1.txt", "letter2.txt", "letter3.txt"],
        )

    def test_read_progress_is_tracked_per_file(self):
        with file_grant_scope("task-1", self.paths, handles=self.handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            read_file_tool("F01", task_id="task-1")
            read_file_tool("F03", task_id="task-1")
            ledger = self._ledger()
        self.assertEqual(ledger["read"], 2)
        self.assertEqual(ledger["unread_ids"], ["F02"])
        by_id = {f["id"]: f for f in ledger["files"]}
        self.assertTrue(by_id["F01"]["read"])
        self.assertFalse(by_id["F02"]["read"])
        self.assertIn("chars", by_id["F01"])

    def test_incomplete_coverage_says_so_in_actionable_terms(self):
        # The single fact chat f248f12e never surfaced: 28 files untouched.
        with file_grant_scope("task-1", self.paths, handles=self.handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            read_file_tool("F01", task_id="task-1")
            ledger = self._ledger()
        self.assertIn("have not been read", ledger["note"])
        self.assertIn("F02", ledger["note"])
        self.assertIn("do not claim full coverage", ledger["note"].lower())

    def test_full_coverage_is_stated_positively(self):
        with file_grant_scope("task-1", self.paths, handles=self.handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            for handle in self.handles:
                read_file_tool(handle, task_id="task-1")
            ledger = self._ledger()
        self.assertEqual(ledger["unread"], 0)
        self.assertEqual(ledger["unread_ids"], [])
        self.assertIn("Every attached file has been read", ledger["note"])

    def test_no_attachments_tells_the_model_not_to_guess(self):
        ledger = self._ledger("task-with-nothing")
        self.assertEqual(ledger["total"], 0)
        self.assertEqual(ledger["files"], [])
        self.assertIn("Do not guess", ledger["note"])

    def test_ledger_never_exposes_paths(self):
        # The model is addressed in handles; leaking the signed path back would
        # reintroduce exactly the string it kept transcribing wrongly.
        with file_grant_scope("task-1", self.paths, handles=self.handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            raw = attachments_tool("task-1")
        for path in self.paths:
            self.assertNotIn(path, raw)
        self.assertNotIn("/handoff", raw)

    def test_ledger_is_scoped_to_its_own_request(self):
        with file_grant_scope("task-1", self.paths, handles=self.handles):
            self.assertEqual(self._ledger("task-2")["total"], 0)

    def test_works_without_a_read_cache_scope(self):
        # Grants without a cache scope (CLI) still list files; read state is
        # simply unknown rather than an error.
        with file_grant_scope("task-1", self.paths, handles=self.handles):
            ledger = self._ledger()
        self.assertEqual(ledger["total"], 3)
        self.assertEqual(ledger["read"], 0)


if __name__ == "__main__":
    unittest.main()
