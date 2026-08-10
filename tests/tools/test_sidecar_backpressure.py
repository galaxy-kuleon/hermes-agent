#!/usr/bin/env python3
"""
A busy sidecar must not be reported to the user as a broken document.

Both conversion sidecars serve a bounded number of requests at once. This user
attaches ten to twenty-five documents in a single message — that is how a legal
bundle arrives — and both sidecars turned that burst into "unreadable".

Measured 2026-08-08 against the live services:

    soffice, 11 concurrent .doc conversions   ->  2 succeeded, 9 got HTTP 503
                                                  in 0.0s (shed, not queued)
    docling, 4 concurrent 3.2 MB scans        ->  50.2s / 50.2s / 96.3s / 96.3s
                                                  (44.2s of work each, alone)

The 503s reached the transcript as "soffice conversion failed … HTTPError" with
no status code, so a busy sidecar and a crashed one read identically. The
docling queue time was inside the client's timeout budget, so requests that
were never slow failed for being queued.

Run with:  python -m pytest tests/tools/test_sidecar_backpressure.py -v
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools import legacy_office, pdf_extract  # noqa: E402
from tools.read_extract import ExtractionError  # noqa: E402


class _Resp:
    def __init__(self, status, content=b"PK\x03\x04ok"):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            err = Exception(f"HTTP {self.status_code}")
            err.response = self
            raise err


class SofficeBusyIsNotBrokenTests(unittest.TestCase):
    def setUp(self):
        fh = tempfile.NamedTemporaryFile(suffix=".doc", delete=False)
        fh.write(b"\xd0\xcf\x11\xe0legacy word payload")
        fh.close()
        self.path = fh.name
        self.addCleanup(os.unlink, self.path)

    def test_a_503_is_retried_and_then_succeeds(self):
        calls = []

        def fake_post(*a, **kw):
            calls.append(1)
            return _Resp(503) if len(calls) < 3 else _Resp(200)

        with patch.object(legacy_office, "RETRY_BACKOFF_SECONDS", 0):
            with patch("requests.post", side_effect=fake_post):
                temp_path, target = legacy_office.convert_to_ooxml(self.path)
        self.addCleanup(os.unlink, temp_path)
        self.assertEqual(target, "docx")
        self.assertEqual(len(calls), 3, "the two 503s must have been retried")

    def test_a_persistent_503_names_the_status_not_just_HTTPError(self):
        with patch.object(legacy_office, "RETRY_BACKOFF_SECONDS", 0):
            with patch("requests.post", return_value=_Resp(503)):
                with self.assertRaises(ExtractionError) as ctx:
                    legacy_office.convert_to_ooxml(self.path)
        message = str(ctx.exception)
        self.assertIn("503", message,
                      f"a busy sidecar must be distinguishable from a crashed one: {message}")

    def test_the_TERMINAL_503_leaves_a_record(self):
        """The shed that actually costs the user the document.

        The only log sat behind `attempt < RETRY_ATTEMPTS`, so the last 503 --
        the one that returns "unreadable" to the user -- emitted nothing at
        all. Proved by adversarial review 2026-08-10.
        """
        with patch.object(legacy_office, "RETRY_BACKOFF_SECONDS", 0):
            with patch("requests.post", return_value=_Resp(503)):
                with self.assertLogs(legacy_office._log, level="WARNING") as caught:
                    with self.assertRaises(ExtractionError):
                        legacy_office.convert_to_ooxml(self.path)
        text = "\n".join(caught.output)
        self.assertIn("sidecar_failed service=soffice", text)
        self.assertNotIn(os.path.basename(self.path), text,
                         "a client filename must never reach the log")

    def test_an_empty_200_is_a_recorded_failure(self):
        """A 200 with an empty body is terminal, and used to log nothing.

        `outcome=exhausted` could not cover it: the payload is b'', not None.
        Proved by adversarial review round 3, 2026-08-10.
        """
        with patch("requests.post", return_value=_Resp(200, content=b"")):
            with self.assertLogs(legacy_office._log, level="WARNING") as caught:
                with self.assertRaises(ExtractionError):
                    legacy_office.convert_to_ooxml(self.path)
        self.assertIn("outcome=empty_payload", "\n".join(caught.output))

    def test_a_broken_log_handler_cannot_change_what_the_user_gets(self):
        """Observability must never turn ExtractionError into RuntimeError.

        Callers catch ExtractionError only, so an unguarded warning turned a
        logging fault into an unhandled crash.
        """
        with patch.object(legacy_office, "RETRY_BACKOFF_SECONDS", 0):
            with patch("requests.post", return_value=_Resp(503)):
                with patch.object(legacy_office._log, "warning",
                                  side_effect=RuntimeError("handler blew up")):
                    with self.assertRaises(ExtractionError):
                        legacy_office.convert_to_ooxml(self.path)

    def test_a_400_is_not_retried(self):
        """A rejected document is a real answer; retrying it wastes the turn."""
        calls = []

        def fake_post(*a, **kw):
            calls.append(1)
            return _Resp(400)

        with patch.object(legacy_office, "RETRY_BACKOFF_SECONDS", 0):
            with patch("requests.post", side_effect=fake_post):
                with self.assertRaises(ExtractionError):
                    legacy_office.convert_to_ooxml(self.path)
        self.assertEqual(len(calls), 1, "a 400 must not be retried")


class DoclingQueuesOutsideTheTimeoutTests(unittest.TestCase):
    def setUp(self):
        fh = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        fh.write(b"%PDF-1.4\n" + b"0" * 1024)
        fh.close()
        self.path = fh.name
        self.addCleanup(os.unlink, self.path)

    def test_concurrent_extractions_are_capped_to_the_inflight_limit(self):
        """The wait happens on our side, so the timeout measures the conversion.

        Without this, a burst of attachments queues INSIDE docling and every
        queued request spends its timeout budget waiting rather than working.
        """
        inflight = 0
        peak = 0
        lock = threading.Lock()

        class _Ctx:
            def __enter__(self_inner):
                nonlocal inflight, peak
                with lock:
                    inflight += 1
                    peak = max(peak, inflight)
                time.sleep(0.05)
                return self_inner

            def __exit__(self_inner, *a):
                nonlocal inflight
                with lock:
                    inflight -= 1
                return False

            def read(self_inner):
                return b'{"status":"success","document":{"md_content":"text"}}'

        # Bounded wait + daemon threads: if the gate ever leaks a slot, these
        # threads must die quickly instead of parking for the full queue budget
        # and hanging the whole suite at interpreter exit.
        def _run():
            try:
                pdf_extract.extract_pdf_text(self.path)
            except ExtractionError:
                pass

        with patch.object(pdf_extract, "_DOCLING_QUEUE_WAIT_SECONDS", 5.0):
            with patch("urllib.request.urlopen", return_value=_Ctx()):
                threads = [threading.Thread(target=_run, daemon=True) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=30)

        # Deliberately NOT compared against _DOCLING_MAX_INFLIGHT: an earlier
        # version did, so raising that constant moved the assertion with it and
        # the test could not fail. The cap docling actually serves is 2.
        self.assertLessEqual(peak, 2, f"{peak} conversions were in flight at once")
        self.assertGreater(peak, 0, "the fake never ran")

    def test_a_slot_is_released_even_when_the_conversion_fails(self):
        """A failing conversion must not leak its slot and wedge every later read."""
        # Short queue wait so a leak surfaces as a fast failure. Without this
        # the leak makes this test hang for the full 600s budget, and a test
        # that hangs is worse than one that fails.
        with patch.object(pdf_extract, "_DOCLING_QUEUE_WAIT_SECONDS", 1.0):
            with patch("urllib.request.urlopen", side_effect=OSError("boom")):
                for _ in range(pdf_extract._DOCLING_MAX_INFLIGHT + 2):
                    with self.assertRaises(ExtractionError):
                        pdf_extract.extract_pdf_text(self.path)
        self.assertTrue(pdf_extract._docling_slots.acquire(timeout=1),
                        "slots were leaked by the failure path")
        pdf_extract._docling_slots.release()

    def test_valid_json_of_the_wrong_shape_stays_an_ExtractionError(self):
        """read_file's caller catches ExtractionError ONLY.

        A list payload, or a string `document`, raised AttributeError straight
        past that contract and became an unhandled crash instead of an honest
        unreadable-document answer. Proved by adversarial review round 3.
        """
        class _R:
            def __init__(self, body):
                self._b = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._b

        for body in (b'[]', b'{"status":"success","document":"oops"}',
                     b'{"status":"success","document":{"md_content":42}}'):
            with self.subTest(body=body):
                with patch("urllib.request.urlopen", return_value=_R(body)):
                    with self.assertRaises(ExtractionError):
                        pdf_extract.extract_pdf_text(self.path)
        self.assertTrue(pdf_extract._docling_slots.acquire(timeout=1),
                        "a wrong-shape payload leaked the slot")
        pdf_extract._docling_slots.release()

    def test_a_slot_is_released_when_the_LOGGER_fails(self):
        """The old test patched only urlopen, which sits inside the try.

        Everything between a successful acquire and the `try` leaked the slot,
        and a logging handler raising is ordinary Python. Proved by adversarial
        review 2026-08-10 with a mutation probe: acquired=1, released=0. Two
        docling slots leak permanently and every later read waits the full
        queue budget for a slot that will never come back.
        """
        with patch.object(pdf_extract, "_DOCLING_QUEUE_WAIT_SECONDS", 1.0):
            with patch.object(pdf_extract._log, "info",
                              side_effect=RuntimeError("handler blew up")):
                with patch.object(pdf_extract, "time") as fake_time:
                    # Force the >1.0s branch so the logger is reached at all.
                    fake_time.monotonic.side_effect = [0.0, 99.0] * 8
                    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
                        for _ in range(pdf_extract._DOCLING_MAX_INFLIGHT + 2):
                            with self.assertRaises(ExtractionError):
                                pdf_extract.extract_pdf_text(self.path)
        self.assertTrue(pdf_extract._docling_slots.acquire(timeout=1),
                        "a raising log handler leaked the slot")
        pdf_extract._docling_slots.release()

    def test_a_slot_is_released_when_the_CLOCK_fails(self):
        """Same gap, the other statement in it."""
        with patch.object(pdf_extract, "_DOCLING_QUEUE_WAIT_SECONDS", 1.0):
            with patch.object(pdf_extract, "time") as fake_time:
                # First call (before acquire) fine, second raises -- exactly
                # the post-acquire, pre-try window.
                fake_time.monotonic.side_effect = [0.0, RuntimeError("clock")] * 8
                with patch("urllib.request.urlopen", side_effect=OSError("boom")):
                    for _ in range(pdf_extract._DOCLING_MAX_INFLIGHT + 2):
                        with self.assertRaises(Exception):
                            pdf_extract.extract_pdf_text(self.path)
        self.assertTrue(pdf_extract._docling_slots.acquire(timeout=1),
                        "a raising clock leaked the slot")
        pdf_extract._docling_slots.release()


if __name__ == "__main__":
    unittest.main()
