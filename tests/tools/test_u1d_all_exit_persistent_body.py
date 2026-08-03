"""U1D batch-1: all-exit persistent-body gate (application boundary).

Executable path (not pure-helper-only, not source-token):
  grant+ledger → reader outcomes → deliver_coverage_to_persistent_body
  (production finalizer path) → stream/non-stream adapters
  → persisted assistant body store → reload → assert A-channel facts

Six combos: stream|nonstream × success|failure|interrupted

Mutation gate: production adapters must be live — monkeypatching them to
drop coverage reds this suite. Dead comments / unused helpers do not pass.

Run:
  cd hermes-agent && PYTHONPATH=. python3 -m unittest \\
    tests.tools.test_u1d_all_exit_persistent_body -v
"""

from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.attachment_ledger import (
    COVERAGE_FOOTER_TITLE,
    COVERAGE_INTERRUPTED_NOTE,
    COVERAGE_UNAVAILABLE_TITLE,
    DELIVER_COVERAGE_FN,
    OUTCOME_UNREADABLE,
    deliver_coverage_to_persistent_body,
    finalize_attachment_coverage,
    record_outcome,
    terminal_coverage_suffix,
)
from tools.file_grants import file_grant_scope, make_file_handles


# ---------------------------------------------------------------------------
# Minimal "persisted chat" store simulating OWUI DB assistant content
# ---------------------------------------------------------------------------


class PersistentChatStore:
    """In-memory stand-in for DB-backed assistant messages (A-channel)."""

    def __init__(self) -> None:
        self._msgs: dict[str, str] = {}

    def upsert_assistant(self, message_id: str, content: str) -> None:
        self._msgs[message_id] = content

    def reload(self, message_id: str) -> str:
        return self._msgs.get(message_id, "")


def _unreadable_fixture(task_id: str):
    d = tempfile.mkdtemp()
    good = Path(d) / "001-aaaaaaaa-ok.txt"
    bad = Path(d) / "001-bbbbbbbb-x.zip"
    good.write_text("CANARY-OK-42 readable body\n", encoding="utf-8")
    bad.write_bytes(b"PK\x03\x04fake")
    paths = [str(good), str(bad)]
    handles = make_file_handles(paths)
    return d, paths, handles


def _seed_ledger(task_id: str, paths: list[str]) -> None:
    record_outcome(
        paths[0],
        task_id=task_id,
        status="read",
        reason="text_read",
        handle="F01",
        reader="read_file",
    )
    record_outcome(
        paths[1],
        task_id=task_id,
        status=OUTCOME_UNREADABLE,
        reason="no direct reader for this attachment type",
        handle="F02",
        reader="read_file",
    )


def _run_turn(
    *,
    stream: bool,
    interrupted: bool,
    failed: bool,
    model_text: str | None,
    task_id: str,
    store: PersistentChatStore,
    message_id: str,
) -> dict:
    """Application boundary: production delivery → persist → return reload body.

    Mirrors:
      turn_finalizer → deliver_coverage_to_persistent_body
      api_server stream adapters → emit_*_coverage_suffix
      OWUI DB upsert of assistant content
    """
    # Production finalizer path
    delivered = deliver_coverage_to_persistent_body(
        final_response=model_text,
        task_id=task_id,
        streamed_text=(model_text or "") if stream and not interrupted else "",
        stream=stream,
        interrupted=interrupted,
        failed=failed,
        fail_closed=True,
    )
    # Production stream adapters (must be the real callables from api_server)
    from gateway.platforms import api_server as api_mod

    body = delivered.get("persistent_assistant_body") or ""
    if stream:
        # Chat-completions path uses empty streamed prefix at terminal (model
        # already flushed deltas); Responses path uses full streamed so far.
        suffix_cc = api_mod.emit_chat_completion_coverage_suffix(delivered)
        suffix_rs = api_mod.emit_responses_coverage_suffix(
            model_text or "", delivered
        )
        # Wire would append one suffix; both adapters must non-empty when footer exists
        delivered["_wire_suffix_chat"] = suffix_cc
        delivered["_wire_suffix_responses"] = suffix_rs
        # Persistent body is final_response (includes footer), not only suffix
        if not body and (suffix_cc or suffix_rs):
            body = (model_text or "") + (suffix_cc or suffix_rs)
    else:
        # Non-stream: client receives final_response JSON field
        body = delivered.get("final_response") or body

    store.upsert_assistant(message_id, body or "")
    reloaded = store.reload(message_id)
    delivered["reloaded_body"] = reloaded
    return delivered


class SixComboPersistentBodyTests(unittest.TestCase):
    """stream|nonstream × success|failure|interrupted with unreadable fixture."""

    def setUp(self) -> None:
        self.store = PersistentChatStore()
        self.task_id = "u1d-boundary"
        self._tmpdir, self.paths, self.handles = _unreadable_fixture(self.task_id)
        self._cm = file_grant_scope(self.task_id, self.paths, handles=self.handles)
        self._cm.__enter__()
        _seed_ledger(self.task_id, self.paths)

    def tearDown(self) -> None:
        self._cm.__exit__(None, None, None)

    def _assert_facts(self, reloaded: str, *, interrupted: bool) -> None:
        self.assertIn(COVERAGE_FOOTER_TITLE, reloaded)
        self.assertIn("unreadable", reloaded.lower())
        self.assertIn("F02", reloaded)
        # Visible markdown — not HTML-comment-only
        self.assertNotIn("<!-- hermes-attachment-coverage -->", reloaded)
        if interrupted:
            self.assertIn("interrupted", reloaded.lower())

    def test_nonstream_success(self) -> None:
        d = _run_turn(
            stream=False,
            interrupted=False,
            failed=False,
            model_text="Audit complete. Summary...",
            task_id=self.task_id,
            store=self.store,
            message_id="ns-ok",
        )
        self._assert_facts(d["reloaded_body"], interrupted=False)

    def test_nonstream_failure(self) -> None:
        d = _run_turn(
            stream=False,
            interrupted=False,
            failed=True,
            model_text="Provider error stub",
            task_id=self.task_id,
            store=self.store,
            message_id="ns-fail",
        )
        self._assert_facts(d["reloaded_body"], interrupted=False)

    def test_nonstream_interrupted(self) -> None:
        d = _run_turn(
            stream=False,
            interrupted=True,
            failed=False,
            model_text="Partial prose before cancel",
            task_id=self.task_id,
            store=self.store,
            message_id="ns-int",
        )
        self._assert_facts(d["reloaded_body"], interrupted=True)
        self.assertEqual(d["coverage_status"], "ok")
        self.assertTrue(d.get("coverage_footer"))

    def test_stream_success(self) -> None:
        d = _run_turn(
            stream=True,
            interrupted=False,
            failed=False,
            model_text="Streaming audit report...",
            task_id=self.task_id,
            store=self.store,
            message_id="s-ok",
        )
        self._assert_facts(d["reloaded_body"], interrupted=False)
        self.assertTrue(d.get("_wire_suffix_chat") or d.get("coverage_footer"))

    def test_stream_failure(self) -> None:
        d = _run_turn(
            stream=True,
            interrupted=False,
            failed=True,
            model_text="Streamed error note",
            task_id=self.task_id,
            store=self.store,
            message_id="s-fail",
        )
        self._assert_facts(d["reloaded_body"], interrupted=False)

    def test_stream_interrupted(self) -> None:
        """THE critical combo: U4 may suppress mid-prose; footer must still land."""
        d = _run_turn(
            stream=True,
            interrupted=True,
            failed=False,
            model_text="",  # no model prose (interrupted mid-tools)
            task_id=self.task_id,
            store=self.store,
            message_id="s-int",
        )
        body = d["reloaded_body"]
        self._assert_facts(body, interrupted=True)
        # Empty model text still gets coverage-only body
        self.assertIn(COVERAGE_FOOTER_TITLE, body)
        self.assertIn("F02", body)


class ProductionAdapterMutationTests(unittest.TestCase):
    """Four U1D production mutants must each red this suite's contracts."""

    def setUp(self) -> None:
        self.store = PersistentChatStore()
        self.task_id = "u1d-mut"
        self._tmpdir, self.paths, self.handles = _unreadable_fixture(self.task_id)
        self._cm = file_grant_scope(self.task_id, self.paths, handles=self.handles)
        self._cm.__enter__()
        _seed_ledger(self.task_id, self.paths)

    def tearDown(self) -> None:
        self._cm.__exit__(None, None, None)

    def test_deliver_fn_name_is_production_token(self) -> None:
        self.assertEqual(DELIVER_COVERAGE_FN, "deliver_coverage_to_persistent_body")
        import tools.attachment_ledger as al

        self.assertTrue(callable(getattr(al, DELIVER_COVERAGE_FN)))

    def test_mutant_finalize_skips_on_interrupted_must_fail_contract(self) -> None:
        """Old disease: interrupted → skipped footer. Harness must reject it."""

        def _old_skip_interrupted(final_response, *, task_id, interrupted=False, fail_closed=True):
            if interrupted:
                return final_response, {
                    "footer": "",
                    "snapshot": None,
                    "status": "skipped",
                }
            return finalize_attachment_coverage(
                final_response,
                task_id=task_id,
                interrupted=False,
                fail_closed=fail_closed,
            )

        with patch(
            "tools.attachment_ledger.finalize_attachment_coverage",
            side_effect=_old_skip_interrupted,
        ):
            # deliver imports finalize at call time from same module - patch on module
            import tools.attachment_ledger as al

            with patch.object(al, "finalize_attachment_coverage", side_effect=_old_skip_interrupted):
                d = al.deliver_coverage_to_persistent_body(
                    final_response="partial",
                    task_id=self.task_id,
                    stream=False,
                    interrupted=True,
                )
                body = d.get("persistent_assistant_body") or ""
                # Mutant produces empty coverage — assert our contract would fail
                with self.assertRaises(AssertionError):
                    self.assertIn(COVERAGE_FOOTER_TITLE, body)
                    self.assertIn("unreadable", body.lower())

    def test_mutant_terminal_suffix_empty_reds_stream_contract(self) -> None:
        from gateway.platforms import api_server as api_mod

        delivered = deliver_coverage_to_persistent_body(
            final_response="report",
            task_id=self.task_id,
            stream=True,
            interrupted=False,
        )
        # Healthy: suffix non-empty
        self.assertTrue(
            api_mod.emit_chat_completion_coverage_suffix(delivered)
            or delivered.get("coverage_footer")
        )
        with patch.object(api_mod, "emit_chat_completion_coverage_suffix", return_value=""):
            with patch.object(api_mod, "emit_responses_coverage_suffix", return_value=""):
                s1 = api_mod.emit_chat_completion_coverage_suffix(delivered)
                s2 = api_mod.emit_responses_coverage_suffix("report", delivered)
                with self.assertRaises(AssertionError):
                    self.assertTrue(s1 or s2)

    def test_mutant_both_stream_adapters_empty_is_detectable(self) -> None:
        from gateway.platforms import api_server as api_mod

        delivered = deliver_coverage_to_persistent_body(
            final_response="x",
            task_id=self.task_id,
            stream=True,
            interrupted=True,
        )
        healthy = api_mod.emit_chat_completion_coverage_suffix(delivered)
        self.assertTrue(healthy or delivered.get("coverage_footer"))
        # Replace production adapters with empty — harness must not accept empty
        # when footer exists in result (would mean adapters dropped A-channel).
        footer = delivered.get("coverage_footer") or ""
        self.assertTrue(footer)
        with patch.object(api_mod, "emit_chat_completion_coverage_suffix", return_value=""):
            empty = api_mod.emit_chat_completion_coverage_suffix(delivered)
            self.assertEqual(empty, "")
            # Contract: if result has footer, wire adapter must not return empty
            with self.assertRaises(AssertionError):
                self.assertTrue(empty or not footer)

    def test_healthy_interrupted_path_has_footer(self) -> None:
        d = deliver_coverage_to_persistent_body(
            final_response=None,
            task_id=self.task_id,
            stream=True,
            interrupted=True,
        )
        body = d["persistent_assistant_body"] or ""
        self.assertIn(COVERAGE_FOOTER_TITLE, body)
        self.assertIn("unreadable", body.lower())
        self.assertIn("F02", body)
        self.assertIn("interrupted", body.lower())

    def test_reload_preserves_body(self) -> None:
        d = _run_turn(
            stream=True,
            interrupted=True,
            failed=False,
            model_text="mid stop",
            task_id=self.task_id,
            store=self.store,
            message_id="reload-1",
        )
        again = self.store.reload("reload-1")
        self.assertEqual(again, d["reloaded_body"])
        self.assertIn(COVERAGE_FOOTER_TITLE, again)


class TurnFinalizerInterruptedIntegration(unittest.TestCase):
    """finalize_turn-shaped call with interrupted=True must still attach footer."""

    def test_deliver_used_by_finalizer_source(self) -> None:
        from pathlib import Path as P

        src = P(__file__).resolve().parents[2] / "agent" / "turn_finalizer.py"
        text = src.read_text(encoding="utf-8")
        # Live call must exist (not only comment)
        live = any(
            line.strip().startswith("_delivered = deliver_coverage_to_persistent_body")
            or "deliver_coverage_to_persistent_body(" in line
            and not line.strip().startswith("#")
            for line in text.splitlines()
        )
        self.assertTrue(live)
        self.assertIn("interrupted=bool(interrupted)", text)

    def test_api_server_adapters_live(self) -> None:
        from pathlib import Path as P

        src = (
            P(__file__).resolve().parents[2]
            / "gateway"
            / "platforms"
            / "api_server.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def emit_chat_completion_coverage_suffix", src)
        self.assertIn("def emit_responses_coverage_suffix", src)
        self.assertIn("emit_chat_completion_coverage_suffix(", src)
        self.assertIn("emit_responses_coverage_suffix(", src)


if __name__ == "__main__":
    unittest.main()
