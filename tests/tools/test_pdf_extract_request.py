#!/usr/bin/env python3
"""
Tests for the request pdf_extract actually sends to docling.

The existing PDF tests in test_read_extract.py mock ``extract_pdf_text``
wholesale, so nothing observed the multipart body or the timeout -- which is
exactly where two defects lived until 2026-08-05, both found in a real 8083
user's chat (a lawyer asking whether their defences held):

1. docling defaults to ``image_export_mode=embedded``. On the 40-page scanned
   letter of claim in that chat, the markdown came back as 40,522,865
   characters of which 40,500,156 (99.94%) were base64 page images and 23,059
   were the text. 40 MB moved to deliver 23 KB, into an agent's context.
2. A flat 60s timeout against a size-independent gate. That same file needed
   54.4s measured, and the 25 MB byte gate rejected it outright before docling
   was ever asked -- so the agent was told the claim it was defending against
   was "unreadable".

Run with:  python -m pytest tests/tools/test_pdf_extract_request.py -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools import pdf_extract  # noqa: E402
from tools.read_extract import ExtractionError  # noqa: E402


class PdfExtractRequestTests(unittest.TestCase):
    def test_multipart_body_asks_docling_not_to_inline_page_images(self):
        body = pdf_extract._build_multipart_body("claim.pdf", b"%PDF-1.4 bytes")
        self.assertIn(b'name="image_export_mode"', body)
        self.assertIn(b"placeholder", body)
        self.assertNotIn(b"embedded", body)
        # the file part must survive alongside the new field
        self.assertIn(b'name="files"; filename="claim.pdf"', body)
        self.assertIn(b"%PDF-1.4 bytes", body)

    def test_timeout_grows_with_the_file_and_stays_bounded(self):
        small = pdf_extract._timeout_for(1 * 1024 * 1024)
        real_scan = pdf_extract._timeout_for(82_890_519)  # the actual letter
        absurd = pdf_extract._timeout_for(10 * 1024 * 1024 * 1024)
        self.assertGreaterEqual(small, pdf_extract._DOCLING_TIMEOUT_BASE_SECONDS)
        self.assertGreater(real_scan, 65, "60s failed this exact file at 54.4s + overhead")
        self.assertGreater(real_scan, small)
        self.assertLessEqual(absurd, pdf_extract._DOCLING_TIMEOUT_MAX_SECONDS)

    def test_a_real_sized_scanned_letter_is_not_rejected_before_docling_is_asked(self):
        """82.9 MB of 40 scanned pages is a normal document, not an abuse case."""
        self.assertGreater(pdf_extract._DOCLING_MAX_PDF_BYTES, 82_890_519)

    def test_oversize_still_fails_closed_without_calling_docling(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"%PDF-1.4\n")
            path = fh.name
        try:
            with patch.object(pdf_extract, "_DOCLING_MAX_PDF_BYTES", 4):
                with patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaisesRegex(ExtractionError, "too large"):
                        pdf_extract.extract_pdf_text(path)
                    urlopen.assert_not_called()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
