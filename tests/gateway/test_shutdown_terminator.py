#!/usr/bin/env python3
"""Terminate open stream bodies on shutdown — the fix, and why it is this one.

WHAT WAS ACTUALLY WRONG
-----------------------
A user mid-answer during a redeploy got an unterminated chunked body: *"Not
enough data to satisfy transfer length header"*. The four earlier terminator
commits made the CANCELLATION path honest, but nothing cancelled the handler at
shutdown, so the terminator never ran.

I first "fixed" this by raising `stop_grace_period` from 30 s to 75 s, reasoning
that aiohttp's 60 s `shutdown_timeout` would expire first and cancel the
handlers. **That was wrong**, and a disposable container built from the deployed
image proved it — same aiohttp, same `tini -g` topology, same
`except BaseException` writing finish + `[DONE]` + `write_eof`:

    docker stop --time=30  ->  30.2s · 35 chunks · curl rc=18 · no [DONE] · exit 137
    docker stop --time=75  ->  75.2s · 80 chunks · curl rc=18 · no [DONE] · exit 137
    with an on_shutdown terminator:
    docker stop --time=30  ->   1.2s ·  6 chunks · curl rc=0  ·    [DONE] · exit 0

`curl rc=18` is "transfer closed with outstanding read data remaining" — the
symptom, on demand. The framework does not force an in-flight `StreamResponse`
down; a longer grace only buys time to finish naturally. Terminating the bodies
ourselves is what makes it clean, and it makes shutdown *faster* (1.2 s, not 30).

An earlier version of this probe ran python as PID 1, where SIGTERM's default
disposition is IGNORE, so it survived to SIGKILL and proved nothing about the
gateway. The container really runs `tini -g -- entrypoint` with the gateway as a
child that catches SIGTERM (`/proc/7/status` `SigCgt` bit 15). The numbers above
are from the corrected topology.

SCOPE, STATED RATHER THAN IMPLIED
---------------------------------
Only the **chat-completions** SSE path registers. The other stream sites speak a
different protocol (Responses API events, not OpenAI chunks), so sending them a
`[DONE]` chunk would be a protocol violation, and `_terminate_stream_body`'s
shape is wrong for them. They remain exposed to the same truncation and that is
a known gap, not an oversight.

    docker run --rm -v "$PWD/tests":/opt/hermes/tests:ro --entrypoint python3 \\
        kg-openwebui-stack-v2-hermes-gateway:<tag> \\
        -m unittest discover -s /opt/hermes/tests/gateway \\
        -p 'test_shutdown_terminator.py'
"""

import ast
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _api_server_source() -> str:
    for candidate in (
        Path(__file__).resolve().parents[2] / "gateway" / "platforms" / "api_server.py",
        Path("/opt/hermes/gateway/platforms/api_server.py"),
    ):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    raise AssertionError("api_server.py not found in the tree or the image")


def _runtime_available() -> str:
    try:
        import gateway.platforms.api_server  # noqa: F401
        return ""
    except Exception as exc:
        return (f"{type(exc).__name__}: {exc}. Run inside the gateway image "
                f"(see this module's docstring for the exact command).")


_SKIP = _runtime_available()


class _FakeResponse:
    def __init__(self, fail_on=()):
        self.writes, self.eof_calls, self.attempts = [], 0, []
        self._fail_on = set(fail_on)

    async def write(self, data):
        n = len(self.attempts)
        self.attempts.append("write")
        if n in self._fail_on:
            raise ConnectionResetError("client gone")
        self.writes.append(data)

    async def write_eof(self):
        n = len(self.attempts)
        self.attempts.append("write_eof")
        if n in self._fail_on:
            raise ConnectionResetError("client gone")
        self.eof_calls += 1


def _mod():
    import gateway.platforms.api_server as m
    return m


@unittest.skipIf(_SKIP, _SKIP)
class ShutdownTerminatorTests(unittest.TestCase):
    def setUp(self):
        _mod()._LIVE_STREAM_BODIES.clear()

    tearDown = setUp

    def test_every_open_body_is_terminated(self):
        m = _mod()
        a, b = _FakeResponse(), _FakeResponse()
        m._LIVE_STREAM_BODIES[a] = ("id-a", 1, "model-x", None)
        m._LIVE_STREAM_BODIES[b] = ("id-b", 2, "model-y", None)
        asyncio.run(m._terminate_live_stream_bodies(None))
        for r in (a, b):
            self.assertEqual(r.eof_calls, 1, "a body was left unterminated")
            self.assertTrue(any(b"[DONE]" in w for w in r.writes))

    def test_the_registry_is_cleared_so_nothing_is_terminated_twice(self):
        """Writing to an already-closed transport is how a clean shutdown turns
        into a traceback at the last moment."""
        m = _mod()
        r = _FakeResponse()
        m._LIVE_STREAM_BODIES[r] = ("id", 1, "model", None)
        asyncio.run(m._terminate_live_stream_bodies(None))
        self.assertEqual(m._LIVE_STREAM_BODIES, {})
        asyncio.run(m._terminate_live_stream_bodies(None))
        self.assertEqual(r.eof_calls, 1, "terminated twice")

    def test_one_dead_client_does_not_cost_the_others_their_ending(self):
        m = _mod()
        dead, alive = _FakeResponse(fail_on={0, 1, 2}), _FakeResponse()
        m._LIVE_STREAM_BODIES[dead] = ("id-d", 1, "m", None)
        m._LIVE_STREAM_BODIES[alive] = ("id-a", 1, "m", None)
        asyncio.run(m._terminate_live_stream_bodies(None))
        self.assertEqual(alive.eof_calls, 1,
                         "a failing peer swallowed another client's termination")

    def test_no_open_bodies_is_a_quiet_no_op(self):
        asyncio.run(_mod()._terminate_live_stream_bodies(None))

    def test_it_is_bounded(self):
        """A hung write must not hold the container open past the grace: a
        truncated body is bad, a container that will not stop is worse.

        Run in a THREAD with a join deadline, not under `asyncio.wait_for`.
        `wait_for` bounds by cancelling, and `_terminate_stream_body` swallows
        cancellation at every step -- so a `wait_for` version of this test does
        not fail when the bound is removed, it HANGS. It caught the defect
        either way, but a test that hangs is a test a runner without its own
        timeout will sit on forever. This one fails in two seconds.
        """
        import threading
        m = _mod()

        class _Hangs(_FakeResponse):
            async def write(self, data):
                await asyncio.sleep(3600)

        m._LIVE_STREAM_BODIES[_Hangs()] = ("id", 1, "m", None)
        saved = m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS
        m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS = 0.05
        finished = threading.Event()

        def _worker():
            try:
                asyncio.run(m._terminate_live_stream_bodies(None))
            finally:
                finished.set()

        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        try:
            self.assertTrue(
                finished.wait(timeout=2.0),
                "shutdown termination did not return within 2s against a write "
                "that hangs -- the timeout is decorative, and a redeploy would "
                "sit here until the orchestrator SIGKILLs the container")
        finally:
            m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS = saved


class WiringTests(unittest.TestCase):
    """A registry nothing registers into is the empty-shell shape this codebase
    keeps producing: it passes every behavioural test and does nothing live."""

    def setUp(self):
        self.tree = ast.parse(_api_server_source())
        self.src = _api_server_source()

    def test_the_shutdown_hook_is_actually_attached(self):
        self.assertIn("self._app.on_shutdown.append(_terminate_live_stream_bodies)",
                      self.src,
                      "the terminator is never registered with the app, so it "
                      "cannot run at shutdown")

    def test_something_registers_a_live_body(self):
        writes = [n for n in ast.walk(self.tree)
                  if isinstance(n, ast.Subscript)
                  and getattr(n.value, "id", "") == "_LIVE_STREAM_BODIES"]
        stores = [n for n in writes if isinstance(n.ctx, ast.Store)]
        self.assertTrue(stores,
                        "nothing ever puts a response INTO the registry")

    def test_registration_happens_after_prepare(self):
        """Before `prepare` there is no body to terminate, and terminating an
        unprepared response raises."""
        idx_reg = self.src.index("_LIVE_STREAM_BODIES[response] = (completion_id")
        prepare = self.src.rindex("await response.prepare(request)", 0, idx_reg)
        between = self.src[prepare:idx_reg]
        self.assertLess(between.count("\n"), 6,
                        "registration drifted away from its prepare()")

    def test_every_exit_path_deregisters(self):
        """A stale entry makes the shutdown terminator write to a closed
        transport — a clean shutdown turning into a traceback at the last
        possible moment."""
        self.assertIn("_LIVE_STREAM_BODIES.pop(response, None)", self.src)
        pop_at = self.src.index("_LIVE_STREAM_BODIES.pop(response, None)")
        head = self.src.rfind("finally:", 0, pop_at)
        self.assertGreater(
            head, 0, "deregistration is not in a finally, so an early return or "
                     "an exception leaves the entry behind")
        self.assertLess(self.src[head:pop_at].count("\n"), 8,
                        "the pop drifted out of its finally block")


if __name__ == "__main__":
    unittest.main()
