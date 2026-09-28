"""No-tool-call (final text) branch of the conversation turn loop: empty/think-only recovery,
intent-ack / stall-guard continuation, length-continuation joining, dropped-tool-call
re-prompt, scaffolding pop, stop gates, then the durable final flush. Extracted from
``run_conversation``; nothing here imports ``agent.conversation_loop`` at module level
(cycle) — loop-internal nudge constants resolve lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import json
from typing import Any, Dict, Optional

from agent.message_metadata import append_message
from agent.turn_empty_response import recover_empty_response
from agent.turn_stop_gates import apply_stop_gates
from agent.message_sanitization import strip_model_tool_disclosures, strip_internal_deliberation_tail
from agent.hk_legal_authority_gate import evaluate_hk_legal_answer
from agent.memory_deletion_gate import evaluate_memory_deletion_answer
from agent.file_receipt_truth_gate import evaluate_file_receipt_truth
from agent.skill_source_gate import evaluate_skill_source_contract
from agent.artifact_delivery import ensure_export_links_in_terminal_answer, latest_successful_export_content

logger = logging.getLogger("agent.conversation_loop")

# Ephemeral retry scaffolding rows popped before the final answer becomes durable.
_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_thinking_prefill", "_empty_recovery_synthetic", "_empty_terminal_sentinel",
    "_dropped_toolcall_nudge",
    '_model_tool_disclosure_nudge',
    '_hk_legal_authority_synthetic',
    '_memory_deletion_synthetic',
    '_file_receipt_truth_synthetic',
    '_skill_source_synthetic',
)


@dataclass
class FinalResponseVerdict:
    """``action``: ``"break"`` (turn ends with ``final_response``), ``"continue"`` (a
    continuation/re-prompt/stop-gate asked for another API call) or ``"return"``
    (``result`` is the turn's result dict). The other fields are the loop locals rebound."""

    action: str
    active_system_prompt: Any
    final_response: Any
    _turn_exit_reason: Any
    _preflight_compression_blocked: Any
    codex_ack_continuations: Any
    model_tool_disclosure_retries: Any
    hk_legal_authority_nudges: Any
    memory_deletion_nudges: Any
    file_receipt_truth_nudges: Any
    skill_source_nudges: Any
    truncated_response_parts: Any
    length_continue_retries: Any
    _pending_verification_response: Any
    _pending_verification_response_previewed: Any
    result: Optional[Dict[str, Any]] = None


def finish_text_response(
    agent: Any, *, assistant_message: Any, response: Any, finish_reason: Any, messages: Any,
    api_messages: Any, conversation_history: Any, api_call_count: Any, user_message: Any,
    active_system_prompt: Any, final_response: Any, _turn_exit_reason: Any,
    _preflight_compression_blocked: Any, codex_ack_continuations: Any,
    truncated_response_parts: Any, length_continue_retries: Any,
    current_turn_user_idx: Any,
    model_tool_disclosure_retries: Any,
    hk_legal_authority_nudges: Any,
    memory_deletion_nudges: Any,
    file_receipt_truth_nudges: Any,
    skill_source_nudges: Any,
    _pending_verification_response: Any, _pending_verification_response_previewed: Any,
) -> FinalResponseVerdict:
    """Finish (or defer) a text-only assistant response in the original guard order. Every
    continuation path sets ``final_response = None`` so an acknowledgment never suppresses
    iteration-limit summarization; the final message is appended and flushed only after the
    stop gates accept it."""
    from agent.conversation_loop import (
        _CODEX_ACK_CONTINUATION_NUDGE, _DEGENERATE_FINAL_NUDGE, _DROPPED_TOOLCALL_NUDGE_CONTENT,
        _join_truncated_parts, _MODEL_TOOL_DISCLOSURE_NUDGE, _MODEL_TOOL_DISCLOSURE_FAILURE,
        _is_action_only_after_tool_disclosure, _discard_held_skill_source_stream,
        _release_held_skill_source_terminal_reply, _deliver_verbatim_terminal_reply,
    )

    def _verdict(action: str, result: Optional[Dict[str, Any]] = None) -> FinalResponseVerdict:
        return FinalResponseVerdict(
            action=action, active_system_prompt=active_system_prompt, final_response=final_response,
            _turn_exit_reason=_turn_exit_reason,
            _preflight_compression_blocked=_preflight_compression_blocked,
            codex_ack_continuations=codex_ack_continuations,
            model_tool_disclosure_retries=model_tool_disclosure_retries,
            hk_legal_authority_nudges=hk_legal_authority_nudges,
            memory_deletion_nudges=memory_deletion_nudges,
            file_receipt_truth_nudges=file_receipt_truth_nudges,
            skill_source_nudges=skill_source_nudges,
            truncated_response_parts=truncated_response_parts,
            length_continue_retries=length_continue_retries,
            _pending_verification_response=_pending_verification_response,
            _pending_verification_response_previewed=_pending_verification_response_previewed,
            result=result,
        )

    # Reasoning-only clean stop: some reasoning parsers (vLLM nemotron_v3 past ~500K
    # prompt tokens) file the whole answer as reasoning when the model omits the closing
    # delimiter. ``finish_reason == "stop"`` means the provider considers generation
    # complete, so the empty-response ladder would only re-bill the same input to arrive
    # at a truncated preview of this text; promote the reasoning to the visible answer
    # BEFORE the ladder. ``length`` (cut off mid-thought) stays on the continuation path.
    # The promoted text is RETURNED as the answer but never written into the assistant
    # row's ``content``: chain-of-thought stored as ordinary content is indistinguishable
    # from a real reply on every history surface (#111761). The row keeps ``content``
    # empty with the text in its reasoning fields and carries the promoted text as the
    # ``api_content`` sidecar, so the next turn still replays it byte-identically.
    _content = assistant_message.content
    _promoted = None
    if (
        finish_reason == "stop"
        and not assistant_message.tool_calls
        and (_content is None or (isinstance(_content, str) and not _content.strip()))
    ):
        _promoted = agent._extract_reasoning(assistant_message) or None
        if _promoted:
            # WARNING, not INFO: a model that keeps ending turns this way is stalled
            # (planning monologue, zero tool calls) while the turn reports "complete".
            logger.warning(
                "Reasoning-only clean stop (%d chars) — returning the reasoning as the final "
                "response (model=%s provider=%s api_calls=%d tool_turns=%d)",
                len(_promoted), agent.model, agent.provider, api_call_count,
                sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")),
            )
    final_response = _promoted or assistant_message.content or ""
    # Unmute: _mute_post_response from a housekeeping tool turn must not silence
    # empty-response warnings on the final response path.
    agent._mute_post_response = False

    # Think-block-only / empty content: recovery path.
    if not agent._has_content_after_think_block(final_response):
        _ev = recover_empty_response(
            agent, assistant_message, response, finish_reason, final_response=final_response,
            messages=messages, api_messages=api_messages, conversation_history=conversation_history,
            active_system_prompt=active_system_prompt, api_call_count=api_call_count,
            turn_exit_reason=_turn_exit_reason,
            preflight_compression_blocked=_preflight_compression_blocked,
        )
        final_response = _ev.final_response
        _turn_exit_reason = _ev.turn_exit_reason
        active_system_prompt = _ev.active_system_prompt
        _preflight_compression_blocked = _ev.preflight_compression_blocked
        if _ev.action == "return":
            return _verdict("return", _ev.result)
        if _ev.action == "break":
            return _verdict("break")
        return _verdict("continue")

    agent._empty_content_retries = 0
    agent._thinking_prefill_retries = 0
    # Surface the one-shot fallback switch notice before dropping the retry buffer so a
    # provider/model switch stays visible on success.
    agent._emit_pending_fallback_notice()
    agent._clear_status_buffer()

    # Defensive: repair malformed role-alternation before API call. Catches cases where the history got
    # wedged into a ``tool → user`` or ``user → user`` tail (e.g. after empty- response scaffolding was
    # stripped and a new user message landed after an orphan tool result). Most providers return empty
    # content on malformed sequences, which would otherwise retrigger the empty-retry loop indefinitely.
    # repair_message_sequence_with_cursor also recomputes the SessionDB flush cursor (_last_flushed_db_idx)
    # when repair compacts the list, so the turn-end flush doesn't skip the assistant/tool chain (#44837).
    # One-time repeated-heal escalation notice (#96870): if the sanitizer above just crossed the per-session
    # heal threshold, deliver the queued notice through the status/warning callback — the normal out-of-band
    # delivery channel (gateway status message / CLI print). NEVER appended to messages/api_messages:
    # conversation context and the cached prompt prefix stay byte-identical.
    from agent.agent_runtime_helpers import (
        intent_ack_continuation_mode, looks_like_degenerate_final, promoted_reasoning_announces_action,
        tool_results_this_turn, trailing_continue_intent,
    )

    _ack_mode = intent_ack_continuation_mode(agent)
    # Said-continue-but-stopped guard: no tool calls but the short reply TAILS with an
    # announced next action. Reuses the SAME bounded continuation counter (max 2 per turn).
    # Promoted reasoning gets the broader first-person-plan tail detector: with tools offered
    # and zero tool calls, chain-of-thought ending on "Let me batch the terminal calls..." is a
    # stalled model, and returning it as the answer aborts the tool loop while reporting
    # "complete" (#111761). Same cap, so a model that never acts still ends after 2 nudges.
    _stall_text = agent._strip_think_blocks(final_response or "")
    _stall_continue_intent = (
        bool(getattr(agent, "_stall_guards", True))
        and agent.valid_tool_names
        and codex_ack_continuations < 2
        and (
            trailing_continue_intent(_stall_text)
            or (bool(_promoted) and promoted_reasoning_announces_action(_stall_text))
        )
    )
    # Degenerate-final guard (#103483): the turn did real tool work and then stopped on a
    # fragment. Same scope knob and the SAME bounded counter as the ack continuation; the nudge
    # row itself closes the tool-work window, so a second fragment ends the turn as the answer.
    _tool_rows = tool_results_this_turn(messages)
    _degenerate_final = (
        bool(getattr(agent, "_stall_guards", True))
        and _ack_mode != "off"
        and codex_ack_continuations < 2
        and _tool_rows > 0
        and looks_like_degenerate_final(_stall_text, user_message=user_message)
    )
    # Precedence: an announced next action outranks the fragment shape; the codex ack is last.
    if _stall_continue_intent:
        _continuation_kind = "stall"
    elif _degenerate_final:
        _continuation_kind = "degenerate"
    elif (
        _ack_mode != "off"
        and agent.valid_tool_names
        and codex_ack_continuations < 2
        and agent._looks_like_codex_intermediate_ack(
            user_message=user_message, assistant_content=final_response, messages=messages,
            require_workspace=(_ack_mode == "codex_only"),
        )
    ):
        _continuation_kind = "ack"
    else:
        _continuation_kind = None
    if _continuation_kind:
        if _continuation_kind == "stall":
            logger.info(
                "Stall guard: turn ending on trailing continue-"
                "intent with no tool calls — re-prompting to act "
                "(%d/2)", codex_ack_continuations + 1,
            )
        elif _continuation_kind == "degenerate":
            logger.warning(
                "Degenerate final: %d-char fragment %r ended the turn after %d tool result(s) — "
                "re-prompting (%d/2)", len(_stall_text), _stall_text[:40], _tool_rows,
                codex_ack_continuations + 1,
            )
        codex_ack_continuations += 1
        interim_msg = agent._build_assistant_message(assistant_message, "incomplete")
        if _promoted:
            # Same sidecar as the final row: the wire copy must carry the promoted text, not only
            # ``reasoning_content``, or the continuation replays an empty assistant turn.
            interim_msg["api_content"] = final_response
        append_message(messages, interim_msg)
        agent._emit_interim_assistant_message(interim_msg)
        append_message(messages, {
            "role": "user",
            "content": (
                _DEGENERATE_FINAL_NUDGE if _continuation_kind == "degenerate"
                else _CODEX_ACK_CONTINUATION_NUDGE
            ),
        })
        agent._session_messages = messages
        # An acknowledgment is non-final: its text must not suppress iteration-limit
        # summarization if the continuation exhausts budget.
        final_response = None
        return _verdict("continue")

    codex_ack_continuations = 0

    if truncated_response_parts:
        final_response = _join_truncated_parts([*truncated_response_parts, final_response])
        truncated_response_parts = []
        length_continue_retries = 0
        # The continuation recovered, so the fragments stay in the transcript.
        for _frag in messages:
            if isinstance(_frag, dict):
                _frag.pop("_length_continuation_fragment", None)
                _frag.pop("_length_continuation_nudge", None)

    final_response = strip_internal_deliberation_tail(
        agent._strip_think_blocks(final_response).strip()
    )

    final_msg = agent._build_assistant_message(assistant_message, finish_reason)
    if _promoted:
        # Replay sidecar only: ``content`` stays empty so the row is never mistaken for a
        # real reply; ``build_api_messages`` substitutes ``api_content`` on the wire.
        final_msg["api_content"] = final_response

    _joined_cleaning = strip_model_tool_disclosures(final_response)
    final_response = _joined_cleaning.text.strip()
    if _promoted:
        final_msg["api_content"] = final_response
    else:
        final_msg["content"] = final_response
    if (
        _joined_cleaning.removed_blocks
        and not final_msg.get("_model_tool_disclosure_removed")
    ):
        final_msg["_model_tool_disclosure_removed"] = {
            "blocks": _joined_cleaning.removed_blocks,
            "chars": _joined_cleaning.removed_chars,
            "unclosed": _joined_cleaning.had_unclosed_block,
        }
    _removed_disclosure = final_msg.get(
        "_model_tool_disclosure_removed"
    )
    if (
        _removed_disclosure
        and _is_action_only_after_tool_disclosure(final_response)
    ):
        if model_tool_disclosure_retries < 1:
            model_tool_disclosure_retries += 1
            logger.warning(
                "Rejected action-only response after removing "
                "model-authored tool disclosure; requesting one "
                "native-tool/substantive recovery (session=%s)",
                getattr(agent, "session_id", None) or "none",
            )
            agent._emit_status(
                "↻ Rejected unauthenticated tool display — "
                "requesting an honest native action or answer"
            )
            final_msg["_model_tool_disclosure_nudge"] = True
            append_message(messages, final_msg)
            append_message(messages, {
                "role": "user",
                "content": _MODEL_TOOL_DISCLOSURE_NUDGE,
                "_model_tool_disclosure_nudge": True,
            })
            agent._session_messages = messages
            final_response = None
            return _verdict("continue")

        logger.error(
            "Model repeated action-only unauthenticated tool "
            "disclosure after bounded recovery (session=%s)",
            getattr(agent, "session_id", None) or "none",
        )
        final_response = _MODEL_TOOL_DISCLOSURE_FAILURE
        _promoted = None
        final_msg.pop("api_content", None)
        final_msg["content"] = final_response
        final_msg["finish_reason"] = "tool_disclosure_rejected"

    # A substantive sanitized answer ends the recovery budget.
    model_tool_disclosure_retries = 0

    # Dropped tool-call recovery (copilot/Claude): finish_reason="tool_calls" with empty
    # tool_calls would end the turn unstarted; re-prompt (max 3 CONSECUTIVE stalls).
    if (
        finish_reason == "tool_calls"
        and not assistant_message.tool_calls
        and getattr(agent, "_dropped_toolcall_retries", 0) < 3
    ):
        agent._dropped_toolcall_retries = getattr(agent, "_dropped_toolcall_retries", 0) + 1
        logger.warning(
            "finish_reason=tool_calls with empty tool_calls array "
            "(narration only) — re-prompting to emit the call "
            "(retry %d/3, model=%s provider=%s)",
            agent._dropped_toolcall_retries, agent.model, agent.provider,
        )
        agent._emit_diagnostic_status(
            "↻ Model signaled a tool call but sent none — "
            f"re-prompting ({agent._dropped_toolcall_retries}/3)"
        )
        # Both halves of the re-prompt pair are ephemeral scaffolding: never persisted,
        # and the finalization pop strips an unanswered tail pair.
        final_msg["_dropped_toolcall_nudge"] = True
        append_message(messages, final_msg)
        append_message(messages, {
            "role": "user",
            "content": _DROPPED_TOOLCALL_NUDGE_CONTENT,
            "_dropped_toolcall_nudge": True,
        })
        agent._session_messages = messages
        final_response = None
        return _verdict("continue")

    # Genuine turn end (no dropped-tool-call mismatch): clear stall budget.
    agent._dropped_toolcall_retries = 0

    # Pop prefill / empty-retry scaffolding before the final response or
    # verification follow-up; it must not become durable transcript.
    while (
        messages
        and isinstance(messages[-1], dict)
        and any(messages[-1].get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS)
    ):
        messages.pop()

    from agent.turn_finalizer import apply_llm_output_transform
    _transformed = False
    if not getattr(agent, "_interrupt_requested", False):
        final_response, _transformed, _ = apply_llm_output_transform(
            agent, final_response, turn_id=getattr(agent, "_current_turn_id", "") or "", logger=logger,
        )
    if _transformed:
        if _promoted:
            final_msg["api_content"] = final_response
        else:
            final_msg["content"] = final_response

    final_response = strip_internal_deliberation_tail(final_response)
    if _promoted:
        final_msg["api_content"] = final_response
    else:
        final_msg["content"] = final_response

    _skill_source_decision = evaluate_skill_source_contract(
        messages=messages,
        current_turn_user_idx=current_turn_user_idx,
        attempts=skill_source_nudges,
        final_response=final_response or "",
    )
    if (
        _skill_source_decision
        and _skill_source_decision.action == "nudge"
    ):
        skill_source_nudges += 1
        final_msg["finish_reason"] = "skill_source_required"
        final_msg["_skill_source_synthetic"] = True
        append_message(messages, final_msg)
        append_message(
            messages,
            {
                "role": "user",
                "content": _skill_source_decision.message,
                "_skill_source_synthetic": True,
            },
        )
        agent._session_messages = messages
        logger.warning(
            "Named skill answer rejected pending declared sources "
            "or answer contract (attempt %d, missing=%s, "
            "diagnostics=%s, session=%s)",
            skill_source_nudges,
            list(_skill_source_decision.missing),
            list(_skill_source_decision.diagnostics),
            getattr(agent, "session_id", None) or "none",
        )
        logger.warning(
            "SKILL_SOURCE_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "nudge",
                    "attempt": skill_source_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "missing": list(_skill_source_decision.missing),
                    "diagnostics": list(
                        _skill_source_decision.diagnostics
                    ),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        agent._emit_status(
            "↻ 技能所需的權威來源尚未載入 — 正在讀取原始資料"
        )
        _discard_held_skill_source_stream(agent)
        final_response = None
        return _verdict("continue")
    if (
        _skill_source_decision
        and _skill_source_decision.action == "fail"
    ):
        logger.warning(
            "SKILL_SOURCE_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "fail",
                    "attempt": skill_source_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "missing": list(_skill_source_decision.missing),
                    "diagnostics": list(
                        _skill_source_decision.diagnostics
                    ),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        final_response = _skill_source_decision.message
        _promoted = None
        final_msg.pop("api_content", None)
        final_msg["content"] = final_response
        final_msg["finish_reason"] = "skill_source_unconfirmed"

    # A memory deletion changes only the active projection. The
    # complete original remains in isolated append-only evidence;
    # reject terminal copy that falsely claims physical erasure.
    _memory_deletion_decision = evaluate_memory_deletion_answer(
        messages=messages,
        current_turn_user_idx=current_turn_user_idx,
        final_response=final_response or "",
        attempts=memory_deletion_nudges,
    )
    if (
        _memory_deletion_decision
        and _memory_deletion_decision.action == "nudge"
    ):
        memory_deletion_nudges += 1
        final_msg["finish_reason"] = "memory_deletion_truth_required"
        final_msg["_memory_deletion_synthetic"] = True
        append_message(messages, final_msg)
        append_message(
            messages,
            {
                "role": "user",
                "content": _memory_deletion_decision.message,
                "_memory_deletion_synthetic": True,
            },
        )
        agent._session_messages = messages
        logger.warning(
            "MEMORY_DELETION_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "nudge",
                    "attempt": memory_deletion_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "diagnostics": list(
                        _memory_deletion_decision.diagnostics
                    ),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        agent._emit_status(
            "↻ 記憶已從使用中移除，原始證據仍保留 — 正在修正說明"
        )
        final_response = None
        return _verdict("continue")
    if (
        _memory_deletion_decision
        and _memory_deletion_decision.action == "replace"
    ):
        logger.warning(
            "MEMORY_DELETION_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "replace",
                    "attempt": memory_deletion_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "diagnostics": list(
                        _memory_deletion_decision.diagnostics
                    ),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        final_response = _memory_deletion_decision.message
        _promoted = None
        final_msg.pop("api_content", None)
        final_msg["content"] = final_response
        final_msg["finish_reason"] = "memory_deletion_truth_corrected"

    # A shortened tool preview is a UI concern, not evidence that
    # the underlying document was partial. Keep terminal claims
    # aligned with the structured read_file receipt and continue a
    # requested packaging workflow instead of sending the user to
    # reattach the same bytes in a fresh chat.
    _file_receipt_truth_decision = evaluate_file_receipt_truth(
        messages=messages,
        current_turn_user_idx=current_turn_user_idx,
        final_response=final_response or "",
        attempts=file_receipt_truth_nudges,
        import_files_available=(
            "skill_manage" in agent.valid_tool_names
        ),
    )
    if (
        _file_receipt_truth_decision
        and _file_receipt_truth_decision.action == "nudge"
    ):
        file_receipt_truth_nudges += 1
        final_msg["finish_reason"] = "file_receipt_truth_required"
        final_msg["_file_receipt_truth_synthetic"] = True
        append_message(messages, final_msg)
        append_message(
            messages,
            {
                "role": "user",
                "content": _file_receipt_truth_decision.message,
                "_file_receipt_truth_synthetic": True,
            },
        )
        agent._session_messages = messages
        logger.warning(
            "FILE_RECEIPT_TRUTH_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "nudge",
                    "attempt": file_receipt_truth_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "diagnostics": list(
                        _file_receipt_truth_decision.diagnostics
                    ),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        agent._emit_status(
            "↻ 文件 receipt 顯示完整 — 正在修正錯誤的截斷判斷並繼續"
        )
        final_response = None
        return _verdict("continue")
    if (
        _file_receipt_truth_decision
        and _file_receipt_truth_decision.action == "replace"
    ):
        logger.warning(
            "FILE_RECEIPT_TRUTH_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "replace",
                    "attempt": file_receipt_truth_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "diagnostics": list(
                        _file_receipt_truth_decision.diagnostics
                    ),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        final_response = _file_receipt_truth_decision.message
        _promoted = None
        final_msg.pop("api_content", None)
        final_msg["content"] = final_response
        final_msg["finish_reason"] = "file_receipt_truth_corrected"

    # ── Hong Kong statutory-law authority stop gate ───────
    # Prompt policy is insufficient when a stale user skill tells
    # a small model to use incorrect provisions. Before exposing a
    # legal conclusion, require a successful official HKeL lookup
    # from THIS user turn and a visible URL + version citation.
    _hk_legal_decision = evaluate_hk_legal_answer(
        messages=messages,
        current_turn_user_idx=current_turn_user_idx,
        final_response=final_response or "",
        attempts=hk_legal_authority_nudges,
        exported_artifact_contents=tuple(
            content
            for content in (
                latest_successful_export_content(
                    messages,
                    current_turn_user_idx=current_turn_user_idx,
                ),
            )
            if content
        ),
    )

    if _hk_legal_decision and _hk_legal_decision.action == "nudge":
        hk_legal_authority_nudges += 1
        final_msg["finish_reason"] = "hk_legal_authority_required"
        final_msg["_hk_legal_authority_synthetic"] = True
        append_message(messages, final_msg)
        append_message(messages, {
            "role": "user",
            "content": _hk_legal_decision.message,
            "_hk_legal_authority_synthetic": True,
        })
        agent._session_messages = messages
        logger.warning(
            "HK legal answer rejected pending official authority "
            "(attempt %d, session=%s)",
            hk_legal_authority_nudges,
            getattr(agent, "session_id", None) or "none",
        )
        logger.warning(
            "HK_LEGAL_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "nudge",
                    "attempt": hk_legal_authority_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "diagnostics": list(_hk_legal_decision.diagnostics),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
            ),
        )
        agent._emit_status(
            "↻ 香港法律答案未通過官方法源閘門 — 正在查核現行法例"
        )
        final_response = None
        return _verdict("continue")

    if _hk_legal_decision and _hk_legal_decision.action == "fail":
        logger.error(
            "HK legal answer failed closed after %d authority nudges "
            "(session=%s)",
            hk_legal_authority_nudges,
            getattr(agent, "session_id", None) or "none",
        )
        logger.error(
            "HK_LEGAL_GATE_EVIDENCE %s",
            json.dumps(
                {
                    "action": "fail",
                    "attempt": hk_legal_authority_nudges,
                    "session_id": getattr(agent, "session_id", None),
                    "diagnostics": list(_hk_legal_decision.diagnostics),
                    "candidate": final_response or "",
                },
                ensure_ascii=False,
            ),
        )
        final_response = _hk_legal_decision.message
        _promoted = None
        final_msg.pop("api_content", None)
        final_msg["content"] = final_response
        final_msg["finish_reason"] = "hk_legal_authority_unconfirmed"

    if _hk_legal_decision and _hk_legal_decision.action == "replace":
        logger.warning(
            "HK legal answer replaced with verified deterministic "
            "minimum after %d authority nudges (session=%s)",
            hk_legal_authority_nudges,
            getattr(agent, "session_id", None) or "none",
        )
        final_response = _hk_legal_decision.message
        _promoted = None
        final_msg.pop("api_content", None)
        final_msg["content"] = final_response
        final_msg["finish_reason"] = "hk_legal_authority_corrected"
        agent._emit_status(
            "✓ 香港法律答案已由已驗證官方法源產生安全版本"
        )

    if (
        _hk_legal_decision
        and _hk_legal_decision.action in {"fail", "replace"}
        and final_response
    ):
        # The gate synthesizes this terminal reply after the model
        # stream has ended. Earlier progress text makes the
        # gateway's empty-stream bridge ineligible, so explicitly
        # project the authoritative replacement to streaming
        # clients as well as persisting/returning it.
        if not getattr(agent, "_skill_source_stream_hold", False):
            _deliver_verbatim_terminal_reply(agent, final_response)

    # A successful export already returned canonical signed links.
    # Do not ask a language model to remember or reconstruct them:
    # append missing links deterministically at the response
    # boundary.  A legal fail/replace may reject the contents of an
    # earlier artifact, so never surface that artifact in those two
    # safety outcomes.
    if not (
        _hk_legal_decision
        and _hk_legal_decision.action in {"fail", "replace"}
    ):
        final_response, _artifact_suffix = (
            ensure_export_links_in_terminal_answer(
                final_response or "",
                messages,
                current_turn_user_idx=current_turn_user_idx,
            )
        )
        if _artifact_suffix:
            if _promoted:
                final_msg["api_content"] = final_response
            else:
                final_msg["content"] = final_response
            try:
                agent._fire_stream_delta(_artifact_suffix)
                if agent.stream_delta_callback:
                    agent.stream_delta_callback(None)
            except Exception:
                logger.warning(
                    "artifact terminal-link stream projection failed "
                    "(session=%s); stored response remains complete",
                    getattr(agent, "session_id", None) or "none",
                    exc_info=True,
                )

    _sg = apply_stop_gates(
        agent, final_msg, final_response=final_response, messages=messages,
        conversation_history=conversation_history,
        pending_verification_response=_pending_verification_response,
        pending_verification_response_previewed=_pending_verification_response_previewed,
    )
    _pending_verification_response = _sg.pending_verification_response
    _pending_verification_response_previewed = _sg.pending_verification_response_previewed
    if _sg.continue_turn:
        final_response = None
        return _verdict("continue")

    # Plugins rewrite the reply BEFORE it is appended and flushed: SQLite treats a non-blank
    # assistant row as settled, so a transform after this write would reach the user but never
    # the stored/replayed transcript (#44239). finalize_turn reads the recorded outcome; like
    # there, an interrupted turn keeps the raw text.
    _release_held_skill_source_terminal_reply(agent, final_response or "")
    append_message(messages, final_msg)
    # Make the answer durable before leaving the loop (_DB_PERSISTED_MARKER keeps
    # _persist_session idempotent). Failure must NOT abort the turn: finalize retries.
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.warning(
            "final text-turn flush failed (session=%s) — reply is "
            "not yet durable; relying on finalize_turn retry",
            getattr(agent, "session_id", None) or "none",
            exc_info=True,
        )

    _turn_exit_reason = f"text_response(finish_reason={finish_reason})"
    if not agent.quiet_mode:
        agent._safe_print(f"🎉 Conversation completed after {api_call_count} OpenAI-compatible API call(s)")
    return _verdict("break")
