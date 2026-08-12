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
        _mod()._SHUTDOWN_TERMINATED.clear()

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

    def test_the_terminator_reports_whether_the_EOF_LANDED(self):
        """The return value is what the shutdown path marks on, so it has to
        mean delivery, not effort. A version that always returned True survived
        every structural test here — the marking code looked right and rested on
        a lie underneath it."""
        m = _mod()
        good = _FakeResponse()
        self.assertIs(
            asyncio.run(m._terminate_stream_body(good, "id", 1, "m", None)),
            True, "a fully successful termination did not report success")
        # write_eof is the third and last step.
        dead = _FakeResponse(fail_on={2})
        self.assertIs(
            asyncio.run(m._terminate_stream_body(dead, "id", 1, "m", None)),
            False, "a failed write_eof still reported the body as terminated, "
                   "which is what lets a real failure be suppressed later")
        # A finish chunk and [DONE] that BOTH failed, with a successful EOF, is
        # not "a proper ending" -- the HTTP body is terminated but the SSE
        # conversation never was, and the client has no finish reason and no
        # [DONE]. Reporting True there is what let the shutdown path mark it as
        # cleanly closed and then suppress a later real failure behind it.
        partial = _FakeResponse(fail_on={0, 1})
        self.assertIs(
            asyncio.run(m._terminate_stream_body(partial, "id", 1, "m", None)),
            False, "an EOF with no finish chunk and no [DONE] was reported as a "
                   "complete ending")
        # ...and one failed step is enough to make it incomplete.
        only_done = _FakeResponse(fail_on={0})
        self.assertIs(
            asyncio.run(m._terminate_stream_body(only_done, "id", 1, "m", None)),
            False, "a missing finish chunk still reported a complete ending")

    def test_a_body_whose_writes_all_fail_is_not_marked(self):
        """End to end: intent must not become a mark."""
        m = _mod()
        dead = _FakeResponse(fail_on={0, 1, 2})
        m._LIVE_STREAM_BODIES[dead] = ("id", 1, "m", None)
        asyncio.run(m._terminate_live_stream_bodies(None))
        self.assertNotIn(dead, m._SHUTDOWN_TERMINATED,
                         "a body that never reached the client was recorded as "
                         "cleanly closed")

    def test_the_user_is_TOLD_the_answer_was_cut_short(self):
        """A terminal alone is worse than the truncation it replaced.

        `finish_reason=stop` plus `[DONE]` tells the reader "this is your
        complete answer". A truncated body at least looks broken. Proved on the
        code before this: VISIBLE_SHUTDOWN_NOTICE_COUNT=0,
        FINISH_REASON_STOP_COUNT=1, HANDLER_STILL_RUNNING=True — an unfinished
        answer presented as finished, with the handler still running.
        """
        m = _mod()
        r = _FakeResponse()
        m._LIVE_STREAM_BODIES[r] = ("id", 1, "model", None)
        asyncio.run(m._terminate_live_stream_bodies(None))
        joined = b"".join(r.writes)
        self.assertIn(b"cut short", joined,
                      "the user is handed a silently incomplete answer that "
                      "looks complete")
        self.assertIn(b"restart", joined)
        # ...and it must arrive BEFORE the terminal, inside the answer.
        notice_at = joined.index(b"cut short")
        done_at = joined.index(b"[DONE]")
        self.assertLess(notice_at, done_at,
                        "the notice lands after the conversation was closed")

    def test_a_normal_termination_carries_no_notice(self):
        """The cancellation path is a different event — a user who pressed stop
        must not be told the service restarted."""
        m = _mod()
        r = _FakeResponse()
        asyncio.run(m._terminate_stream_body(r, "id", 1, "model", None))
        self.assertNotIn(b"cut short", b"".join(r.writes))

    def test_the_owning_handler_is_cancelled_before_we_write(self):
        """Two writers on one StreamResponse is the remaining user-visible race.

        Scheduling the real functions produced NOTICE,MODEL_DELTA,FINISH,DONE,EOF
        and NOTICE,FINISH,FINISH,DONE,DONE,EOF — interleaved output and a double
        terminal — and could label a COMPLETE answer "cut short" if its handler
        had not yet deregistered. The callback must hand over to the one task
        that owns the response.
        """
        m = _mod()

        async def _scenario():
            done_first = asyncio.Event()

            async def _owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    done_first.set()
                    raise

            task = asyncio.ensure_future(_owner())
            await asyncio.sleep(0)
            r = _FakeResponse()
            m._LIVE_STREAM_BODIES[r] = ("id", 1, "model", None, task)
            await m._terminate_live_stream_bodies(None)
            return r, task, done_first.is_set()

        r, task, cancelled = asyncio.run(_scenario())
        self.assertTrue(cancelled, "the owning handler was never cancelled, so "
                                   "it can still write while we do")
        self.assertTrue(task.cancelled() or task.done())

    def test_a_handler_that_finished_its_own_ending_is_left_alone(self):
        """A completed handler already terminated honestly. Writing again would
        duplicate the terminal and could tell a user whose answer arrived in
        full that it was cut short."""
        m = _mod()

        async def _scenario():
            async def _already(): return None
            task = asyncio.ensure_future(_already())
            await task
            r = _FakeResponse()
            m._LIVE_STREAM_BODIES[r] = ("id", 1, "model", None, task)
            await m._terminate_live_stream_bodies(None)
            return r

        r = asyncio.run(_scenario())
        self.assertEqual(r.writes, [],
                         "wrote a second ending over a handler that had already "
                         "finished its own")
        self.assertEqual(r.eof_calls, 0)
        self.assertNotIn(r, m._SHUTDOWN_TERMINATED,
                         "marked a body we never wrote to")

    def test_the_handover_actually_WAITS_for_the_owner(self):
        """Cancelling is not handing over.

        A handler does not finish the instant it is cancelled — it still has to
        run its own terminator. Cancel without waiting and we write anyway,
        which is the two-writer race with an extra step. This owner needs a
        moment after cancellation; the callback must not write over it.
        """
        m = _mod()

        async def _scenario():
            async def _owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # The real handler's cancellation path: emit its own
                    # ending, which takes a turn of the loop.
                    await asyncio.sleep(0.05)
                    return None

            task = asyncio.ensure_future(_owner())
            await asyncio.sleep(0)
            r = _FakeResponse()
            m._LIVE_STREAM_BODIES[r] = ("id", 1, "model", None, task)
            await m._terminate_live_stream_bodies(None)
            return r

        r = asyncio.run(_scenario())
        self.assertEqual(
            r.writes, [],
            "the callback wrote over an owner that was still finishing its own "
            "ending -- cancelling is not handing over")

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
        import time as _time
        m = _mod()

        class _Hangs(_FakeResponse):
            async def write(self, data):
                await asyncio.sleep(3600)

        m._LIVE_STREAM_BODIES[_Hangs()] = ("id", 1, "m", None)
        saved = m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS
        m.STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS = 0.05
        # A private loop, closed WITHOUT awaiting stragglers. `asyncio.run`
        # cancels leftover tasks and then awaits them -- and the per-step guard
        # in the terminator swallows that cancellation, so the teardown hangs
        # on a task nobody is waiting for. That measured asyncio.run's cleanup,
        # not the property under test, which is narrower and is the one that
        # matters: does the shutdown callback RETURN inside its bound.
        # BOTH properties, because each alone was wrong once:
        #  * a private loop closed without awaiting stragglers, so the test
        #    measures the callback rather than asyncio.run's teardown (the
        #    teardown hangs on a task whose cancellation the per-step guard
        #    swallows);
        #  * inside a daemon thread with a join deadline, so removing the bound
        #    makes this FAIL in two seconds instead of hanging the suite. I had
        #    each of these and lost the other while fixing the first.
        import threading
        finished, elapsed = threading.Event(), []

        def _worker():
            loop = asyncio.new_event_loop()
            try:
                started = _time.monotonic()
                loop.run_until_complete(m._terminate_live_stream_bodies(None))
                elapsed.append(_time.monotonic() - started)
            except BaseException:
                pass
            finally:
                loop.close()
                finished.set()

        threading.Thread(target=_worker, daemon=True).start()
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

    def test_only_a_LANDED_eof_may_be_marked(self):
        """Intent is not delivery, and this direction of the error is worse.

        The first version marked the response before terminating it, so a body
        whose every write failed was still recorded as cleanly closed — and a
        later, genuine handler exception was then suppressed and rendered to the
        operator as "the user got a proper ending". Proved by a probe that made
        every write fail. A false negative on a real failure is worse than the
        false positive it replaced.
        """
        self.assertIn("_t.result() is True", self.src,
                      "the mark does not depend on the terminator succeeding")
        add_at = self.src.index("_SHUTDOWN_TERMINATED.add(")
        wait_at = self.src.index("_asyncio.wait(")
        self.assertGreater(add_at, wait_at,
                           "the mark is applied before the writes are awaited")

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
