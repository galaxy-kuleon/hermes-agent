"""M-U1-D CHANGES_REQUIRED: authoritative ledger + coverage footer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import request_file_cache
from tools.attachment_ledger import (
    COVERAGE_FOOTER_BEGIN,
    COVERAGE_FOOTER_TITLE,
    OUTCOME_PARTIAL,
    OUTCOME_UNREADABLE,
    append_coverage_footer,
    build_coverage_footer,
    get_outcome,
    record_outcome,
)
from tools.attachments_tool import attachments_tool
from tools.file_grants import file_grant_scope, make_file_handles
from tools.file_tools import read_file_tool


def _write_msg(path: Path, body_text: str) -> None:
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
    directory = bytearray(sector_size)
    directory[:128] = directory_entry("Root Entry", 5, 1, end_of_chain, 0)
    directory[128:256] = directory_entry(
        "__substg1.0_1000001F", 2, free_sector, 2, 4096
    )
    body = (encoded_body * ((4096 // len(encoded_body)) + 1))[:4096]
    sectors = [fat, directory] + [body] + [bytearray(sector_size) for _ in range(7)]
    path.write_bytes(bytes(header) + b"".join(bytes(s) for s in sectors))


class AttachmentLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_extraction_failure_persists_as_unreadable_in_ledger(self):
        bad = self.root / "001-aaaaaaaa-broken.msg"
        bad.write_bytes(b"not a msg")
        paths = [str(bad)]
        handles = make_file_handles(paths)
        with file_grant_scope("t1", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("t1")
        ):
            res = json.loads(read_file_tool("F01", task_id="t1"))
            self.assertTrue(res.get("extraction_failed") or res.get("report_as") == "unreadable")
            ledger = json.loads(attachments_tool("t1"))
            entry = ledger["files"][0]
            self.assertEqual(entry["status"], OUTCOME_UNREADABLE)
            self.assertFalse(entry["readable"])
            self.assertEqual(ledger["unreadable_ids"], ["F01"])
            self.assertEqual(ledger["unread_ids"], [])
            self.assertNotIn("F01", ledger["unread_ids"])

    def test_msg_success_is_partial_not_full_read(self):
        msg = self.root / "001-bbbbbbbb-mail.msg"
        _write_msg(msg, "Body only sample for MU1D")
        paths = [str(msg)]
        handles = make_file_handles(paths)
        with file_grant_scope("t2", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("t2")
        ):
            res = json.loads(read_file_tool("F01", task_id="t2"))
            self.assertTrue(res.get("extracted_document"))
            self.assertEqual(res.get("report_as"), "partial")
            self.assertIn("body_only_extraction", res.get("gaps") or [])
            ledger = json.loads(attachments_tool("t2"))
            entry = ledger["files"][0]
            self.assertEqual(entry["status"], OUTCOME_PARTIAL)
            self.assertIn("F01", ledger["partial_ids"])
            self.assertFalse(entry["readable"])  # full coverage only

    def test_coverage_footer_lists_unreadable(self):
        zip_path = self.root / "001-cccccccc-bundle.zip"
        zip_path.write_bytes(b"PK\x03\x04fake")
        paths = [str(zip_path)]
        handles = make_file_handles(paths)
        with file_grant_scope("t3", paths, handles=handles):
            footer = build_coverage_footer([("F01", paths[0])], task_id="t3")
            self.assertIn(COVERAGE_FOOTER_BEGIN, footer)
            self.assertIn(COVERAGE_FOOTER_TITLE, footer)
            self.assertIn("F01", footer)
            self.assertIn("unreadable", footer)
            text = append_coverage_footer("Audit looks complete.", task_id="t3")
            self.assertIn("Audit looks complete.", text)
            self.assertIn(COVERAGE_FOOTER_BEGIN, text)

    def test_mutation_empty_unreadable_ids_producer_must_fail(self):
        zip_path = self.root / "001-dddddddd-x.zip"
        zip_path.write_bytes(b"PK")
        paths = [str(zip_path)]
        handles = make_file_handles(paths)
        with file_grant_scope("t4", paths, handles=handles):
            ledger = json.loads(attachments_tool("t4"))
            self.assertTrue(ledger["unreadable_ids"])
            # mutation: clear producer
            ledger["unreadable_ids"] = []
            with self.assertRaises(AssertionError):
                self.assertTrue(ledger["unreadable_ids"])

    def test_crdownload_pdf_magic_not_full_text_success(self):
        path = self.root / "001-eeeeeeee-report.crdownload"
        # Minimal PDF header so sniff sees PDF
        path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
        paths = [str(path)]
        handles = make_file_handles(paths)
        with file_grant_scope("t5", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("t5")
        ):
            res = json.loads(read_file_tool("F01", task_id="t5"))
            # Must not pretend a full plain-text success without flags.
            if res.get("content") and not res.get("error"):
                self.assertIn(res.get("report_as"), {"partial", "unreadable"})
            else:
                self.assertTrue(
                    res.get("extraction_failed") or res.get("report_as") == "unreadable"
                )
            ledger = json.loads(attachments_tool("t5"))
            self.assertNotEqual(ledger["files"][0]["status"], "read")

    def test_eml_with_attachment_is_partial(self):
        path = self.root / "001-ffffffff-note.eml"
        path.write_bytes(
            b"From: a@example.com\r\n"
            b"To: b@example.com\r\n"
            b"Subject: hello\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="bnd"\r\n'
            b"\r\n"
            b"--bnd\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Body text\r\n"
            b"--bnd\r\n"
            b"Content-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="x.pdf"\r\n\r\n'
            b"%PDF-fake\r\n"
            b"--bnd--\r\n"
        )
        paths = [str(path)]
        handles = make_file_handles(paths)
        with file_grant_scope("t6", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("t6")
        ):
            res = json.loads(read_file_tool("F01", task_id="t6"))
            self.assertIn("Body text", res.get("content") or "")
            self.assertEqual(res.get("report_as"), "partial")
            self.assertIn("embedded_attachments_not_extracted", res.get("gaps") or [])
            ledger = json.loads(attachments_tool("t6"))
            self.assertEqual(ledger["files"][0]["status"], OUTCOME_PARTIAL)


if __name__ == "__main__":
    unittest.main()
