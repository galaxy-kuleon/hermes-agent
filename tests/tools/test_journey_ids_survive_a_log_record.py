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
        self.assertEqual(suffix, " uid=user%20one chat=chat%20two")
        for field in suffix.split():
            self.assertIn("=", field, f"{field!r} would be read as a stray token")

    def test_ids_with_other_whitespace_too(self):
        set_journey("a\tb", "c\nd")
        self.assertEqual(journey_suffix(), " uid=a%09b chat=c%0Ad")

    def test_the_encoding_is_INJECTIVE(self):
        """Two distinct conversations must never share one correlator.

        The first fix replaced whitespace with "_", so `case 15`, `case_15`
        and `case  15` all became `case_15` -- three conversations, one
        correlator, and a failure attachable to the wrong one. Round 17.
        """
        distinct = ["case 15", "case_15", "case  15", "case%2015", "case\t15"]
        seen = {}
        for value in distinct:
            set_journey("u", value)
            encoded = journey_suffix().split("chat=", 1)[1]
            self.assertNotIn(encoded, seen,
                             f"{value!r} collides with {seen.get(encoded)!r}")
            seen[encoded] = value

    def test_an_ordinary_id_is_untouched(self):
        set_journey("0063b5cf-11c5-4c7d-bd14-ccb717f1adcd", "c14")
        self.assertEqual(journey_suffix(),
                         " uid=0063b5cf-11c5-4c7d-bd14-ccb717f1adcd chat=c14")


if __name__ == "__main__":
    unittest.main()
