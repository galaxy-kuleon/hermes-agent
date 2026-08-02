"""Short attached-file handles, and the per-request repeated-read memo.

Both exist because of one live incident: a 46-file audit on 8083 issued 96
`read_file` calls that resolved to 18 distinct files, re-read the first five
10-14 times each, addressed three files by paths that never existed, and
produced no report. See `.debug/issues/2026-07-28-file-audit-slop/STATE.md`.
"""

import json
import unittest

from tools.file_grants import (
    file_grant_error,
    file_grant_scope,
    make_file_handles,
    resolve_file_grant,
    resolve_grant_alias,
)
from tools import request_file_cache


class FileHandleResolutionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.granted = root / "001-da36574d-report.pdf"
        self.granted.write_text("granted contents\n", encoding="utf-8")
        self.other = root / "002-b485f245-secret.pdf"
        self.other.write_text("other contents\n", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_handle_resolves_to_its_granted_path(self):
        handles = make_file_handles([str(self.granted)])
        self.assertEqual(list(handles), ["F01"])
        with file_grant_scope("task-1", [str(self.granted)], handles=handles):
            resolved = resolve_grant_alias("F01", task_id="task-1")
            self.assertEqual(resolved, str(self.granted.resolve()))
            self.assertIsNone(
                file_grant_error("F01", task_id="task-1", operation="read")
            )

    def test_handle_is_case_insensitive_and_accepts_hash_prefix(self):
        handles = make_file_handles([str(self.granted)])
        with file_grant_scope("task-1", [str(self.granted)], handles=handles):
            for spelling in ("F01", "f01", "#F01", " #f01 "):
                self.assertEqual(
                    resolve_grant_alias(spelling, task_id="task-1"),
                    str(self.granted.resolve()),
                    spelling,
                )

    def test_unknown_handle_is_denied_not_silently_allowed(self):
        handles = make_file_handles([str(self.granted)])
        with file_grant_scope("task-1", [str(self.granted)], handles=handles):
            denial = file_grant_error("F99", task_id="task-1", operation="read")
        self.assertIsNotNone(denial)
        self.assertIn("F99", denial)

    def test_denial_lists_the_valid_handles(self):
        # The old message told the model to "use an exact path supplied in this
        # request", which it could not do — the paths were what it kept getting
        # wrong. An actionable denial names the handles it may use.
        handles = make_file_handles([str(self.granted), str(self.other)])
        with file_grant_scope(
            "task-1", [str(self.granted), str(self.other)], handles=handles
        ):
            denial = file_grant_error(
                "/handoff/user/x/chat/y/message/z/001-invented.pdf",
                task_id="task-1",
                operation="read",
            )
        self.assertIsNotNone(denial)
        self.assertIn("F01", denial)
        self.assertIn("F02", denial)

    def test_handle_pointing_outside_the_grant_list_is_still_denied(self):
        # An alias must not become a second, weaker authorization path.
        with file_grant_scope(
            "task-1",
            [str(self.granted)],
            handles={"F01": str(self.other)},
        ):
            canonical, denial = resolve_file_grant(
                "F01", task_id="task-1", operation="read"
            )
        self.assertIsNone(canonical)
        self.assertIsNotNone(denial)

    def test_handles_do_not_leak_across_tasks(self):
        handles = make_file_handles([str(self.granted)])
        with file_grant_scope("task-1", [str(self.granted)], handles=handles):
            self.assertEqual(
                resolve_grant_alias("F01", task_id="task-2"), "F01"
            )

    def test_real_paths_are_never_mistaken_for_handles(self):
        with file_grant_scope("task-1", [str(self.granted)], handles={"F01": "/tmp/x"}):
            self.assertEqual(
                resolve_grant_alias(str(self.granted), task_id="task-1"),
                str(self.granted),
            )

    def test_scope_exit_clears_aliases(self):
        handles = make_file_handles([str(self.granted)])
        with file_grant_scope("task-1", [str(self.granted)], handles=handles):
            pass
        self.assertEqual(resolve_grant_alias("F01", task_id="task-1"), "F01")


class ReadFileHandleAndCacheTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "001-da36574d-letter.txt"
        self.path.write_text("line one\nline two\nline three\n", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _read(self, arg, task_id="task-1"):
        from tools.file_tools import read_file_tool

        return json.loads(read_file_tool(arg, task_id=task_id))

    def test_read_file_accepts_a_handle(self):
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-1", [str(self.path)], handles=handles):
            result = self._read("F01")
        self.assertNotIn("error", result)
        self.assertIn("line two", result["content"])

    def test_second_identical_read_returns_a_memo_not_the_text(self):
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-1", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            first = self._read("F01")
            second = self._read("F01")
            third = self._read(str(self.path))  # same file, spelled as a path

        self.assertIn("line two", first["content"])
        for repeat in (second, third):
            self.assertTrue(repeat.get("already_read"))
            self.assertNotIn("content", repeat)
            self.assertIn("already read", repeat["note"])
        # The memo must identify the file well enough to be actionable.
        self.assertIn("F01", second["note"])

    def test_a_different_offset_is_not_a_repeat(self):
        from tools.file_tools import read_file_tool

        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-1", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            json.loads(read_file_tool("F01", task_id="task-1"))
            paged = json.loads(read_file_tool("F01", offset=2, task_id="task-1"))
        self.assertFalse(paged.get("already_read"))
        self.assertIn("line two", paged["content"])

    def test_memo_does_not_outlive_the_request(self):
        # A later turn may run in a fresh session whose context no longer holds
        # the earlier tool result, so the memo must not survive scope exit.
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-1", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            self._read("F01")
        with file_grant_scope("task-1", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            self.assertIsNone(
                request_file_cache.lookup(
                    str(self.path), 1, 500, task_id="task-1"
                )
            )

    def test_without_a_cache_scope_the_legacy_dedup_still_owns_repeats(self):
        # No request scope (CLI/TUI) must leave the pre-existing per-task
        # (path, offset, limit) dedup in charge, byte-for-byte as before.
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-9", [str(self.path)], handles=handles):
            first = self._read("F01", task_id="task-9")
            second = self._read("F01", task_id="task-9")
        self.assertIn("line two", first["content"])
        self.assertNotIn("already_read", first)
        self.assertTrue(second.get("dedup"))
        self.assertFalse(second.get("content_returned"))
        self.assertNotIn("already_read", second)

    def test_denied_reads_are_never_memoised(self):
        with file_grant_scope("task-1", [str(self.path)]), (
            request_file_cache.request_file_cache_scope("task-1")
        ):
            first = self._read("/etc/hostname")
            second = self._read("/etc/hostname")
        for result in (first, second):
            self.assertIn("error", result)
            self.assertFalse(result.get("already_read"))


class VisionReadRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.image = self.root / "001-da36574d-diagram.png"
        self.image.write_bytes(b"fixture")
        self.addCleanup(self._tmp.cleanup)
        # Default: auto-vision returns None so legacy recovery errors still fire.
        self._vision_patch = patch(
            "tools.file_tools._vision_auto_read_image",
            return_value=None,
        )
        self._vision_patch.start()
        self.addCleanup(self._vision_patch.stop)

    def _read(self, path, task_id):
        from tools.file_tools import read_file_tool

        return json.loads(read_file_tool(path, task_id=task_id))

    def test_image_handle_error_names_the_exact_vision_call(self):
        handles = make_file_handles([str(self.image)])
        with file_grant_scope(
            "vision-handle",
            [str(self.image)],
            handles=handles,
        ):
            result = self._read("F01", "vision-handle")

        self.assertTrue(
            result["error"].endswith(
                'Call vision_analyze(image_url="F01") instead.'
            ),
            result["error"],
        )

    def test_attached_image_absolute_path_recovers_to_its_handle(self):
        handles = make_file_handles([str(self.image)])
        with file_grant_scope(
            "vision-absolute",
            [str(self.image)],
            handles=handles,
        ):
            result = self._read(str(self.image), "vision-absolute")

        self.assertIn(
            'Call vision_analyze(image_url="F01") instead.',
            result["error"],
        )

    def test_unaliased_absolute_image_uses_its_resolved_path(self):
        result = self._read(str(self.image), "vision-cli")

        self.assertIn(
            f'Call vision_analyze(image_url="{self.image.resolve()}") instead.',
            result["error"],
        )

    def test_image_handle_auto_routes_to_vision_when_available(self):
        """issue #50: mistaken read_file on an image should not waste a turn."""
        from unittest.mock import patch

        handles = make_file_handles([str(self.image)])
        with file_grant_scope(
            "vision-auto",
            [str(self.image)],
            handles=handles,
        ), patch(
            "tools.file_tools._vision_auto_read_image",
            return_value={"success": True, "analysis": "diagram shows two boxes"},
        ):
            result = self._read("F01", "vision-auto")

        self.assertTrue(result.get("success"), result)
        self.assertEqual(result.get("routed_from"), "read_file")
        self.assertEqual(result.get("read_with"), "vision_analyze")
        self.assertIn("two boxes", result.get("content", ""))

    def test_non_image_binary_keeps_the_generic_error(self):
        archive = self.root / "bundle.zip"
        archive.write_bytes(b"fixture")

        result = self._read(str(archive), "non-image-binary")

        self.assertIn(
            "Use vision_analyze for images, or terminal to inspect binary files.",
            result["error"],
        )
        self.assertNotIn("Call vision_analyze(", result["error"])

    def test_extractable_document_is_still_read(self):
        notebook = self.root / "notes.ipynb"
        notebook.write_text(
            json.dumps(
                {
                    "cells": [
                        {
                            "cell_type": "markdown",
                            "source": ["extractable marker"],
                        }
                    ],
                    "metadata": {},
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
            ),
            encoding="utf-8",
        )

        result = self._read(str(notebook), "extractable-document")

        self.assertTrue(result["extracted_document"])
        self.assertIn("extractable marker", result["content"])

    def test_unknown_extension_is_still_read_as_text(self):
        unknown = self.root / "notes.future-format"
        unknown.write_text("unknown extension marker\n", encoding="utf-8")

        result = self._read(str(unknown), "unknown-extension")

        self.assertNotIn("error", result)
        self.assertIn("unknown extension marker", result["content"])


if __name__ == "__main__":
    unittest.main()


_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_xml(text):
    return (
        f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}"><w:body>'
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )


class ExtractedDocumentRepeatTests(unittest.TestCase):
    """The branch the legacy dedup never reaches.

    `read_file_tool` returns from the structured-document branch
    (`file_tools.py` ~L1211) before the `(path, offset, limit)` dedup at
    ~L1258. Every extractable type — PDF, DOCX, XLSX, MSG, notebooks — is
    therefore exempt from that dedup, which is why a 46-file legal audit
    re-read the same five documents 10-14 times each and got the full text
    back every single time. The request memo sits ahead of all branches.
    """

    def setUp(self):
        import tempfile
        import zipfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.docx = Path(self._tmp.name) / "001-da36574d-letter.docx"
        with zipfile.ZipFile(self.docx, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", _docx_xml("Registrar of Trade Marks"))
        self.addCleanup(self._tmp.cleanup)

    def _read(self, arg, task_id):
        from tools.file_tools import read_file_tool

        return json.loads(read_file_tool(arg, task_id=task_id))

    def test_legacy_dedup_does_not_cover_extracted_documents(self):
        # Characterisation of the defect, so a future reorder is a visible
        # change rather than a silent one.
        with file_grant_scope("task-doc-legacy", [str(self.docx)]):
            first = self._read(str(self.docx), "task-doc-legacy")
            second = self._read(str(self.docx), "task-doc-legacy")
        self.assertTrue(first.get("extracted_document"))
        self.assertTrue(second.get("extracted_document"))
        self.assertIn("Registrar of Trade Marks", second["content"])
        self.assertFalse(second.get("dedup"))

    def test_request_memo_does_cover_extracted_documents(self):
        handles = make_file_handles([str(self.docx)])
        with file_grant_scope("task-doc-memo", [str(self.docx)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-doc-memo")
        ):
            first = self._read("F01", "task-doc-memo")
            second = self._read("F01", "task-doc-memo")
        self.assertIn("Registrar of Trade Marks", first["content"])
        self.assertTrue(second.get("already_read"))
        self.assertNotIn("content", second)

    def test_five_files_read_round_robin_are_each_read_once(self):
        # The exact live pattern: F01..F05 then F01..F05 again. Interleaving
        # is what let the incident bypass every existing repeat control.
        import zipfile
        from pathlib import Path

        root = Path(self._tmp.name)
        paths = []
        for index in range(1, 6):
            doc = root / f"00{index}-da36574d-doc{index}.docx"
            with zipfile.ZipFile(doc, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", _docx_xml(f"body {index}"))
            paths.append(str(doc))

        handles = make_file_handles(paths)
        with file_grant_scope("task-rr", paths, handles=handles), (
            request_file_cache.request_file_cache_scope("task-rr")
        ):
            first_pass = [self._read(h, "task-rr") for h in handles]
            second_pass = [self._read(h, "task-rr") for h in handles]
            third_pass = [self._read(h, "task-rr") for h in handles]

        self.assertTrue(all("content" in r for r in first_pass))
        for pass_results in (second_pass, third_pass):
            self.assertTrue(all(r.get("already_read") for r in pass_results))
            self.assertFalse(any("content" in r for r in pass_results))


class MemoCompressionInvalidationTests(unittest.TestCase):
    """A memo must never outlive the tool result it points at.

    The memo tells the model "the full text is in the earlier tool result
    above". Mid-request context compression can delete or summarise that
    result, and the model then has no way to recover the content — it would
    have to guess at different pagination arguments, because there is no
    explicit force-reread. Codex's round-2 review demonstrated exactly this
    with a compressed-message reproduction, so it is a correctness gap rather
    than an efficiency tradeoff.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "001-da36574d-long.txt"
        self.path.write_text("unique body marker\n" * 40, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _read(self, arg, task_id="task-c"):
        from tools.file_tools import read_file_tool

        return json.loads(read_file_tool(arg, task_id=task_id))

    def _clear(self, task_id):
        """What _compress_context does: release both suppression layers."""
        from tools.file_tools import forget_task_reads

        return request_file_cache.invalidate(task_id) + forget_task_reads(task_id)

    def test_invalidate_reports_what_it_dropped(self):
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-c", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-c")
        ):
            self._read("F01")
            self.assertEqual(request_file_cache.invalidate("task-c"), 1)
            self.assertEqual(request_file_cache.invalidate("task-c"), 0)

    def test_content_is_available_again_after_invalidation(self):
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-c", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-c")
        ):
            first = self._read("F01")
            memoed = self._read("F01")
            self._clear("task-c")
            after = self._read("F01")

        self.assertIn("unique body marker", first["content"])
        self.assertTrue(memoed.get("already_read"))
        self.assertNotIn("content", memoed)
        # The whole point: once the earlier result may be gone, a repeat read
        # must return the text rather than pointing at something absent.
        self.assertIn("unique body marker", after["content"])
        self.assertFalse(after.get("already_read"))

    def test_invalidation_is_scoped_to_one_task(self):
        handles = make_file_handles([str(self.path)])
        with file_grant_scope("task-c", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-c")
        ), file_grant_scope("task-d", [str(self.path)], handles=handles), (
            request_file_cache.request_file_cache_scope("task-d")
        ):
            self._read("F01", "task-c")
            self._read("F01", "task-d")
            self._clear("task-c")
            self.assertTrue(self._read("F01", "task-d").get("already_read"))
            self.assertIn("unique body marker", self._read("F01", "task-c")["content"])

    def test_invalidate_is_safe_without_a_scope(self):
        self.assertEqual(request_file_cache.invalidate("no-such-task"), 0)
