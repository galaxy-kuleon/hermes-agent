"""The journey a piece of work belongs to: which user, which conversation.

WHY THIS IS NOT THE LOGGING SESSION ID
--------------------------------------
The first attempt reused ``hermes_logging``'s session id, because it already
encodes both (``api-<hash>-user-<uuid>-chat-<id>``). Adversarial review round
10 proved that fails in three ways, and one of them is worse than having no
correlator at all:

* **Delegation loses it.** A delegated child agent generates its own session id
  (``20260810_123456_abcdef``) with no user or chat in it. A legacy ``.doc``
  read by a child, failing at soffice, is durable but anonymous.
* **Unwrapped async fan-out never gets it.** The session id is thread-local, so
  anything that does not go through ``propagate_context_to_thread`` starts
  blank.
* **A reused worker can inherit a STALE id**, attributing one user's failure to
  another's journey. Wrong attribution is worse than absence: absence is
  visible, a wrong uid is believed.

So the journey is carried explicitly, in ``ContextVar``s:

* ``contextvars`` propagate into asyncio tasks and through
  ``contextvars.copy_context()`` -- which ``propagate_context_to_thread``
  already captures -- so both wrapped workers and async fan-out inherit them.
* A brand-new thread that never inherited a context reads the DEFAULT, which is
  empty. It can be missing; it can never be stale.
* Nothing about generating a child session id touches these, so delegation
  keeps the root journey.

Ids only -- the same uid and chat the gateway already logs once per request.
Never a path, a filename, a header value or any content.
"""

from __future__ import annotations

import contextvars

_JOURNEY_UID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_journey_uid", default=""
)
_JOURNEY_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_journey_chat", default=""
)


def _id_safe(value: str) -> str:
    """Make an id survive a whitespace-delimited log record, LOSSLESSLY.

    The ledger reads these back as `key=value` with a non-space value, so an id
    carrying a space would truncate the correlation to its first word (round
    16). My first fix replaced whitespace with "_", which is NOT injective:
    `case 15`, `case_15` and `case  15` all became `case_15`, so three distinct
    conversations shared one correlator and a failure could be attached to the
    wrong one. Round 17 proved it on the deployed session-chat handler.

    Percent-encoding is the inverse of the reader's grammar rather than a
    lossy rewrite: distinct ids stay distinct, `%` itself is encoded, and an
    ordinary UUID passes through untouched.
    """
    from urllib.parse import quote
    return quote(str(value or ""), safe="") or ""


def set_journey(uid: str, chat: str) -> None:
    """Bind the current context to a user's conversation. Never raises."""
    try:
        _JOURNEY_UID.set(_id_safe(uid))
        _JOURNEY_CHAT.set(_id_safe(chat))
    except Exception:  # pragma: no cover - a correlator must never break a turn
        pass


def get_journey() -> "tuple[str, str]":
    try:
        return _JOURNEY_UID.get(), _JOURNEY_CHAT.get()
    except Exception:  # pragma: no cover
        return "", ""


def journey_suffix() -> str:
    """``" uid=… chat=…"`` for the current journey, or ``""``.

    Appended to failure records so a sidecar outcome names the journey it
    killed rather than only the layer it died in. The ledger reads these as
    ordinary key=value fields.
    """
    uid, chat = get_journey()
    if not uid and not chat:
        return ""
    return f" uid={uid or '-'} chat={chat or '-'}"


__all__ = ["set_journey", "get_journey", "journey_suffix"]
