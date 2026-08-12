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


def tearDownModule():
    """`_STREAMS_SHUTTING_DOWN` is a module global that production never
    clears. Leaving it set leaks into every suite that runs after this file in
    the same process -- their stream owners get cancelled at registration."""
    if not _SKIP:
        _mod()._reset_stream_shutdown_gate()


def _registry_store_line(src: str) -> int:
    """The one legitimate store: the one inside `_register_stream_body`."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_register_stream_body":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    for tgt in inner.targets:
                        if (isinstance(tgt, ast.Subscript)
                                and getattr(tgt.value, "id", "") == "_LIVE_STREAM_BODIES"):
                            return inner.lineno
    raise AssertionError("_register_stream_body no longer registers anything")


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

    def test_deregistration_during_the_sweep_does_not_explode(self):
        """A handler deregistering mid-shutdown must not break the sweep.

        HONEST SCOPE, because a mutation proved the limit: this covers a
        handler popping its own entry while shutdown runs. It does NOT cover
        the reason the `list()` snapshot is there — a WeakKeyDictionary
        shrinking under GC *during* the comprehension. Single-threaded asyncio
        cannot interleave a comprehension with another coroutine, so removing
        the snapshot still passes here. The snapshot is correct by construction
        against collection, and that part is untestable deterministically;
        saying so is better than a test that implies otherwise.
        """
        m = _mod()

        async def _scenario():
            keep = _FakeResponse()
            doomed = _FakeResponse()

            async def _owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # exactly what the handler's `finally` does, mid-sweep
                    m._LIVE_STREAM_BODIES.pop(doomed, None)
                    return None

            task = asyncio.ensure_future(_owner())
            await asyncio.sleep(0)
            _register(m, keep, task)
            _register(m, doomed)
            await m._terminate_live_stream_bodies(None)
            return keep

        keep = asyncio.run(_scenario())
        self.assertEqual(keep.eof_calls, 1,
                         "shutdown died partway through the sweep")

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
        idx_reg = self.src.index("_register_stream_body(\n            response,")
        prepare = self.src.rindex("await response.prepare(request)", 0, idx_reg)
        between = self.src[prepare:idx_reg]
        self.assertLess(between.count("\n"), 6,
                        "registration drifted away from its prepare()")

    def test_the_helper_is_the_only_way_into_the_registry(self):
        """The registration gate only closes the arrive-during-shutdown hole if
        every registration goes through it. A second, direct
        `_LIVE_STREAM_BODIES[x] = ...` in a handler is a body nobody ends."""
        tree = ast.parse(self.src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and getattr(tgt.value, "id", "") == "_LIVE_STREAM_BODIES"):
                    offenders.append(node.lineno)
        self.assertEqual(
            offenders, [_registry_store_line(self.src)],
            f"a registration bypasses _register_stream_body at lines "
            f"{offenders} — those bodies are invisible to the shutdown gate")

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




@unittest.skipIf(_SKIP, _SKIP)


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

    def test_the_CANCELLED_branch_asks_the_lifecycle_before_calling_it_an_abort(self):
        """A deploy cancels every open stream. Logging that as `stream_aborted`
        files an explained restart under the same name as an unexplained
        mid-answer failure: the daily report then shows an abort and NO deploy
        interruption, so whoever reads it goes looking for a fault that was a
        release. The existing guard covered the ordinary-Exception branch only;
        cancellation never entered it."""
        marker = "stream_closed_at_shutdown service=gateway phase=cancelled"
        self.assertIn(marker, self.src,
                      "the cancellation branch has no honest label at all")
        idx = self.src.index(marker)
        window = self.src[max(0, idx - 800):idx]
        self.assertIn("_LIVE_STREAM_BODIES.get(response)", window,
                      "the cancellation branch does not consult the lifecycle")
        self.assertIn(".shutdown", window,
                      "the cancellation branch does not read the shutdown "
                      "reason, so it cannot tell a deploy from a fault")
        self.assertIn("logger.info", self.src[max(0, idx - 200):idx],
                      "the honest label is logged above info, which puts a "
                      "clean shutdown back in the failure counts")

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




@unittest.skipIf(_SKIP, _SKIP)
class IrreversibleProgressTests(unittest.TestCase):
    """Round 11's finding: one boolean cannot represent wire progress.

    `terminated=False` is honest about "the ending was not complete" and says
    nothing about the only question that matters afterwards — whether anything
    can still be done about it. Once EOF is on the wire, nothing can. A retry
    then writes into a closed body and looks like a repair while the user keeps
    an incomplete answer that reads as complete.
    """

    def setUp(self):
        self.m = _mod()
        self._flag = self.m._STREAMS_SHUTTING_DOWN
        self.m._LIVE_STREAM_BODIES.clear()

    def tearDown(self):
        self.m._STREAMS_SHUTTING_DOWN = self._flag
        self.m._LIVE_STREAM_BODIES.clear()

    def test_an_undeliverable_notice_withholds_the_claim_of_completeness(self):
        """If we cannot say it was cut short, we must not say it is complete.

        `finish_reason: stop` is this server asserting "that was the whole
        answer". Sending it after the explanation failed converts a visibly
        broken stream into an invisibly wrong one.
        """
        resp = _FakeResponse(fail_on={0})          # the notice write fails
        lc = _register(self.m, resp)
        ok = asyncio.run(self.m._terminate_stream_body(
            resp, "id", 1, "model", None,
            notice=self.m.SHUTDOWN_INTERRUPTION_NOTICE))
        self.assertFalse(ok)
        wire = b"".join(resp.writes)
        self.assertNotIn(b"finish_reason", wire,
                         "claimed the answer was complete after failing to say "
                         "it was cut short")
        self.assertNotIn(b"[DONE]", wire)
        self.assertEqual(resp.eof_calls, 1,
                         "the connection was left hanging instead of released")
        self.assertFalse(lc.terminated)
        self.assertTrue(lc.eof_sent, "EOF happened but was not recorded")

    def test_a_closed_body_is_never_retried_as_if_it_could_be_repaired(self):
        resp = _FakeResponse(fail_on={0})
        lc = _register(self.m, resp)
        asyncio.run(self.m._terminate_stream_body(
            resp, "id", 1, "model", None,
            notice=self.m.SHUTDOWN_INTERRUPTION_NOTICE))
        self.assertTrue(lc.eof_sent)
        attempts_before = len(resp.attempts)
        resp._fail_on = set()                       # "the client came back"
        ok = asyncio.run(self.m._terminate_stream_body(
            resp, "id", 1, "model", None,
            notice=self.m.SHUTDOWN_INTERRUPTION_NOTICE))
        self.assertFalse(ok, "reported success for a body that cannot receive")
        self.assertEqual(len(resp.attempts), attempts_before,
                         "wrote into a closed body — a repair that repairs "
                         "nothing and hides that the answer is incomplete")

    def test_eof_is_recorded_even_when_the_ending_was_incomplete(self):
        resp = _FakeResponse(fail_on={1})           # [DONE] fails, EOF lands
        lc = _register(self.m, resp)
        ok = asyncio.run(self.m._terminate_stream_body(
            resp, "id", 1, "model", None))
        self.assertFalse(ok)
        self.assertEqual(resp.eof_calls, 1)
        self.assertTrue(lc.eof_sent,
                        "an incomplete ending still closed the body; a later "
                        "caller must be told it is past repair")

    def test_a_body_registering_AFTER_the_sweep_has_finished_ends_itself(self):
        """The hole no sweep can close. The old code returned immediately when
        it found nothing open, so a request preparing one millisecond later
        streamed into a dying process and was never ended at all."""
        m = self.m
        state = {}

        async def scenario():
            await m._terminate_live_stream_bodies()   # nothing open: returns
            self.assertTrue(m._STREAMS_SHUTTING_DOWN)

            async def owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    state["cancelled"] = True
                    raise

            task = asyncio.ensure_future(owner())
            await asyncio.sleep(0)
            resp = _FakeResponse()
            lc = m._register_stream_body(resp, ("id", 1, "model", None), task)
            self.assertTrue(lc.shutdown,
                            "a late arrival was not told the process is dying")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled() or state.get("cancelled"),
                            "the late arrival was left streaming into a "
                            "process that is going away")

        asyncio.run(scenario())

    def test_the_drain_keeps_going_while_new_bodies_keep_arriving(self):
        """Two fixed passes miss a third wave exactly as one missed a second.
        Every sweep awaits, and every await is a chance to register.

        The arrival is triggered from inside the fake's `write` so it lands
        deterministically DURING a sweep, one per pass. An owner-cancellation
        chain cannot express this: the gate cancels a late owner while the
        first sweep is still waiting, so waves 2 and 3 both arrive before pass
        2 and the whole chain collapses into two passes — which is why an
        earlier version of this test passed against a two-pass drain.
        """
        m = self.m
        made, depth = [], {"n": 0}

        class _Arriving(_FakeResponse):
            def __init__(self):
                super().__init__()
                self._spawned = False

            async def write(self, data):
                if not self._spawned:
                    self._spawned = True
                    if depth["n"] < 2:
                        depth["n"] += 1
                        nxt = _Arriving()
                        made.append(nxt)
                        m._register_stream_body(
                            nxt, ("id", 1, "model", None), None)
                await super().write(data)

        async def scenario():
            first = _Arriving()
            made.append(first)
            m._register_stream_body(first, ("id", 1, "model", None), None)
            await m._terminate_live_stream_bodies()

        asyncio.run(scenario())
        self.assertEqual(len(made), 3, "the scenario did not produce 3 waves")
        for i, resp in enumerate(made, 1):
            self.assertEqual(resp.eof_calls, 1,
                             f"the body from wave {i} was never ended — the "
                             f"drain stopped before the arrivals did")

    def test_the_drain_is_bounded_by_a_named_constant(self):
        """A drain that loops while work keeps appearing needs a stop, or a
        pathological arrival rate holds the container open for ever."""
        m = self.m
        self.assertGreater(m.STREAM_SHUTDOWN_MAX_DRAIN_WAVES, 2,
                           "a bound of 2 is the fixed-passes bug with a "
                           "constant in front of it")
        self.assertIn("while waves < STREAM_SHUTDOWN_MAX_DRAIN_WAVES:",
                      _api_server_source(), "the drain is not bounded")


@unittest.skipIf(_SKIP, _SKIP)
class BoundedWriterTests(unittest.TestCase):
    """Round 11 A4: the sweep was bounded, the writer it spawned was not.

    Cancelling a task only REQUESTS that it stop. A task blocked in a write
    does not stop until that write returns -- and the per-step guard was
    catching the cancellation and starting the NEXT unbounded write, so the
    task lived on holding its lifecycle lock, past the callback, into loop
    teardown, and lost to SIGKILL. Which is the truncation this whole path
    exists to prevent, reached the long way round.
    """

    # SCALED, not chosen. Hand-picked fast constants are how the real defect
    # hid: production allowed four 2.0s steps inside a 5.0s deadline plus a
    # 2.0s drain (8s of work in a 7s budget), while this fixture used four
    # 0.05s steps inside 0.10 + 0.50 (0.2s of work in a 0.6s budget). The
    # fixture was generous exactly where production was short, so a test that
    # looked like it modelled the deadline modelled its opposite. One factor,
    # applied to every constant, keeps the ratio the code actually ships.
    _SCALE = 0.02
    _SCALED = (
        "STREAM_SHUTDOWN_STEP_TIMEOUT_SECONDS",
        "STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS",
        "STREAM_SHUTDOWN_WRITER_DRAIN_SECONDS",
        "STREAM_SHUTDOWN_HANDOVER_SECONDS",
    )

    def setUp(self):
        self.m = _mod()
        self._saved = {k: getattr(self.m, k) for k in self._SCALED}
        self._flag = self.m._STREAMS_SHUTTING_DOWN
        for k, v in self._saved.items():
            setattr(self.m, k, v * self._SCALE)
        self.m._LIVE_STREAM_BODIES.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.m, k, v)
        self.m._STREAMS_SHUTTING_DOWN = self._flag
        self.m._LIVE_STREAM_BODIES.clear()

    def _blocking_response(self):
        class _Blocking(_FakeResponse):
            async def write(self, data):
                self.attempts.append("write")
                await asyncio.sleep(3600)

            async def write_eof(self):
                self.attempts.append("write_eof")
                await asyncio.sleep(3600)

        return _Blocking()

    def test_a_writer_blocked_on_every_step_still_ends(self):
        m, seen = self.m, {}

        async def scenario():
            resp = self._blocking_response()
            lc = m._register_stream_body(resp, ("id", 1, "model", None), None)
            await m._terminate_live_stream_bodies()
            seen["locked"] = lc.lock.locked()
            seen["alive"] = [t for t in asyncio.all_tasks()
                             if t is not asyncio.current_task() and not t.done()]
            seen["attempts"] = list(resp.attempts)

        asyncio.run(scenario())
        self.assertFalse(seen["locked"],
                         "the writer still holds the lifecycle lock after the "
                         "sweep returned — the next wave can never terminate "
                         "this body")
        self.assertEqual(seen["alive"], [],
                         "a terminator task outlived the shutdown callback; at "
                         "process teardown it becomes a truncated response")
        self.assertGreater(len(seen["attempts"]), 1,
                           "one blocked step swallowed the whole ending")
        self.assertIn("write_eof", seen["attempts"],
                      "a blocked content write cost the client its EOF")

    def test_the_deadline_can_actually_accommodate_the_work_it_authorises(self):
        """The arithmetic, checked rather than assumed.

        Four bounded steps at 2.0s each is 8s of permitted work; the sweep gave
        up at 5s and drained for 2. The sweep was therefore GUARANTEED, on a
        slow transport, to return while its own writer was still legitimately
        working -- the exact state the drain exists to prevent, written into
        the constants.
        """
        m = self.m
        worst_case = (self._saved["STREAM_SHUTDOWN_STEP_TIMEOUT_SECONDS"]
                      * m.STREAM_SHUTDOWN_MAX_TERMINAL_STEPS)
        budget = (self._saved["STREAM_SHUTDOWN_TERMINATE_TIMEOUT_SECONDS"]
                  + self._saved["STREAM_SHUTDOWN_WRITER_DRAIN_SECONDS"])
        self.assertLessEqual(
            worst_case, budget,
            f"the terminator may legitimately spend {worst_case}s, and the "
            f"sweep waits {budget}s — a deadline that cannot accommodate the "
            f"work it authorises is a lie with a number in it")

    def test_a_writer_cancelled_at_the_deadline_STOPS_instead_of_walking_on(self):
        """Round 12 A1. One cancellation policy cannot serve two owners.

        The handler's own terminator is deliberately stubborn: it is already
        being cancelled, which is WHY it is writing an ending, so a second
        cancellation must not stop it mid-sentence. The shutdown callback's
        writer was cancelled BECAUSE the deadline expired -- if it walks on to
        the next step it spends budget nobody is waiting for, and the sweep
        returns while it still holds the lifecycle lock.

        The NOTICE must succeed first. A cancellation on the notice takes the
        undeliverable-notice gate and returns immediately, so it exercises none
        of this -- which is exactly what an earlier version of this test did,
        and it passed against a writer that walked on.
        """
        m, seen = self.m, {}

        class _BlockAfterFirst(_FakeResponse):
            async def write(self, data):
                self.attempts.append("write")
                if len(self.attempts) > 1:
                    await asyncio.sleep(3600)
                self.writes.append(data)

            async def write_eof(self):
                self.attempts.append("write_eof")
                await asyncio.sleep(3600)

        async def scenario():
            resp = _BlockAfterFirst()
            lc = m._register_stream_body(resp, ("id", 1, "model", None), None)
            task = asyncio.ensure_future(m._terminate_stream_body(
                resp, "id", 1, "model", None,
                notice=m.SHUTDOWN_INTERRUPTION_NOTICE, shutdown_writer=True))
            # Land inside the SECOND step, after the notice has gone out.
            await asyncio.sleep(m.STREAM_SHUTDOWN_STEP_TIMEOUT_SECONDS * 0.5)
            task.cancel()
            await asyncio.sleep(m.STREAM_SHUTDOWN_STEP_TIMEOUT_SECONDS * 5)
            seen["done"] = task.done()
            seen["cancelled"] = task.done() and task.cancelled()
            seen["locked"] = lc.lock.locked()
            seen["attempts"] = list(resp.attempts)

        asyncio.run(scenario())
        self.assertTrue(seen["done"],
                        "the cancelled shutdown writer is still running")
        self.assertFalse(seen["locked"],
                         "the cancelled shutdown writer still holds the "
                         "lifecycle lock, so no later wave can end this body")
        self.assertTrue(
            seen["cancelled"],
            "it swallowed its own cancellation and returned normally — the "
            "sweep's drain then has nothing to observe finishing")
        writes = [a for a in seen["attempts"] if a == "write"]
        self.assertLess(
            len(writes), 3,
            f"it kept writing after the deadline cancelled it: "
            f"{seen['attempts']}")
        self.assertIn("write_eof", seen["attempts"],
                      "stopping cost the client the one step it waits for")

    def test_the_HANDLER_owned_terminator_still_finishes_its_sentence(self):
        """The other half of the same rule. Making the callback writer stop
        must not make the handler's own terminator give up -- that stubbornness
        is a previous fix (a4930218) and re-swallowing it here would reopen the
        truncation it closed."""
        m, seen = self.m, {}

        class _CancelOnce(_FakeResponse):
            def __init__(self):
                super().__init__()
                self._fired = False

            async def write(self, data):
                self.attempts.append("write")
                if not self._fired:
                    self._fired = True
                    raise asyncio.CancelledError()
                self.writes.append(data)

        async def scenario():
            resp = _CancelOnce()
            m._register_stream_body(resp, ("id", 1, "model", None), None)
            await m._terminate_stream_body(
                resp, "id", 1, "model", None,
                notice=m.SHUTDOWN_INTERRUPTION_NOTICE)   # handler-owned
            seen["attempts"] = list(resp.attempts)

        asyncio.run(scenario())
        self.assertIn("write_eof", seen["attempts"],
                      "a cancellation on one step cost the client its EOF")

    def test_the_sweep_summary_cannot_report_more_outcomes_than_attempts(self):
        """Round 13 C. `attempted` counted the tasks spawned; the outcomes were
        counted over the whole input batch. A body that finishes DURING the
        handover is in the batch and not in the tasks, so the summary read
        `attempted=1 complete=2` — describing a set that does not exist.

        The drain loop filters already-finished bodies before the sweep sees
        them, so the mixed batch only arises in that handover window: the owner
        is cancelled, ends its own body honestly, and the sweep then has one
        body left to write and two to account for.
        """
        m, seen = self.m, {}

        class _Log:
            def __init__(self): self.lines = []
            def info(self, fmt, *args): self.lines.append(fmt % args if args else fmt)
            def warning(self, *a, **k): pass
            def error(self, *a, **k): pass

        async def scenario():
            log = _Log()

            # Body A: an owner that ends its own body when cancelled, exactly
            # as the handler's CancelledError branch does.
            a_resp = _FakeResponse()

            async def a_owner():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await m._terminate_stream_body(
                        a_resp, "id", 1, "model", log,
                        notice=m.SHUTDOWN_INTERRUPTION_NOTICE)
                    raise

            a_task = asyncio.ensure_future(a_owner())
            await asyncio.sleep(0)
            m._register_stream_body(a_resp, ("id", 1, "model", log), a_task)

            # Body B: nobody to finish it, so the sweep must write this one.
            b_resp = _FakeResponse()
            m._register_stream_body(b_resp, ("id", 1, "model", log), None)

            await m._terminate_live_stream_bodies()
            seen["lines"] = [l for l in log.lines if "stream_shutdown_sweep" in l]

        asyncio.run(scenario())
        self.assertTrue(seen["lines"], "the sweep logged no summary at all")
        line = seen["lines"][0]
        nums = {k: int(v) for k, v in
                (kv.split("=") for kv in line.split() if "=" in kv)
                if v.isdigit()}
        outcomes = nums["complete"] + nums["closed_bare"] + nums["still_open"]
        self.assertEqual(
            outcomes, nums["attempted"],
            f"the summary reports {outcomes} outcomes for {nums['attempted']} "
            f"attempts — a set that does not exist: {line}")

    def test_a_body_the_owner_ALREADY_ended_gets_no_second_attempt(self):
        """Found by deploying the previous commit to the live 8083 gateway.

        One stream, restarted mid-answer. The client received the notice,
        `finish_reason: stop` and `[DONE]` — curl exited 0. And the log said:

            stream_closed_at_shutdown ... phase=cancelled
            stream_notice_undeliverable service=gateway phase=terminal
            stream_shutdown_sweep attempted=1 complete=1 closed_bare=0 still_open=0

        A perfectly delivered interruption reported as a user who was never
        told. The owner ended the body and deregistered; the sweep, still
        holding a lifecycle that said `terminated=True`, looked the response up
        in the registry, found nothing, took the unregistered path and wrote
        into a closed body. Left alone it would put a false failure in every
        deploy's numbers — and the whole point of these rounds is that the
        operator signal must not lie.
        """
        m, seen = self.m, {}

        async def scenario():
            resp = _FakeResponse()
            lc = m._register_stream_body(resp, ("id", 1, "model", None), None)
            # The owner ends it properly, then deregisters, exactly as the
            # handler's `finally` does.
            await m._terminate_stream_body(
                resp, "id", 1, "model", None,
                notice=m.SHUTDOWN_INTERRUPTION_NOTICE)
            self.assertTrue(lc.terminated)
            m._LIVE_STREAM_BODIES.pop(resp, None)
            attempts_after_owner = len(resp.attempts)
            # The sweep, holding the lifecycle it snapshotted earlier.
            ok = await m._terminate_stream_body(
                resp, "id", 1, "model", None,
                notice=m.SHUTDOWN_INTERRUPTION_NOTICE,
                shutdown_writer=True, lifecycle=lc)
            seen["ok"] = ok
            seen["extra"] = len(resp.attempts) - attempts_after_owner

        asyncio.run(scenario())
        self.assertEqual(seen["extra"], 0,
                         "the sweep wrote a second ending into a body the "
                         "owner had already closed")
        self.assertTrue(seen["ok"],
                        "an already-complete body was reported as a failure")

    def test_the_SWEEP_leaves_no_writer_alive_when_the_notice_landed_first(self):
        """The end-to-end shape of round 12 A1, at the level it was measured.

        Its probe reported, immediately after `_terminate_live_stream_bodies()`
        returned and before loop teardown:

            CANCEL_DRAIN elapsed=0.302 alive=1 lock=True
                         attempts=write1,write2,write3,eof

        A writer still on the wire, still holding the lifecycle lock, after the
        callback said it was finished. Blocking on the FIRST write cannot show
        this -- that path returns through the undeliverable-notice gate.
        """
        m, seen = self.m, {}

        class _BlockAfterFirst(_FakeResponse):
            async def write(self, data):
                self.attempts.append("write")
                if len(self.attempts) > 1:
                    await asyncio.sleep(3600)
                self.writes.append(data)

            async def write_eof(self):
                self.attempts.append("write_eof")
                await asyncio.sleep(3600)

        async def scenario():
            resp = _BlockAfterFirst()
            lc = m._register_stream_body(resp, ("id", 1, "model", None), None)
            await m._terminate_live_stream_bodies()
            # No await in between: whatever is running here was running when
            # the shutdown callback returned.
            seen["alive"] = [tk for tk in asyncio.all_tasks()
                             if tk is not asyncio.current_task()
                             and not tk.done()]
            seen["locked"] = lc.lock.locked()

        asyncio.run(scenario())
        self.assertEqual(seen["alive"], [],
                         "the shutdown callback returned while a writer it "
                         "started was still on the wire")
        self.assertFalse(seen["locked"],
                         "that writer still owns the lifecycle lock")

    def test_the_sweep_does_not_report_done_while_a_writer_is_still_running(self):
        """`asyncio.wait(..., timeout)` returning is not the writer stopping."""
        m, seen = self.m, {}

        async def scenario():
            resp = self._blocking_response()
            m._register_stream_body(resp, ("id", 1, "model", None), None)
            await m._terminate_live_stream_bodies()
            # No await between the sweep returning and this check: anything
            # still running here was still running when the callback returned.
            seen["alive"] = [t for t in asyncio.all_tasks()
                             if t is not asyncio.current_task() and not t.done()]

        asyncio.run(scenario())
        self.assertEqual(seen["alive"], [],
                         "the shutdown callback returned while a writer it "
                         "started was still on the wire")


class TestFileHygieneTests(unittest.TestCase):
    def test_no_test_class_is_shadowed_by_a_later_copy(self):
        """Ten tests in this file were defined twice. Python kept the second
        definition and silently discarded the first, so a green run was
        reporting on tests that did not exist. Suites do not shrink loudly."""
        src = Path(__file__).read_text(encoding="utf-8")
        names = [n.name for n in ast.parse(src).body
                 if isinstance(n, ast.ClassDef)]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [],
                         f"shadowed test class(es) {dupes}: the earlier copy "
                         f"never runs, whatever it asserts")
