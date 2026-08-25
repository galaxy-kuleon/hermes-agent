"""Pure tool-call loop guardrail primitives.

The controller in this module is intentionally side-effect free: it tracks
per-turn tool-call observations and returns decisions. Runtime code owns whether
those decisions become warning guidance, synthetic tool results, or controlled
turn halts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from utils import safe_json_loads
from agent.tool_result_classification import file_mutation_result_landed


_HK_LABELED_PROVISION_RE = re.compile(
    r"(?:section|sec(?:tion)?|s|rule|r)\.?\s*([0-9]{1,4}[A-Z]?)",
    re.IGNORECASE,
)
_HK_BARE_PROVISION_RE = re.compile(
    r"^(?:s|r)?([0-9]{1,4}[A-Z]?)(?:\s*\([0-9A-Za-z]+\))*$",
    re.IGNORECASE,
)

# Policy bundle for the registered-mark remedy gate. Once this exact minimum
# has been read successfully in a turn, more exploratory Cap. 559 lookups are
# not additional verification; they are the recurrent local-model loop that
# swaps the correct ss.52/53 taxonomy for unrelated provisions.
_HK_REGISTERED_MARK_COMPLETE_BUNDLE = frozenset(
    {"4", "11", "12", "44", "45", "52", "53"}
)
_STRICT_SKILL_BOUNDARY_EXTERNAL_KNOWLEDGE_TOOLS = frozenset(
    {
        "memory",
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


IDEMPOTENT_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        # Read-only lookups that return the same answer for the same arguments,
        # so repeating one unchanged is by definition no progress.
        #
        # `attachments` was missing here and a brand-new user paid for it on
        # 2026-08-07 (chat 6090080e): their second message produced roughly
        # eighty-five identical `attachments({})` calls -- each correctly
        # answering "no files are attached" -- and then an EMPTY reply. The
        # guardrail below already hard-stops this exact shape on api_server;
        # it simply never saw the tool.
        "attachments",
        "skills_list",
        "skill_view",
        "viking_search",
        "viking_browse",
        "hk_legal_authority",
        "mcp_soc_v2_list_conversions",
        "mcp_soc_v2_conversion_status",
        "web_search",
        "web_extract",
        "session_search",
        "browser_snapshot",
        "browser_console",
        "browser_get_images",
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_read_multiple_files",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_list_directory_with_sizes",
        "mcp_filesystem_directory_tree",
        "mcp_filesystem_get_file_info",
        "mcp_filesystem_search_files",
    }
)

MUTATING_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute_code",
        "write_file",
        "patch",
        "todo",
        "memory",
        "skill_manage",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_navigate",
        "send_message",
        "cronjob",
        "delegate_task",
        "process",
    }
)


@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings are enabled by default and never prevent tool execution. Hard stops
    are explicit opt-in so interactive CLI/TUI sessions get a gentle nudge unless
    the user enables circuit-breaker behavior in config.yaml.
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    # Platforms where hard stops are on regardless of ``hard_stop_enabled``.
    # A CLI/TUI loop has a human watching who can interrupt it within seconds.
    # An api_server (OpenWebUI) loop does not: a live 46-file audit spent 45
    # minutes and 96 read calls looping over 18 files while the guardrail
    # emitted 48 advisory warnings that the model ignored, and the only thing
    # that ended it was the user pressing Stop.
    hard_stop_platforms: frozenset[str] = field(
        default_factory=lambda: frozenset({"api_server"})
    )
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)
    loop_caps: "LoopCapConfig" = field(default_factory=lambda: LoopCapConfig())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ToolCallGuardrailConfig":
        """Build config from the `tool_loop_guardrails` config.yaml section."""
        if not isinstance(data, Mapping):
            return cls()

        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}

        defaults = cls()
        raw_platforms = data.get("hard_stop_platforms")
        platforms = (
            frozenset(str(name).strip() for name in raw_platforms if str(name).strip())
            if isinstance(raw_platforms, (list, tuple, set, frozenset))
            else defaults.hard_stop_platforms
        )
        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=_as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled),
            hard_stop_platforms=platforms,
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
            loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")),
        )


# Default session-wide caps, matching Claude Code's v2.1.212 runaway-loop
# Per-turn (per-agent-loop) caps on runaway-prone tool calls. Counts reset at
# the start of every agent loop (reset_for_turn), so the limit is "within a
# single turn" rather than cumulative over the whole session. A single loop
# issuing dozens of web searches or spawning dozens of subagents is already
# pathological, so the defaults are deliberately low.
_DEFAULT_MAX_WEB_SEARCHES_PER_TURN = 50
_DEFAULT_MAX_SUBAGENTS_PER_TURN = 50


@dataclass(frozen=True)
class LoopCapConfig:
    """Per-turn caps on runaway-prone tool calls.

    Inspired by Claude Code v2.1.212 (Week 29, July 2026), which added caps on
    WebSearch calls and subagent spawns to stop runaway search / delegation
    loops. Here the caps count *within a single agent loop* (one turn): the
    counters reset in ``reset_for_turn`` at the start of every
    ``run_conversation``, so a legitimate multi-turn session is never starved,
    but a single turn that spirals into an unbounded search / delegation loop
    is stopped.

    Semantics differ from the per-turn loop *detector* above (which keys on
    repeated identical/failing calls): these caps are a hard ceiling on the
    total count of a tool within the turn and fire regardless of
    ``hard_stop_enabled``. A value of ``0`` disables the cap (unlimited).
    """

    max_web_searches: int = _DEFAULT_MAX_WEB_SEARCHES_PER_TURN
    max_subagents: int = _DEFAULT_MAX_SUBAGENTS_PER_TURN

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "LoopCapConfig":
        """Build config from the ``tool_loop_guardrails.loop_caps`` section."""
        if not isinstance(data, Mapping):
            return cls()
        defaults = cls()
        return cls(
            max_web_searches=_non_negative_int(
                data.get("max_web_searches"), defaults.max_web_searches
            ),
            max_subagents=_non_negative_int(
                data.get("max_subagents"), defaults.max_subagents
            ),
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable, non-reversible identity for a tool name plus canonical args."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict[str, str]:
        """Return public metadata without raw argument values."""
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the tool-call guardrail controller."""

    action: str = "allow"  # allow | warn | reuse | block | halt
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
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature is not None:
            data["signature"] = self.signature.to_metadata()
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def classify_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Safety-fallback classifier used only when callers don't pass ``failed``.

    Mirrors ``agent.display._detect_tool_failure`` exactly so the guardrail
    never disagrees with the CLI's user-visible ``[error]`` tag. Production
    callers in ``run_agent.py`` always pass an explicit ``failed=`` derived
    from ``_detect_tool_failure``; this function exists so standalone callers
    (tests, tooling) still get consistent behavior.
    """
    if result is None:
        return False, ""
    if file_mutation_result_landed(tool_name, result):
        return False, ""

    if tool_name == "terminal":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        return False, ""

    if tool_name == "memory":
        data = safe_json_loads(result)
        if isinstance(data, dict):
            if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                return True, " [full]"

    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


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

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._hk_legal_request_sets: dict[str, set[frozenset[str]]] = {}
        self._hk_legal_covered: dict[str, set[str]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # Per-turn runaway-loop cap counters. Reset every turn (this method
        # runs at the start of each run_conversation), so the caps bound a
        # single agent loop rather than accumulating across the session.
        self._turn_web_search_count = 0
        self._turn_subagent_count = 0
        self._strict_skill_source_boundary = False
        self._strict_skill_source_contract_seen = False
        self._strict_skill_source_requested_classes: tuple[int, ...] = ()
        self._strict_skill_source_allowed: set[tuple[str, str]] = set()
        self._strict_skill_source_loaded: set[tuple[str, str]] = set()
        self._strict_skill_successful_source_calls: set[ToolCallSignature] = set()
        self._skill_mutation_allowed = False

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

    def set_skill_mutation_allowed(self, enabled: bool) -> None:
        """Bind skill writes to explicit user mutation intent for this turn."""
        self._skill_mutation_allowed = bool(enabled)

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))
        # ── Per-turn runaway-loop caps ──────────────────────────────────
        # These are hard ceilings on how many times a runaway-prone tool may
        # be called within a single agent loop (turn). They apply regardless
        # of hard_stop_enabled (which only governs the per-turn loop detector).
        # We block BEFORE the call runs once the count is already at the cap,
        # then increment for an allowed call so the (cap+1)-th is refused.
        cap_block = self._check_loop_cap(tool_name, _coerce_args(args), signature)
        if cap_block is not None:
            return cap_block

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
                    "The user did not ask to create, edit, patch, publish, delete, "
                    "or write a skill in this turn. Skip this mutation and continue "
                    "with read-only skill_view/source calls. Do not claim that the "
                    "skill or its files changed."
                ),
                tool_name=tool_name,
                signature=signature,
            )

        if not self._hard_stops_active():
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

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

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same tool call failed {exact_count} "
                    "times with identical arguments. Stop retrying it unchanged; "
                    "change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        # Written as an instruction to CONTINUE, not as a report.
                        # Observed live 2026-08-08: the previous wording stopped
                        # a real skill_view loop correctly, and the model then
                        # handed the user 257 characters explaining the
                        # guardrail by name instead of answering their question.
                        # A control that halts a loop and becomes the answer has
                        # traded one useless reply for another.
                        message=(
                            f"Blocked {tool_name}: this read-only call returned the same "
                            f"result {repeat_count} times, so it cannot tell you anything "
                            "new. You already have that result — answer the user now from "
                            "what you have, or take a genuinely different action. This is "
                            "an internal control: do not mention it, name it, or describe "
                            "it in your reply to the user."
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)

        if failed:
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            if self._hard_stops_active() and same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this turn. "
                        "Stop retrying the same failing tool path and choose a different approach."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with identical arguments. "
                        "This looks like a loop; inspect the error and change strategy "
                        "instead of retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

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
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query instead of "
                    "repeating it unchanged."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def _check_loop_cap(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        signature: ToolCallSignature,
    ) -> ToolGuardrailDecision | None:
        """Enforce and advance the per-turn runaway-loop counters.

        Returns a ``block`` decision when the cap is already reached, otherwise
        increments the relevant counter for the allowed call and returns
        ``None``. A cap of 0 disables that limit entirely. Counters reset each
        turn via ``reset_for_turn``.
        """
        caps = self.config.loop_caps

        if tool_name == "web_search":
            cap = caps.max_web_searches
            if cap and self._turn_web_search_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_web_search_cap",
                    message=(
                        f"Blocked web_search: this turn has already made {cap} "
                        "web searches, the per-turn limit. This looks like a "
                        "runaway search loop. Work with the results you already "
                        "have and give the user your answer."
                    ),
                    tool_name=tool_name,
                    count=self._turn_web_search_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_web_search_count += 1
            return None

        if tool_name == "delegate_task":
            cap = caps.max_subagents
            if not cap:
                return None
            spawn_count = _subagent_spawn_count(args)
            if spawn_count == 0:
                # Control action (list/steer/stop) — spawns nothing. Never
                # block: once the spawn cap is hit, steering/stopping the
                # existing children is exactly what should still work.
                return None
            if self._turn_subagent_count >= cap:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="loop_subagent_cap",
                    message=(
                        f"Blocked delegate_task: this turn has already spawned "
                        f"{self._turn_subagent_count} subagents (limit {cap}). "
                        "This looks like a runaway delegation loop. Finish the "
                        "work with the results you have and answer the user."
                    ),
                    tool_name=tool_name,
                    count=self._turn_subagent_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision
            self._turn_subagent_count += spawn_count
            return None

        return None


def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic role=tool content string for a blocked tool call."""
    return json.dumps(
        {
            "error": decision.message,
            "guardrail": decision.to_metadata(),
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """Append runtime guidance to the current tool result content."""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    suffix = (
        f"\n\n[{label}: "
        f"{decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result or "") + suffix


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


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = safe_json_loads(result or "")
    if parsed is not None:
        try:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            canonical = str(parsed)
    else:
        canonical = result or ""
    return _sha256(canonical)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _non_negative_int(value: Any, default: int) -> int:
    """Parse a session-cap value. 0 is a valid (disable) value; negatives and
    junk fall back to the default."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _subagent_spawn_count(args: Mapping[str, Any]) -> int:
    """How many subagents a single delegate_task call spawns.

    delegate_task runs in one of two modes: a batch (``tasks`` is a non-empty
    list, one child per item) or a single task (``goal``). Count the batch size
    when present, otherwise 1, so the session subagent cap reflects real spawns
    rather than delegate_task invocations. Control actions (list/steer/stop)
    spawn nothing and must not consume the cap.
    """
    if isinstance(args, Mapping):
        action = str(args.get("action") or "").strip().lower()
        if action in ("list", "steer", "stop"):
            return 0
    tasks = args.get("tasks") if isinstance(args, Mapping) else None
    if isinstance(tasks, list) and tasks:
        return len(tasks)
    return 1


def _sha256(value: str) -> str:
    # surrogatepass: tool results scraped from the web can carry unpaired
    # UTF-16 surrogates (e.g. half of a mathematical-bold pair); a strict
    # encode raises and takes down the whole conversation loop. The hash only
    # needs deterministic bytes, not valid UTF-8.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
