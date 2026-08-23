from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import hermes_logging
from tools.thread_context import propagate_context_to_thread


def _current_log_session():
    return getattr(hermes_logging._session_context, "session_id", None)


def test_reused_worker_never_inherits_previous_log_session():
    try:
        hermes_logging.set_session_context("first-request")
        first = propagate_context_to_thread(_current_log_session)

        hermes_logging.clear_session_context()
        second = propagate_context_to_thread(_current_log_session)

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(first).result() == "first-request"
            assert pool.submit(second).result() is None
            assert pool.submit(_current_log_session).result() is None
    finally:
        hermes_logging.clear_session_context()

