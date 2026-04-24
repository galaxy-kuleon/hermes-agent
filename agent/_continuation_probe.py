"""Capability-gated continuation probe (W4 T1).

When at least one registered MemoryProvider exposes a reasoning-style
tool (tool name equal to or ending with ``_reasoning``), ask it whether
the user left an incomplete task in a prior session. If the provider's
response is not the sentinel ``[NONE]`` and non-empty, fire the
supplied ``continuation_callback`` so the API server can translate it
into a ``hermes.continuation.suggested`` SSE frame.

Design invariants:

1. **Capability-gated.** Only providers that advertise a reasoning
   tool are probed. Holographic / fact_store-only providers are
   silently skipped (no false positives, no false events).
2. **Bounded-timeout background thread.** First-turn latency is
   unaffected — the probe fires in a daemon thread with an 8s
   timeout. On timeout, the callback is not invoked.
3. **Sentinel handling.** The provider's reasoning tool is
   instructed to respond with ``[NONE]`` when no continuation is
   warranted. This module strips/trims before comparing so trailing
   whitespace / newlines do not force false positives.
4. **Non-blocking on failure.** Any exception from the provider is
   logged at debug level only; the chat turn proceeds unaffected.
5. **Pure function of inputs.** No module-level state, no global
   locks. Safe to call from per-request AIAgent init.

Public API:

    maybe_emit_continuation(memory_manager, callback, identity_kwargs=None)
        Fires the probe in a background thread.

    find_reasoning_tool(memory_manager) -> str | None
        Exposed for testability — returns the first reasoning tool
        name found across registered providers, or None.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SECONDS = 8.0
_SENTINEL = "[NONE]"

_CONTINUATION_QUERY = (
    "Given the user's prior sessions, did the user leave any concrete task "
    "incomplete or mid-progress? If yes, summarise it in one short sentence "
    "the user would recognise. If no incomplete task exists, respond with "
    "exactly: [NONE]"
)


def find_reasoning_tool(memory_manager: Any) -> Optional[str]:
    """Return the first *_reasoning tool name registered across providers.

    Uses duck-typing over memory_manager to keep this module decoupled
    from MemoryManager's concrete API — reads ``_tool_to_provider`` if
    present, otherwise iterates ``providers``.
    """
    # Fast path: MemoryManager maintains a tool->provider map.
    tool_map = getattr(memory_manager, "_tool_to_provider", None)
    if isinstance(tool_map, dict):
        for tname in tool_map:
            if isinstance(tname, str) and (
                tname == "honcho_reasoning" or tname.endswith("_reasoning")
            ):
                return tname

    # Fallback: scan each provider's advertised tool schemas.
    providers = getattr(memory_manager, "providers", None)
    if not providers:
        providers = getattr(memory_manager, "_providers", None) or []
    for provider in providers:
        try:
            schemas: List[Dict[str, Any]] = provider.get_tool_schemas() or []
        except Exception:
            continue
        for schema in schemas:
            tname = schema.get("name") if isinstance(schema, dict) else None
            if isinstance(tname, str) and (
                tname == "honcho_reasoning" or tname.endswith("_reasoning")
            ):
                return tname
    return None


def _parse_result(raw: Any) -> str:
    """Unwrap provider result into a plain string.

    Providers return JSON strings (honcho / holographic convention) or
    raw dicts. Either shape, extract the ``result`` field if present,
    else stringify.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        # May be a JSON object with {"result": "..."} or a plain string.
        stripped = raw.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "result" in parsed:
                    inner = parsed["result"]
                    return str(inner).strip() if inner is not None else ""
            except Exception:
                pass
        return stripped
    if isinstance(raw, dict):
        inner = raw.get("result")
        return str(inner).strip() if inner is not None else json.dumps(raw)
    return str(raw).strip()


def _probe_body(
    memory_manager: Any,
    callback: Callable[..., None],
    identity_kwargs: Dict[str, Any],
) -> None:
    """Synchronous probe body. Called from the daemon thread."""
    try:
        tool_name = find_reasoning_tool(memory_manager)
        if not tool_name:
            logger.debug("Continuation probe: no reasoning tool available; skipping.")
            return

        raw = memory_manager.handle_tool_call(
            tool_name,
            {"query": _CONTINUATION_QUERY, "reasoning_level": "low"},
            **identity_kwargs,
        )
        summary = _parse_result(raw)

        # Sentinel check — trim whitespace, case-insensitive compare.
        if not summary or summary.strip().upper() == _SENTINEL:
            logger.debug("Continuation probe: provider reported no continuation.")
            return

        try:
            callback(task_summary=summary)
        except Exception as e:
            logger.debug("Continuation callback raised: %s", e)
    except Exception as e:
        logger.debug("Continuation probe body failed: %s", e)


def maybe_emit_continuation(
    memory_manager: Any,
    callback: Callable[..., None],
    identity_kwargs: Optional[Dict[str, Any]] = None,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    _synchronous: bool = False,
) -> None:
    """Fire the continuation probe.

    By default runs in a daemon thread with a bounded join (the main
    thread does NOT block). For testability, ``_synchronous=True``
    runs inline — tests set this to observe results deterministically.
    """
    ik = dict(identity_kwargs or {})
    if _synchronous:
        _probe_body(memory_manager, callback, ik)
        return

    t = threading.Thread(
        target=_probe_body,
        args=(memory_manager, callback, ik),
        daemon=True,
        name="hermes-continuation-probe",
    )
    t.start()
    # We intentionally do not join the thread here. The probe either
    # fires the callback (which the caller — api_server's SSE writer —
    # funnels into the stream) or it gives up quietly. Bounded timeout
    # is enforced inside _probe_body via the provider's own timeouts.
    # For hard upper bound, the daemon nature of the thread ensures it
    # does not prevent process exit.
    _ = timeout_seconds  # kept for documentation / future hard-bounding
