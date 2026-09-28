"""Propagate agent-turn context into worker threads that dispatch Hermes tools.

A bare ``threading.Thread`` / ``ThreadPoolExecutor`` worker starts with an empty
``contextvars.Context`` and no thread-local approval/sudo callbacks, so tool dispatch inside it
silently loses the approval ContextVars (gateway sessions then auto-approve dangerous commands)
and the CLI callbacks (``prompt_dangerous_approval`` cannot reach the user, GHSA-qg5c-hvr5-hjgr).
Call :func:`propagate_context_to_thread` **on the parent thread** (it snapshots at call time) and
use the result as the worker target; callbacks are installed for the worker's lifetime and
always cleared on exit.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """(getter, setter) pairs for every thread-local prompt callback a tool may need mid-dispatch
    (lazy: terminal_tool imports tools.approval at load, so a top-level import risks a cycle).
    Add a new per-thread prompt here — a callback missing from this table is silently absent on
    every parallel/timeout worker, so the tool believes nobody can answer."""
    from agent.vault_backends import unlock as vault_unlock
    from tools import terminal_tool as tt

    return ((tt._get_approval_callback, tt.set_approval_callback),
            (tt._get_sudo_password_callback, tt.set_sudo_password_callback),
            (vault_unlock.get_unlock_prompt_callback, vault_unlock.set_unlock_prompt_callback),
            (vault_unlock.get_save_login_prompt_callback, vault_unlock.set_save_login_prompt_callback),
            (vault_unlock.get_code_prompt_callback, vault_unlock.set_code_prompt_callback))


def propagate_context_to_thread(target: Callable) -> Callable:
    """Wrap *target* to run with the *current* thread's ContextVars and per-thread prompt callbacks
    (approval, sudo, password-manager unlock).

    Fail-closed: if callback installation raises they stay ``None`` — dangerous commands are then
    denied by ``prompt_dangerous_approval`` and the gateway approval queue blocks.
    """
    ctx = contextvars.copy_context()
    # The session id is thread-local, and a tool runs on its own worker, so
    # every failure a tool recorded was anonymous: the ledger knew WHICH LAYER
    # failed but never WHOSE request. That is the one thing the plan's
    # per-journey contract needs, and the id already encodes user and chat.
    # Proved blocking by adversarial review round 9, 2026-08-10.
    parent_session_id = None
    try:
        from hermes_logging import _session_context as _sc
        parent_session_id = getattr(_sc, "session_id", None)
    except Exception:
        logger.debug("Could not capture parent session context", exc_info=True)
    # (setter, parent callback) pairs; None when the callback API could not be captured.
    installs = None
    try:
        installs = tuple((setter, getter()) for getter, setter in _callback_api())
    except Exception:
        logger.debug("Could not capture parent approval/sudo callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            # ThreadPoolExecutor workers are reused. Clear before installing so
            # a request with no parent session cannot inherit the previous
            # request's id; clear again in finally so early failures do not
            # poison the next job on this worker.
            try:
                from hermes_logging import clear_session_context
                clear_session_context()
            except Exception:
                logger.debug("Could not clear stale worker session context",
                             exc_info=True)
            if parent_session_id:
                try:
                    from hermes_logging import set_session_context
                    set_session_context(parent_session_id)
                except Exception:
                    logger.debug("Could not install session context on worker",
                                 exc_info=True)
            try:
                for setter, cb in installs or ():
                    if cb is not None:
                        setter(cb)
            except Exception:
                logger.debug("Failed to install propagated approval/sudo callbacks; "
                             "dangerous-command approval will fail closed", exc_info=True)
            try:
                return target(*args, **kwargs)
            finally:
                try:
                    from hermes_logging import clear_session_context
                    clear_session_context()
                except Exception:
                    logger.debug("Could not clear worker session context",
                                 exc_info=True)
                try:
                    for setter, _cb in installs or ():
                        setter(None)
                except Exception:
                    logger.debug("Failed to clear propagated approval/sudo callbacks",
                                 exc_info=True)

        return ctx.run(_inner)

    return _runner
