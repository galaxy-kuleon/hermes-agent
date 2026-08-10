#!/usr/bin/env python3
"""A correlator that truncates is worse than one that is missing.

The ledger reads uid/chat back as whitespace-delimited `key=value`, so an id
carrying a space truncates to its first word and attaches the failure to a
shorter, wrong id. Reachable on the session-chat route grammar; proved by
adversarial review round 16, 2026-08-10.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.journey_context import set_journey, journey_suffix  # noqa: E402


class IdsSurviveTheLogRecordTests(unittest.TestCase):
    def test_an_id_with_a_space_cannot_truncate_the_correlation(self):
        set_journey("user one", "chat two")
        suffix = journey_suffix()
        self.assertEqual(suffix, " uid=user_one chat=chat_two")
        for field in suffix.split():
            self.assertIn("=", field, f"{field!r} would be read as a stray token")

    def test_ids_with_other_whitespace_too(self):
        set_journey("a\tb", "c\nd")
        self.assertEqual(journey_suffix(), " uid=a_b chat=c_d")

    def test_an_ordinary_id_is_untouched(self):
        set_journey("0063b5cf-11c5-4c7d-bd14-ccb717f1adcd", "c14")
        self.assertEqual(journey_suffix(),
                         " uid=0063b5cf-11c5-4c7d-bd14-ccb717f1adcd chat=c14")


if __name__ == "__main__":
    unittest.main()
