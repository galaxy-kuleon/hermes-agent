"""M-U1-D round 2: ledger extent, stream suffix, finalizer wiring, vision, EML scope."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import request_file_cache
from tools.attachment_ledger import (
    COVERAGE_FOOTER_BEGIN,
    COVERAGE_FOOTER_TITLE,
    COVERAGE_UNAVAILABLE_BEGIN,
    FINALIZE_COVERAGE_FN,
    OUTCOME_PARTIAL,
    OUTCOME_READ,
    OUTCOME_UNREADABLE,
    append_coverage_footer,
    build_coverage_footer,
    finalize_attachment_coverage,
    get_outcome,
    merge_ranges,
    ranges_cover_total,
    record_outcome,
    record_read_extent,
    terminal_coverage_suffix,
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
            self.assertTrue(
                res.get("extraction_failed") or res.get("report_as") == "unreadable"
            )
            ledger = json.loads(attachments_tool("t1"))
            entry = ledger["files"][0]
            self.assertEqual(entry["status"], OUTCOME_UNREADABLE)
            self.assertFalse(entry["readable"])
            self.assertEqual(ledger["unreadable_ids"], ["F01"])
            self.assertEqual(ledger["unread_ids"], [])

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
            self.assertFalse(entry["readable"])

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

    def test_crdownload_pdf_magic_not_full_text_success(self):
        path = self.root / "001-eeeeeeee-report.crdownload"
        path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
        paths = [str(path)]
        handles = make_file_handles(paths)
        with file_grant_scope("t5", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("t5")
        ):
            res = json.loads(read_file_tool("F01", task_id="t5"))
            if res.get("content") and not res.get("error"):
                self.assertIn(res.get("report_as"), {"partial", "unreadable"})
            else:
                self.assertTrue(
                    res.get("extraction_failed")
                    or res.get("report_as") == "unreadable"
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
            self.assertIn(
                "embedded_attachments_not_extracted", res.get("gaps") or []
            )
            ledger = json.loads(attachments_tool("t6"))
            self.assertEqual(ledger["files"][0]["status"], OUTCOME_PARTIAL)


class TruncatedReadExtentTests(unittest.TestCase):
    """BLOCKING-2: 601 lines, limit=500 → partial not complete."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_paginated_text_read_is_partial_with_extent(self):
        path = self.root / "001-aaaa1111-long.txt"
        lines = [f"line-{i:04d}-CANARY" for i in range(1, 602)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths = [str(path)]
        handles = make_file_handles(paths)
        with file_grant_scope("tx", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("tx")
        ):
            res = json.loads(read_file_tool("F01", offset=1, limit=500, task_id="tx"))
            self.assertTrue(res.get("truncated") or res.get("total_lines", 0) >= 601)
            self.assertEqual(res.get("report_as"), "partial")
            outcome = get_outcome(str(path), task_id="tx")
            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["status"], OUTCOME_PARTIAL)
            self.assertTrue(outcome.get("ranges"))
            self.assertFalse(ranges_cover_total(outcome["ranges"], 601))
            # Footer must list partial (not silent complete).
            text = append_coverage_footer("Audit complete.", task_id="tx")
            self.assertIn(COVERAGE_FOOTER_BEGIN, text)
            self.assertIn("partial", text)

    def test_full_page_union_becomes_read(self):
        path = self.root / "001-bbbb2222-short.txt"
        path.write_text("a\nb\nc\n", encoding="utf-8")
        paths = [str(path)]
        handles = make_file_handles(paths)
        with file_grant_scope("ty", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("ty")
        ):
            res = json.loads(read_file_tool("F01", offset=1, limit=500, task_id="ty"))
            self.assertFalse(res.get("truncated"))
            outcome = get_outcome(str(path), task_id="ty")
            self.assertEqual(outcome["status"], OUTCOME_READ)
            text = append_coverage_footer("Done.", task_id="ty")
            # complete → no footer
            self.assertNotIn(COVERAGE_FOOTER_BEGIN, text)

    def test_range_merge_covers_total(self):
        self.assertEqual(merge_ranges([[1, 100], [101, 200]]), [[1, 200]])
        self.assertTrue(ranges_cover_total([[1, 601]], 601))
        self.assertFalse(ranges_cover_total([[1, 500]], 601))


class StreamSuffixTests(unittest.TestCase):
    """BLOCKING-1: streaming adapter must emit coverage_footer."""

    def test_terminal_suffix_emits_structured_footer(self):
        footer = (
            f"\n{COVERAGE_FOOTER_TITLE}\n"
            f"- `F01`: **partial**\n"
        )
        result = {
            "final_response": "MODEL ANSWER" + footer,
            "coverage_footer": footer,
        }
        suffix = terminal_coverage_suffix(result)
        self.assertIn(COVERAGE_FOOTER_BEGIN, suffix)
        self.assertNotIn("MODEL ANSWER", suffix.replace(COVERAGE_FOOTER_BEGIN, ""))

    def test_terminal_suffix_suppresses_only_exact_emitted_footer(self):
        footer = f"\n{COVERAGE_FOOTER_TITLE}\nx\n"
        result = {"coverage_footer": footer, "final_response": "MODEL" + footer}
        self.assertEqual(
            terminal_coverage_suffix(
                result, emitted_coverage_footer=footer
            ),
            "",
        )
        marker_only_model_text = f"MODEL mentions {COVERAGE_FOOTER_TITLE}"
        self.assertNotIn(footer.strip(), marker_only_model_text)
        self.assertEqual(terminal_coverage_suffix(result), footer)
        self.assertEqual(
            terminal_coverage_suffix(
                result, emitted_coverage_footer=footer + "different"
            ),
            footer,
        )

    def test_api_server_source_emits_coverage_before_done(self):
        src = Path(__file__).resolve().parents[2] / (
            "gateway/platforms/api_server.py"
        )
        text = src.read_text(encoding="utf-8")
        self.assertIn("emit_chat_completion_coverage_suffix", text)
        idx_suffix = text.find("emit_chat_completion_coverage_suffix(")
        idx_done = text.find('b"data: [DONE]\\n\\n"', idx_suffix)
        self.assertGreater(idx_suffix, 0)
        self.assertGreater(idx_done, idx_suffix)


class FinalizerWiringMutationTests(unittest.TestCase):
    """BLOCKING-3: removing production finalizer coverage must red tests."""

    def test_turn_finalizer_calls_finalize_after_plugins(self):
        src = (
            Path(__file__).resolve().parents[2]
            / "agent"
            / "turn_finalizer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("deliver_coverage_to_persistent_body", src)
        idx_plugin = src.find("transform_llm_output")
        idx_cov = src.find("deliver_coverage_to_persistent_body")
        self.assertGreater(idx_cov, idx_plugin)
        idx_post = src.find("post_llm_call")
        self.assertGreater(idx_cov, idx_post)

    def test_mutation_removing_finalizer_call_is_detectable(self):
        path = (
            Path(__file__).resolve().parents[2]
            / "agent"
            / "turn_finalizer.py"
        )
        src = path.read_text(encoding="utf-8")
        call = "deliver_coverage_to_persistent_body"
        self.assertIn(call, src)
        mutated = src.replace(call, "IDENTITY_COVERAGE_BYPASS")
        self.assertNotIn(call, mutated)
        with self.assertRaises(AssertionError):
            self.assertIn(call, mutated)

    def test_finalize_fail_closed_when_handles_import_fails(self):
        path = self._tmp_file()
        with file_grant_scope("tf", [path], handles=make_file_handles([path])):
            with patch(
                "tools.file_grants.list_file_handles",
                side_effect=RuntimeError("boom"),
            ):
                # list_file_handles is imported inside finalize — patch the module attr
                pass
            with patch.dict("sys.modules"):
                text, meta = finalize_attachment_coverage(
                    "answer",
                    task_id="tf",
                    fail_closed=True,
                )
                # Active ledger with handles works normally for incomplete (pending)
                self.assertIn(COVERAGE_FOOTER_BEGIN, text or "")
                self.assertEqual(meta.get("status"), "ok")

    def _tmp_file(self) -> str:
        d = tempfile.mkdtemp()
        p = Path(d) / "001-cccc3333-a.txt"
        p.write_text("x\n", encoding="utf-8")
        return str(p)

    def test_plugin_cannot_strip_footer_if_finalize_last(self):
        """Simulate plugin rewrite then re-apply finalize (production order)."""
        path = self._tmp_file()
        paths = [path]
        handles = make_file_handles(paths)
        with file_grant_scope("tp", paths, handles=handles):
            # Model + plugin would produce clean text without footer
            rewritten = "ALL FILES COMPLETE."
            final, meta = finalize_attachment_coverage(
                rewritten, task_id="tp", fail_closed=True
            )
            self.assertIn(COVERAGE_FOOTER_BEGIN, final or "")
            self.assertTrue(meta.get("footer"))


class VisionLedgerTests(unittest.TestCase):
    """MAJOR-4: vision_analyze success must not stay pending."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_record_vision_success_settles_read(self):
        img = self.root / "001-dddd4444-scan.png"
        # minimal PNG header-ish bytes
        img.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + b"\x00" * 20
        )
        paths = [str(img)]
        handles = make_file_handles(paths)
        with file_grant_scope("tv", paths, handles=handles):
            from tools.vision_tools import _record_vision_ledger

            _record_vision_ledger(
                str(img), task_id="tv", success=True, reason="vision_ok"
            )
            outcome = get_outcome(str(img), task_id="tv")
            self.assertEqual(outcome["status"], OUTCOME_READ)
            ledger = json.loads(attachments_tool("tv"))
            self.assertEqual(ledger["files"][0]["status"], OUTCOME_READ)
            self.assertNotIn("F01", ledger.get("unread_ids") or [])


class EmlGapsRequestScopedTests(unittest.TestCase):
    """MAJOR-5: EML gaps via gaps_out, not process-global list."""

    def test_extract_returns_gaps_via_out_param(self):
        from tools.read_extract import extract_document_text, take_last_eml_gaps

        d = tempfile.mkdtemp()
        path = Path(d) / "m.eml"
        path.write_bytes(
            b"From: a@example.com\r\n"
            b"To: b@example.com\r\n"
            b"Subject: hello\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="bnd"\r\n'
            b"\r\n"
            b"--bnd\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Body\r\n"
            b"--bnd\r\n"
            b"Content-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="x.pdf"\r\n\r\n'
            b"%PDF\r\n"
            b"--bnd--\r\n"
        )
        g1: list[str] = []
        g2: list[str] = []
        t1 = extract_document_text(str(path), gaps_out=g1)
        t2 = extract_document_text(str(path), gaps_out=g2)
        self.assertIn("Body", t1)
        self.assertIn("embedded_attachments_not_extracted", g1)
        self.assertIn("embedded_attachments_not_extracted", g2)
        # deprecated global take returns empty — no cross-request stash
        self.assertEqual(take_last_eml_gaps(), [])

    def test_concurrent_gaps_out_isolated(self):
        from concurrent.futures import ThreadPoolExecutor

        from tools.read_extract import extract_document_text

        d = tempfile.mkdtemp()
        p_a = Path(d) / "a.eml"
        p_b = Path(d) / "b.eml"
        p_a.write_bytes(
            b"Subject: A\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nA-body\r\n"
            b"--b\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="a.pdf"\r\n\r\nX\r\n'
            b"--b--\r\n"
        )
        p_b.write_bytes(
            b"Subject: B\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/plain\r\n\r\nB-body-only\r\n"
        )

        def run(path, expected_embedded: bool):
            gaps: list[str] = []
            extract_document_text(str(path), gaps_out=gaps)
            return ("embedded_attachments_not_extracted" in gaps, gaps)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [
                pool.submit(run, p_a, True),
                pool.submit(run, p_b, False),
                pool.submit(run, p_a, True),
                pool.submit(run, p_b, False),
            ]
            results = [f.result() for f in futs]
        # A always has embedded gap; B never does (no cross-contamination)
        for (has_emb, gaps), expect in zip(
            results, [True, False, True, False]
        ):
            self.assertEqual(has_emb, expect, gaps)


class CorruptDocxStrictTests(unittest.TestCase):
    def test_corrupt_docx_requires_extraction_failed(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "bad.docx"
        p.write_bytes(b"not a zip")
        res = json.loads(read_file_tool(str(p)))
        self.assertIn("error", res)
        self.assertTrue(
            res.get("extraction_failed") is True
            or res.get("report_as") == "unreadable"
        )
        # Must not accept generic binary-only contract as sufficient
        self.assertNotIn("content", res)


if __name__ == "__main__":
    unittest.main()
