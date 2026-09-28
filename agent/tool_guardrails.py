"""Pure tool-call loop guardrail primitives.

The controller is side-effect free: it tracks per-turn tool-call observations
and returns decisions. Runtime code decides whether a decision becomes warning
guidance, a synthetic tool result, or a controlled turn halt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed, is_guardrail_refusal


_HK_LABELED_PROVISION_RE = re.compile(
    r"(?:section|sec(?:tion)?|s|rule|r)\.?\s*([0-9]{1,4}[A-Z]?)",
    re.IGNORECASE,
)

_HK_BARE_PROVISION_RE = re.compile(
    r"^(?:s|r)?([0-9]{1,4}[A-Z]?)(?:\s*\([0-9A-Za-z]+\))*$",
    re.IGNORECASE,
)

_HK_REGISTERED_MARK_COMPLETE_BUNDLE = frozenset(
    {"4", "11", "12", "44", "45", "52", "53"}
)

_STRICT_SKILL_BOUNDARY_EXTERNAL_KNOWLEDGE_TOOLS = frozenset(
    {
        # ``memory`` is a state mutation, not a knowledge read.  Its own
        # mutation boundary validates explicit intent and safe content.
        "session_search",
        "viking_search",
        "viking_browse",
        "viking_read",
        "web_search",
    }
)

STRICT_SKILL_SOURCE_IO_TOOLS = frozenset(
    {"skill_view", "read_file", "search_files"}
)

def _disables_mandatory_hk_authority(args: Mapping[str, Any]) -> bool:
    """Reject skill text that explicitly removes the statutory safety floor."""
    action = str(args.get("action") or "").strip()
    if action not in {"create", "edit", "patch", "write_file"}:
        return False
    candidate = "\n".join(
        str(args.get(key) or "")
        for key in ("content", "new_string", "file_content")
    )
    if not candidate.strip():
        return False
    from agent.hk_legal_authority_gate import (
        is_authority_disable_request,
        preserves_authority_contract,
    )

    return is_authority_disable_request(candidate) and not preserves_authority_contract(
        candidate
    )

def _hk_authority_request(args: Mapping[str, Any]) -> tuple[str, frozenset[str]]:
    chapter = str(args.get("chapter") or "").strip().upper()
    raw_provisions = args.get("provisions")
    if not chapter or not isinstance(raw_provisions, list):
        return chapter, frozenset()
    normalized = set()
    for raw in raw_provisions:
        value = str(raw or "").strip()
        match = _HK_LABELED_PROVISION_RE.search(value) or _HK_BARE_PROVISION_RE.fullmatch(value)
        if match:
            normalized.add(match.group(1).upper())
    return chapter, frozenset(normalized)

def _successful_hk_authority_result(result: str | None) -> tuple[str, frozenset[str]]:
    payload = safe_json_loads(result or "")
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return "", frozenset()
    chapter = str(payload.get("chapter") or "").strip().upper()
    provisions = {
        str(row.get("provision") or "").strip().upper()
        for row in payload.get("requested_provisions") or []
        if isinstance(row, dict) and row.get("found") is True
    }
    return chapter, frozenset(value for value in provisions if value)

def _is_frontmatter_only_skill_patch(args: Mapping[str, Any]) -> bool:
    """Identify a metadata-only description patch on a behavioral turn."""
    if str(args.get("action") or "").strip().lower() != "patch":
        return False
    old = str(args.get("old_string") or "").strip()
    new = str(args.get("new_string") or "").strip()
    return (
        "\n" not in old
        and "\n" not in new
        and old.lower().startswith("description:")
        and new.lower().startswith("description:")
    )

IDEMPOTENT_TOOL_NAMES = frozenset({
    "attachments", "viking_search", "viking_browse", "hk_legal_authority",
    "mcp_soc_v2_list_conversions", "mcp_soc_v2_conversion_status",
    "read_file", "search_files", "web_search", "web_extract", "session_search", "skill_view", "skills_list",
    "browser_snapshot", "browser_console", "browser_get_images", "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file", "mcp_filesystem_read_multiple_files", "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes", "mcp_filesystem_directory_tree", "mcp_filesystem_get_file_info",
    "mcp_filesystem_search_files",
})

MUTATING_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "write_file", "patch", "todo_list", "memory", "skill_manage",
    "browser_click", "browser_type", "browser_press", "browser_scroll", "browser_navigate",
    "send_message", "cronjob_manage", "delegate_task", "process_manage",
})

# Pollers: legitimately re-invoked with identical args; the identical-call NOTICE never fires.
STALL_GUARD_REPEATABLE_TOOLS = frozenset({"process_manage"})
_STALL_GUARD_REPEATABLE_SUFFIXES = ("_get_result", "_poll")  # generated / MCP poller conventions
# Nth consecutive identical (tool, args, result) call that fires the notice; 3 tolerates one double-check.
STALL_GUARD_IDENTICAL_CALL_THRESHOLD = 3
# Repeating multi-call cycles (A,B,A,B,... with identical args AND results) defeat the
# consecutive streak above — every alternation resets it, so a model replaying the same
# 2–4 call batch each iteration ran to the budget unflagged (port of can1357/oh-my-pi#10521,
# which widened their loop guard from single-call turns to whole tool-call batches).
# Longest cycle period detected; laps reuse the streak thresholds (notice at
# STALL_GUARD_IDENTICAL_CALL_THRESHOLD laps, halt at no_progress_block_after laps).
_STALL_GUARD_MAX_CYCLE_PERIOD = 4
# History window: enough for block_after laps of the longest cycle plus slack.
_STALL_GUARD_CYCLE_HISTORY = 64
# From the 2nd byte-identical repeat the duplicate payload becomes a reference stub; smaller results
# aren't worth it, errors never are. The args preview keeps WHAT was called if compression evicts the original.
IDENTICAL_RESULT_STUB_MIN_CHARS = 512
_RESULT_STUB_ARGS_PREVIEW_CHARS = 120

# Tools whose "failure" is normal work output (red test run, empty grep, page timeout).
# same_tool_failure (DIFFERENT commands) never halts these; only an exact-args replay with
# no intervening change, or an identical-result streak, can.
FAILURE_TOLERANT_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "process_manage", "process", "browser_navigate", "web_extract",
})

# A successful call to one of these marks progress for every failing signature still counted
# this turn: the next retry is a new experiment (edit -> re-run), not a replay.
PROGRESS_RESET_TOOL_NAMES = frozenset({
    "write_file", "patch", "terminal", "execute_code", "browser_click", "browser_type", "browser_press",
    "browser_navigate", "process_manage", "process", "delegate_task", "send_message", "cronjob",
    "cronjob_manage", "todo", "todo_list", "memory", "skill_manage",
})

_BOOL_FIELDS = ("warnings_enabled", "hard_stop_enabled", "non_interactive_hard_stop_enabled")
# Threshold field -> (nested section, nested key). The flat legacy key is the field name itself.
_THRESHOLD_SOURCES: dict[str, tuple[str, str]] = {
    "exact_failure_warn_after": ("warn_after", "exact_failure"),
    "same_tool_failure_warn_after": ("warn_after", "same_tool_failure"),
    "no_progress_warn_after": ("warn_after", "idempotent_no_progress"),
    "exact_failure_block_after": ("hard_stop_after", "exact_failure"),
    "same_tool_failure_halt_after": ("hard_stop_after", "same_tool_failure"),
    "no_progress_block_after": ("hard_stop_after", "idempotent_no_progress"),
}

# Per-turn caps on runaway-prone tools (counters reset in reset_for_turn).
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50

# Interactive surfaces plus bounded supervised task loops (subagent stopped by its parent;
# api_server has a live client) doing real edit -> re-run work keep the warn-only default.
_ATTENDED_PLATFORMS = frozenset({"cli", "tui", "desktop", "acp", "subagent", "api_server"})


def is_stall_guard_repeatable(tool_name: str) -> bool:
    """Whether a tool is exempt from the identical-call loop notice."""
    return tool_name in STALL_GUARD_REPEATABLE_TOOLS or tool_name.endswith(_STALL_GUARD_REPEATABLE_SUFFIXES)


def _is_non_interactive_platform(platform: str | None) -> bool:
    """True for gateway/cron sessions where tool loops are unattended."""
    if not isinstance(platform, str) or not platform.strip():
        return False
    return platform.strip().lower() not in _ATTENDED_PLATFORMS


@dataclass(frozen=True)
class LoopCapConfig:
    """Per-turn hard ceilings on web_search calls / subagent spawns; count total calls (not
    repeats), fire regardless of ``hard_stop_enabled``; ``0`` disables a cap."""

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        """Build config from the ``tool_loop_guardrails.loop_caps`` section."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(**{f.name: _int_at_least(data.get(f.name), f.default, 0) for f in fields(cls)})


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection. Warnings never prevent execution; hard
    stops are opt-in on interactive platforms, default on for unattended gateway/cron platforms."""

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    non_interactive_hard_stop_enabled: bool = True
    hard_stop_platforms: frozenset[str] = field(default_factory=lambda: frozenset({"api_server"}))
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    loop_caps: LoopCapConfig = field(default_factory=LoopCapConfig)

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any] | None, *, platform: str | None = None,
    ) -> "ToolCallGuardrailConfig":
        """Build config from `tool_loop_guardrails`; nested ``warn_after`` / ``hard_stop_after`` win over flat legacy keys."""
        if not isinstance(data, Mapping):
            data = {}
        d = cls()
        raw_platforms = data.get("hard_stop_platforms")
        platforms = (
            frozenset(str(name).strip() for name in raw_platforms if str(name).strip())
            if isinstance(raw_platforms, (list, tuple, set, frozenset))
            else d.hard_stop_platforms
        )
        flags = {name: _as_bool(data.get(name), getattr(d, name)) for name in _BOOL_FIELDS}
        if flags["non_interactive_hard_stop_enabled"] and _is_non_interactive_platform(platform):
            flags["hard_stop_enabled"] = True

        def threshold(name: str, section_name: str, key: str) -> int:
            section = data.get(section_name)
            nested = section.get(key, data.get(name)) if isinstance(section, Mapping) else data.get(name)
            return _int_at_least(nested, getattr(d, name), 1)

        thresholds = {name: threshold(name, *src) for name, src in _THRESHOLD_SOURCES.items()}
        return cls(loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")), hard_stop_platforms=platforms, **flags, **thresholds)


@dataclass(frozen=True)
class IdenticalCallObservation:
    """``notice`` is appended after the result, ``stub`` replaces a byte-identical duplicate result."""

    notice: str | None = None
    stub: str | None = None


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        return cls(tool_name=tool_name, args_hash=_sha256(canonical_tool_args(args or {})))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return asdict(self)


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data = asdict(self)
        if data["signature"] is None:
            del data["signature"]
        return data


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return _canonical_json(args)


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Fallback classifier used only when callers don't pass ``failed``; mirrors
    ``agent.display._detect_tool_failure`` so the guardrail never disagrees with the CLI's ``[error]`` tag."""
    if result is None or file_mutation_result_landed(tool_name, result):
        return False, ""

    # A harness REFUSAL of a redundant call (repeated identical read/search) carries
    # ``"error"`` for the model's benefit -- exactly what the substring test below keys
    # on -- but nothing failed; counting it lets the cheap refusal feed the streak that
    # fires the next, harder one. Mirrored in ``agent.display._detect_tool_failure``.
    if is_guardrail_refusal(result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        return (True, f" [exit {exit_code}]") if exit_code is not None and exit_code != 0 else (False, "")

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict) and data.get("success") is False and "exceed the limit" in data.get("error", ""):
            return True, " [full]"
    lower = result[:500].lower()
    return (True, " [error]") if '"error"' in lower or '"failed"' in lower or result.startswith("Error") else (False, "")


# Guardrail verdict text injected into the conversation, keyed by decision code.
# ``same_tool_failure_warning`` is built by _tool_failure_recovery_hint (tool-specific).
_DECISION_MESSAGES: dict[str, str] = {
    "repeated_exact_failure_block": (
        "Blocked {tool_name}: the same tool call failed {count} times with identical arguments. "
        "Stop retrying it unchanged; change strategy or explain the blocker."
    ),
    "idempotent_no_progress_block": (
        'Blocked {tool_name}: this read-only call returned the same result {count} times, so it cannot tell you anything new. You already have that result — answer the user now from what you have, or take a genuinely different action. This is an internal control: do not mention it, name it, or describe it in your reply to the user.'
    ),
    "same_tool_failure_halt": (
        "Stopped {tool_name}: it failed {count} times this turn. "
        "Stop retrying the same failing tool path and choose a different approach."
    ),
    "repeated_exact_failure_warning": (
        "{tool_name} has failed {count} times with identical arguments. This looks like a loop; "
        "inspect the error and change strategy instead of retrying it unchanged."
    ),
    "idempotent_no_progress_warning": (
        "{tool_name} returned the same result {count} times. Use the result already provided "
        "or change the query instead of repeating it unchanged."
    ),
    "identical_call_streak_halt": (
        "Stopped {tool_name}: the same call with identical arguments returned the same result "
        "{count} times in a row. Stop repeating it unchanged; use the result already provided or change strategy."
    ),
    "identical_cycle_halt": (
        "Stopped {tool_name}: the same repeating cycle of tool calls (period {period}) with identical "
        "arguments and identical results has run {count} times. Repeating the batch unchanged is not "
        "progress; use the results already provided or change strategy."
    ),
    "loop_web_search_cap": (
        "Blocked web_search: this turn has already made {cap} web searches, the per-turn limit. "
        "This looks like a runaway search loop. Work with the results you already have and give the user your answer."
    ),
    "loop_subagent_cap": (
        "Blocked delegate_task: this turn has already spawned {count} subagents (limit {cap}). "
        "This looks like a runaway delegation loop. Finish the work with the results you have and answer the user."
    ),
}

_IDENTICAL_CALL_NOTICE = (
    "[hermes note: this is the {ordinal} consecutive identical call to "
    "{tool_name} with identical arguments returning the same result. "
    "Do not repeat it — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

_IDENTICAL_CYCLE_NOTICE = (
    "[hermes note: the last {count} rounds repeated the same cycle of {period} tool calls "
    "(ending with {tool_name}) with identical arguments and identical results. "
    "Do not repeat the batch — change arguments, use a different tool, or "
    "proceed with what you have.]"
)

# tool -> (LoopCapConfig field, controller counter attribute, decision code)
_LOOP_CAPS: dict[str, tuple[str, str, str]] = {
    "web_search": ("max_web_searches", "_turn_web_search_count", "loop_web_search_cap"),
    "delegate_task": ("max_subagents", "_turn_subagent_count", "loop_subagent_cap"),
}


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls."""

    def __init__(
        self,
        config: ToolCallGuardrailConfig | None = None,
        platform_resolver: Callable[[], str] | None = None,
    ):
        """``platform_resolver`` is consulted at decision time, not construction.

        Resolving the platform when the controller is built is unsafe: one
        api_server entry point creates the agent before it binds the session
        contextvars, so a construction-time read saw an empty platform and
        silently left hard stops off on exactly the surface that needs them.
        Injecting a callable keeps this module free of ambient reads while
        making the decision independent of call ordering.
        """
        self.config = config or ToolCallGuardrailConfig()
        self._platform_resolver = platform_resolver
        self.reset_for_turn()

    def _hard_stops_active(self) -> bool:
        if self.config.hard_stop_enabled:
            return True
        if not self.config.hard_stop_platforms or self._platform_resolver is None:
            return False
        try:
            platform = str(self._platform_resolver() or "").strip()
        except Exception:
            return False
        return platform in self.config.hard_stop_platforms

    def set_strict_skill_source_boundary(
        self,
        enabled: bool,
        *,
        requested_classes: tuple[int, ...] = (),
    ) -> None:
        """Apply the current user's explicit one-skill knowledge boundary."""
        self._strict_skill_source_boundary = bool(enabled)
        self._strict_skill_source_requested_classes = tuple(requested_classes)

    def observe_skill_source_result(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: Any,
    ) -> None:
        """Register successful named-skill and declared-source reads."""
        if not self._strict_skill_source_boundary:
            return
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return
        call_args = _coerce_args(args)
        if tool_name in STRICT_SKILL_SOURCE_IO_TOOLS and (
            isinstance(payload.get("source_contract"), dict)
            or payload.get("file")
            or isinstance(payload.get("routing"), dict)
        ):
            self._strict_skill_successful_source_calls.add(
                ToolCallSignature.from_call(tool_name, call_args)
            )
        loaded_source = (
            str(payload.get("name") or call_args.get("name") or "").strip(),
            str(
                payload.get("file")
                or (payload.get("routing") or {}).get("file_path")
                or call_args.get("file_path")
                or ""
            ).strip(),
        )
        if (
            payload.get("content_complete") is True
            and loaded_source in self._strict_skill_source_allowed
        ):
            self._strict_skill_source_loaded.add(loaded_source)
        contract = payload.get("source_contract")
        if not isinstance(contract, dict):
            return
        examples = contract.get("declared_skill_view_examples") or []
        if not isinstance(examples, list):
            return
        allowed: set[tuple[str, str]] = set()
        for example in examples:
            if not isinstance(example, dict):
                continue
            name = str(example.get("name") or "").strip()
            file_path = str(example.get("file_path") or "").strip()
            if not name or not file_path:
                continue
            if "class-N." in file_path and self._strict_skill_source_requested_classes:
                allowed.update(
                    (name, file_path.replace("class-N.", f"class-{number}."))
                    for number in self._strict_skill_source_requested_classes
                )
            else:
                allowed.add((name, file_path))
        if allowed or not self._strict_skill_source_contract_seen:
            self._strict_skill_source_contract_seen = True
            self._strict_skill_source_allowed = allowed

    def observe_skill_view_result(
        self,
        args: Mapping[str, Any] | None,
        result: Any,
    ) -> None:
        """Backward-compatible wrapper for existing callers and integrations."""
        self.observe_skill_source_result("skill_view", args, result)

    def set_skill_mutation_allowed(
        self,
        enabled: bool,
        *,
        referential: bool = False,
    ) -> None:
        """Bind skill writes to explicit user mutation intent for this turn."""
        self._skill_mutation_allowed = bool(enabled)
        self._referential_skill_mutation_required = bool(enabled and referential)

    @property
    def referential_skill_mutation_required(self) -> bool:
        """Whether this turn must resolve a confirmed skill mutation."""
        return self._referential_skill_mutation_required

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        # signature -> a mutating call succeeded since its last failure
        self._progress_since_failure: dict[ToolCallSignature, bool] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # Identical-call streak: CONSECUTIVE identical (tool, args, result) calls; any different call or
        # result resets it, so re-reads after edits and varied polling are never flagged.
        # Identical-call loop-breaker state (agent.stall_guards): tracks the CONSECUTIVE streak of identical
        # (tool, canonical args) calls whose results were also identical. Per-turn, like everything else
        # here. NOTE: open PR #85352 (patrykkopycinski) tracks no-progress loops ACROSS turns via a
        # detection window — a different mechanism from this per-turn consecutive streak. Coordinate future
        # work there.
        self._identical_streak_sig: ToolCallSignature | None = None
        self._identical_streak_result_hash: str = ""
        self._identical_streak_count: int = 0
        self._identical_streak_first_call_id: str = ""
        # Batch-cycle loop breaker (port of can1357/oh-my-pi#10521): sequence of
        # (signature, result_hash, repeatable) for every observed call this turn, so a repeating
        # multi-call cycle (A,B,A,B,...) is caught even though it resets the consecutive streak above.
        self._call_history: deque[tuple[ToolCallSignature, str, bool]] = deque(maxlen=_STALL_GUARD_CYCLE_HISTORY)
        # tool_call_id -> spillover path, so a stub referencing a persisted-output preview can't dangle.
        self._persisted_result_paths: dict[str, str] = {}
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0
        self._hk_legal_request_sets: dict[str, set[frozenset[str]]] = {}
        self._hk_legal_covered: dict[str, set[str]] = {}
        self._legal_docx_apply_sources: set[str] = set()
        self._strict_skill_source_boundary = False
        self._strict_skill_source_contract_seen = False
        self._strict_skill_source_requested_classes: tuple[int, ...] = ()
        self._strict_skill_source_allowed: set[tuple[str, str]] = set()
        self._strict_skill_source_loaded: set[tuple[str, str]] = set()
        self._strict_skill_successful_source_calls: set[ToolCallSignature] = set()
        self._skill_mutation_allowed = False
        self._referential_skill_mutation_required = False
        self._referential_skill_mutation_in_flight = False
        self._referential_skill_mutation_landed = False

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def _decide(
        self, action: str, code: str, tool_name: str, count: int, signature: ToolCallSignature,
        *, message: str | None = None, **fmt: Any,
    ) -> ToolGuardrailDecision:
        """Build a warn/block/halt decision; block/halt is also recorded as the turn's halt decision."""
        if message is None:
            message = _DECISION_MESSAGES[code].format(tool_name=tool_name, count=count, **fmt)
        decision = ToolGuardrailDecision(action, code, message, tool_name, count, signature)
        if decision.should_halt:
            self._halt_decision = decision
        return decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        allow = ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        # Loop caps apply regardless of hard_stop_enabled (which only governs the detector).
        cap_block = self._check_loop_cap(tool_name, args, signature)
        if cap_block is not None:
            return cap_block
        if tool_name == "mcp__legal_docx__apply":
            source_file_id = str(_coerce_args(args).get("source_file_id") or "").upper()
            source_key = source_file_id or signature.args_hash
            if source_key in self._legal_docx_apply_sources:
                return ToolGuardrailDecision(
                    action="reuse",
                    code="legal_docx_single_apply_enforced",
                    message=(
                        "One legal DOCX apply call has already been attempted for this "
                        "request-owned source in the current turn. Do not retry, split the "
                        "manifest, or create diagnostic artifacts. If the first call "
                        "succeeded, continue from its native output artifact receipt; if it "
                        "failed, stop and report that native error to the user."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )
            self._legal_docx_apply_sources.add(source_key)

        if (
            self._referential_skill_mutation_required
            and tool_name in {"memory", "viking_remember"}
        ):
            return ToolGuardrailDecision(
                action="reuse",
                code="referential_skill_mutation_requires_skill_manage",
                message=(
                    "This turn confirms a skill mutation, not a profile or "
                    "OpenViking memory write. Skip this persistence call and "
                    "resolve the confirmation with exactly one skill_manage "
                    "call. Do not ask the user to repeat the request."
                ),
                tool_name=tool_name,
                signature=signature,
            )

        if self._referential_skill_mutation_required and tool_name == "skill_manage":
            call_args = _coerce_args(args)
            if self._referential_skill_mutation_landed:
                return ToolGuardrailDecision(
                    action="reuse",
                    code="referential_skill_mutation_already_landed",
                    message=(
                        "One coherent skill mutation already landed for this "
                        "confirmation. Do not split or repeat it. Use the receipt "
                        "and answer the user truthfully."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )
            if self._referential_skill_mutation_in_flight:
                return ToolGuardrailDecision(
                    action="reuse",
                    code="referential_skill_mutation_in_flight",
                    message=(
                        "One coherent skill mutation is already executing for "
                        "this confirmation. Do not split the same behavioral "
                        "change into parallel calls."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )
            if _is_frontmatter_only_skill_patch(call_args):
                return ToolGuardrailDecision(
                    action="reuse",
                    code="referential_skill_mutation_metadata_only",
                    message=(
                        "This confirmation is for a behavioral skill rule, not "
                        "a metadata-only description edit. Skip this call and "
                        "make one coherent behavioral skill mutation instead."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )
            self._referential_skill_mutation_in_flight = True

        if (
            self._strict_skill_source_boundary
            and tool_name in _STRICT_SKILL_BOUNDARY_EXTERNAL_KNOWLEDGE_TOOLS
        ):
            return ToolGuardrailDecision(
                action="reuse",
                code="strict_skill_source_boundary",
                message=(
                    "The user explicitly limited this turn to the named skill's "
                    "knowledge and its authorized official sources. Skip this "
                    "outside memory/search source. Continue with skill_view and only "
                    "the source-specific tool or file that the skill directs you to; "
                    "if those sources cannot answer, say cannot-confirm or do not know."
                ),
                tool_name=tool_name,
                signature=signature,
            )

        if (
            self._strict_skill_source_boundary
            and tool_name in STRICT_SKILL_SOURCE_IO_TOOLS
            and signature in self._strict_skill_successful_source_calls
        ):
            missing_sources = sorted(
                self._strict_skill_source_allowed
                - self._strict_skill_source_loaded
            )
            missing_calls = ", ".join(
                f"skill_view(name={name!r}, file_path={file_path!r})"
                for name, file_path in missing_sources
            )
            return ToolGuardrailDecision(
                action="reuse",
                code="strict_skill_source_call_already_loaded",
                message=(
                    "This exact named-skill/source read already succeeded in "
                    "this turn. Reuse its earlier full-fidelity result and "
                    "continue; do not repeat the call or mention this internal "
                    "control in the user-visible answer."
                    + (
                        " Load the still-missing declared sources with exactly: "
                        f"{missing_calls}."
                        if missing_calls
                        else ""
                    )
                ),
                tool_name=tool_name,
                signature=signature,
            )

        if (
            self._strict_skill_source_boundary
            and self._strict_skill_source_contract_seen
            and tool_name in {"read_file", "search_files"}
        ):
            call_args = _coerce_args(args)
            requested_path = str(
                call_args.get("path") or call_args.get("file_path") or ""
            ).replace("\\", "/")
            path_is_declared = any(
                requested_path == file_path
                or requested_path.endswith(f"/{name}/{file_path}")
                or requested_path.endswith(f"/{file_path}")
                for name, file_path in self._strict_skill_source_allowed
            )
            if not path_is_declared:
                exact_calls = ", ".join(
                    f"skill_view(name={name!r}, file_path={file_path!r})"
                    for name, file_path in sorted(self._strict_skill_source_allowed)
                )
                return ToolGuardrailDecision(
                    action="reuse",
                    code="strict_skill_source_path",
                    message=(
                        "This named-skill turn may read or search only its "
                        "declared support files. Skip this guessed path and use "
                        f"exactly: {exact_calls or 'no support-file call is authorized'}."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )

        if (
            self._strict_skill_source_boundary
            and self._strict_skill_source_contract_seen
            and tool_name == "skill_view"
        ):
            call_args = _coerce_args(args)
            requested = (
                str(call_args.get("name") or "").strip(),
                str(call_args.get("file_path") or "").strip(),
            )
            if requested not in self._strict_skill_source_allowed:
                exact_calls = ", ".join(
                    f"skill_view(name={name!r}, file_path={file_path!r})"
                    for name, file_path in sorted(self._strict_skill_source_allowed)
                )
                return ToolGuardrailDecision(
                    action="reuse",
                    code="strict_skill_source_path",
                    message=(
                        "This named-skill turn may read only the support files "
                        "declared by the loaded skill contract. Skip this "
                        "undeclared or repeated skill path and use exactly: "
                        f"{exact_calls or 'no support-file call is authorized'}."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )
            if requested in self._strict_skill_source_loaded:
                return ToolGuardrailDecision(
                    action="reuse",
                    code="strict_skill_source_already_loaded",
                    message=(
                        "This exact declared source was already loaded completely "
                        "in this turn. Reuse its earlier full-fidelity result and "
                        "answer the user now; do not request it again or mention "
                        "this internal control in the user-visible answer."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                )

        if (
            self._strict_skill_source_boundary
            and self._strict_skill_source_contract_seen
            and self._strict_skill_source_allowed
            and self._strict_skill_source_allowed.issubset(
                self._strict_skill_source_loaded
            )
            and tool_name in STRICT_SKILL_SOURCE_IO_TOOLS
        ):
            return ToolGuardrailDecision(
                action="reuse",
                code="strict_skill_sources_complete",
                message=(
                    "Every declared source required for this turn is already "
                    "loaded completely. Stop source I/O and answer the user "
                    "from those full-fidelity results now; do not mention this "
                    "internal control in the user-visible answer."
                ),
                tool_name=tool_name,
                signature=signature,
            )

        if tool_name == "skill_manage" and not self._skill_mutation_allowed:
            return ToolGuardrailDecision(
                action="reuse",
                code="skill_mutation_intent_required",
                message=(
                    "The user did not ask to create, edit, import, share, publish, "
                    "delete, or write a skill in this turn. Skip this mutation and continue "
                    "with read-only skill_view/source calls. Do not claim that the "
                    "skill or its files changed."
                ),
                tool_name=tool_name,
                signature=signature,
            )

        if tool_name == "skill_manage" and _disables_mandatory_hk_authority(
            _coerce_args(args)
        ):
            return ToolGuardrailDecision(
                action="reuse",
                code="authority_contract_mutation_rejected",
                message=(
                    "The user's referential confirmation was accepted as skill-edit "
                    "authorization, but this proposed content would disable mandatory "
                    "official-authority verification for Hong Kong statutory legal "
                    "conclusions. Do not apply or retry that unsafe mutation, do not "
                    "ask the user to repeat the edit request, and answer plainly that "
                    "the skill may remain skill-first while the official-authority "
                    "safety floor remains mandatory."
                ),
                tool_name=tool_name,
                signature=signature,
            )


        if not self._hard_stops_active():
            return allow

        if tool_name == "hk_legal_authority":
            chapter, requested = _hk_authority_request(_coerce_args(args))
            covered = self._hk_legal_covered.get(chapter) or set()
            if requested and requested.issubset(covered):
                return ToolGuardrailDecision(
                    action="reuse",
                    code="hk_authority_already_read",
                    message=(
                        "These official provisions were already read successfully in this "
                        "turn. Use the prior full tool result and answer the user now; do not "
                        "retry the same provisions under another label. This is an internal "
                        "control: do not mention it in the user-visible answer."
                    ),
                    tool_name=tool_name,
                    count=len(requested),
                    signature=signature,
                )
            if (
                chapter == "559"
                and _HK_REGISTERED_MARK_COMPLETE_BUNDLE.issubset(covered)
            ):
                return ToolGuardrailDecision(
                    action="reuse",
                    code="hk_authority_registered_mark_research_complete",
                    message=(
                        "The complete registered-mark authority bundle was already "
                        "verified in this turn. Do not expand into unrelated Cap. 559 "
                        "provisions or re-read the skill; answer from sections 4, 11, "
                        "12, 44, 45, 52, and 53 now. This is an internal control: do "
                        "not mention it in the user-visible answer."
                    ),
                    tool_name=tool_name,
                    count=len(covered),
                    signature=signature,
                )


        # A mutation since this call last failed makes the retry a new experiment.
        exact_count = 0 if self._progress_since_failure.get(signature) else self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            return self._decide("block", "repeated_exact_failure_block", tool_name, exact_count, signature)
        record = self._no_progress.get(signature) if self._is_idempotent(tool_name) else None
        if record is not None and record[1] >= self.config.no_progress_block_after:
            return self._decide("block", "idempotent_no_progress_block", tool_name, record[1], signature)
        return allow

    def after_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)
        warnings = self.config.warnings_enabled

        if self._referential_skill_mutation_required and tool_name == "skill_manage":
            self._referential_skill_mutation_in_flight = False
            payload = safe_json_loads(result or "")
            if (
                not failed
                and isinstance(payload, dict)
                and payload.get("success") is True
                and payload.get("execution_skipped") is not True
            ):
                self._referential_skill_mutation_landed = True

        if failed:
            # An identical failing call is only a REPLAY if nothing landed in between;
            # a mutation since the last identical failure restarts the exact-args streak.
            if self._progress_since_failure.pop(signature, False):
                self._exact_failure_counts.pop(signature, None)
            exact_count = self._exact_failure_counts[signature] = self._exact_failure_counts.get(signature, 0) + 1
            same_count = self._same_tool_failure_counts[tool_name] = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._no_progress.pop(signature, None)
            # same_tool_failure counts DIFFERENT args on one tool; for failure-tolerant
            # tools a run of distinct red commands is diagnosis, not a loop — warn, never halt.
            if (
                # Hard-stop widening (#89069 / #100849 bundle): the per-turn no-progress BLOCK above only
                # covers tools in idempotent_tools, so a model replaying the same successful
                # `terminal`/`skill_view` call with a byte-identical result ran until the iteration budget.
                # The consecutive-identical streak is tool-agnostic; when hard stops are enabled, halt at
                # the same idempotent_no_progress threshold. Pollers stay exempt (an unchanged poll is
                # progress).
                self._hard_stops_active()
                and tool_name not in FAILURE_TOLERANT_TOOL_NAMES
                and same_count >= self.config.same_tool_failure_halt_after
            ):
                return self._decide("halt", "same_tool_failure_halt", tool_name, same_count, signature)
            if warnings and exact_count >= self.config.exact_failure_warn_after:
                return self._decide("warn", "repeated_exact_failure_warning", tool_name, exact_count, signature)
            if warnings and same_count >= self.config.same_tool_failure_warn_after:
                return self._decide(
                    "warn", "same_tool_failure_warning", tool_name, same_count, signature,
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                )
            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)
        # A successful mutation is progress for every failing signature still counted
        # this turn. Pure loops never mutate between attempts, so the replay detector keeps its teeth.
        if tool_name in PROGRESS_RESET_TOOL_NAMES or file_mutation_result_landed(tool_name, result):
            self._progress_since_failure.update(dict.fromkeys(self._exact_failure_counts, True))
            self._same_tool_failure_counts.clear()
        if tool_name == "hk_legal_authority":
            chapter, provisions = _successful_hk_authority_result(result)
            if chapter and provisions:
                self._hk_legal_request_sets.setdefault(chapter, set()).add(provisions)
                self._hk_legal_covered.setdefault(chapter, set()).update(provisions)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = previous[1] + 1 if previous is not None and previous[0] == result_hash else 1
        self._no_progress[signature] = (result_hash, repeat_count)
        if warnings and repeat_count >= self.config.no_progress_warn_after:
            return self._decide("warn", "idempotent_no_progress_warning", tool_name, repeat_count, signature)
        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        return tool_name not in self.config.mutating_tools and tool_name in self.config.idempotent_tools

    def observe_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, tool_call_id: str = "", failed: bool = False,
    ) -> IdenticalCallObservation:
        """Track consecutive identical calls; return notice + dedupe stub info.

        ``notice`` fires from the threshold-th consecutive identical (tool, args, result) call
        (observational, pollers exempt). ``stub`` replaces the CURRENT result from the 2nd byte-identical
        repeat — the tool still executed, only the context representation is deduplicated, so polling
        semantics survive; pollers are NOT exempt here since an unchanged poll is where it saves most.
        """
        is_plain_str = isinstance(result, str)
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        result_hash = _result_hash(result) if is_plain_str else ""

        if is_plain_str and (signature, result_hash) == (self._identical_streak_sig, self._identical_streak_result_hash):
            self._identical_streak_count += 1
        else:
            # New streak; non-string (multimodal) results never form one.
            self._identical_streak_sig = signature if is_plain_str else None
            self._identical_streak_result_hash = result_hash
            self._identical_streak_count = 1 if is_plain_str else 0
            self._identical_streak_first_call_id = tool_call_id or ""
        count = self._identical_streak_count

        notice = None
        if not is_stall_guard_repeatable(tool_name) and count >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
            notice = _IDENTICAL_CALL_NOTICE.format(ordinal=_ordinal(count), tool_name=tool_name)
            # The no-progress BLOCK in before_call only covers idempotent_tools; this streak
            # is tool-agnostic, so with hard stops on, halt at the same threshold (a model
            # replaying a successful `terminal` call otherwise runs to the budget).
            if self._hard_stops_active() and count >= self.config.no_progress_block_after and self._halt_decision is None:
                self._decide("halt", "identical_call_streak_halt", tool_name, count, signature)

        # Batch-cycle detection (oh-my-pi#10521): a repeating multi-call cycle resets the
        # consecutive streak on every alternation, so check the call history for a period-p lap.
        if is_plain_str:
            self._call_history.append((signature, result_hash, is_stall_guard_repeatable(tool_name)))
        else:
            self._call_history.clear()
        if notice is None and is_plain_str:
            cycle = self._detect_identical_cycle()
            if cycle is not None:
                period, laps = cycle
                notice = _IDENTICAL_CYCLE_NOTICE.format(count=laps, period=period, tool_name=tool_name)
                if self._hard_stops_active() and laps >= self.config.no_progress_block_after and self._halt_decision is None:
                    self._decide("halt", "identical_cycle_halt", tool_name, laps, signature, period=period)

        stub = None
        if is_plain_str and count >= 2 and not failed and len(result) >= IDENTICAL_RESULT_STUB_MIN_CHARS:
            stub = self._build_result_reference_stub(tool_name, args)
        return IdenticalCallObservation(notice=notice, stub=stub)

    def _detect_identical_cycle(self) -> tuple[int, int] | None:
        """Detect a repeating identical-call cycle ending at the latest observed call.

        Returns ``(period, laps)`` for the smallest period 2..max whose trailing laps
        (identical signature AND result per position) reach the notice threshold, else None.
        Period 1 is the consecutive streak's job. A cycle made ONLY of poller-exempt tools
        is exempt (an unchanged poll loop is legitimate waiting); one non-exempt call in
        the cycle keeps the guard armed, matching the single-call exemption semantics.
        """
        history = self._call_history
        for period in range(2, _STALL_GUARD_MAX_CYCLE_PERIOD + 1):
            if len(history) < period * STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
                continue
            laps = 1
            # Count how many consecutive trailing laps equal the final lap.
            while True:
                base = len(history) - period * (laps + 1)
                if base < 0:
                    break
                lap_equal = all(
                    history[base + i][:2] == history[len(history) - period + i][:2]
                    for i in range(period)
                )
                if not lap_equal:
                    break
                laps += 1
            if laps >= STALL_GUARD_IDENTICAL_CALL_THRESHOLD:
                tail = [history[len(history) - period + i] for i in range(period)]
                if all(repeatable for _, _, repeatable in tail):
                    continue
                # A constant sub-cycle would already have fired at a smaller period.
                return period, laps
        return None

    def record_persisted_result(self, tool_call_id: str, file_path: str) -> None:
        """Remember the spillover path a persisted result was saved to."""
        if tool_call_id and file_path:
            self._persisted_result_paths[tool_call_id] = file_path

    def _build_result_reference_stub(self, tool_name: str, args: Mapping[str, Any] | None) -> str:
        """Reference stub for a byte-identical duplicate result (tool + args preview)."""
        args_preview = canonical_tool_args(_coerce_args(args))
        if len(args_preview) > _RESULT_STUB_ARGS_PREVIEW_CHARS:
            args_preview = args_preview[:_RESULT_STUB_ARGS_PREVIEW_CHARS] + "…"
        first_id = self._identical_streak_first_call_id
        ref = f" (tool_call_id {first_id})" if first_id else ""
        stub = (
            f"[hermes note: this result is byte-identical to the {tool_name} "
            f"result earlier this turn{ref}. Refer to that result; it has not "
            f"changed. Args: {args_preview}]"
        )
        spill_path = self._persisted_result_paths.get(first_id) if first_id else None
        if spill_path:
            stub += f"\n[The referenced result was persisted to: {spill_path} — page through it with read_file if you need the full content.]"
        return stub

    def _check_loop_cap(
        self, tool_name: str, args: Mapping[str, Any], signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Block once a per-turn cap is reached (BEFORE the call, so the (cap+1)-th is refused), else advance
        the counter and return None. delegate_task control actions spawn nothing and keep working after the cap."""
        spec = _LOOP_CAPS.get(tool_name)
        if spec is None:
            return None
        cap_field, count_attr, code = spec
        cap, count = getattr(self.config.loop_caps, cap_field), getattr(self, count_attr)
        increment = 1 if tool_name == "web_search" else (_subagent_spawn_count(args) if cap else 0)
        if increment and cap and count >= cap:
            return self._decide("block", code, tool_name, count, signature, cap=cap)
        setattr(self, count_attr, count + increment)
        return None


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a blocked tool call."""
    return json.dumps({"error": decision.message, "guardrail": decision.to_metadata()}, ensure_ascii=False)


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    return (result or "") + f"\n\n[{label}: {decision.code}; count={decision.count}; {decision.message}]"


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """Action-oriented guidance for recovering from repeated tool failures."""
    common = (
        f"{tool_name} has failed {count} times this turn. This looks like a loop. "
        "Do not switch to text-only replies; keep using tools, but diagnose before retrying. "
        "First inspect the latest error/output and verify your assumptions. "
    )
    if tool_name == "terminal":
        return common + (
            "For terminal failures, run a small diagnostic such as `pwd && ls -la` "
            "in the same tool, then try an absolute path, a simpler command, a different "
            "working directory, or a different tool such as read_file/write_file/patch."
        )
    return common + (
        "Try different arguments, a narrower query/path, an absolute path when relevant, "
        "or a different tool that can make progress. If the blocker is external, report "
        "the blocker after one diagnostic attempt instead of repeating the same failing path."
    )


def _ordinal(count: int) -> str:
    return f"{count}{'th' if 11 <= count % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(count % 10, 'th')}"


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    return _sha256(_canonical_json(parsed) if parsed is not None else (result or ""))


_BOOL_WORDS = {w: True for w in ("1", "true", "yes", "on", "enabled")} | {w: False for w in ("0", "false", "no", "off", "disabled")}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, str):
        return _BOOL_WORDS.get(value.strip().lower(), default)
    return default


def _int_at_least(value: Any, default: int, minimum: int) -> int:
    """junk/None/below-minimum fall back to default (caps use minimum 0 so 0 = disabled)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    """Subagents one delegate_task call spawns: ``len(tasks)`` for a non-empty batch, else 1; control actions 0."""
    if str(args.get("action") or "").strip().lower() in ("list", "steer", "stop"):
        return 0
    tasks = args.get("tasks")
    return len(tasks) if isinstance(tasks, list) and tasks else 1


def _sha256(value: str) -> str:
    # surrogatepass: web-scraped results can carry unpaired UTF-16 surrogates; a
    # strict encode would raise and take down the conversation loop.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
