"""OpenAI-compatible routes for the API server adapter.

``OpenAICompatRoutesMixin`` (inherited by ``APIServerAdapter``) carries ``/v1/chat/completions``,
``/v1/responses`` (+ GET/DELETE), their SSE writers and the Responses-transcript helpers.
api_server-internal helpers are imported lazily inside each method: the origin imports this
module (top-level import = cycle), and lazy lookup keeps ``patch("...api_server.X")`` effective.
"""

import asyncio
import threading
import json
import logging
import re
import time
import uuid
from contextlib import suppress
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - mirrors api_server's optional import
    web = None  # type: ignore[assignment]

# Logger parity with the origin module (moved log records keep their name).
logger = logging.getLogger("gateway.platforms.api_server")

async def _iter_stream_items(stream_q, agent_task, response):
    """Yield agent stream items until EOS, writing SSE keepalives while idle.

    Yields the ``None`` sentinel once so callers can run EOS-only work; when ``agent_task``
    is already done the remaining queue is drained and the sentinel swallowed.
    """
    from gateway.platforms.api_server import CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS

    last_activity = time.monotonic()
    while True:
        try:
            item = await asyncio.wait_for(stream_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if agent_task.done():
                while True:
                    try:
                        item = stream_q.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    if item is None:
                        return
                    yield item
                    last_activity = time.monotonic()
            if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                await response.write(b": keepalive\n\n")
                last_activity = time.monotonic()
            continue
        if item is None:
            yield None
            return
        yield item
        last_activity = time.monotonic()


def _result_flags(result: Any) -> tuple:
    """``(completed, partial, failed, error)`` from an agent result dict (defaults if not a dict)."""
    if not isinstance(result, dict):
        return True, False, False, None
    return (bool(result.get("completed", True)), bool(result.get("partial")),
            bool(result.get("failed")), result.get("error"))


def _finish_reason(completed, is_partial, is_failed, err_msg, agent_error=None) -> str:
    """OpenAI ``finish_reason``: "length" for truncation, "error" for failure, else "stop"."""
    # OpenAI uses "length" for truncation, "stop" for normal completion, and downstream SDKs accept "error"
    # / custom codes. See issue #22496.
    if is_partial and err_msg and "truncat" in err_msg.lower():
        return "length"
    if agent_error is not None or is_failed or (not completed and err_msg):
        return "error"
    return "stop"


def _hermes_extras(completed, is_partial, is_failed, err_msg, finish_reason: str) -> Dict[str, Any]:
    return {
        "completed": completed, "partial": is_partial, "failed": is_failed, "error": err_msg,
        "error_code": "output_truncated" if finish_reason == "length" else "agent_error"}


def _message_item(text: Any) -> Dict[str, Any]:
    """Responses ``message`` output item carrying one ``output_text`` part."""
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def _cap_text(text: str, keep: int) -> str:
    """Head of ``text`` plus a marker saying how much was cut (the Responses truncation rule)."""
    return text[:keep] + "...[" + str(len(text) - keep) + " more chars]"


def _cap_history_tool_outputs(history: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    """Copy of ``history`` with tool outputs and string tool-call arguments longer than
    ``max_chars`` cut down. Only tool rows and ``tool_calls`` blobs change; user/assistant text
    is left alone, and the agent's own transcript rows are never mutated (rows are copied).
    Opt-in via gateway.api_server.history_tool_output_max_chars: a single stored snapshot
    embeds the full cumulative history, so a few large tool outputs pushed one
    response_store.db write to ~677 KB (#82513)."""
    if max_chars <= 0:
        return history
    out: List[Dict[str, Any]] = []
    for msg in history:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if msg.get("role") == "tool" and isinstance(content, str) and len(content) > max_chars:
            msg = {**msg, "content": _cap_text(content, max_chars)}
        tool_calls = msg.get("tool_calls")
        if msg.get("role") == "assistant" and isinstance(tool_calls, list):
            capped_calls = []
            for call in tool_calls:
                fn = call.get("function") if isinstance(call, dict) else None
                raw = fn.get("arguments") if isinstance(fn, dict) else None
                if isinstance(raw, str) and len(raw) > max_chars:
                    try:
                        args = json.loads(raw)
                    except ValueError:
                        args = None
                    if isinstance(args, dict):
                        for k, v in args.items():
                            if isinstance(v, str) and len(v) > max_chars:
                                args[k] = _cap_text(v, max_chars)
                        call = {**call, "function": {**fn, "arguments": json.dumps(args)}}
                capped_calls.append(call)
            msg = {**msg, "tool_calls": capped_calls}
        out.append(msg)
    return out


def _reasoning_item(text: str) -> Dict[str, Any]:
    """Completed Responses ``reasoning`` output item (same shape the SSE writer closes with)."""
    return {"id": f"rs_{uuid.uuid4().hex[:24]}", "type": "reasoning", "status": "completed",
            "summary": [{"type": "summary_text", "text": text}]}


def _is_reasoning_input_item(item: Any) -> bool:
    """Echoed-back ``reasoning`` output item: Responses SDK clients replay a prior response's
    ``output`` list as the next ``input``. It carries no message content, so it must be
    skipped rather than parsed into an empty ``user`` turn (#99552)."""
    return isinstance(item, dict) and item.get("type") == "reasoning"


def _turn_reasoning_text(
        conversation_history: List[Dict[str, Any]], user_message: Any, result: Dict[str, Any]) -> str:
    """Reasoning the model produced on this turn, joined for a non-streaming
    ``message.reasoning_content``. Read from the assistant messages the agent already
    persisted (``build_assistant_message`` stores the structured reasoning under
    ``reasoning``) rather than re-accumulating callback deltas, so it is exactly what the
    stream would have carried and cannot double-count the post-response fallback."""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return ""
    start = OpenAICompatRoutesMixin._response_messages_turn_start_index(
        conversation_history, user_message, result)
    parts = [m["reasoning"] for m in messages[start:]
             if isinstance(m, dict) and m.get("role") == "assistant"
             and isinstance(m.get("reasoning"), str) and m["reasoning"].strip()]
    return "\n\n".join(parts)


def _trim_tool_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim large tool payloads in place so response.completed stays under ~100KB (clients
    already received the full details via the incremental events)."""
    for item in items:
        if item.get("type") == "function_call":
            try:
                raw = item.get("arguments")
                args = json.loads(raw) if isinstance(raw, str) else item.get("arguments", {})
                if isinstance(args, dict):
                    for k in ("content", "query", "pattern", "old_string", "new_string"):
                        if isinstance(args.get(k), str) and len(args[k]) > 500:
                            args[k] = f"[{len(args[k])} chars — truncated for response.completed]"
                    item["arguments"] = json.dumps(args)
            except Exception:
                pass
        elif item.get("type") == "function_call_output":
            output = item.get("output", [])
            if isinstance(output, list) and output:
                first = output[0]
                if isinstance(first, dict) and first.get("type") == "input_text":
                    text = first.get("text", "")
                    if len(text) > 1000:
                        first["text"] = _cap_text(text, 500)
                        item["output"] = [first]
    return items


class _ResponsesStream:
    """Per-request state and event emitters for the POST /v1/responses SSE writer.

    Every event carries a monotonic ``sequence_number`` (canonical Responses SSE schema). Text
    deltas are batched (50ms) against Open WebUI re-render storms; tool events flush first.
    """

    def __init__(self, adapter, response, *, response_id: str, model: str, created_at: int,
                 conversation_history: List[Dict[str, str]], user_message: str,
                 instructions: Optional[str], conversation: Optional[str], store: bool, session_id: str, user_id: str = ""):
        from gateway.platforms import api_server as api
        self._api = api
        self.user_id = user_id
        self.emitted_coverage_footer = None
        self.adapter, self.response, self.response_id = adapter, response, response_id
        self.model, self.created_at, self.conversation_history = model, created_at, conversation_history
        self.user_message, self.instructions = user_message, instructions
        self.conversation, self.store, self.session_id = conversation, store, session_id
        # Resolved in the request's profile scope: a snapshot written after it (disconnect) must not follow another.
        self.response_store = adapter._current_response_store()
        self.final_text_parts: List[str] = []
        self.pending_tool_calls: List[Dict[str, Any]] = []  # open function_call items, in order
        self.emitted_items: List[Dict[str, Any]] = []  # output items so far (terminal payload)
        self.output_index = 0
        self.call_counter = 0  # call_id fallback when the agent supplies no tool_call_id
        self.sequence_number = 0
        self.message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.message_output_index: Optional[int] = None
        self.message_opened = False
        self.reasoning_item: Optional[Dict[str, Any]] = None  # open ``reasoning`` output item
        self.final_response_text = ""
        self.agent_error: Optional[str] = None
        self.usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.terminal_snapshot_persisted = False
        self.result: Any = None
        self._batch_buf: List[str] = []
        self._batch_timer: Optional[asyncio.Task] = None
        self._batch_lock = asyncio.Lock()

    async def write_event(self, event_type: str, data: Dict[str, Any]) -> None:
        if "sequence_number" not in data:
            data["sequence_number"] = self.sequence_number
        self.sequence_number += 1
        await self.response.write(self._api._sse_frame(data, event=event_type))

    def envelope(self, status: str) -> Dict[str, Any]:
        return {"id": self.response_id, "object": "response", "status": status,
                "created_at": self.created_at, "model": self.model}

    def terminal_envelope(self, status: str, output: List[Dict[str, Any]], *, error=None) -> dict:
        """``envelope`` + ``output`` (+ ``error`` when given) + ``usage``, in wire key order."""
        env = self.envelope(status)
        env["output"] = output
        if error is not None:
            env["error"] = {"message": error, "type": "server_error"}
        env["usage"] = self._api._responses_usage_payload(self.usage)
        return env

    def _history_with_user(self) -> List[Dict[str, Any]]:
        return list(self.conversation_history) + [{"role": "user", "content": self.user_message}]

    def persist_snapshot(self, response_env: Dict[str, Any], *, history=None, session_id=None):
        if not self.store:
            return
        self.response_store.put(self.response_id, {
            "response": response_env,
            "conversation_history": self._history_with_user() if history is None else history,
            "instructions": self.instructions,
            "user_id": self.user_id,
            "session_id": session_id or self.session_id})
        if self.conversation:
            self.response_store.set_conversation(self.conversation, self.response_id, user_id=self.user_id)

    def persist_incomplete_if_needed(self) -> None:
        """Persist an ``incomplete`` snapshot when no terminal one was written (disconnect /
        cancel paths), so GET /v1/responses/{id} and ``previous_response_id`` chaining survive."""
        if not self.store or self.terminal_snapshot_persisted:
            return
        text = "".join(self.final_text_parts) or self.final_response_text
        items = list(self.emitted_items)
        history = self._history_with_user()
        if text:
            items.append(_message_item(text))
            history.append({"role": "assistant", "content": text})
        self.persist_snapshot(self.terminal_envelope("incomplete", items), history=history)

    async def emit_created(self) -> None:
        env = self.envelope("in_progress")
        env["output"] = []
        await self.write_event("response.created", {"type": "response.created", "response": env})
        self.persist_snapshot(env)

    async def _open_message_item(self) -> None:
        """Emit output_item.added for the assistant message on the first text delta."""
        if self.message_opened:
            return
        self.message_opened = True
        self.message_output_index = self.output_index
        self.output_index += 1
        await self.write_event("response.output_item.added", {
            "type": "response.output_item.added", "output_index": self.message_output_index,
            "item": {"id": self.message_item_id, "type": "message", "status": "in_progress",
                     "role": "assistant", "content": []}})

    async def emit_text_delta(self, delta_text: str) -> None:
        await self.close_reasoning_item()
        await self._open_message_item()
        self.final_text_parts.append(delta_text)
        await self.write_event("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": self.message_item_id,
            "output_index": self.message_output_index, "content_index": 0, "delta": delta_text,
            "logprobs": []})

    async def emit_reasoning_delta(self, delta_text: str) -> None:
        """Responses reasoning-summary family (#99552): one ``reasoning`` output item per
        thinking burst, closed before the next message/tool item opens."""
        if self.reasoning_item is None:
            item = {"id": f"rs_{uuid.uuid4().hex[:24]}", "type": "reasoning", "status": "in_progress",
                    "summary": []}
            self.reasoning_item = {"item": item, "output_index": self.output_index, "parts": []}
            self.output_index += 1
            await self.write_event("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": self.reasoning_item["output_index"], "item": item})
            await self.write_event("response.reasoning_summary_part.added", {
                "type": "response.reasoning_summary_part.added", "item_id": item["id"],
                "output_index": self.reasoning_item["output_index"], "summary_index": 0,
                "part": {"type": "summary_text", "text": ""}})
        rs = self.reasoning_item
        rs["parts"].append(delta_text)
        await self.write_event("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta", "item_id": rs["item"]["id"],
            "output_index": rs["output_index"], "summary_index": 0, "delta": delta_text})

    async def close_reasoning_item(self) -> None:
        rs, self.reasoning_item = self.reasoning_item, None
        if rs is None:
            return
        text = "".join(rs["parts"])
        item = dict(rs["item"], status="completed", summary=[{"type": "summary_text", "text": text}])
        base = {"item_id": item["id"], "output_index": rs["output_index"], "summary_index": 0}
        await self.write_event("response.reasoning_summary_text.done", {
            "type": "response.reasoning_summary_text.done", **base, "text": text})
        await self.write_event("response.reasoning_summary_part.done", {
            "type": "response.reasoning_summary_part.done", **base,
            "part": {"type": "summary_text", "text": text}})
        self.emitted_items.append(item)
        await self.write_event("response.output_item.done", {
            "type": "response.output_item.done", "output_index": rs["output_index"], "item": item})

    async def emit_commentary(self, text: str) -> None:
        """Mid-turn assistant commentary as its own completed ``message`` item carrying
        ``"phase": "commentary"`` — never appended to ``final_text_parts``, so the final answer
        item stays clean (#67580). Closes any open reasoning item first so a reasoning item
        never straddles a message item."""
        await self.close_reasoning_item()
        item = {"id": f"msg_{uuid.uuid4().hex[:24]}", "status": "completed", "phase": "commentary",
                **_message_item(text)}
        idx = self.output_index
        self.output_index += 1
        self.emitted_items.append({"phase": "commentary", **_message_item(text)})
        for event in ("response.output_item.added", "response.output_item.done"):
            await self.write_event(event, {"type": event, "output_index": idx, "item": item})

    async def emit_tool_started(self, payload: Dict[str, Any]) -> None:
        """function_call ``output_item.added``; the agent's tool_call_id beats a generated call id."""
        await self.close_reasoning_item()
        self.call_counter += 1
        call_id = payload.get("tool_call_id") or f"call_{self.response_id[5:]}_{self.call_counter}"
        args = payload.get("arguments", {})
        arguments_str = json.dumps(args) if isinstance(args, dict) else str(args)
        name = payload.get("name", "")
        item = {"id": f"fc_{uuid.uuid4().hex[:24]}", "type": "function_call",
                "status": "in_progress", "name": name, "call_id": call_id, "arguments": arguments_str}
        idx = self.output_index
        self.output_index += 1
        self.pending_tool_calls.append({
            "call_id": call_id, "name": name, "arguments": arguments_str, "item_id": item["id"],
            "output_index": idx})
        self.emitted_items.append(
            {"type": "function_call", "name": name, "arguments": arguments_str, "call_id": call_id})
        await self.write_event("response.output_item.added", {
            "type": "response.output_item.added", "output_index": idx, "item": item})

    async def emit_tool_completed(self, payload: Dict[str, Any]) -> None:
        """function_call ``output_item.done`` + function_call_output added/done; orphans skipped."""
        call_id = payload.get("tool_call_id")
        pending = next((p for p in self.pending_tool_calls if p["call_id"] == call_id), None)
        if not call_id or pending is None:
            return
        self.pending_tool_calls.remove(pending)
        done_item = {"id": pending["item_id"], "type": "function_call", "status": "completed",
                     "name": pending["name"], "call_id": pending["call_id"],
                     "arguments": pending["arguments"]}
        await self.write_event("response.output_item.done", {
            "type": "response.output_item.done", "output_index": pending["output_index"],
            "item": done_item})
        result = payload.get("result", "")
        result_str = result if isinstance(result, str) else json.dumps(result)
        output_parts = [{"type": "input_text", "text": result_str}]
        output_item = {"id": f"fco_{uuid.uuid4().hex[:24]}", "type": "function_call_output",
                       "call_id": pending["call_id"], "output": output_parts, "status": "completed"}
        idx = self.output_index
        self.output_index += 1
        self.emitted_items.append(
            {"type": "function_call_output", "call_id": pending["call_id"], "output": output_parts})
        for event in ("response.output_item.added", "response.output_item.done"):
            await self.write_event(event, {"type": event, "output_index": idx, "item": output_item})

    async def emit_status(self, payload: Dict[str, Any]) -> None:
        """Lifecycle/warning status (provider wait, auto-recovery countdown, fallback switch) as a
        ``hermes.status`` custom event; not a Responses output item."""
        await self.response.write(self._api._sse_frame(payload, event="hermes.status"))

    # queue tag -> (method name, payload adapter)
    _TAG_HANDLERS = {
        "__tool_started__": ("emit_tool_started", lambda p: p),
        "__tool_completed__": ("emit_tool_completed", lambda p: p),
        "__commentary__": ("emit_commentary", lambda p: p["text"]),
        "__reasoning__": ("emit_reasoning_delta", lambda p: p),
        "__status__": ("emit_status", lambda p: p),
    }

    async def dispatch(self, item: Any) -> None:
        """Route one queue item: tagged tuples emit immediately, strings are batched, others dropped."""
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
            tag, payload = item
            await self.flush_batch()
            handler = self._TAG_HANDLERS.get(tag)
            if handler is not None:
                method, adapt = handler
                await getattr(self, method)(adapt(payload))
        elif isinstance(item, str):
            self._batch_buf.append(item)
            if self._batch_timer is None:
                self._batch_timer = asyncio.create_task(self._batch_flush_after(0.05))

    async def _batch_flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # Clear the timer BEFORE flushing so new deltas can start a fresh timer while we emit.
        self._batch_timer = None
        await self.flush_batch()

    def cancel_batch_timer(self) -> None:
        if self._batch_timer and not self._batch_timer.done():
            self._batch_timer.cancel()
            self._batch_timer = None

    async def flush_batch(self) -> None:
        """Emit a single delta for all buffered text."""
        if not self._batch_buf:
            return
        async with self._batch_lock:
            if self._batch_buf:
                combined = "".join(self._batch_buf)
                self._batch_buf = []
                await self.emit_text_delta(combined)

    async def collect_result(self, agent_task) -> None:
        """Await the agent; when it produced a final_response but streamed no deltas
        (some providers only emit the full text at the end), emit one fallback delta."""
        try:
            result, agent_usage = await agent_task
            self.result = result
            self.usage = agent_usage or self.usage
            agent_final = result.get("final_response", "") if isinstance(result, dict) else ""
            if agent_final and not self.final_text_parts:
                await self.emit_text_delta(agent_final)
                self.emitted_coverage_footer = result.get("coverage_footer") if isinstance(result, dict) else None
            if agent_final and not self.final_response_text:
                self.final_response_text = agent_final
            if isinstance(result, dict) and result.get("error") and not self.final_response_text:
                self.agent_error = self._api._redact_api_error_text(result["error"])
            coverage = self._api.emit_responses_coverage_suffix(result if isinstance(result, dict) else None, emitted_coverage_footer=self.emitted_coverage_footer)
            if coverage:
                await self.emit_text_delta(coverage)
        except Exception as e:  # noqa: BLE001
            logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
            self.agent_error = self._api._redact_api_error_text(e)

    async def close_message_item(self) -> None:
        await self.close_reasoning_item()
        self.final_response_text = "".join(self.final_text_parts) or self.final_response_text
        if not self.message_opened:
            return
        await self.write_event("response.output_text.done", {
            "type": "response.output_text.done", "item_id": self.message_item_id,
            "output_index": self.message_output_index, "content_index": 0,
            "text": self.final_response_text, "logprobs": []})
        await self.write_event("response.output_item.done", {
            "type": "response.output_item.done", "output_index": self.message_output_index,
            "item": {"id": self.message_item_id, "type": "message", "status": "completed",
                     "role": "assistant",
                     "content": [{"type": "output_text", "text": self.final_response_text}]}})

    def _final_items(self) -> List[Dict[str, Any]]:
        """Emitted items (trimmed) plus a final message item, so clients that only parse
        the terminal payload still see the assistant text (mirrors _extract_output_items)."""
        items = _trim_tool_items(list(self.emitted_items))
        redact = self._api._redact_api_error_text
        text = self.final_response_text or (redact(self.agent_error) if self.agent_error else "")
        items.append(_message_item(text))
        return items

    async def emit_failed(self) -> None:
        redact = self._api._redact_api_error_text
        env = self.terminal_envelope("failed", self._final_items(), error=redact(self.agent_error))
        history = self._history_with_user()
        history.append(
            {"role": "assistant", "content": self.final_response_text or redact(self.agent_error)})
        self.persist_snapshot(env, history=history)
        self.terminal_snapshot_persisted = True
        await self.write_event("response.failed", {"type": "response.failed", "response": env})

    async def emit_completed(self) -> None:
        env = self.terminal_envelope("completed", self._final_items())
        result = self.result
        full_history = self.adapter._build_response_conversation_history(
            self.conversation_history, self.user_message, result, self.final_response_text,
            tool_output_max_chars=self.adapter._history_tool_output_max_chars)
        # Transcript substitution for result["_compressed"] happens in the history builder; only
        # a compression-rotated session_id is propagated so chaining resumes the child session.
        sid = result.get("session_id") if isinstance(result, dict) else None
        self.persist_snapshot(
            env, history=full_history, session_id=sid if isinstance(sid, str) and sid else None)
        self.terminal_snapshot_persisted = True
        await self.write_event(
            "response.completed", {"type": "response.completed", "response": env})

    async def emit_crash(self, exc: BaseException) -> None:
        error = self._api._redact_api_error_text(exc, limit=500)
        env = self.terminal_envelope("failed", list(self.emitted_items), error=error)
        await self.write_event("response.failed", {"type": "response.failed", "response": env})


class OpenAICompatRoutesMixin:
    """/v1/chat/completions and /v1/responses handlers + SSE writers."""

    def _select_request_route(
        self, body: Dict[str, Any], *, session_id, gateway_session_key, model_alias) -> tuple:
        """Resolve the model_routes alias + per-request overrides ->
        ``(route, agent_overrides, error_response_or_None)``."""
        from gateway.platforms.api_server import _error_response, _request_agent_overrides
        route = self._resolve_route(model_alias)
        overrides = _request_agent_overrides(
            body, virtual_model=self._model_name, allow_bare_model=self._direct_model_requests)
        err = self._request_route_conflict_error(
            session_id=session_id, gateway_session_key=gateway_session_key,
            requested_model=overrides.get("requested_model"),
            requested_provider=overrides.get("requested_provider"), route=route)
        return route, overrides, (_error_response(err, 400) if err else None)

    def _spawn_stream_agent(self, stream_q, **run_kwargs) -> tuple:
        """Start ``_run_agent`` for an SSE writer -> ``(agent_task, agent_ref)``. ``agent_ref[0]``
        lets the writer interrupt on disconnect; the EOS sentinel is enqueued from the task's done
        callback so drain loops never race a polled ``agent_task.done()``."""
        from gateway.platforms.api_server import ToolDisclosureStreamScrubber
        scrubber = ToolDisclosureStreamScrubber()
        def _on_delta(delta):
            # None from the agent is a CLI box-close signal, not EOS — forwarding it would end
            # the stream early. Called from the run_conversation worker thread: put_threadsafe.
            if delta is not None:
                if isinstance(delta, str):
                    delta = scrubber.feed(delta)
                if delta:
                    stream_q.put_threadsafe(delta)
        def _on_reasoning(text):
            # Structured reasoning deltas (#99552): the agent's reasoning_callback, not the
            # lossy 500-char ``reasoning.available`` progress preview. Tagged so the writers
            # keep them distinct from answer text.
            if text:
                stream_q.put_threadsafe(("__reasoning__", text))
        def _on_status(kind, message=None):
            # Lifecycle/warning status (provider wait, auto-recovery countdown, fallback switch) as a
            # ``hermes.status`` event, so a client sees why the stream is silent instead of a dead socket.
            from gateway.platforms.api_server import _redact_api_error_text
            text = _redact_api_error_text(message if message is not None else kind or "").strip()
            if text:
                stream_q.put_threadsafe(("__status__", {"kind": str(kind), "text": text}))
        agent_ref = [None]
        agent_task = asyncio.ensure_future(self._run_agent(
            stream_delta_callback=_on_delta, reasoning_callback=_on_reasoning, status_callback=_on_status,
            agent_ref=agent_ref, **run_kwargs))
        def _finished(_fut):
            tail = scrubber.flush()
            if tail:
                stream_q.put_nowait(tail)
            stream_q.put_nowait(None)
        agent_task.add_done_callback(_finished)
        return agent_task, agent_ref

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        from gateway.platforms.api_server import (
            _extract_owui_scope, _missing_user_id_error, _scope_session_id,
            _augment_message_with_handoff_context, _bounded_tool_progress_payload,
            _legal_docx_publish_receipt, _legal_docx_download_suffix, _stop_cancelled_sse_agent)
        from gateway.platforms.api_server import (
            ThreadSafeAsyncQueue, _chat_usage_payload, _coerce_request_bool,
            _content_has_visible_payload, _derive_chat_session_id, _error_response, _invalid_request,
            _multimodal_validation_error, _normalize_chat_content, _normalize_multimodal_content,
            _openai_error, _redact_api_error_text, _resolve_media_to_data_urls)
        # Bound total in-flight agent runs (configurable; #7483).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited
        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()
        try:
            body = await request.json()
        except Exception:
            return _error_response("Invalid JSON in request body", 400)
        from gateway.platforms.api_server import _request_relay_metadata
        relay_metadata = _request_relay_metadata(body)
        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return _invalid_request("Missing or invalid 'messages' field")
        stream = _coerce_request_bool(body.get("stream"), default=False)

        # System messages -> ephemeral system prompt layered ON TOP of core, flattened to text
        # (Anthropic rejects images there, OpenAI text models ignore them).
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []
        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                content = _normalize_chat_content(raw_content)
                system_prompt = content if system_prompt is None else system_prompt + "\n" + content
            elif role in {"user", "assistant"}:
                try:
                    content = _normalize_multimodal_content(raw_content)
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"messages[{idx}].content")
                conversation_messages.append({"role": role, "content": content})
        user_message: Any = (conversation_messages[-1].get("content", "") if conversation_messages else "")
        history = conversation_messages[:-1]
        granted_file_paths = []
        granted_file_aliases = {}
        user_message = _augment_message_with_handoff_context(user_message, scope, granted_file_paths, granted_file_aliases)
        if not _content_has_visible_payload(user_message):
            return _invalid_request("No user message found in messages")

        # X-Hermes-Session-Key scopes long-term memory per channel; independent of
        # X-Hermes-Session-Id (the key persists across transcripts, the id rotates on /new).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        if gateway_session_key:
            gateway_session_key = _scope_session_id(gateway_session_key, scope)
        # X-Hermes-Session-Id continues an existing session (history from state.db, not the body);
        # requires a configured API key or any client could read history by guessing ids.
        provided_session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        if provided_session_id:
            if not self._api_key:
                logger.warning(
                    "Session continuation via X-Hermes-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity.")
                return _error_response("Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature.", 403)
            # Same guard as the native gateway: ids are interpolated into on-disk filenames.
            from gateway.session import _is_path_unsafe
            if re.search(r'[\r\n\x00]', provided_session_id) or _is_path_unsafe(provided_session_id):
                return _invalid_request("Invalid session ID")
            if len(provided_session_id) > self._MAX_SESSION_HEADER_LEN:
                return _invalid_request("Session ID too long")
            session_id = _scope_session_id(provided_session_id, scope)
            try:
                db = await self._ensure_session_db_async()
                if db is not None:
                    # #98619/#13437: a client-addressed id from before a compression rotation
                    # must adopt the live continuation tip — history loads from it, the turn and
                    # the wake target bind it, and a detached delegation delivery row persisted
                    # on the tip is what this continuation consumes. Same canonical resolution
                    # the delivery writer (gateway/wake.py) and /v1/runs use; fails open.
                    from gateway.platforms.api_server_runs import _resolve_live_session_id
                    session_id = await _resolve_live_session_id(self, session_id)
                    history = await asyncio.to_thread(db.get_messages_as_conversation, session_id)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # Stable id from the conversation fingerprint so Open WebUI-style clients map onto
            # one Hermes session.
            first_user = next(
                (cm.get("content", "") for cm in conversation_messages if cm.get("role") == "user"), "")
            session_id = _scope_session_id(_derive_chat_session_id(system_prompt, first_user), scope)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._model_name)
        created = int(time.time())
        route, agent_overrides, selection_error = self._select_request_route(
            body, session_id=session_id, gateway_session_key=gateway_session_key,
            model_alias=model_name)
        if selection_error is not None:
            return selection_error
        self._touch_session_activity(session_id, scope)
        run_kwargs = dict(
            user_id=scope["user_id"], chat_id=scope["chat_id"], user_name=scope["user_name"],
            user_role=scope["user_role"], user_groups=scope["user_groups"],
            granted_file_paths=granted_file_paths, granted_file_aliases=granted_file_aliases,
            user_message=user_message, conversation_history=history,
            ephemeral_system_prompt=system_prompt, session_id=session_id,
            gateway_session_key=gateway_session_key, **agent_overrides, route=route,
            relay_metadata=relay_metadata,
            # #98619: only an explicitly provided X-Hermes-Session-Id is wake-capable (the
            # header is 403-gated on API_SERVER_KEY, so the wake self-post can authenticate
            # and the client can resume the session by sending it again). A fingerprint-derived
            # id from a header-less client is NOT: delegate_task keeps its forced-sync fallback
            # there — the wake would hard-fail or land in history that client never reloads.
            session_history_delivery=("1" if provided_session_id else ""))
        # This is presentation only. The ordinary API-key/session authorization
        # above still applies; it grants no internal ingress or control authority.
        if provided_session_id and body.get("hermes_notification_category") == "diagnostic":
            run_kwargs["notification_category"] = "diagnostic"
        if stream:
            _stream_q = ThreadSafeAsyncQueue()
            agent_cancel_event = threading.Event()
            # tool_call_ids with an emitted "running": a "completed" without one (internal/
            # filtered tools) is dropped rather than orphaned on the wire.
            _started_tool_call_ids: set[str] = set()

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Emit ``hermes.tool.progress`` with ``status: running``.

                Replaces the old ``tool_progress_callback("tool.started",
                ...)`` emit so SSE consumers receive a single event per
                tool start, carrying both the legacy ``tool``/``emoji``/
                ``label`` payload (for #6972 frontends) and the new
                ``toolCallId``/``status`` correlation fields (#16588).

                Skips tools whose names start with ``_`` so internal
                events (``_thinking``, …) stay off the wire — matching
                the prior ``_on_tool_progress`` filter exactly.
                """
                if not tool_call_id or function_name.startswith("_"):
                    return
                _started_tool_call_ids.add(tool_call_id)
                from agent.display import build_tool_preview, get_tool_emoji
                try:
                    label = (
                        build_tool_preview(function_name, function_args)
                        or function_name
                    )
                except (AttributeError, TypeError, ValueError):
                    # Tool implementations normally send an argument mapping,
                    # but the transport boundary must remain valid for legacy
                    # or hostile scalar payloads too.
                    label = function_name
                payload = _bounded_tool_progress_payload({
                    "tool": function_name,
                    "emoji": get_tool_emoji(function_name),
                    "label": label,
                    "toolCallId": tool_call_id,
                    "status": "running",
                    "arguments": function_args,
                })
                _stream_q.put_threadsafe(("__tool_progress__", payload))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Emit the matching ``status: completed`` event.

                Dropped if the start was filtered (internal tool, missing
                id, or never seen) so clients never get an orphaned
                ``completed`` they can't correlate to a prior ``running``.
                """
                publish_receipt = _legal_docx_publish_receipt(
                    function_name, function_result
                )
                if publish_receipt is not None:
                    # This private queue item is consumed by the adapter and
                    # never exposed as a custom event.  It lets the response
                    # layer guarantee delivery even when the model omits or
                    # misformats the signed URL in its prose.
                    _stream_q.put_threadsafe(
                        ("__legal_docx_publish_receipt__", publish_receipt)
                    )
                if not tool_call_id or tool_call_id not in _started_tool_call_ids:
                    return
                _started_tool_call_ids.discard(tool_call_id)
                payload = _bounded_tool_progress_payload({
                    "tool": function_name,
                    "toolCallId": tool_call_id,
                    "status": "completed",
                    "arguments": function_args,
                    "result": function_result,
                })
                _stream_q.put_threadsafe(("__tool_progress__", payload))

            # tool_progress_callback deliberately NOT wired: it would duplicate the structured
            # start/complete callbacks (which carry the tool_call id).
            agent_task, agent_ref = self._spawn_stream_agent(
                _stream_q, tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete, agent_cancel_event=agent_cancel_event, **run_kwargs)
            # #13437 identity contract: an explicit-header client keeps addressing the id it
            # sent; the response echoes that stable id while reads/writes adopt the live tip,
            # so a rotation mid-turn (after these headers are prepared) never changes what the
            # client should send next — it re-sends the same id and the tip resolution above
            # finds whatever session is live by then.
            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=(provided_session_id or session_id),
                gateway_session_key=gateway_session_key, agent_cancel_event=agent_cancel_event)

        nonstream_publish_receipts = []
        def _capture_receipt(tool_call_id, function_name, function_args, function_result):
            receipt = _legal_docx_publish_receipt(function_name, function_result)
            if receipt is not None:
                nonstream_publish_receipts.append(receipt)

        async def _compute_completion():
            agent_ref = [None]
            cancel_event = threading.Event()
            task = asyncio.create_task(self._run_agent(**run_kwargs, agent_ref=agent_ref, agent_cancel_event=cancel_event, tool_complete_callback=_capture_receipt))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(_stop_cancelled_sse_agent(task, agent_ref, cancel_event, completion_id, source="non_stream"))
                raise
        outcome, err = await self._run_idempotent(
            request, body, _compute_completion, log_label="chat completions",
            fingerprint_keys=["model", "provider", "model_options", "messages", "tools", "tool_choice", "stream",
                              "hermes_notification_category"],
            route="chat_completions",
        )
        if err is not None:
            return err
        result, usage = outcome
        presentation_muted = result.get("_notification_presentation_suppressed") is True
        final_response = _resolve_media_to_data_urls(result.get("final_response") or "")
        if nonstream_publish_receipts and not presentation_muted:
            final_response += _legal_docx_download_suffix(nonstream_publish_receipts[-1], final_response)
        completed, is_partial, is_failed, err_msg = _result_flags(result)
        if err_msg:
            err_msg = _redact_api_error_text(err_msg)
        finish_reason = _finish_reason(completed, is_partial, is_failed, err_msg)
        # Same #13437 identity contract as the SSE path: an explicit-header client is echoed
        # the stable id it sent; a fingerprint-derived (header-less) turn keeps reporting the
        # id the agent actually resolved, so headerless clients still learn where the turn went.
        response_headers = {"X-Hermes-Session-Id": (provided_session_id or result.get("session_id", session_id))}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        # Hard fail (no usable text AND a real failure) -> 502 OpenAI error envelope so SDK
        # clients raise instead of rendering the failure string as message.content.
        if not final_response and (is_failed or is_partial):
            err_body = _openai_error(
                "" if presentation_muted else (err_msg or "Agent run did not produce a response."), err_type="server_error",
                code="agent_incomplete")
            err_body["error"]["hermes"] = {
                "completed": completed, "partial": is_partial, "failed": is_failed}
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = "true" if is_partial else "false"
            return web.json_response(err_body, status=502, headers=response_headers)
        # Soft partial (some text, run incomplete): 200 + finish_reason="length"/Hermes extras.
        response_data = {
            "id": completion_id, "object": "chat.completion", "created": created,
            "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "" if presentation_muted else final_response},
                         "finish_reason": finish_reason}],
            "usage": _chat_usage_payload(usage)}
        # Non-streaming twin of ``delta.reasoning_content`` (#99552).
        reasoning_text = _turn_reasoning_text(history, user_message, result)
        if reasoning_text and not presentation_muted:
            response_data["choices"][0]["message"]["reasoning_content"] = reasoning_text
        if is_partial or is_failed or not completed:
            response_data["hermes"] = _hermes_extras(
                completed, is_partial, is_failed, "" if presentation_muted else err_msg, finish_reason)
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = "true" if is_partial else "false"
            if err_msg and not presentation_muted:
                response_headers["X-Hermes-Error"] = _redact_api_error_text(err_msg, limit=200)
        return web.json_response(response_data, headers=response_headers)

    async def _run_idempotent(
        self, request: "web.Request", body: Dict[str, Any], compute, *,
        log_label: str, fingerprint_keys: List[str], route: str) -> tuple:
        """Run ``compute()`` once per (principal scope, logical route, Idempotency-Key) + body fingerprint
        -> ``((result, usage), None)`` or ``(None, 500 response)``.

        ``_idem_cache`` is process-global: under ``gateway.multiplex_profiles`` every profile's
        ``/p/<profile>/v1/...`` mirror shares it, so the key carries ``_run_idempotency_scope`` (the same
        ``sha256(profile, expected API key)`` namespace the durable ``/v1/runs`` API uses) — a client key
        colliding across profiles, or a rotated API_SERVER_KEY, never replays another principal's response.
        ``route`` is the logical endpoint (``/v1/...`` and its ``/p/<profile>/v1/...`` alias are the same
        route), folded into the key because the store keeps the fingerprint only as the slot's value.
        """
        from gateway.platforms.api_server import _error_response, _idem_cache, _make_request_fingerprint
        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            if idempotency_key:
                principal_scope = self._run_idempotency_scope(request)
                scoped_key = f"{principal_scope}\0{route}\0{idempotency_key}"
                fp = _make_request_fingerprint(body, keys=fingerprint_keys)
                result, usage = await _idem_cache.get_or_set(scoped_key, fp, compute)
            else:
                result, usage = await compute()
            return (result, usage), None
        except Exception as e:
            logger.error("Error running agent for %s: %s", log_label, e, exc_info=True)
            message = "" if getattr(e, "_notification_presentation_suppressed", False) is True else f"Internal server error: {e}"
            return None, _error_response(message, 500, err_type="server_error")

    async def _prepare_sse_response(
        self, request: "web.Request", session_id: Optional[str], gateway_session_key: Optional[str],
    ) -> "web.StreamResponse":
        """Open a prepared SSE StreamResponse with CORS + session headers (the CORS middleware
        can't inject headers after ``prepare()`` flushes them, so they are resolved here)."""
        sse_headers = {
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        origin = request.headers.get("Origin", "")
        if origin:
            sse_headers.update(self._cors_headers_for_origin(origin) or {})
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)
        return response

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
        gateway_session_key: str = None, agent_cancel_event=None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        from gateway.platforms.api_server import (
            CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS, TOOL_PROGRESS_SSE_LINE_MAX_BYTES,
            _BARE_EXCEPTION_RE, _EMPTY_REPLY_SENTENCES, _LIVE_STREAM_BODIES, _TRACEBACK_SHAPE_RE,
            _empty_reply_sentence, _journey_suffix_safe, _legal_docx_download_suffix,
            _minimal_tool_progress_payload,
            _register_stream_body, _serialize_tool_progress_payload, _sse_frame,
            _stop_cancelled_sse_agent, _terminate_stream_body,
            emit_chat_completion_coverage_suffix, _chat_usage_payload)
        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        try:
            await response.prepare(request)
        except BaseException:
            await asyncio.shield(_stop_cancelled_sse_agent(
                agent_task, agent_ref, agent_cancel_event, completion_id))
            raise
        # Registered AFTER prepare (before it, there is no body to terminate)
        # and removed in `finally` (a normal return terminates its own body, so
        # a stale entry would make shutdown write to a closed transport).
        _register_stream_body(
            response,
            (completion_id, created, model, logger), asyncio.current_task())

        try:
            last_activity = time.monotonic()

            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(_sse_frame(role_chunk))
            last_activity = time.monotonic()

            # OpenWebUI compatibility state.  OpenWebUI's frontend ignores
            # custom SSE event names and only reads ``data:`` chunks, so we
            # need a second channel: tool-call lifecycle is mirrored as
            # inline ``<details type="tool_calls">`` HTML inside
            # ``delta.content`` (open-webui middleware.py:497-503), and
            # reasoning is forwarded as ``delta.reasoning_content`` which
            # OpenWebUI's backend natively converts to a live "Thinking…"
            # block (open-webui middleware.py:4117-4150).
            _owui_state = {
                "reasoning_open": False,
                "legal_docx_publish_receipt": None,
            }
            import html as _html_mod

            def _render_tool_call_html(payload: Dict[str, Any]) -> str:
                """Build a ``<details type="tool_calls">`` block matching the
                exact attribute shape OpenWebUI's marked-extension expects
                (open-webui middleware.py:497 for done=true, :502 for
                done=false).
                """
                name = payload.get("tool", "") or ""
                call_id = payload.get("toolCallId", "") or ""
                status = payload.get("status", "")
                args = payload.get("arguments")
                args_str = (
                    args if isinstance(args, str)
                    else json.dumps(
                        args or {},
                        ensure_ascii=False,
                        default=str,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
                args_attr = _html_mod.escape(
                    json.dumps(args_str, ensure_ascii=False, allow_nan=False)
                )

                if status == "running":
                    return (
                        f'\n<details type="tool_calls" done="false" '
                        f'id="{_html_mod.escape(call_id)}" '
                        f'name="{_html_mod.escape(name)}" '
                        f'arguments="{args_attr}">\n'
                        f'<summary>Executing {_html_mod.escape(name)}…</summary>\n'
                        f'</details>\n'
                    )

                # status == "completed"
                raw_result = payload.get("result", "")
                result_str = (
                    raw_result if isinstance(raw_result, str)
                    else json.dumps(
                        raw_result,
                        ensure_ascii=False,
                        default=str,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
                if payload.get("resultTruncated"):
                    original_chars = payload.get("resultOriginalChars")
                    if isinstance(original_chars, int) and not isinstance(
                        original_chars,
                        bool,
                    ):
                        result_str += (
                            "\n…[truncated, full output is "
                            f"{original_chars} chars]"
                        )
                    else:
                        result_str += "\n…[truncated output]"
                result_body = _html_mod.escape(
                    json.dumps(
                        result_str,
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )
                return (
                    f'\n<details type="tool_calls" done="true" '
                    f'id="{_html_mod.escape(call_id)}" '
                    f'name="{_html_mod.escape(name)}" '
                    f'arguments="{args_attr}">\n'
                    f'<summary>Tool Executed</summary>\n{result_body}\n</details>\n'
                )

            # Did anything the user can read actually reach the wire this turn?
            # A dict, not a local, because the write happens inside a nested
            # scope. Proved 2026-08-11: the turn-completion explainer replaces
            # final_response in the RESULT, and this writer never emitted the
            # result -- so the explainer fired, the text existed, and the user
            # still got an empty box.
            _wire = {
                "content": False,
                "content_tail": "",
                "emitted_coverage_footer": None,
            }

            def _encode_content_delta(text: str) -> bytes:
                """Encode the exact OpenAI ``delta.content`` bytes to be written."""
                content_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                return (
                    f"data: {json.dumps(content_chunk, ensure_ascii=False, default=str, allow_nan=False)}\n\n".encode()
                )

            async def _write_content_delta(text: str) -> None:
                """Send ``text`` using the shared checked content encoder."""
                await response.write(_encode_content_delta(text))
                if isinstance(text, str) and text:
                    # Enough context to detect a URL the model already emitted
                    # without retaining an unbounded duplicate of its answer.
                    _wire["content_tail"] = (
                        _wire["content_tail"] + text
                    )[-131_072:]
                if (text or "").strip():
                    # "Wrote any string" is not "the user received something
                    # readable": a whitespace-only delta suppressed the
                    # fallback and still presented as an empty box.
                    _wire["content"] = True

            def _encode_tool_progress_event(event_data: str) -> bytes:
                return (
                    f"event: hermes.tool.progress\ndata: {event_data}\n\n".encode()
                )

            def _max_physical_line_bytes(encoded_event: bytes) -> int:
                return max(
                    (len(line) for line in encoded_event.splitlines()),
                    default=0,
                )

            # Helper — route a queue item to the correct SSE event.
            async def _emit(item):
                """Write a single queue item to the SSE stream.

                Three queue item shapes:

                * ``("__tool_progress__", payload)`` — emitted in two
                  channels: (a) the legacy custom
                  ``event: hermes.tool.progress`` for native clients
                  (TUI/ACP, see #6972/#16588); (b) an inline
                  ``<details type="tool_calls">`` HTML block in
                  ``delta.content`` so OpenWebUI users see live tool
                  activity (its frontend drops custom SSE event names).
                * ``("__reasoning_delta__", text)`` — sent as a
                  ``delta.reasoning_content`` chunk; OpenWebUI converts
                  these to a streaming ``<details type="reasoning">``
                  block automatically.
                * Plain strings — standard ``delta.content`` chunks.
                """
                if (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and item[0] == "__legal_docx_publish_receipt__"
                ):
                    receipt = item[1]
                    if isinstance(receipt, dict):
                        _owui_state["legal_docx_publish_receipt"] = receipt
                elif isinstance(item, tuple) and len(item) == 2 and item[0] == "__tool_progress__":
                    payload = item[1]
                    # (a) legacy custom event for native clients — fires
                    # on BOTH running and completed so native UIs (TUI,
                    # ACP) can show live tool start.
                    event_data, wire_payload = _serialize_tool_progress_payload(payload)
                    custom_event_bytes = _encode_tool_progress_event(event_data)
                    html_event_bytes = None

                    if wire_payload.get("status") == "completed":
                        html_event_bytes = _encode_content_delta(
                            _render_tool_call_html(wire_payload)
                        )
                        html_line_bytes = _max_physical_line_bytes(html_event_bytes)
                        if html_line_bytes > TOOL_PROGRESS_SSE_LINE_MAX_BYTES:
                            custom_line_bytes = _max_physical_line_bytes(
                                custom_event_bytes
                            )
                            minimal_payload = _minimal_tool_progress_payload(
                                wire_payload
                            )
                            fallback_data, fallback_wire_payload = (
                                _serialize_tool_progress_payload(minimal_payload)
                            )
                            fallback_custom_event_bytes = (
                                _encode_tool_progress_event(fallback_data)
                            )
                            fallback_html_event_bytes = _encode_content_delta(
                                _render_tool_call_html(fallback_wire_payload)
                            )
                            fallback_custom_line_bytes = (
                                _max_physical_line_bytes(
                                    fallback_custom_event_bytes
                                )
                            )
                            fallback_html_line_bytes = _max_physical_line_bytes(
                                fallback_html_event_bytes
                            )
                            if (
                                fallback_custom_line_bytes
                                > TOOL_PROGRESS_SSE_LINE_MAX_BYTES
                                or fallback_html_line_bytes
                                > TOOL_PROGRESS_SSE_LINE_MAX_BYTES
                            ):
                                raise ValueError(
                                    "minimal completed tool-progress lifecycle "
                                    "payload exceeds SSE limit"
                                )
                            logger.warning(
                                "tool_progress_html_fallback status=%s "
                                "custom_line_bytes=%d html_line_bytes=%d "
                                "fallback_custom_line_bytes=%d "
                                "fallback_html_line_bytes=%d limit_bytes=%d",
                                fallback_wire_payload.get("status", ""),
                                custom_line_bytes,
                                html_line_bytes,
                                fallback_custom_line_bytes,
                                fallback_html_line_bytes,
                                TOOL_PROGRESS_SSE_LINE_MAX_BYTES,
                            )
                            wire_payload = fallback_wire_payload
                            custom_event_bytes = fallback_custom_event_bytes
                            html_event_bytes = fallback_html_event_bytes

                    # Completed custom + HTML bytes are chosen together above;
                    # never write the normal custom event before the HTML gate.
                    await response.write(custom_event_bytes)
                    # (b) inline HTML for OpenWebUI — fires ONLY on
                    # ``completed``.  We deliberately skip the running
                    # placeholder: OpenWebUI's marked-extension snapshots
                    # the ``done="false"`` attribute when the message
                    # text stabilises, so a placeholder followed by a
                    # completed block leaves the placeholder spinning
                    # forever ("Executing… 🌀") because content is
                    # append-only and we can never rewrite the prior
                    # ``done="false"`` to ``done="true"``.  Single
                    # ``done="true"`` block per tool gives a clean
                    # checkmark + result, matching the pattern Claude
                    # Desktop and the OpenAI Responses-API path use.
                    if wire_payload.get("status") == "completed":
                        # Any non-empty content delta implicitly closes
                        # a streaming reasoning block on OpenWebUI's
                        # side (middleware.py:4153-4179).
                        _owui_state["reasoning_open"] = False
                        assert html_event_bytes is not None
                        await response.write(html_event_bytes)
                elif isinstance(item, tuple) and len(item) == 2 and item[0] == "__status__":
                    await response.write(_sse_frame(item[1], event="hermes.status"))
                elif isinstance(item, tuple) and len(item) == 2 and item[0] in {"__reasoning_delta__", "__reasoning__"}:
                    text = item[1]
                    chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": text},
                            "finish_reason": None,
                        }],
                    }
                    await response.write(_sse_frame(chunk))
                    _owui_state["reasoning_open"] = True
                else:
                    # Plain content delta — close any open reasoning block
                    # first so OpenWebUI flips the "Thinking…" indicator
                    # to "Thought for N seconds" before the answer text
                    # arrives.
                    if _owui_state["reasoning_open"]:
                        _owui_state["reasoning_open"] = False
                    await _write_content_delta(item)
                return time.monotonic()

            # Stream content chunks as they arrive from the agent. Woken
            # directly by put_threadsafe's call_soon_threadsafe — no
            # executor hop, no poll-interval latency (see
            # ThreadSafeAsyncQueue's docstring).
            while True:
                try:
                    delta = await asyncio.wait_for(stream_q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except asyncio.QueueEmpty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # End of stream sentinel
                    break

                last_activity = await _emit(delta)

            # Get usage from completed agent. The agent can fail two ways
            # after the content queue terminates cleanly: (1) ``agent_task``
            # raises, or (2) it returns a ``result`` dict flagged
            # failed/partial/incomplete. Both previously fell through to a
            # ``finish_reason: "stop"`` chunk, so OpenAI-compatible clients
            # saw a fake success. Surface either as a non-"stop" finish so
            # the failure is detectable — mirroring the non-streaming path's
            # decision logic (see the finish_reason block above).
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            result = None
            agent_error = None
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception as exc:
                agent_error = exc
                logger.error(
                    "Agent task %s failed during SSE streaming: %s", completion_id, exc
                )

            # Inspect the result dict for a flagged (non-exception) failure.
            is_partial = bool(result.get("partial")) if isinstance(result, dict) else False
            is_failed = bool(result.get("failed")) if isinstance(result, dict) else False
            completed = bool(result.get("completed", True)) if isinstance(result, dict) else True
            err_msg = result.get("error") if isinstance(result, dict) else None
            presentation_muted = (isinstance(result, dict) and result.get("_notification_presentation_suppressed") is True) or getattr(agent_error, "_notification_presentation_suppressed", False) is True
            if agent_error is not None:
                is_failed = True
                err_msg = err_msg or str(agent_error)

            # Decide finish_reason, matching the non-streaming logic: "length"
            # for truncation, "error" for failure, "stop" for normal completion.
            if is_partial and err_msg and "truncat" in err_msg.lower():
                finish_reason = "length"
            elif agent_error is not None or is_failed or (not completed and err_msg):
                finish_reason = "error"
            else:
                finish_reason = "stop"

            # The missing bridge. The turn-completion explainer replaces
            # final_response inside finalize_turn, and this writer consumed
            # only what the live callbacks streamed -- so when a turn produced
            # no deltas the explainer fired, its text sat in the result, and
            # the user got an empty message box anyway. Six real users hit this
            # in the last 30 days. The Responses-API writer in this same file
            # already had the bridge; chat completions did not.
            #
            # Only when nothing readable reached the wire: a turn that streamed
            # normally is untouched.
            try:
                if not _wire["content"] and not presentation_muted:
                    _late = ((result or {}).get("final_response") or "").strip()
                    _late_is_final_response = True
                    # final_response is user-facing by convention, not by
                    # invariant -- producers can put raw provider or internal
                    # error text in it, and this path is the one place it would
                    # reach a user unreviewed. Anything that looks like a
                    # traceback or an internal dump is replaced by an honest
                    # sentence rather than shown. Proved reachable by QA review
                    # 2026-08-11.
                    # STRUCTURE, not a list of module names I happened to
                    # think of. The first version matched "openai.",
                    # "aiohttp." and "asyncio." as substrings, which withheld a
                    # legitimate answer to a Python question mentioning
                    # `asyncio.create_task()` while letting
                    # `AsyncOpenAIError: <internal>` straight through -- wrong
                    # in both directions at once. Proved by QA review round 2,
                    # 2026-08-11.
                    #
                    # A traceback has a shape no ordinary answer has: the
                    # header line, or an indented `File "...", line N`. That is
                    # the only thing claimed here, and it is best-effort: this
                    # is a last-resort path, not a redaction boundary.
                    _looks_internal = bool(
                        _TRACEBACK_SHAPE_RE.search(_late)
                        or _BARE_EXCEPTION_RE.match(_late)
                    )
                    if _late and _looks_internal:
                        logger.warning(
                            "empty_reply service=gateway reason=internal_text_withheld"
                            + _journey_suffix_safe())
                        # No claim about what was or was not saved: a tool may
                        # already have had a side effect this turn, which made
                        # the old "Nothing was saved" simply false.
                        _late = ("The agent stopped before it produced an answer. "
                                 "Please check the conversation and try again.")
                        _late_is_final_response = False
                    if _late:
                        await response.write(_encode_content_delta(_late))
                        _wire["content"] = True
                        if _late_is_final_response and isinstance(result, dict):
                            _footer = result.get("coverage_footer")
                            if isinstance(_footer, str) and _footer.strip():
                                _wire["emitted_coverage_footer"] = _footer
                        logger.info(
                            "empty_stream_recovered service=gateway chars=%d"
                            + _journey_suffix_safe(), len(_late))
                    else:
                        # The loop ALREADY KNOWS why, and this path was throwing
                        # it away. Six early returns in conversation_loop.py --
                        # three invalid tool calls, repeated truncated tool
                        # calls, unrecoverable first-response truncation, a
                        # nonretryable provider error, an incomplete scratchpad,
                        # Codex incomplete after continuations -- return
                        # {final_response: None, partial: True, error: "..."}
                        # and bypass TurnFinalizer entirely, so the empty-turn
                        # explainer can never fire for them. The NON-streaming
                        # handler turns the same dict into a 502; the streaming
                        # writer ignored `error`/`partial`/`failed` and emitted
                        # a normal finish + [DONE] with no content. That is
                        # "finished without any content" as the user meets it.
                        #
                        # The reason is a bounded internal string produced by our
                        # own loop, not model output, so surfacing it tells the
                        # user what happened instead of showing a blank box.
                        _err = result.get("error") if isinstance(result, dict) else None
                        _known = any(
                            isinstance(_err, str) and _err.strip().startswith(_p)
                            for _p, _ in _EMPTY_REPLY_SENTENCES)
                        # ALWAYS say something. Gating emission on a recognised
                        # reason left the generic sentence dead in this path,
                        # so an unrecognised loop outcome still handed the user
                        # a blank box -- the exact defect this branch exists to
                        # close, surviving inside its own fix. `_known` now only
                        # decides HOW specific the sentence is, never whether
                        # there is one.
                        if True:
                            _msg = ("\u26a0\ufe0f No reply: "
                                    + _empty_reply_sentence(_err)
                                    + " Send `continue` to retry.")
                            # The log must not claim more than the wire got. The
                            # first version logged "explained" from inside a
                            # `try`, so a failed write still reported that the
                            # user had been told why -- a metric lying about the
                            # exact thing it was built to measure.
                            _delivered = False
                            try:
                                await response.write(_encode_content_delta(_msg))
                                _wire["content"] = True
                                _delivered = True
                            except (asyncio.CancelledError, GeneratorExit):
                                # Cancellation must NOT be swallowed here. This
                                # `except BaseException: pass` was mine, written
                                # today, and it is the exact anti-pattern the
                                # four earlier terminator commits exist to undo:
                                # eating the cancellation loses the explanation,
                                # leaves a pending cancellation on the task, and
                                # then lets the code below send [DONE] as though
                                # the request ended normally. Re-raise and let
                                # the cancellation path terminate the body
                                # honestly.
                                raise
                            except Exception:
                                # An ordinary write failure (dead client) is
                                # genuinely best-effort: nothing more can reach
                                # them, and `_delivered` stays False so the log
                                # does not claim otherwise.
                                pass
                            from tools.journey_context import journey_suffix as _js1
                            # Three states, because they mean different things:
                            # a specific sentence delivered, a generic one
                            # delivered, and nothing delivered at all.
                            logger.warning(
                                "empty_reply service=gateway reason="
                                + ("no_content_and_no_final"
                                   if not _delivered else
                                   "no_content_and_no_final_explained"
                                   if _known else
                                   "no_content_and_no_final_generic")
                                + _js1())
                        else:
                            from tools.journey_context import journey_suffix as _js1
                            logger.warning(
                                "empty_reply service=gateway reason=no_content_and_no_final"
                                + _js1())
            except Exception as _late_err:
                logger.warning("late final_response emit failed for %s: %s",
                               completion_id, _late_err)

            # A legal DOCX publication is not complete from the user's point
            # of view until the signed download is visible.  The receipt came
            # directly from the successful MCP tool call and passed the
            # same-origin route validator above; do not rely on model prose to
            # preserve it.  If the model already emitted the exact URL, this is
            # a no-op so the answer contains only one download affordance.
            try:
                download_suffix = _legal_docx_download_suffix(
                    _owui_state.get("legal_docx_publish_receipt"),
                    _wire.get("content_tail", ""),
                )
                if download_suffix and not presentation_muted:
                    await _write_content_delta(download_suffix)
            except Exception as _artifact_emit_err:
                logger.warning(
                    "legal DOCX download link emit failed for %s: %s",
                    completion_id,
                    _artifact_emit_err,
                )

            # M-U1-D all-exit A-channel: coverage suffix BEFORE stop/[DONE].
            # Production adapter (mutation target): emit_chat_completion_coverage_suffix
            try:
                suffix = emit_chat_completion_coverage_suffix(
                    result if isinstance(result, dict) else None,
                    # Set only by the successful late-final-response write above.
                    # Model prose containing the same heading is not provenance.
                    emitted_coverage_footer=_wire.get(
                        "emitted_coverage_footer"),
                )
                if suffix and not presentation_muted:
                    cov_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": suffix},
                                "finish_reason": None,
                            }
                        ],
                    }
                    await response.write(
                        f"data: {json.dumps(cov_chunk)}\n\n".encode()
                    )
            except Exception as _cov_emit_err:
                logger.warning(
                    "coverage suffix emit failed for %s: %s",
                    completion_id,
                    _cov_emit_err,
                )

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                "usage": _chat_usage_payload(usage),
            }
            if finish_reason != "stop":
                finish_chunk["choices"][0]["delta"] = {}
                if err_msg and not presentation_muted:
                    finish_chunk["error"] = {
                        "message": err_msg,
                        "type": type(agent_error).__name__ if agent_error else "agent_error",
                    }
                finish_chunk["hermes"] = {
                    "completed": completed,
                    "partial": is_partial,
                    "failed": is_failed,
                    "error": "" if presentation_muted else err_msg,
                    "error_code": "output_truncated" if finish_reason == "length" else "agent_error",
                }
            await response.write(_sse_frame(finish_chunk))
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            await _stop_cancelled_sse_agent(
                agent_task, agent_ref, agent_cancel_event, completion_id)
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)
        except asyncio.CancelledError:
            # THE truncation cause, proved by reproduction 2026-08-11.
            # CancelledError is a BaseException, not an Exception, so it walked
            # straight past every handler below after prepare() had already
            # committed the chunked framing. aiohttp then force-closes the
            # transport without finish_response()/write_eof(), and the client
            # sees exactly "Not enough data to satisfy transfer length header"
            # -- an answer that stops dead with no explanation. A control
            # RuntimeError did not truncate; only cancellation did.
            #
            # Reachable in production: docker reports StopTimeout=1 for this
            # container while aiohttp's graceful shutdown allows 60s, so any
            # stream still running one second after a stop is cancelled.
            #
            # Terminate the body honestly, then RE-RAISE: swallowing
            # cancellation would break asyncio's contract and leave the task
            # looking alive.
            from tools.journey_context import journey_suffix as _js0
            # WHICH cancellation this was decides which log line is true. A
            # deploy cancels every open stream, and calling that `stream_aborted`
            # files a real, explained restart under the same name as an
            # unexplained mid-answer failure -- the daily report then counts an
            # abort and shows no deploy interruption at all, so the operator
            # reading it sees a fault where there was a release. The lifecycle
            # knows: shutdown set the flag before it cancelled us.
            _lc0 = _LIVE_STREAM_BODIES.get(response)
            if _lc0 is not None and _lc0.shutdown:
                logger.info(
                    "stream_closed_at_shutdown service=gateway phase=cancelled"
                    + _js0())
            else:
                logger.error(
                    "stream_aborted service=gateway phase=cancelled "
                    "error=CancelledError" + _js0())
            # One guard per step. Wrapping all three in a single try meant that
            # catching a second cancellation exited the whole block and skipped
            # `[DONE]` and -- far worse -- `write_eof()`, handing the client the
            # exact TransferEncodingError this exists to prevent. Proved by QA
            # review round 2, 2026-08-11.
            await _terminate_stream_body(
                response, completion_id, created, model, logger)
            await _stop_cancelled_sse_agent(
                agent_task, agent_ref, agent_cancel_event, completion_id)
            raise
        except Exception as _exc:
            # Agent crashed mid-stream.  Try to emit an error chunk
            # so the client gets a proper response instead of a
            # TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            # stream_aborted, with the journey. This is the failure class that
            # actually reaches users as a blank screen, and it was invisible to
            # the journey ledger: it carried only a completion_id, and a
            # traceback spans lines, so the record broke in two. Found
            # 2026-08-11 while root-causing 23 real blank replies.
            from tools.journey_context import journey_suffix as _js
            _lc_ab = _LIVE_STREAM_BODIES.get(response)
            if _lc_ab is not None and _lc_ab.shutdown:
                # NOT an abort. The shutdown callback already closed this body
                # cleanly -- the client has its finish chunk, [DONE] and EOF --
                # and the handler is only now discovering the response is gone.
                # Logging `stream_aborted` here put a FALSE failure into the
                # journey ledger for every stream open at deploy time, which
                # would have made the very metric built to count blank screens
                # report one for a user who got a clean ending. Proved on a
                # disposable container: CLIENT_EOF clean=1 done=1, followed by
                # stream_aborted phase=mid_stream.
                # No mark to consume: the lifecycle carries the reason and
                # dies with the response, so a later genuine abort on a
                # different response is unaffected by construction.
                logger.info(
                    "stream_closed_at_shutdown service=gateway" + _js())
            else:
                logger.error(
                    "stream_aborted service=gateway phase=mid_stream error=%s" + _js(),
                    type(_exc).__name__)
                logger.error("Agent crashed mid-stream for %s: %s", completion_id,
                             " | ".join(_tb.format_exc()[:300].splitlines()))
            try:
                error_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
                await response.write(_sse_frame(error_chunk))
                await response.write(b"data: [DONE]\n\n")
            except Exception:
                pass

        except BaseException as _be:
            # The rest of the family. CancelledError was the one that hurt
            # users, but GeneratorExit, SystemExit and KeyboardInterrupt reach
            # this point the same way -- past `except Exception`, after the
            # chunked framing is already committed -- and leave the body
            # unterminated for exactly the same reason. Fixing the instance
            # and not the class is a mistake I have made three times today.
            #
            # Ordered AFTER `except Exception` so the richer error path above
            # still handles ordinary failures. Always re-raises.
            try:
                from tools.journey_context import journey_suffix as _js3
                logger.error(
                    "stream_aborted service=gateway phase=base_exception error=%s"
                    + _js3(), type(_be).__name__)
            except BaseException:
                pass
            # Same per-step terminator as the cancellation branch: a single
            # guard around all three writes let a second cancellation skip the
            # write_eof the client is actually waiting for.
            await _terminate_stream_body(
                response, completion_id, created, model, logger)
            raise

        finally:
            # Every exit path, including the two that terminate the body
            # themselves. A stale entry would make the shutdown terminator
            # write to an already-closed transport.
            _LIVE_STREAM_BODIES.pop(response, None)

        return response

    async def _write_sse_responses(
        self, request: "web.Request", response_id: str, model: str, created_at: int, stream_q,
        agent_task, agent_ref, conversation_history: List[Dict[str, str]], user_message: str,
        instructions: Optional[str], conversation: Optional[str], store: bool, session_id: str,
        gateway_session_key: Optional[str] = None, user_id: str = "", agent_cancel_event=None) -> "web.StreamResponse":
        """Write the SSE stream for POST /v1/responses.

        Events: ``response.created`` -> ``output_text.delta/done`` + ``output_item.added/done``
        (reasoning / function_call / function_call_output) + ``reasoning_summary_part/text.*``
        -> ``response.completed`` (non-streaming envelope)
        or ``response.failed``. On disconnect the agent is interrupted and, with ``store=True``,
        an ``incomplete`` snapshot replaces ``in_progress`` so GET / chaining still work.
        """
        from gateway.platforms.api_server import _stop_cancelled_sse_agent, _redact_api_error_text
        try:
            response = await self._prepare_sse_response(request, session_id, gateway_session_key)
            st = _ResponsesStream(
                self, response, response_id=response_id, model=model, created_at=created_at,
                conversation_history=conversation_history, user_message=user_message,
                instructions=instructions, conversation=conversation, store=store, session_id=session_id, user_id=user_id)
        except BaseException:
            await asyncio.shield(_stop_cancelled_sse_agent(
                agent_task, agent_ref, agent_cancel_event, response_id))
            raise
        try:
            await st.emit_created()
            async for item in _iter_stream_items(stream_q, agent_task, response):
                if item is None:  # EOS sentinel
                    st.cancel_batch_timer()
                    await st.flush_batch()
                    break
                await st.dispatch(item)
            await st.flush_batch()
            await st.collect_result(agent_task)
            await st.close_message_item()
            if st.agent_error:
                await st.emit_failed()
            else:
                await st.emit_completed()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            st.persist_incomplete_if_needed()
            await _stop_cancelled_sse_agent(
                agent_task, agent_ref, agent_cancel_event, response_id)
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)
        except asyncio.CancelledError:
            # Server-side cancellation (shutdown, timeout): persist incomplete, then re-raise.
            st.persist_incomplete_if_needed()
            await _stop_cancelled_sse_agent(agent_task, agent_ref, agent_cancel_event, response_id)
            logger.info("SSE task cancelled; persisted incomplete snapshot for %s", response_id)
            raise
        except Exception as exc:
            # Unhandled agent error (BadRequestError, AuthenticationError, ...): emit
            # response.failed and end the stream cleanly (no TransferEncodingError).
            import traceback as _tb
            st.persist_incomplete_if_needed()
            st.agent_error = _redact_api_error_text(_tb.format_exc())
            with suppress(Exception):
                await st.emit_crash(exc)
            logger.error("Agent crashed mid-stream for %s: %s", response_id, str(st.agent_error)[:300])
        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        from gateway.platforms.api_server import (
            _extract_owui_scope, _missing_user_id_error, _scope_session_id,
            _augment_message_with_handoff_context, _bounded_tool_progress_payload,
            _legal_docx_publish_receipt, _legal_docx_download_suffix, _stop_cancelled_sse_agent)
        from gateway.platforms.api_server import (
            ThreadSafeAsyncQueue, _auto_truncate_response_history, _coerce_request_bool,
            _content_has_visible_payload, _error_response, _invalid_request,
            _multimodal_validation_error, _normalize_multimodal_content, _redact_api_error_text,
            _resolve_media_to_data_urls, _responses_usage_payload)
        # Bound total in-flight agent runs (configurable; #7483).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited
        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        if gateway_session_key:
            gateway_session_key = _scope_session_id(gateway_session_key, scope)
        try:
            body = await request.json()
        except Exception:
            return _invalid_request("Invalid JSON in request body")
        from gateway.platforms.api_server import _request_relay_metadata
        relay_metadata = _request_relay_metadata(body)
        raw_input = body.get("input")
        if raw_input is None:
            return _error_response("Missing 'input' field", 400)
        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = _coerce_request_bool(body.get("store"), default=True)
        if conversation and previous_response_id:
            return _error_response("Cannot use both 'conversation' and 'previous_response_id'", 400)
        if conversation:
            # A conversation name resolves to its latest response_id (unknown = new conversation).
            previous_response_id = self._current_response_store().get_conversation(conversation, user_id=scope["user_id"])

        input_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif _is_reasoning_input_item(item):
                    continue
                elif isinstance(item, dict):
                    try:
                        content = _normalize_multimodal_content(item.get("content", ""))
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                    input_messages.append({"role": item.get("role", "user"), "content": content})
        else:
            return _error_response("'input' must be a string or array", 400)

        # Explicit conversation_history (stateless clients) beats previous_response_id chaining.
        conversation_history: List[Dict[str, Any]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return _error_response("'conversation_history' must be an array of message objects", 400)
            for i, entry in enumerate(raw_history):
                if _is_reasoning_input_item(entry):
                    continue
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return _error_response(f"conversation_history[{i}] must have 'role' and 'content' fields", 400)
                try:
                    entry_content = _normalize_multimodal_content(entry["content"])
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                conversation_history.append({"role": str(entry["role"]), "content": entry_content})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")
        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._current_response_store().get(previous_response_id)
            if stored is None or (stored.get("user_id") and stored["user_id"] != scope["user_id"]):
                return _error_response(f"Previous response not found: {previous_response_id}", 404)
            conversation_history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            if instructions is None:
                instructions = stored.get("instructions")
        # All input messages but the last become history; the last is the user message.
        conversation_history.extend(input_messages[:-1])
        user_message: Any = input_messages[-1].get("content", "") if input_messages else ""
        granted_file_paths = []
        granted_file_aliases = {}
        user_message = _augment_message_with_handoff_context(user_message, scope, granted_file_paths, granted_file_aliases)
        if not _content_has_visible_payload(user_message):
            return _error_response("No user message found in input", 400)
        if body.get("truncation") == "auto":
            conversation_history = _auto_truncate_response_history(conversation_history)

        # Session precedence: previous_response_id chain > declared X-Hermes-Session-Key > fresh
        # id. Binding the declared key follows the same precedence: a chain-selected session must
        # not have its routing key rewritten to this header.
        _declared_selected = not stored_session_id and bool(gateway_session_key)
        session_id = (
            stored_session_id
            or await asyncio.to_thread(self._declared_conversation_session, gateway_session_key)
            or str(uuid.uuid4()))
        session_id = _scope_session_id(session_id, scope)
        stream = _coerce_request_bool(body.get("stream"), default=False)
        route, agent_overrides, selection_error = self._select_request_route(
            body, session_id=session_id, gateway_session_key=gateway_session_key,
            model_alias=body.get("model"))
        if selection_error is not None:
            return selection_error
        self._touch_session_activity(session_id, scope)
        run_kwargs = dict(
            user_id=scope["user_id"], chat_id=scope["chat_id"], user_name=scope["user_name"],
            user_role=scope["user_role"], user_groups=scope["user_groups"],
            granted_file_paths=granted_file_paths, granted_file_aliases=granted_file_aliases,
            user_message=user_message, conversation_history=conversation_history,
            ephemeral_system_prompt=instructions, session_id=session_id,
            gateway_session_key=gateway_session_key, bind_declared_conversation=_declared_selected,
            **agent_overrides, route=route, relay_metadata=relay_metadata)
        if stream:
            _stream_q = ThreadSafeAsyncQueue()
            agent_cancel_event = threading.Event()

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                return  # structured start/complete callbacks carry the call id; progress ignored

            def _on_tool_start(tool_call_id, function_name, function_args):
                _stream_q.put_threadsafe(("__tool_started__", {
                    "tool_call_id": tool_call_id, "name": function_name,
                    "arguments": function_args or {}}))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                _stream_q.put_threadsafe(("__tool_completed__", {
                    "tool_call_id": tool_call_id, "name": function_name,
                    "arguments": function_args or {}, "result": function_result}))

            def _on_commentary(text, *, already_streamed: bool = False):
                # Already-streamed text went out as output_text.delta of the final item; a second
                # copy as a commentary item would duplicate it.
                if not already_streamed and isinstance(text, str) and text.strip():
                    _stream_q.put_threadsafe(("__commentary__", {"text": text}))
            agent_task, agent_ref = self._spawn_stream_agent(
                _stream_q, tool_progress_callback=_on_tool_progress,
                tool_start_callback=_on_tool_start, tool_complete_callback=_on_tool_complete,
                interim_assistant_callback=_on_commentary, agent_cancel_event=agent_cancel_event, **run_kwargs)
            return await self._write_sse_responses(
                request=request, response_id=f"resp_{uuid.uuid4().hex[:28]}",
                model=body.get("model", self._model_name), created_at=int(time.time()),
                stream_q=_stream_q, agent_task=agent_task, agent_ref=agent_ref,
                conversation_history=conversation_history, user_message=user_message,
                instructions=instructions, conversation=conversation, store=store,
                session_id=session_id, gateway_session_key=gateway_session_key, user_id=scope["user_id"], agent_cancel_event=agent_cancel_event)

        async def _compute_response():
            return await self._run_agent(**run_kwargs)
        outcome, err = await self._run_idempotent(
            request, body, _compute_response, log_label="responses",
            fingerprint_keys=["input", "instructions", "previous_response_id", "conversation", "model", "provider", "model_options", "tools"],
            route="responses",
        )
        if err is not None:
            return err
        result, usage = outcome
        final_response = _resolve_media_to_data_urls(result.get("final_response", ""))
        if not final_response:
            final_response = _redact_api_error_text(result.get("error", "(No response generated)"))
        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())
        full_history = self._build_response_conversation_history(
            conversation_history, user_message, result, final_response,
            tool_output_max_chars=self._history_tool_output_max_chars)
        # _run_agent's effective session id carries compression rotations; storing it keeps
        # previous_response_id chaining off the pre-rotation session (else compression re-fires).
        _result_sid = result.get("session_id") if isinstance(result, dict) else None
        _effective_session_id = (
            _result_sid if isinstance(_result_sid, str) and _result_sid else session_id)
        # Output items = current turn only (AIAgent returns a full transcript; mocked paths
        # only the current-turn suffix).
        output_start_index = self._response_messages_turn_start_index(
            conversation_history, user_message, result)
        response_data = {
            "id": response_id, "object": "response", "status": "completed",
            "created_at": created_at, "model": body.get("model", self._model_name),
            "output": self._extract_output_items(result, start_index=output_start_index),
            "usage": _responses_usage_payload(usage)}
        if store:
            response_store = self._current_response_store()
            response_store.put(response_id, {
                "response": response_data, "conversation_history": full_history,
                "instructions": instructions, "session_id": _effective_session_id, "user_id": scope["user_id"]})
            if conversation:
                response_store.set_conversation(conversation, response_id, user_id=scope["user_id"])
        response_headers = {"X-Hermes-Session-Id": _effective_session_id}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(response_data, headers=response_headers)

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        from gateway.platforms.api_server import _extract_owui_scope, _missing_user_id_error, _openai_error
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        response_id = request.match_info["response_id"]
        stored = self._current_response_store().get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        # Owner check — return 404 (not 403) on mismatch so we don't leak the
        # existence of another user's response_id.
        stored_user = stored.get("user_id", "")
        if stored_user and stored_user != scope["user_id"]:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        from gateway.platforms.api_server import _extract_owui_scope, _missing_user_id_error, _openai_error
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        scope = _extract_owui_scope(request)
        if not scope["user_id"]:
            return _missing_user_id_error()

        response_id = request.match_info["response_id"]
        # Read-then-check-then-delete to enforce ownership.  Race window is
        # acceptable: no security impact, the worst case is a concurrent
        # delete by the same user winning twice.
        stored = self._current_response_store().get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)
        stored_user = stored.get("user_id", "")
        if stored_user and stored_user != scope["user_id"]:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        deleted = self._current_response_store().delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    @staticmethod
    def _build_response_conversation_history(
        conversation_history: List[Dict[str, Any]], user_message: Any, result: Dict[str, Any],
        final_response: Any, *, tool_output_max_chars: int = 0) -> List[Dict[str, Any]]:
        """Build the stored Responses transcript without duplicating history.

        A compressed transcript (``result["_compressed"]``) shares no input-history prefix, so
        turn-start detection fails; prepending the uncompressed history would bloat the stored
        context and re-trigger compression every request — it is stored as-is instead.

        ``tool_output_max_chars`` > 0 caps tool outputs / tool-call argument blobs in the
        stored copy (gateway.api_server.history_tool_output_max_chars; 0 = store verbatim).
        """
        from gateway.platforms.api_server import APIServerAdapter
        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if isinstance(agent_messages, list) and agent_messages:
            turn_start = APIServerAdapter._response_messages_turn_start_index(
                conversation_history, user_message, result)
            # turn_start == 0: compression rewrote the transcript or agent_messages is turn-only.
            if turn_start or result.get("_compressed"):
                history = list(agent_messages)
            else:
                history = prior + [current_user] + agent_messages
        else:
            history = prior + [current_user, {"role": "assistant", "content": final_response}]
        return _cap_history_tool_outputs(history, tool_output_max_chars)

    @staticmethod
    def _response_messages_turn_start_index(
        conversation_history: List[Dict[str, Any]], user_message: Any, result: Dict[str, Any],
    ) -> int:
        """Index where this turn starts in a transcript-shaped result["messages"] (0 = all)."""
        from gateway.platforms.api_server_turn_boundary import response_turn_start_index
        return response_turn_start_index(conversation_history, user_message, result)

    @classmethod
    def _turn_transcript_messages(
        cls, conversation_history: List[Dict[str, Any]], user_message: Any, result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """This turn's assistant/tool messages in client-safe shape: clients accumulating
        ``assistant.delta`` into one buffer cannot reconstruct assistant segments that preceded
        tool calls, so ``run.completed`` carries the authoritative per-turn transcript.

        Emitting the authoritative per-turn transcript on ``run.completed`` lets any SSE consumer reconcile
        its live view against ground truth without a separate ``GET /messages`` round-trip. Purely additive:
        clients that ignore the field are unaffected. Refs #34703.
        """
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return []
        start = cls._response_messages_turn_start_index(conversation_history, user_message, result)
        out: List[Dict[str, Any]] = []
        for msg in agent_messages[start:]:
            if not isinstance(msg, dict) or msg.get("role") not in {"assistant", "tool"}:
                continue
            # _message_response projects compaction scaffolding; pure handoffs are "hidden".
            projected = cls._message_response(msg)
            if projected.get("display_kind") != "hidden":
                out.append(projected)
        return out

    @staticmethod
    def _extract_output_items(result: Dict[str, Any], start_index: int = 0) -> List[Dict[str, Any]]:
        """Output items from ``result["messages"][start_index:]``: ``function_call`` per assistant
        tool_call, ``function_call_output`` per tool message, then the final ``message``."""
        from gateway.platforms.api_server import _redact_api_error_text
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])
        if start_index > 0:
            messages = messages[start_index:]
        for msg in messages:
            role = msg.get("role")
            reasoning = msg.get("reasoning") if role == "assistant" else None
            if isinstance(reasoning, str) and reasoning.strip():
                # Precedes this message's function_call items, like the SSE writer closes a
                # thinking burst before the next tool item opens (#99552).
                items.append(_reasoning_item(reasoning))
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    # Already executed server-side; replayed for structured tool UI only, so
                    # marked completed (matching the SSE path) — never pending client calls.
                    items.append({
                        "id": f"fc_{uuid.uuid4().hex[:24]}", "type": "function_call",
                        "status": "completed", "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", "")})
            elif role == "tool":
                items.append({
                    "id": f"fco_{uuid.uuid4().hex[:24]}", "type": "function_call_output",
                    "status": "completed", "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", "")})
        final = result.get("final_response", "") or _redact_api_error_text(
            result.get("error", "(No response generated)"))
        items.append(_message_item(final))
        return items
