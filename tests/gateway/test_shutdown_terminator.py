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


def _register(m, resp, owner=None):
    """Register a body the way the real handler does."""
    lc = m._StreamLifecycle(("id", 1, "model", None), owner)
    m._LIVE_STREAM_BODIES[resp] = lc
    return lc


@unittest.skipIf(_SKIP, _SKIP)
class ShutdownTerminatorTests(unittest.TestCase):
    def setUp(self):
        _mod()._LIVE_STREAM_BODIES.clear()

    tearDown = setUp

    def test_every_open_body_is_terminated(self):
        m = _mod()
        a, b = _FakeResponse(), _FakeResponse()
        _register(m, a); _register(m, b)
        asyncio.run(m._terminate_live_stream_bodies(None))
        for r in (a, b):
            self.assertEqual(r.eof_calls, 1, "a body was left unterminated")
            self.assertTrue(any(b"[DONE]" in w for w in r.writes))

    def test_the_user_is_TOLD_the_answer_was_cut_short(self):
        m = _mod()
        r = _FakeResponse()
        _register(m, r)
        asyncio.run(m._terminate_live_stream_bodies(None))
        joined = b"".join(r.writes)
        self.assertIn(b"cut short", joined)
        self.assertLess(joined.index(b"cut short"), joined.index(b"[DONE]"),
                        "the notice landed after the conversation was closed")

    def test_a_second_ending_is_never_written(self):
        m = _mod()
        r = _FakeResponse()
        _register(m, r)
        asyncio.run(m._terminate_live_stream_bodies(None))
        asyncio.run(m._terminate_live_stream_bodies(None))
        self.assertEqual(r.eof_calls, 1, "terminated twice")

    def test_one_dead_client_does_not_cost_the_others_their_ending(self):
        m = _mod()
        dead, alive = _FakeResponse(fail_on={0, 1, 2, 3}), _FakeResponse()
        _register(m, dead); _register(m, alive)
        asyncio.run(m._terminate_live_stream_bodies(None))
        self.assertEqual(alive.eof_calls, 1,
                         "a failing peer swallowed another client's ending")

    def test_a_RESPONSIVE_owner_still_tells_the_user_it_was_cut_short(self):
        """The composition round 9 taught me to keep — and which I dropped
        while rewriting the suite for the lifecycle.

        The owner terminates correctly on cancellation; it simply does not know
        WHY it was cancelled, and passes no notice. The lifecycle carries the
        reason, so its own termination still tells the user. Without this, a
        mutant that never sets `shutdown` survives: the callback passes the
        notice explicitly and every other test goes through the callback.
        """
        m = _mod()

        async def _scenario():
            r = _FakeResponse()

            async def _owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # the real handler's cancellation path: no notice argument
                    await m._terminate_stream_body(r, "id", 1, "model", None)
                    return None

            task = asyncio.ensure_future(_owner())
            await asyncio.sleep(0)
            _register(m, r, task)
            await m._terminate_live_stream_bodies(None)
            await asyncio.sleep(0.05)
            return r

        r = asyncio.run(_scenario())
        joined = b"".join(r.writes)
        self.assertIn(b"cut short", joined,
                      "the owner ended cleanly without telling the user the "
                      "answer was truncated")
        self.assertEqual(joined.count(b"[DONE]"), 1, "two endings were written")
        self.assertEqual(r.eof_calls, 1, "EOF written twice")

    def test_no_open_bodies_is_a_quiet_no_op(self):
        asyncio.run(_mod()._terminate_live_stream_bodies(None))

    # ---- the three defects round 10 proved, as acceptance tests -----------

    def test_an_owner_ALREADY_INSIDE_the_terminator_cannot_be_raced(self):
        """The mark version re-checked nothing once the owner was inside.

        Marks are read before a call; the defect lived inside it. The lock
        means there is no second writer to interleave with, so this is closed
        by construction rather than by another check.
        """
        m = _mod()

        async def _scenario():
            r = _FakeResponse()
            lc = _register(m, r)
            # Owner is mid-terminal: it holds the lock.
            await lc.lock.acquire()
            cb = asyncio.ensure_future(m._terminate_live_stream_bodies(None))
            await asyncio.sleep(0.05)
            wrote_while_owner_held = len(r.writes)
            # Owner finishes its ending, then releases.
            await m._terminate_stream_body_locked(r, "id", 1, "model", None,
                                                  m.SHUTDOWN_INTERRUPTION_NOTICE)
            lc.terminated = True
            lc.lock.release()
            await cb
            return r, wrote_while_owner_held

        r, during = asyncio.run(_scenario())
        self.assertEqual(during, 0,
                         "the callback wrote while the owner held the terminal")
        self.assertEqual(r.eof_calls, 1, "two endings were written")
        self.assertEqual(b"".join(r.writes).count(b"[DONE]"), 1)

    def test_a_PARTIAL_owner_ending_is_completed_not_trusted(self):
        """`owner.done()` said "nothing left to write" even when the owner's
        notice write had failed and only finish/[DONE]/EOF landed — so an
        incomplete answer looked complete again. `terminated` is set only when
        EVERY step lands, so the callback finishes the job."""
        m = _mod()

        async def _scenario():
            r = _FakeResponse(fail_on={0})       # the NOTICE write fails
            lc = _register(m, r)
            lc.shutdown = True
            ok = await m._terminate_stream_body(r, "id", 1, "model", None,
                                                notice="x")
            return r, ok, lc.terminated

        r, ok, terminated = asyncio.run(_scenario())
        self.assertFalse(ok, "a partial ending reported success")
        self.assertFalse(terminated,
                         "a partial ending was recorded as terminated, so "
                         "nothing would ever complete it")

    def test_a_body_registered_DURING_shutdown_is_not_missed(self):
        """A one-time snapshot before the handover misses anything that
        registers while we wait."""
        m = _mod()

        async def _scenario():
            first = _FakeResponse()
            late = _FakeResponse()

            async def _owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    _register(m, late)      # arrives during the handover
                    return None

            task = asyncio.ensure_future(_owner())
            await asyncio.sleep(0)
            _register(m, first, task)
            await m._terminate_live_stream_bodies(None)
            return late

        late = asyncio.run(_scenario())
        self.assertEqual(late.eof_calls, 1,
                         "a body registered during shutdown was never ended")

    def test_it_is_bounded(self):
        """A hung write must not hold the container open past the grace."""
        import threading, time as _time
        m = _mod()

        class _Hangs(_FakeResponse):
            async def write(self, data):
                await asyncio.sleep(3600)

        _register(m, _Hangs())
        saved = m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS
        m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS = 0.05
        finished = threading.Event()

        def _worker():
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(m._terminate_live_stream_bodies(None))
            except BaseException:
                pass
            finally:
                loop.close(); finished.set()

        threading.Thread(target=_worker, daemon=True).start()
        try:
            self.assertTrue(finished.wait(timeout=3.0),
                            "shutdown did not return against a hung write")
        finally:
            m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS = saved

    def test_the_lifecycle_does_not_pin_the_response(self):
        import gc
        m = _mod()
        r = _FakeResponse()
        _register(m, r)
        self.assertEqual(len(m._LIVE_STREAM_BODIES), 1)
        del r
        gc.collect()
        self.assertEqual(len(m._LIVE_STREAM_BODIES), 0,
                         "the registry holds a response nobody else references")


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
        idx_reg = self.src.index("_LIVE_STREAM_BODIES[response] = _StreamLifecycle(")
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
        """Structural, not window-based. A character-count window broke the
        moment a comment grew, which is a test failing for the wrong reason."""
        idx = self.src.index('_err = result.get("error")')
        # Everything up to the next `else:` at the same nesting is this branch.
        end = self.src.index("\n                        else:", idx)
        branch = self.src[idx:end]
        self.assertIn("_encode_content_delta(_msg)", branch,
                      "the reason is computed and then never written to the "
                      "client -- a log line is not an answer")

    def test_the_no_reason_case_still_logs_the_bare_shape(self):
        """Not every early return carries an error string. That case must stay
        distinguishable in the logs rather than being folded into the explained
        one, or the ledger loses the ability to count them apart."""
        for label in ('"no_content_and_no_final_explained"',
                      '"no_content_and_no_final_generic"',
                      '"no_content_and_no_final"'):
            self.assertIn(label, self.src, f"{label} is gone")
        # The label must be chosen by whether the write SUCCEEDED, not merely
        # attempted. The first version logged it from inside the try, so a
        # failed write still reported the user had been told why -- a metric
        # lying about the one thing it was built to measure.
        self.assertIn("if not _delivered else", self.src,
                      "the label is not conditioned on delivery")
        # ...and a generic sentence must not be reported as an explanation.
        self.assertIn("if _known else", self.src,
                      "a generic delivery is logged as an explained one")

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

    def test_an_unrecognised_reason_still_gets_a_sentence(self):
        """The generic sentence was dead in the wire path.

        Emission was gated on a RECOGNISED reason, so an unrecognised loop
        outcome still handed the user a blank box — the very defect this branch
        exists to close, surviving inside its own fix.
        """
        src = _api_server_source()
        self.assertNotIn(
            "                        if _known:\n", src,
            "emission is gated on recognition again, so unknown reasons are "
            "silent")
        self.assertIn("no_content_and_no_final_generic", src,
                      "a generic delivery is indistinguishable from a specific "
                      "one in the logs")

    def test_cancellation_is_not_swallowed_by_the_explanation_write(self):
        """My own `except BaseException: pass`, written today.

        Eating a cancellation here loses the explanation, leaves the
        cancellation pending on the task, and then lets the code below emit
        `[DONE]` as though the request ended normally — the exact defect the
        four earlier terminator commits exist to undo, reintroduced inside
        their own fix.
        """
        src = _api_server_source()
        idx = src.index("_delivered = True")
        window = src[idx:idx + 900]
        self.assertIn("except (asyncio.CancelledError, GeneratorExit)", window,
                      "cancellation is caught by a bare BaseException handler "
                      "again")
        self.assertIn("raise", window, "cancellation is caught and not re-raised")
        self.assertNotIn("except BaseException:\n                                pass",
                         window, "the swallow-everything guard is back")

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

    The real writer got `CLIENT_EOF clean=1 done=1` and the handler, still
    running, then discovered the closed transport and logged `stream_aborted
    phase=mid_stream` — the record the journey ledger counts as a user-visible
    failure. Every stream open at deploy time would have produced a false one.

    The guard now reads the LIFECYCLE rather than a global mark set: the reason
    lives with the response and dies with it, so a later genuine abort on a
    different response is unaffected by construction rather than by remembering
    to consume a flag.
    """

    def setUp(self):
        self.src = _api_server_source()

    def test_the_handler_asks_the_lifecycle_before_calling_it_an_abort(self):
        idx = self.src.index("stream_aborted service=gateway phase=mid_stream")
        head = self.src.rfind("_LIVE_STREAM_BODIES.get(response)", 0, idx)
        self.assertGreater(head, 0, "the abort log is not guarded at all")
        self.assertLess(self.src[head:idx].count("\n"), 20,
                        "the guard drifted away from the log it protects")
        self.assertIn(".shutdown:", self.src[head:idx],
                      "the guard does not read the shutdown reason")

    def test_the_honest_label_exists_and_is_not_an_error(self):
        self.assertIn("stream_closed_at_shutdown", self.src)
        idx = self.src.index("stream_closed_at_shutdown")
        self.assertIn("logger.info", self.src[max(0, idx - 200):idx],
                      "a clean shutdown close is logged at error level, which "
                      "puts it back in the failure counts by another name")

    def test_the_old_global_marks_are_gone(self):
        """Three mark sets used as an implicit state machine were the thing
        that kept producing a new defect inside each fix. If one reappears,
        that design is creeping back."""
        for gone in ("_SHUTDOWN_CANCELLED", "_SHUTDOWN_TAKEOVER"):
            self.assertNotIn(f"{gone}.add(", self.src,
                             f"{gone} is being written to again")


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
        """Structural, not window-based. A character-count window broke the
        moment a comment grew, which is a test failing for the wrong reason."""
        idx = self.src.index('_err = result.get("error")')
        # Everything up to the next `else:` at the same nesting is this branch.
        end = self.src.index("\n                        else:", idx)
        branch = self.src[idx:end]
        self.assertIn("_encode_content_delta(_msg)", branch,
                      "the reason is computed and then never written to the "
                      "client -- a log line is not an answer")

    def test_the_no_reason_case_still_logs_the_bare_shape(self):
        """Not every early return carries an error string. That case must stay
        distinguishable in the logs rather than being folded into the explained
        one, or the ledger loses the ability to count them apart."""
        for label in ('"no_content_and_no_final_explained"',
                      '"no_content_and_no_final_generic"',
                      '"no_content_and_no_final"'):
            self.assertIn(label, self.src, f"{label} is gone")
        # The label must be chosen by whether the write SUCCEEDED, not merely
        # attempted. The first version logged it from inside the try, so a
        # failed write still reported the user had been told why -- a metric
        # lying about the one thing it was built to measure.
        self.assertIn("if not _delivered else", self.src,
                      "the label is not conditioned on delivery")
        # ...and a generic sentence must not be reported as an explanation.
        self.assertIn("if _known else", self.src,
                      "a generic delivery is logged as an explained one")

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

    def test_an_unrecognised_reason_still_gets_a_sentence(self):
        """The generic sentence was dead in the wire path.

        Emission was gated on a RECOGNISED reason, so an unrecognised loop
        outcome still handed the user a blank box — the very defect this branch
        exists to close, surviving inside its own fix.
        """
        src = _api_server_source()
        self.assertNotIn(
            "                        if _known:\n", src,
            "emission is gated on recognition again, so unknown reasons are "
            "silent")
        self.assertIn("no_content_and_no_final_generic", src,
                      "a generic delivery is indistinguishable from a specific "
                      "one in the logs")

    def test_cancellation_is_not_swallowed_by_the_explanation_write(self):
        """My own `except BaseException: pass`, written today.

        Eating a cancellation here loses the explanation, leaves the
        cancellation pending on the task, and then lets the code below emit
        `[DONE]` as though the request ended normally — the exact defect the
        four earlier terminator commits exist to undo, reintroduced inside
        their own fix.
        """
        src = _api_server_source()
        idx = src.index("_delivered = True")
        window = src[idx:idx + 900]
        self.assertIn("except (asyncio.CancelledError, GeneratorExit)", window,
                      "cancellation is caught by a bare BaseException handler "
                      "again")
        self.assertIn("raise", window, "cancellation is caught and not re-raised")
        self.assertNotIn("except BaseException:\n                                pass",
                         window, "the swallow-everything guard is back")

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


