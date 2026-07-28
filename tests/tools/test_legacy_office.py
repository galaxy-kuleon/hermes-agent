"""Legacy `.doc`/`.xls` reading via the soffice sidecar.

A live 31-file trademark audit on 2026-07-28 hit the binary guard on
`legacy-power-of-attorney.doc` and was told to "use vision_analyze for images, or
terminal to inspect binary files" — neither of which applies to a Word 97
document, and terminal is not available to that caller. Powers of attorney and
older correspondence in these matters are routinely still `.doc`.
"""

import os
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import legacy_office
from tools.read_extract import (
    EXTRACTABLE_EXTENSIONS,
    ExtractionError,
    extract_document_text,
    is_extractable_document,
)

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_bytes(text, path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}"><w:body>'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return Path(path).read_bytes()


class LegacyRecognitionTests(unittest.TestCase):
    def test_legacy_formats_are_now_extractable(self):
        self.assertIn(".doc", EXTRACTABLE_EXTENSIONS)
        self.assertIn(".xls", EXTRACTABLE_EXTENSIONS)
        self.assertTrue(is_extractable_document("legacy-power-of-attorney.doc"))
        self.assertTrue(is_extractable_document("ledger.XLS"))

    def test_ppt_is_deliberately_not_claimed(self):
        # There is no PPTX extractor, so converting .ppt would swap one
        # unreadable file for another and lose the honest error.
        self.assertNotIn(".ppt", EXTRACTABLE_EXTENSIONS)
        self.assertEqual(legacy_office.legacy_target_extension("deck.ppt"), "")

    def test_target_mapping(self):
        self.assertEqual(legacy_office.legacy_target_extension("a.doc"), "docx")
        self.assertEqual(legacy_office.legacy_target_extension("a.XLS"), "xlsx")
        self.assertEqual(legacy_office.legacy_target_extension("a.pdf"), "")


class LegacyConversionTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.doc = self.root / "legacy-power-of-attorney.doc"
        self.doc.write_bytes(b"\xd0\xcf\x11\xe0legacy word payload")
        self.addCleanup(self._tmp.cleanup)

    def _fake_response(self, payload, status=200):
        class R:
            content = payload

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

        return R()

    def test_doc_is_converted_then_extracted(self):
        converted = _docx_bytes("Power of Attorney", self.root / "src.docx")
        captured = {}

        def fake_post(url, params=None, files=None, timeout=None):
            captured.update(url=url, params=params, timeout=timeout)
            return self._fake_response(converted)

        with patch("requests.post", fake_post):
            text = extract_document_text(str(self.doc))

        self.assertIn("Power of Attorney", text)
        self.assertTrue(captured["url"].endswith("/convert"))
        self.assertEqual(captured["params"], {"to": "docx"})
        self.assertEqual(
            captured["timeout"], legacy_office.DEFAULT_SOFFICE_TIMEOUT_SECONDS
        )

    def test_temp_file_is_removed_even_on_extraction_failure(self):
        before = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("hermes-legacy-*"))

        def fake_post(url, params=None, files=None, timeout=None):
            return self._fake_response(b"not a zip at all")

        with patch("requests.post", fake_post):
            with self.assertRaises(ExtractionError):
                extract_document_text(str(self.doc))

        after = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("hermes-legacy-*"))
        self.assertEqual(after - before, set())

    def test_sidecar_failure_degrades_to_extraction_error(self):
        # A soffice outage must land on the caller's existing fallback, not a
        # new failure mode.
        def fake_post(url, params=None, files=None, timeout=None):
            raise OSError("connection refused")

        with patch("requests.post", fake_post):
            with self.assertRaises(ExtractionError):
                extract_document_text(str(self.doc))

    def test_empty_conversion_is_an_error_not_empty_text(self):
        def fake_post(url, params=None, files=None, timeout=None):
            return self._fake_response(b"")

        with patch("requests.post", fake_post):
            with self.assertRaises(ExtractionError):
                extract_document_text(str(self.doc))

    def test_oversized_legacy_document_is_refused_before_any_request(self):
        called = []

        def fake_post(*a, **k):
            called.append(1)
            raise AssertionError("must not reach the sidecar")

        with patch.dict(os.environ, {legacy_office.MAX_LEGACY_BYTES_ENV: "8"}), patch(
            "requests.post", fake_post
        ):
            with self.assertRaises(ExtractionError):
                extract_document_text(str(self.doc))
        self.assertEqual(called, [])

    def test_soffice_url_and_timeout_are_configurable(self):
        converted = _docx_bytes("configured", self.root / "src2.docx")
        captured = {}

        def fake_post(url, params=None, files=None, timeout=None):
            captured.update(url=url, timeout=timeout)
            return self._fake_response(converted)

        with patch.dict(
            os.environ,
            {
                legacy_office.SOFFICE_URL_ENV: "http://soffice-alt:9999/",
                legacy_office.SOFFICE_TIMEOUT_ENV: "7",
            },
        ), patch("requests.post", fake_post):
            extract_document_text(str(self.doc))

        self.assertEqual(captured["url"], "http://soffice-alt:9999/convert")
        self.assertEqual(captured["timeout"], 7)

    def test_garbage_env_values_fall_back_to_defaults(self):
        converted = _docx_bytes("defaults", self.root / "src3.docx")
        captured = {}

        def fake_post(url, params=None, files=None, timeout=None):
            captured.update(timeout=timeout)
            return self._fake_response(converted)

        with patch.dict(
            os.environ,
            {legacy_office.SOFFICE_TIMEOUT_ENV: "not-a-number"},
        ), patch("requests.post", fake_post):
            extract_document_text(str(self.doc))
        self.assertEqual(
            captured["timeout"], legacy_office.DEFAULT_SOFFICE_TIMEOUT_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
