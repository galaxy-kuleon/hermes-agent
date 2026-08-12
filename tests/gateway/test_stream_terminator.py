#!/usr/bin/env python3
"""The stream terminator, which four fixes changed and no test covered.

On 2026-08-12 I recorded that these four commits rest on code review plus unit
tests. Then I checked, and the second half was false:

    3613ac6cf  the cancelled handler now owns its agent task to the end
    ea8387b41  the terminator skipped the one step the client waits for
    744c22053  the terminator was itself cancellable, and the flag meant the wrong thing
    035472837  terminate the stream for the whole BaseException family

All four changed `gateway/platforms/api_server.py` and nothing else. No test in
the tree referenced `_terminate_stream_body`, `_stop_cancelled_sse_agent` or
`SSE_AGENT_CANCEL_DRAIN_SECONDS`. They are also on a path unreachable from
outside — a client disconnect completes cleanly with a zero-byte 200 and never
raises in the handler, OpenWebUI registers a stoppable task only for its own
request shape, and the gateway port refuses an unauthenticated request — so
there was no RBV either. Their correctness rested on having read them.

What the user loses if these regress is specific: the browser is mid-answer,
the chunked body is never terminated, and the answer stops dead under a
"Not enough data to satisfy transfer length header". No error, no explanation.

Run inside the gateway image, where aiohttp and the rest of the runtime exist:

    docker run --rm -v "$PWD/tests":/mnt/tests:ro --entrypoint python3 \\
        kg-openwebui-stack-v2-hermes-gateway:<tag> \\
        -m pytest /mnt/tests/gateway/test_stream_terminator.py -q
"""

import ast
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _api_server_source() -> str:
    """The deployed source, read as text so the structural tests need no import.

    Inside the image the tests are mounted next to a baked `/opt/hermes`; on a
    checkout they sit in the tree. Try both rather than assume one.
    """
    for candidate in (
        Path(__file__).resolve().parents[2] / "gateway" / "platforms" / "api_server.py",
        Path("/opt/hermes/gateway/platforms/api_server.py"),
    ):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    raise AssertionError("api_server.py not found in the tree or the image")


class _FakeResponse:
    """Records what the terminator managed to write, and can fail on demand."""

    def __init__(self, fail_on=(), exc=asyncio.CancelledError):
        self.writes = []
        self.eof_calls = 0
        self.attempts = []
        self._fail_on = set(fail_on)
        self._exc = exc

    async def write(self, data):
        n = len(self.attempts)
        self.attempts.append("write")
        if n in self._fail_on:
            raise self._exc()
        self.writes.append(data)

    async def write_eof(self):
        n = len(self.attempts)
        self.attempts.append("write_eof")
        if n in self._fail_on:
            raise self._exc()
        self.eof_calls += 1


def _runtime_available() -> str:
    """Empty string if the gateway runtime imports here, else why it does not.

    The behavioural tests need aiohttp and the rest of the image; the
    structural ones only need the source text. Skipping is stated rather than
    silent, and the skip reason names the command that DOES run them -- a test
    that quietly skips everywhere is the same as no test.
    """
    try:
        import gateway.platforms.api_server  # noqa: F401
        return ""
    except Exception as exc:
        return (f"{type(exc).__name__}: {exc}. Run inside the gateway image: "
                f"docker run --rm -v \"$PWD/tests\":/opt/hermes/tests:ro "
                f"--entrypoint python3 <image> -m unittest discover "
                f"-s /opt/hermes/tests/gateway -p 'test_stream_terminator.py'")


_SKIP = _runtime_available()


def _terminator():
    from gateway.platforms.api_server import _terminate_stream_body
    return _terminate_stream_body


def _run(coro):
    return asyncio.run(coro)


@unittest.skipIf(_SKIP, _SKIP)
class TerminatorBehaviourTests(unittest.TestCase):
    """Every step is attempted, whatever the previous one did."""

    def _terminate(self, resp):
        _run(_terminator()(resp, "chatcmpl-test", 1, "model-x", None))

    def test_the_happy_path_writes_finish_then_done_then_eof(self):
        r = _FakeResponse()
        self._terminate(r)
        self.assertEqual(r.attempts, ["write", "write", "write_eof"])
        self.assertEqual(r.eof_calls, 1, "write_eof must be called exactly once")
        self.assertIn(b"[DONE]", r.writes[1])
        self.assertIn(b'"finish_reason": "stop"', r.writes[0])
        self.assertIn(b"chatcmpl-test", r.writes[0])

    def test_a_cancellation_on_the_finish_chunk_does_not_cost_the_eof(self):
        """The exact defect of 744c22053.

        A single try around all three steps meant a second cancellation while
        writing the finish chunk exited the block and skipped `write_eof` --
        the one step the client is actually waiting for.
        """
        r = _FakeResponse(fail_on={0})
        self._terminate(r)
        self.assertEqual(r.attempts, ["write", "write", "write_eof"])
        self.assertEqual(r.eof_calls, 1, "the body was never terminated")

    def test_a_cancellation_on_DONE_does_not_cost_the_eof(self):
        r = _FakeResponse(fail_on={1})
        self._terminate(r)
        self.assertEqual(r.eof_calls, 1)

    def test_every_step_failing_still_returns_quietly(self):
        """The terminator runs inside an exception handler that must re-raise
        the ORIGINAL exception. If the terminator raised its own, cancellation
        would be replaced by a write error and the task would look like it
        failed for a different reason."""
        r = _FakeResponse(fail_on={0, 1, 2})
        self._terminate(r)
        self.assertEqual(r.attempts, ["write", "write", "write_eof"])

    def test_it_survives_the_whole_BaseException_family(self):
        """035472837: not just cancellation. A GeneratorExit or SystemExit
        during the finish chunk must not skip the termination either."""
        for exc in (GeneratorExit, SystemExit, KeyboardInterrupt, RuntimeError, OSError):
            with self.subTest(exc=exc.__name__):
                r = _FakeResponse(fail_on={0}, exc=exc)
                self._terminate(r)
                self.assertEqual(r.eof_calls, 1, f"{exc.__name__} skipped write_eof")


class HandlerOrderTests(unittest.TestCase):
    """Ordering is the bug. Assert it against the source, not from memory."""

    def _handler_clauses(self):
        """Every `try` in the module that handles CancelledError, as a list of
        the exception names its clauses catch, in source order."""
        tree = ast.parse(_api_server_source())
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            names = []
            for h in node.handlers:
                t = h.type
                if t is None:
                    names.append(("bare", h))
                elif isinstance(t, ast.Tuple):
                    names.append(("|".join(ast.unparse(e) for e in t.elts), h))
                else:
                    names.append((ast.unparse(t), h))
            if any("CancelledError" in n for n, _ in names):
                out.append(names)
        return out

    def test_cancellation_is_caught_before_the_broad_handlers(self):
        """CancelledError is a BaseException, so `except Exception` never sees
        it -- but `except BaseException` does. Ordering CancelledError after it
        would silently take the generic path and lose the agent-ownership and
        re-raise semantics."""
        found = self._handler_clauses()
        self.assertTrue(found, "no try block handles CancelledError any more")
        for names in found:
            order = [n for n, _ in names]
            idx = next(i for i, n in enumerate(order) if "CancelledError" in n)
            for i, n in enumerate(order):
                if n == "BaseException" or n == "bare":
                    self.assertGreater(
                        i, idx,
                        f"a catch-all precedes CancelledError: {order}")

    def test_every_handler_that_terminates_the_stream_re_raises(self):
        """The streaming contract, pinned where it matters.

        "Every CancelledError handler must re-raise" is too strong: a handler
        that cancels a task and then awaits it legitimately absorbs THAT
        cancellation -- `_handle_stop_run` does exactly this. The property that
        actually protects a user's answer is narrower: if you terminated the
        stream body, you were in the streaming path, and swallowing the
        cancellation there leaves the task looking alive while the client has
        already been closed out.
        """
        tree = ast.parse(_api_server_source())
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for h in node.handlers:
                calls = [
                    n for n in ast.walk(h)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "id", getattr(n.func, "attr", ""))
                    == "_terminate_stream_body"
                ]
                if not calls:
                    continue
                checked += 1
                raises = [n for n in ast.walk(h) if isinstance(n, ast.Raise)]
                self.assertTrue(
                    raises,
                    f"the handler at line {h.lineno} terminates the stream and "
                    f"then swallows the exception")
        self.assertGreaterEqual(
            checked, 1,
            "no handler terminates the stream any more -- the contract this "
            "test guards has been removed, not satisfied")

    def test_the_terminator_guards_each_step_separately(self):
        """One try around all three steps is the defect 744c22053 fixed. The
        loop-with-per-step-guard shape is what keeps write_eof reachable.

        The writes moved into `_terminate_stream_body_locked` when the
        lifecycle lock arrived, and this test kept naming the old function --
        so it went red and stayed red, because the suite that owns the
        terminator was run and this neighbouring one was not. Look for the
        function that actually writes, whatever it is called."""
        tree = ast.parse(_api_server_source())
        fns = [n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name.startswith("_terminate_stream_body")]
        fn = next((n for n in fns
                   if any(isinstance(x, (ast.For, ast.AsyncFor))
                          for x in ast.walk(n))), None)
        self.assertIsNotNone(
            fn, f"no terminator function has a per-step loop any more "
                f"(looked at {[n.name for n in fns]})")
        loops = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.AsyncFor))]
        self.assertTrue(loops, "the per-step loop is gone; steps are guarded together again")
        # The guard may sit inline in the loop, or in a small helper the loop
        # calls -- the shape that matters is one guard PER STEP, not one around
        # all of them. When per-step bounding arrived the guard moved into
        # `_bounded`, which is the same property expressed once instead of
        # three times.
        tries = [n for n in ast.walk(loops[0]) if isinstance(n, ast.Try)]
        if not tries:
            helpers = {h.name: h for h in ast.walk(fn)
                       if isinstance(h, (ast.AsyncFunctionDef, ast.FunctionDef))
                       and h is not fn}
            called = {n.func.id for n in ast.walk(loops[0])
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            for name in called & set(helpers):
                tries.extend(n for n in ast.walk(helpers[name])
                             if isinstance(n, ast.Try))
        self.assertTrue(
            tries, "no per-step guard: neither inside the loop nor in any "
                   "helper the loop calls")
        caught = [ast.unparse(h.type) if h.type else "bare" for h in tries[0].handlers]
        self.assertIn(
            "BaseException", caught,
            f"the per-step guard must cover BaseException, not just {caught}")
        # The point of the shape: EOF stays reachable when an earlier step
        # fails. It is written OUTSIDE the loop, after it, so no per-step
        # failure can skip it. (Behavioural cover for this lives in
        # tests/gateway/test_shutdown_terminator.py --
        # `test_eof_is_recorded_even_when_the_ending_was_incomplete`.)
        eof_calls = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Attribute) and n.attr == "write_eof"]
        self.assertTrue(eof_calls, "the terminator no longer closes the body")
        loop_lines = range(loops[0].lineno, loops[0].end_lineno + 1)
        self.assertTrue(
            any(n.lineno not in loop_lines for n in eof_calls),
            "write_eof only happens inside the per-step loop; a failure "
            "pattern that exits the loop early would leave the body open")


if __name__ == "__main__":
    unittest.main()
