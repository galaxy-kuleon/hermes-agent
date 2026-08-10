#!/usr/bin/env python3
"""A structured turn record must be ONE line, whatever the exception said.

The turn's exit reason can come from str(exception). A newline in it split the
record before its model, counters, response length and session, so neither
physical line parsed, the failure was absent from the journey ledger, and the
cursor acknowledged it anyway. This is the same high-value
error_near_max_iterations family whose spaces were fixed in round 7 -- its
newline form was found in round 15, 2026-08-10.
"""

import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)


def _load_one_line():
    """Load the helper without importing the module's heavy dependencies."""
    src = open(os.path.join(_ROOT, "agent", "turn_finalizer.py")).read()
    # Anchored on the helper itself, not on what happens to sit above it: the
    # loader broke the moment another function was inserted between the
    # constant and the def, and a test that fails for the wrong reason teaches
    # nothing.
    const = re.search(r"_ONE_LINE_MAX = \d+", src)
    body = re.search(r"def _one_line.*?return text\[:_ONE_LINE_MAX\]\n", src, re.S)
    assert const and body, ("the one-line helper is gone; the turn record can "
                            "break in two again")
    source = const.group(0) + "\n\n\n" + body.group(0)
    ns = {}
    exec(compile(source, "<turn_finalizer>", "exec"), ns)
    return ns["_one_line"], src


class TurnRecordStaysOneLineTests(unittest.TestCase):
    def setUp(self):
        self.one_line, self.src = _load_one_line()

    def test_a_traceback_in_the_reason_cannot_split_the_record(self):
        got = self.one_line("error_near_max_iterations(Traceback:\nValueError: bad\r\nmore)")
        self.assertNotIn("\n", got)
        self.assertNotIn("\r", got)
        self.assertIn("error_near_max_iterations", got)

    def test_NOTHING_python_treats_as_a_line_break_can_split_it(self):
        """The collector frames with str.splitlines(), which splits on far more
        than CR/LF. The collapse is defined as the inverse of that framing, so
        this enumerates the whole set rather than the ones I thought of."""
        for ch in ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e",
                   "\x85", "\u2028", "\u2029"):
            with self.subTest(ch=repr(ch)):
                got = self.one_line(f"error_near_max_iterations(a{ch}b)")
                self.assertEqual(len(got.splitlines()), 1,
                                 f"{ch!r} still splits the turn record in two")

    def test_a_stack_trace_cannot_push_the_counters_off_the_line(self):
        self.assertLessEqual(len(self.one_line("A" * 5000)), 300)

    def test_the_session_id_is_ONE_token_whatever_the_caller_sends(self):
        """It is caller-supplied on some routes, and the record is
        whitespace-delimited key=value: a space in it turns the rest of the id
        into attacker-chosen fields, including a uid/chat pair ahead of the
        genuine one. Round 18."""
        src = self.src
        m = re.search(r"def _id_safe_session.*?safe=\"\"\) or \"none\"\n", src, re.S)
        self.assertTrue(m, "the session id is no longer encoded to one token")
        ns = {}
        exec(compile(m.group(0), "<turn_finalizer>", "exec"), ns)
        encoded = ns["_id_safe_session"]("opaque uid=1111 chat=victim")
        self.assertNotIn(" ", encoded)
        self.assertNotIn("=", encoded)
        self.assertIn("_id_safe_session(agent.session_id", src,
                      "the emitter no longer encodes the session id")

    def test_the_emitter_actually_uses_it(self):
        """A helper nobody calls protects nothing."""
        self.assertIn("_one_line(_turn_exit_reason)", self.src,
                      "the turn record no longer passes its reason through the collapser")


if __name__ == "__main__":
    unittest.main()
