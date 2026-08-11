"""Construct the environment a chat-turn child process receives.

WHY A CONSTRUCTOR AND NOT A FILTER
----------------------------------
The existing path starts from ``os.environ`` (plus a caller's ``base_env`` and
``extra_env``) and subtracts names that look secret. That shape has failed
three times in this codebase: a name nobody had listed got through; two late
channels were never shown to the frozen blocklist at all; and the "vocabulary"
version, which tried to guess secrets by spelling, broke the skill credentials
that are *supposed* to reach their subprocess.

Subtraction can only remove what someone anticipated. So this module inverts
it. A chat child receives only:

1. a small base copied from an explicitly supplied ``source``;
2. names an operator or skill explicitly granted for that child kind;
3. feature values the calling code owns;
4. values generated for that one invocation.

It never begins with the ambient environment.

PURITY IS THE POINT, AND IT IS TESTED
-------------------------------------
Nothing here reads ``os.environ``, a config file, a ContextVar, or the
filesystem. Every input arrives as an argument, so identical arguments produce
an identical result under any ambient environment -- which the tests assert
directly, constructing twice under two different ambient environments and
comparing the whole result.

``ChildEnvSpec`` snapshots its mutable arguments at construction:
``@dataclass(frozen=True)`` freezes the shell, not a ``set`` or ``dict`` stored
inside it, and a caller who mutated the collection afterwards would otherwise
change a later result.

REJECTION HAPPENS AT THE FINAL AUTHORITY, NOT AT ONE INPUT
----------------------------------------------------------
Every channel a caller can influence -- base, grants, feature values -- is
checked against the same rule, because validating one input and trusting the
rest is precisely how ``base_env`` and ``extra_env`` became bypasses. Only
``generated``, which the mechanism itself owns and no caller can reach, writes
last.

``_HERMES_FORCE_X`` is never decoded into ``X`` here: the request is rejected
and recorded under the name the caller actually used, so a rejection cannot be
read as an approval of a different name.

NAME CONTRACT (decided, not inherited)
--------------------------------------
POSIX ``execve`` semantics, case-sensitive. A name must be a non-empty string
containing neither ``=`` nor NUL; anything else is rejected with a receipt
rather than passed on to fail obscurely inside ``subprocess``. Values must be
strings; an empty string is a legitimate value and is kept.

This module is scoped to POSIX. On a case-insensitive platform ``api_key``
would coexist with a blocked ``API_KEY``; that platform is out of scope and
:func:`construct_chat_child_env` says so rather than pretending otherwise.

Not wired into any spawn path yet -- and a test asserts that, by parsing the
production tree. This is the mechanism; the enforcement commit is a wiring
change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

# The force-prefix is an escape hatch belonging to the legacy trusted-operator
# policy. A restricted chat child may not use it, under either spelling.
FORCE_PREFIX = "_HERMES_FORCE_"

# Names copied from `source` when present. Deliberately tiny: a chat child on
# this deployment was receiving 41 names, of which a handful were POSIX base
# and the rest were stack configuration it had no reason to see.
DEFAULT_BASE_NAMES: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "USER", "SHELL", "PWD",
})

# Rejection reasons. Closed set: an operator reading a receipt is reading a
# label this module owns, never a rendered caller value.
REASON_FORCE_PREFIX = "force_prefix"
REASON_DENIED = "denied_name"
REASON_INVALID_NAME = "invalid_name"
REASON_INVALID_VALUE = "non_string_value"

# Channel labels, in authority order. `generated` is last and is not a caller
# channel.
CHANNEL_BASE = "base"
CHANNEL_GRANT = "grant"
CHANNEL_FIXED = "fixed"
CHANNEL_GENERATED = "generated"


@dataclass(frozen=True)
class ChildEnvSpec:
    """What one kind of chat child is allowed to receive.

    Build with :meth:`create`, which snapshots mutable arguments into immutable
    values. Constructing the dataclass directly is possible but then the caller
    owns the immutability guarantee.
    """

    kind: str
    grants: frozenset[str] = frozenset()
    fixed_env: tuple[tuple[str, str], ...] = ()
    base_names: frozenset[str] = DEFAULT_BASE_NAMES

    @staticmethod
    def create(
        kind: str,
        *,
        grants: Iterable[str] = (),
        fixed_env: Mapping[str, str] | None = None,
        base_names: Iterable[str] | None = None,
    ) -> "ChildEnvSpec":
        return ChildEnvSpec(
            kind=str(kind),
            grants=frozenset(grants),
            fixed_env=tuple(sorted((fixed_env or {}).items())),
            base_names=(frozenset(base_names)
                        if base_names is not None
                        else DEFAULT_BASE_NAMES),
        )


@dataclass(frozen=True)
class ChildEnvResult:
    """The constructed environment, and why each name is or is not in it.

    ``provenance`` names the channel that wrote each surviving name -- the
    *final* writer, so precedence is directly assertable instead of inferred.
    ``rejected`` and ``missing`` carry names and reasons only, never values: a
    receipt that quoted a value would leak the thing this module exists to
    withhold.
    """

    env: dict[str, str]
    provenance: tuple[tuple[str, str], ...] = ()
    rejected: tuple[tuple[str, str, str], ...] = ()   # (channel, name, reason)
    missing: tuple[str, ...] = ()                      # granted, absent from source


def _name_reason(name: object, denied: frozenset[str]) -> str:
    """Return a rejection reason, or ``""`` if this name may be used."""
    if not isinstance(name, str) or not name:
        return REASON_INVALID_NAME
    if "=" in name or "\x00" in name:
        return REASON_INVALID_NAME
    # Prefix check first: a force-spelling must be reported as a force-spelling
    # even if someone also listed it in `denied`, so the receipt says which rule
    # actually fired.
    if name.startswith(FORCE_PREFIX):
        return REASON_FORCE_PREFIX
    if name in denied:
        return REASON_DENIED
    return ""


def construct_chat_child_env(
    *,
    source: Mapping[str, str],
    spec: ChildEnvSpec,
    denied: frozenset[str] = frozenset(),
    generated: Mapping[str, str] | None = None,
) -> ChildEnvResult:
    """Build a chat child's environment from nothing. POSIX only.

    ``source`` is the only place ambient values may come from, and only the
    names listed in ``spec.base_names`` and ``spec.grants`` are read out of it;
    everything else in ``source`` is ignored no matter how it is spelled.

    ``generated`` is the mechanism's own channel and writes last, so no grant
    and no feature value can forge a name the mechanism is responsible for. It
    is still name-validated -- an unspawnable environment helps nobody -- but it
    is not subject to ``denied``, because ``denied`` describes what a caller may
    delegate, not what this module may set.
    """
    env: dict[str, str] = {}
    provenance: dict[str, str] = {}
    rejected: list[tuple[str, str, str]] = []
    missing: list[str] = []

    def _take(channel: str, name: object, value: object) -> None:
        if not isinstance(value, str):
            rejected.append((channel, str(name), REASON_INVALID_VALUE))
            return
        env[str(name)] = value
        provenance[str(name)] = channel

    # 1. base -- read from source, checked like everything else.
    for name in sorted(spec.base_names, key=str):
        reason = _name_reason(name, denied)
        if reason:
            rejected.append((CHANNEL_BASE, str(name), reason))
            continue
        if name in source:
            _take(CHANNEL_BASE, name, source[name])

    # 2. explicit grants -- operator- or skill-authorised passthrough.
    for name in sorted(spec.grants, key=str):
        reason = _name_reason(name, denied)
        if reason:
            rejected.append((CHANNEL_GRANT, str(name), reason))
            continue
        if name not in source:
            missing.append(str(name))
            continue
        _take(CHANNEL_GRANT, name, source[name])

    # 3. feature values the calling code owns -- checked too, otherwise every
    #    future call site becomes a bypass API.
    for name, value in spec.fixed_env:
        reason = _name_reason(name, denied)
        if reason:
            rejected.append((CHANNEL_FIXED, str(name), reason))
            continue
        _take(CHANNEL_FIXED, name, value)

    # 4. the mechanism's own channel. Last writer.
    for name, value in sorted((generated or {}).items(), key=lambda kv: str(kv[0])):
        reason = _name_reason(name, frozenset())
        if reason:
            rejected.append((CHANNEL_GENERATED, str(name), reason))
            continue
        _take(CHANNEL_GENERATED, name, value)

    return ChildEnvResult(
        env=env,
        provenance=tuple(sorted(provenance.items())),
        rejected=tuple(rejected),
        missing=tuple(missing),
    )


__all__ = [
    "ChildEnvSpec",
    "ChildEnvResult",
    "construct_chat_child_env",
    "DEFAULT_BASE_NAMES",
    "FORCE_PREFIX",
    "CHANNEL_BASE",
    "CHANNEL_GRANT",
    "CHANNEL_FIXED",
    "CHANNEL_GENERATED",
    "REASON_FORCE_PREFIX",
    "REASON_DENIED",
    "REASON_INVALID_NAME",
    "REASON_INVALID_VALUE",
]
