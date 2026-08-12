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


class EmptyReplyReasonTests(unittest.TestCase):
    """'finished without any content' — the loop knew why, and we discarded it.

    Six early returns in `conversation_loop.py` produce
    `{final_response: None, partial: True, error: "..."}` and **bypass
    TurnFinalizer**, so the empty-turn explainer cannot fire for any of them.
    The non-streaming handler turns that same dict into an HTTP 502; the
    streaming writer ignored `error` and emitted a normal finish + `[DONE]` with
    no content. The user got a blank box while the reason sat unread in the
    result dict.

    Structural, because the branch needs a full streaming turn to reach and the
    point is that the value is READ at all.
    """

    def setUp(self):
        self.src = _api_server_source()

    def test_the_reason_is_read_from_the_result(self):
        self.assertIn('_err = result.get("error")', self.src,
                      "the streaming writer still discards the reason the loop "
                      "computed, so the user keeps getting a blank box")

    def test_it_is_surfaced_on_the_wire_not_only_logged(self):
        idx = self.src.index('_err = result.get("error")')
        window = self.src[idx:idx + 1400]
        self.assertIn("_encode_content_delta(_msg)", window,
                      "the reason is computed and then never written to the "
                      "client -- a log line is not an answer")

    def test_the_no_reason_case_still_logs_the_bare_shape(self):
        """Not every early return carries an error string. That case must stay
        distinguishable in the logs rather than being folded into the explained
        one, or the ledger loses the ability to count them apart."""
        self.assertIn('"no_content_and_no_final_explained"', self.src)
        self.assertIn("no_content_and_no_final", self.src)
        # ...and the explained label must be chosen by whether the write
        # SUCCEEDED, not merely attempted. The first version logged it from
        # inside the try, so a failed write still reported the user had been
        # told why -- a metric lying about the one thing it was built to
        # measure.
        self.assertIn("if _delivered else", self.src,
                      "the explained label is not conditioned on delivery")

    def test_the_providers_text_never_reaches_the_wire(self):
        """The M4 boundary, and the reason this is a closed vocabulary.

        I first rendered `result["error"]` directly with a 300-character bound,
        calling it "an internal string our own loop produced". It is not:
        `_summarize_api_error` accepts `body.error.message` and raw
        `str(error)`, and the invalid-tool path interpolates a MODEL-CHOSEN
        name. Adversarial review put a synthetic filename through it and watched
        it reach the wire. A length bound bounds length, not provenance.
        """
        self.assertNotIn("EMPTY_REPLY_REASON_MAX_CHARS", self.src,
                         "the length-bounded raw-error path is back")
        self.assertIn("_EMPTY_REPLY_SENTENCES", self.src)
        self.assertNotIn("_why = _err.strip()", self.src,
                         "the raw error is being rendered again")


@unittest.skipIf(_SKIP, _SKIP)
class EmptyReplyVocabularyTests(unittest.TestCase):
    """Behavioural: only sentences this file owns may be produced."""

    def test_a_provider_message_with_a_filename_is_not_echoed(self):
        m = _mod()
        leak = "HTTP 400: Cannot process Smith-v-Jones-SETTLEMENT-DRAFT.docx"
        out = m._empty_reply_sentence(leak)
        self.assertNotIn("Smith-v-Jones", out)
        self.assertNotIn(".docx", out)
        self.assertEqual(out, m._EMPTY_REPLY_GENERIC)

    def test_a_model_chosen_tool_name_is_not_echoed(self):
        m = _mod()
        out = m._empty_reply_sentence(
            "Model generated invalid tool call: read_/Users/kg/clients/acme.pdf")
        self.assertNotIn("acme", out)
        self.assertNotIn("/Users", out)
        self.assertIn("does not exist", out)

    def test_each_known_reason_maps_to_its_own_sentence(self):
        m = _mod()
        seen = set()
        for prefix, sentence in m._EMPTY_REPLY_SENTENCES:
            got = m._empty_reply_sentence(prefix + " ...trailing junk...")
            self.assertEqual(got, sentence)
            seen.add(got)
        self.assertEqual(len(seen), len(m._EMPTY_REPLY_SENTENCES),
                         "two reasons collapsed onto one sentence")

    def test_every_rendered_sentence_is_literal_in_this_file(self):
        """The property that actually protects the boundary: whatever comes out
        must appear verbatim in the source, so no input can shape it."""
        m = _mod()
        src = _api_server_source()
        for probe in ("", "unknown thing", "HTTP 500 " + "x" * 500, None, 42):
            out = m._empty_reply_sentence(probe)
            self.assertIn(out, src,
                          f"a sentence not written in this file reached the "
                          f"user for input {probe!r}")


class FalseAbortTests(unittest.TestCase):
    """A clean shutdown must not be recorded as a blank screen.

    Proved on a disposable container running the REAL writer: the client got
    `CLIENT_EOF clean=1 done=1` — a finish chunk, `[DONE]`, a properly closed
    body — and the handler, still running, then discovered the closed transport
    and logged `stream_aborted phase=mid_stream`. That record is what the
    journey ledger counts as a user-visible failure, so every stream open at
    deploy time would have produced a false one, in the exact metric built to
    count blank screens.
    """

    def setUp(self):
        self.src = _api_server_source()

    def test_shutdown_closed_bodies_are_marked(self):
        self.assertIn("_SHUTDOWN_TERMINATED.add(", self.src,
                      "nothing records that WE closed the body, so the handler "
                      "cannot tell a shutdown from a crash")

    def test_the_handler_checks_before_calling_it_an_abort(self):
        idx = self.src.index("stream_aborted service=gateway phase=mid_stream")
        head = self.src.rfind("_SHUTDOWN_TERMINATED", 0, idx)
        self.assertGreater(head, 0, "the abort log is not guarded at all")
        self.assertLess(self.src[head:idx].count("\n"), 20,
                        "the guard drifted away from the log it protects")

    def test_the_honest_label_exists_and_is_not_an_error(self):
        """It still has to be VISIBLE — a shutdown that closed live streams is
        worth counting, just not as a failure."""
        self.assertIn("stream_closed_at_shutdown", self.src)
        idx = self.src.index("stream_closed_at_shutdown")
        window = self.src[max(0, idx - 200):idx]
        self.assertIn("logger.info", window,
                      "a clean shutdown close is logged at error level, which "
                      "puts it back in the failure counts by another name")

    def test_the_mark_is_consumed_so_a_later_real_abort_still_reports(self):
        """A response object could be reused or a second exception could follow;
        a sticky mark would silence a genuine abort."""
        self.assertIn("_SHUTDOWN_TERMINATED.discard(", self.src)
