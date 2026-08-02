"""M-U1-D: honest readability for attachments (readable / unreadable / mixed).

Corpus evidence on 8083 (2026-07-27+): .msg is ~57% of attached filenames and
appears in ~66% of file chats. The extractor already succeeds on live .msg
samples; the disease is false-success — extraction failure fell through to a
raw text open, the request memo marked the file read, and the model produced a
complete-looking audit that never read the mail body.

These tests lock three shapes the model must see, plus a mutation check that
the unreadable label is real mechanism (not a soft comment).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import request_file_cache
from tools.attachments_tool import attachments_tool
from tools.file_grants import file_grant_scope, make_file_handles
from tools.file_reader_routing import UNREADABLE_REPORT_INSTRUCTION
from tools.file_tools import read_file_tool
from tools.read_extract import ExtractionError  # noqa: F401 — used by mutation tests


def _write_msg(path: Path, body_text: str) -> None:
    """Minimal Unicode-body MSG (same geometry as test_read_extract)."""
    import struct

    free_sector = 0xFFFFFFFF
    end_of_chain = 0xFFFFFFFE
    fat_sector = 0xFFFFFFFD
    sector_size = 512

    header = bytearray(sector_size)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHH", header, 24, 0x003E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into(
        "<IIIIIIIII",
        header,
        40,
        0,
        1,
        1,
        0,
        4096,
        end_of_chain,
        0,
        end_of_chain,
        0,
    )
    struct.pack_into("<I", header, 76, 0)
    for offset in range(80, sector_size, 4):
        struct.pack_into("<I", header, offset, free_sector)

    fat = bytearray(b"\xff" * sector_size)
    fat_entries = [fat_sector, end_of_chain] + list(range(3, 10)) + [end_of_chain]
    for index, value in enumerate(fat_entries):
        struct.pack_into("<I", fat, index * 4, value)

    def directory_entry(name, entry_type, child, start_sector, stream_size):
        entry = bytearray(128)
        encoded_name = (name + "\0").encode("utf-16le")
        entry[: len(encoded_name)] = encoded_name
        struct.pack_into(
            "<HBBIII",
            entry,
            64,
            len(encoded_name),
            entry_type,
            1,
            free_sector,
            free_sector,
            child,
        )
        struct.pack_into("<I", entry, 116, start_sector)
        struct.pack_into("<Q", entry, 120, stream_size)
        return entry

    encoded_body = (body_text + "\r\n\0").encode("utf-16le")
    body_start, body_size = 2, 4096
    directory = bytearray(sector_size)
    directory[:128] = directory_entry("Root Entry", 5, 1, end_of_chain, 0)
    directory[128:256] = directory_entry(
        "__substg1.0_1000001F",
        2,
        free_sector,
        body_start,
        body_size,
    )
    body = (encoded_body * ((4096 // len(encoded_body)) + 1))[:4096]
    sectors = [fat, directory] + [body] + [bytearray(sector_size) for _ in range(7)]
    path.write_bytes(bytes(header) + b"".join(bytes(s) for s in sectors))


class AttachmentReadabilityMu1dTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _ledger(self, paths, task_id):
        handles = make_file_handles(paths)
        with file_grant_scope(task_id, paths, handles=handles), (
            request_file_cache.request_file_cache_scope(task_id)
        ):
            return json.loads(attachments_tool(task_id)), handles

    def test_readable_msg_and_text_are_routed_to_read_file(self):
        msg_path = self.root / "001-aaaaaaa1-brief.msg"
        txt_path = self.root / "002-aaaaaaa2-notes.txt"
        _write_msg(msg_path, "Readable MSG body for MU1D")
        txt_path.write_text("plain notes\n", encoding="utf-8")
        paths = [str(msg_path), str(txt_path)]
        ledger, handles = self._ledger(paths, "readable-batch")

        self.assertEqual(ledger["total"], 2)
        self.assertEqual(ledger["unreadable"], 0)
        self.assertEqual(ledger["unreadable_ids"], [])
        for entry in ledger["files"]:
            self.assertEqual(entry["read_with"], "read_file")
            self.assertTrue(entry.get("reader_available", entry.get("read_with") == "read_file"))

        with file_grant_scope("readable-batch", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("readable-batch")
        ):
            msg_result = json.loads(read_file_tool("F01", task_id="readable-batch"))
            txt_result = json.loads(read_file_tool("F02", task_id="readable-batch"))
            after = json.loads(attachments_tool("readable-batch"))

        self.assertTrue(msg_result.get("extracted_document"))
        # MSG body-only extraction is partial, not full coverage.
        self.assertEqual(msg_result.get("report_as"), "partial")
        self.assertIn("Readable MSG body for MU1D", msg_result["content"])
        self.assertIn("plain notes", txt_result["content"])
        self.assertNotIn("error", msg_result)
        self.assertIn("F01", after["partial_ids"])
        self.assertEqual(after["files"][1]["status"], "read")

    def test_unreadable_formats_are_labelled_before_any_read(self):
        # Formats that hit the same gap class: no direct Path-B reader.
        names = ["archive.zip", "mailbox.pst", "deck.pptx", "shortcut.lnk"]
        paths = []
        for index, name in enumerate(names, start=1):
            path = self.root / f"{index:03d}-bbbbbbbb-{name}"
            path.write_bytes(b"not-really-this-format")
            paths.append(str(path))

        ledger, _handles = self._ledger(paths, "unreadable-batch")
        self.assertEqual(ledger["total"], 4)
        self.assertEqual(ledger["unreadable"], 4)
        self.assertEqual(ledger["unread"], 0)  # not "unread" — unreadable
        self.assertEqual(len(ledger["unreadable_ids"]), 4)
        self.assertTrue(UNREADABLE_REPORT_INSTRUCTION.strip())
        self.assertIn(UNREADABLE_REPORT_INSTRUCTION, ledger["note"])
        self.assertIn("unreadable materials", ledger["note"])
        self.assertIn("do not invent", ledger["note"].lower())
        for entry in ledger["files"]:
            self.assertEqual(entry["read_with"], "unsupported")
            self.assertIs(entry["readable"], False)
            self.assertEqual(entry["report_as"], "unreadable")
            self.assertIn("unreadable materials", entry["read_instruction"])
            self.assertIn(UNREADABLE_REPORT_INSTRUCTION, entry["read_instruction"])

    def test_mixed_batch_separates_readable_unread_and_unreadable(self):
        msg_path = self.root / "001-cccccccc-mail.msg"
        zip_path = self.root / "002-cccccccc-bundle.zip"
        txt_path = self.root / "003-cccccccc-memo.txt"
        _write_msg(msg_path, "Mixed-batch MSG body")
        zip_path.write_bytes(b"PK\x03\x04fake")
        txt_path.write_text("memo body\n", encoding="utf-8")
        paths = [str(msg_path), str(zip_path), str(txt_path)]

        ledger, handles = self._ledger(paths, "mixed-batch")
        by_id = {entry["id"]: entry for entry in ledger["files"]}

        self.assertEqual(ledger["total"], 3)
        self.assertEqual(ledger["unreadable"], 1)
        self.assertEqual(ledger["unreadable_ids"], ["F02"])
        self.assertEqual(sorted(ledger["unread_ids"]), ["F01", "F03"])
        self.assertEqual(by_id["F01"]["read_with"], "read_file")
        self.assertTrue(by_id["F01"].get("reader_available", True))
        self.assertIs(by_id["F02"]["readable"], False)
        self.assertEqual(by_id["F03"]["read_with"], "read_file")
        self.assertIn("F02(", ledger["note"])
        self.assertIn(UNREADABLE_REPORT_INSTRUCTION, ledger["note"])

        with file_grant_scope("mixed-batch", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("mixed-batch")
        ):
            ok = json.loads(read_file_tool("F01", task_id="mixed-batch"))
            bad = json.loads(read_file_tool("F02", task_id="mixed-batch"))
            after = json.loads(attachments_tool("mixed-batch"))

        self.assertTrue(ok.get("extracted_document"))
        self.assertIn("Mixed-batch MSG body", ok["content"])
        self.assertIn("error", bad)
        self.assertEqual(after["unread_ids"], ["F03"])
        self.assertEqual(after["unreadable_ids"], ["F02"])
        self.assertIn("have not been read", after["note"])
        self.assertIn(UNREADABLE_REPORT_INSTRUCTION, after["note"])

    def test_msg_extraction_failure_is_honest_error_not_garbage_content(self):
        bad_msg = self.root / "001-dddddddd-broken.msg"
        bad_msg.write_bytes(b"not a cfbf compound file!!!!!")
        paths = [str(bad_msg)]
        handles = make_file_handles(paths)
        with file_grant_scope("msg-fail", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("msg-fail")
        ):
            result = json.loads(read_file_tool("F01", task_id="msg-fail"))
            # Failed extract must NOT be memoised as a successful read.
            ledger = json.loads(attachments_tool("msg-fail"))

        self.assertTrue(result.get("extraction_failed"))
        self.assertIs(result.get("readable"), False)
        self.assertEqual(result.get("report_as"), "unreadable")
        self.assertIn("error", result)
        self.assertNotIn("content", result)
        self.assertTrue(UNREADABLE_REPORT_INSTRUCTION.strip())
        self.assertIn("unreadable materials", result["error"])
        self.assertIn(UNREADABLE_REPORT_INSTRUCTION, result["error"])
        self.assertNotIn("not a cfbf", result.get("content", ""))
        self.assertEqual(ledger["unreadable_ids"], ["F01"])
        self.assertEqual(ledger["unread_ids"], [])
        self.assertEqual(ledger["files"][0]["status"], "unreadable")

    def test_mutation_removing_unreadable_label_must_fail(self):
        """If the shared unreadable report duty disappears, these gates red."""
        self.assertIn("unreadable materials", UNREADABLE_REPORT_INSTRUCTION)
        self.assertIn("do not invent", UNREADABLE_REPORT_INSTRUCTION.lower())

        # Simulate the pre-fix disease: extraction fails but read_file
        # would have fallen through. The constant is what product code embeds;
        # stripping it from the module is the mutation.
        import tools.file_reader_routing as routing

        original = routing.UNREADABLE_REPORT_INSTRUCTION
        try:
            routing.UNREADABLE_REPORT_INSTRUCTION = ""  # mutation
            with patch(
                "tools.read_extract.extract_document_text",
                side_effect=ExtractionError("mutated failure"),
            ):
                path = self.root / "001-eeeeeeee-mut.msg"
                _write_msg(path, "body ignored by mock")
                paths = [str(path)]
                handles = make_file_handles(paths)
                with file_grant_scope("mut", paths, handles=handles):
                    result = json.loads(read_file_tool("F01", task_id="mut"))
            # Empty instruction must drop the report-duty phrase from the error.
            with self.assertRaises(AssertionError):
                self.assertIn("unreadable materials", result["error"])
            with self.assertRaises(AssertionError):
                self.assertTrue(routing.UNREADABLE_REPORT_INSTRUCTION.strip())
        finally:
            routing.UNREADABLE_REPORT_INSTRUCTION = original


if __name__ == "__main__":
    unittest.main()
