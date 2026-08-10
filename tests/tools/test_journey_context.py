#!/usr/bin/env python3
"""The journey must survive delegation, and must never be stale.

Round 10 proved the logging session id could not carry the correlator: a
delegated child agent generates its own id with no user in it, unwrapped async
fan-out never receives one, and a reused worker could inherit a STALE id --
attributing one user's failure to another's journey, which is worse than having
none, because absence is visible and a wrong uid is believed.
"""

import asyncio
import contextvars
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.journey_context import set_journey, journey_suffix  # noqa: E402


class JourneySurvivesEveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        set_journey("0063b5cf-11c5-4c7d-bd14-ccb717f1adcd", "chat-42")
        self.expected = " uid=0063b5cf-11c5-4c7d-bd14-ccb717f1adcd chat=chat-42"

    def test_the_conversation_thread_has_it(self):
        self.assertEqual(journey_suffix(), self.expected)

    def test_a_propagated_worker_has_it(self):
        ctx = contextvars.copy_context()
        got = {}
        t = threading.Thread(target=lambda: got.update(v=ctx.run(journey_suffix)))
        t.start()
        t.join()
        self.assertEqual(got["v"], self.expected)

    def test_an_async_task_has_it(self):
        async def main():
            return await asyncio.create_task(_read())

        async def _read():
            return journey_suffix()

        self.assertEqual(asyncio.run(main()), self.expected)

    def test_an_UNPROPAGATED_thread_is_empty_never_stale(self):
        """The one that matters. A wrong uid is believed; a missing one is not."""
        got = {}
        t = threading.Thread(target=lambda: got.update(v=journey_suffix()))
        t.start()
        t.join()
        self.assertEqual(got["v"], "", "a fresh thread inherited a journey it was never given")

    def test_generating_a_child_session_id_does_not_disturb_it(self):
        """Delegation replaces the LOGGING session; it must not touch this."""
        try:
            from hermes_logging import set_session_context
            set_session_context("20260810_123456_abcdef")
        except Exception:
            self.skipTest("hermes_logging unavailable")
        self.assertEqual(journey_suffix(), self.expected)


if __name__ == "__main__":
    unittest.main()
