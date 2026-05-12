"""OpenWebUI ⇄ Hermes memory bridge backed by OpenViking.

This module is intentionally small and stdlib-first so the aiohttp API server
can expose scoped memory/dreaming endpoints without adding any mandatory
service, queue, database, worker, or sidecar.  OpenViking remains the durable
context store; in-process state is limited to non-sensitive run status.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx


MEMORY_TYPES = frozenset({"preferences", "recent_tasks", "needs", "imported"})
DERIVED_MEMORY_TYPES = frozenset({"preferences", "recent_tasks", "needs"})
logger = logging.getLogger(__name__)
_OPENVIKING_SCOPE_USER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "openviking_scope_user",
    default=None,
)


class OpenVikingNotFound(Exception):
    """Raised when a Viking URI does not exist."""


class OpenVikingConflict(Exception):
    """Raised when create-only writes encounter an existing Viking URI."""


class OpenVikingListingTruncated(Exception):
    """Raised when OpenViking returns a limit-sized listing without pagination."""


class MemoryRetracted(Exception):
    """Raised when a deterministic memory is blocked by a durable retraction."""


class NightlyLeaseLost(Exception):
    """Raised when a nightly runner no longer owns its durable lock."""


@contextmanager
def _scoped_openviking_user(user_id: str):
    token = _OPENVIKING_SCOPE_USER.set(user_id)
    try:
        yield
    finally:
        _OPENVIKING_SCOPE_USER.reset(token)


class OpenVikingClientProtocol(Protocol):
    async def read_json(self, uri: str) -> Dict[str, Any]: ...

    async def write_json(
        self,
        uri: str,
        payload: Dict[str, Any],
        *,
        create: bool = False,
        replace_only: bool = False,
    ) -> Dict[str, Any]: ...

    async def delete_json(self, uri: str) -> Dict[str, Any]: ...

    async def refresh_uri(self, uri: str, *, regenerate: bool = True, wait: bool = True) -> Dict[str, Any]: ...

    async def list_json(self, prefix_uri: str) -> List[Dict[str, Any]]: ...

    async def search_json(self, query: str, target_uri: str, *, limit: int = 10) -> List[Dict[str, Any]]: ...

    async def list_user_ids(self) -> List[str]: ...


@dataclass(frozen=True)
class DreamingWindowConfig:
    timezone: str = "Asia/Taipei"
    start_hour: int = 2
    end_hour: int = 8

    def is_within_window(self, now: Optional[datetime] = None) -> bool:
        """Return whether *now* is inside the daily local dreaming window.

        The configured window is start-inclusive/end-exclusive.  Windows that
        wrap midnight are supported even though v1's default (02:00-08:00 Asia/Taipei)
        does not wrap.
        """
        tz = ZoneInfo(self.timezone)
        local_now = now.astimezone(tz) if now is not None else datetime.now(tz)
        hour = local_now.hour
        if self.start_hour == self.end_hour:
            return True
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    @property
    def local_date_key(self) -> str:
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()


@dataclass(frozen=True)
class SkillEvolutionGuard:
    """Path/provenance guard for optional runtime-generated skills/tools."""

    hermes_home: Path
    enabled: bool = False
    allowed_subdirs: tuple[str, ...] = ("skills", "scripts", "workspace")

    def allowed_roots(self) -> tuple[Path, ...]:
        home = self.hermes_home.resolve()
        return tuple((home / part).resolve() for part in self.allowed_subdirs)

    def ensure_allowed_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.hermes_home / candidate
        resolved = candidate.resolve()
        roots = self.allowed_roots()
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise ValueError(
                f"Generated skill/tool path must stay under HERMES_HOME {self.allowed_subdirs}: {resolved}"
            )
        return resolved

    @staticmethod
    def with_agent_provenance(payload: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(payload)
        provenance = dict(out.get("provenance") or {})
        provenance.setdefault("created_by", "hermes-agent")
        provenance.setdefault("agent_created", True)
        out["provenance"] = provenance
        return out


class OpenVikingURIBuilder:
    """Central builder for all OpenWebUI/Hermes-owned Viking URIs."""

    @staticmethod
    def _segment(value: Any, *, field: str, max_len: int = 128) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field} is required")
        if raw in {".", ".."} or ".." in raw or "/" in raw or "\\" in raw:
            raise ValueError(f"{field} contains unsafe path characters")
        if any(ord(ch) < 32 for ch in raw):
            raise ValueError(f"{field} contains control characters")
        encoded = quote(raw[:max_len], safe="-._~")
        if not encoded:
            raise ValueError(f"{field} is empty after normalization")
        return encoded

    @classmethod
    def feedback_signal(cls, user_id: str, event_id: str) -> str:
        return (
            f"viking://user/{cls._segment(user_id, field='user_id')}/signals/feedback/"
            f"{cls._segment(event_id, field='event_id', max_len=160)}.json"
        )

    @classmethod
    def feedback_prefix(cls, user_id: str) -> str:
        return f"viking://user/{cls._segment(user_id, field='user_id')}/signals/feedback"

    @classmethod
    def memory(cls, user_id: str, memory_type: str, memory_id: str) -> str:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Unsupported memory type: {memory_type}")
        return (
            f"viking://user/{cls._segment(user_id, field='user_id')}/memories/{memory_type}/"
            f"{cls._segment(memory_id, field='memory_id', max_len=160)}.json"
        )

    @classmethod
    def memory_prefix(cls, user_id: str, memory_type: str) -> str:
        if memory_type not in MEMORY_TYPES and memory_type != "tombstones":
            raise ValueError(f"Unsupported memory type: {memory_type}")
        return f"viking://user/{cls._segment(user_id, field='user_id')}/memories/{memory_type}"

    @classmethod
    def memory_root(cls, user_id: str) -> str:
        return f"viking://user/{cls._segment(user_id, field='user_id')}/memories"

    @classmethod
    def tombstone(cls, user_id: str, memory_id: str) -> str:
        return (
            f"viking://user/{cls._segment(user_id, field='user_id')}/memories/tombstones/"
            f"{cls._segment(memory_id, field='memory_id', max_len=160)}.json"
        )

    @classmethod
    def org_insight(cls, identifier: str) -> str:
        return f"viking://resources/hermes/org-insights/{cls._segment(identifier, field='identifier')}.json"

    @classmethod
    def dreaming_run(cls, identifier: str) -> str:
        return f"viking://resources/hermes/dreaming-runs/{cls._segment(identifier, field='identifier')}.json"

    @classmethod
    def dreaming_run_lock(cls, identifier: str) -> str:
        return f"viking://resources/hermes/dreaming-runs/{cls._segment(identifier, field='identifier')}.lock.json"


@dataclass
class OpenVikingHTTPClient:
    base_url: str
    api_key: str = ""
    account: str = "org"
    user: str = "default"
    agent: str = "hermes"
    timeout: float = 15.0
    list_limit: Optional[int] = None
    _client: Optional[httpx.AsyncClient] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        env_limit = int(os.getenv("OPENVIKING_LIST_JSON_LIMIT", "10000"))
        self.list_limit = max(1, int(self.list_limit if self.list_limit is not None else env_limit))

    def _headers(self) -> Dict[str, str]:
        scoped_user = _OPENVIKING_SCOPE_USER.get() or self.user
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-OpenViking-Account": self.account,
            "X-OpenViking-User": scoped_user,
            "X-OpenViking-Agent": self.agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _unwrap_response(response: httpx.Response) -> Any:
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}
        if response.status_code == 404:
            raise OpenVikingNotFound(str(data))
        if response.status_code == 409:
            raise OpenVikingConflict(str(data))
        response.raise_for_status()
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    async def read_json(self, uri: str) -> Dict[str, Any]:
        response = await self.client.get(
            "/api/v1/content/read",
            params={"uri": uri},
            headers=self._headers(),
        )
        result = self._unwrap_response(response)
        if isinstance(result, str):
            return json.loads(result)
        if isinstance(result, dict):
            return result
        raise ValueError(f"OpenViking content at {uri} is not a JSON object")

    async def write_json(
        self,
        uri: str,
        payload: Dict[str, Any],
        *,
        create: bool = False,
        replace_only: bool = False,
    ) -> Dict[str, Any]:
        body = {
            "uri": uri,
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "mode": "create" if create else "replace",
            "wait": False,
        }
        response = await self.client.post("/api/v1/content/write", json=body, headers=self._headers())
        try:
            result = self._unwrap_response(response)
        except OpenVikingNotFound:
            # Upsert path: replace failed because the file does not exist.
            if create or replace_only:
                raise
            body["mode"] = "create"
            response = await self.client.post("/api/v1/content/write", json=body, headers=self._headers())
            result = self._unwrap_response(response)
        return result if isinstance(result, dict) else {"result": result}

    async def delete_json(self, uri: str) -> Dict[str, Any]:
        response = await self.client.delete(
            "/api/v1/fs",
            params={"uri": uri, "recursive": "false"},
            headers=self._headers(),
        )
        result = self._unwrap_response(response)
        return result if isinstance(result, dict) else {"result": result}

    async def refresh_uri(self, uri: str, *, regenerate: bool = True, wait: bool = True) -> Dict[str, Any]:
        response = await self.client.post(
            "/api/v1/maintenance/reindex",
            json={"uri": uri, "regenerate": regenerate, "wait": wait},
            headers=self._headers(),
        )
        result = self._unwrap_response(response)
        return result if isinstance(result, dict) else {"result": result}

    async def list_json(self, prefix_uri: str) -> List[Dict[str, Any]]:
        response = await self.client.get(
            "/api/v1/fs/ls",
            params={
                "uri": prefix_uri,
                "recursive": "true",
                "output": "original",
                "show_all_hidden": "true",
                "limit": self.list_limit,
            },
            headers=self._headers(),
        )
        try:
            result = self._unwrap_response(response)
        except OpenVikingNotFound:
            return []
        entries: Iterable[Any]
        if isinstance(result, dict):
            entries = result.get("items") or result.get("children") or result.get("entries") or []
        elif isinstance(result, list):
            entries = result
        else:
            entries = []
        entries = list(entries)
        if len(entries) >= self.list_limit:
            raise OpenVikingListingTruncated(
                f"OpenViking listing for {prefix_uri} reached limit {self.list_limit}; refusing partial data"
            )

        payloads: List[Dict[str, Any]] = []
        for entry in entries:
            uri = None
            is_dir = False
            if isinstance(entry, dict):
                uri = entry.get("uri") or entry.get("path")
                is_dir = bool(entry.get("isDir") or entry.get("is_dir"))
            elif isinstance(entry, str):
                uri = entry if entry.startswith("viking://") else f"{prefix_uri.rstrip('/')}/{entry.lstrip('/')}"
            if not uri or is_dir or not uri.endswith(".json"):
                continue
            try:
                payload = await self.read_json(uri)
            except OpenVikingNotFound:
                continue
            payload.setdefault("uri", uri)
            payloads.append(payload)
        return payloads

    async def search_json(self, query: str, target_uri: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        response = await self.client.post(
            "/api/v1/search/find",
            json={
                "query": query,
                "target_uri": target_uri,
                "limit": max(1, limit),
                "include_provenance": False,
            },
            headers=self._headers(),
        )
        result = self._unwrap_response(response)
        entries = result.get("memories") if isinstance(result, dict) else []
        if not isinstance(entries, list):
            return []

        payloads: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            uri = str(entry.get("uri") or "")
            if not uri.endswith(".json"):
                continue
            try:
                payload = await self.read_json(uri)
            except OpenVikingNotFound:
                continue
            normalized = dict(payload)
            normalized.setdefault("uri", uri)
            if entry.get("score") is not None:
                normalized["score"] = entry.get("score")
            if entry.get("abstract") and not normalized.get("abstract"):
                normalized["abstract"] = entry.get("abstract")
            payloads.append(normalized)
        return payloads

    async def list_user_ids(self) -> List[str]:
        response = await self.client.get(
            "/api/v1/fs/ls",
            params={"uri": "viking://user", "output": "original", "limit": self.list_limit},
            headers=self._headers(),
        )
        try:
            result = self._unwrap_response(response)
        except OpenVikingNotFound:
            return []
        entries = result.get("items") if isinstance(result, dict) else result if isinstance(result, list) else []
        entries = list(entries)
        if len(entries) >= self.list_limit:
            raise OpenVikingListingTruncated(
                f"OpenViking user listing reached limit {self.list_limit}; refusing partial nightly processing"
            )
        users: List[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                name = entry.get("name")
                uri = entry.get("uri") or ""
                if not name and uri.startswith("viking://user/"):
                    name = uri[len("viking://user/") :].strip("/").split("/")[0]
                if name:
                    users.append(str(name))
            elif isinstance(entry, str):
                users.append(entry.strip("/").split("/")[-1])
        return sorted(set(users))


class OpenWebUIBridgeService:
    def __init__(
        self,
        client: OpenVikingClientProtocol,
        *,
        window: Optional[DreamingWindowConfig] = None,
        skill_guard: Optional[SkillEvolutionGuard] = None,
    ) -> None:
        self.client = client
        self.uris = OpenVikingURIBuilder()
        self.window = window or DreamingWindowConfig()
        self.skill_guard = skill_guard
        self._status_by_user: Dict[str, Dict[str, Any]] = {}
        self._org_status: Dict[str, Any] = {}
        self._running: set[str] = set()
        self._last_scheduler_date: Optional[str] = None
        self._nightly_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "OpenWebUIBridgeService":
        endpoint = os.getenv("OPENVIKING_ENDPOINT") or os.getenv("OPENVIKING_URL") or "http://openviking:1933"
        client = OpenVikingHTTPClient(
            base_url=endpoint,
            api_key=os.getenv("OPENVIKING_API_KEY", ""),
            account=os.getenv("OPENVIKING_ACCOUNT", "org"),
            user=os.getenv("OPENVIKING_USER", "default"),
            agent=os.getenv("OPENVIKING_AGENT", "hermes"),
            timeout=float(os.getenv("OPENVIKING_TIMEOUT_SECONDS", "15")),
        )
        window = DreamingWindowConfig(
            timezone=os.getenv("HERMES_DREAMING_TIMEZONE", "Asia/Taipei"),
            start_hour=int(os.getenv("HERMES_DREAMING_WINDOW_START_HOUR", "2")),
            end_hour=int(os.getenv("HERMES_DREAMING_WINDOW_END_HOUR", "8")),
        )
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
        guard = SkillEvolutionGuard(
            hermes_home=hermes_home,
            enabled=os.getenv("HERMES_SKILL_EVOLUTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
        )
        return cls(client, window=window, skill_guard=guard)

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _memory_id(memory_type: str, content: str) -> str:
        digest = hashlib.sha256(f"{memory_type}\n{content}".encode("utf-8")).hexdigest()[:20]
        return f"dream-{digest}"

    async def _tombstone_ids(self, user_id: str) -> set[str]:
        payloads = await self.client.list_json(self.uris.memory_prefix(user_id, "tombstones"))
        return {str(item.get("memory_id") or item.get("id")) for item in payloads if item.get("memory_id") or item.get("id")}

    async def ingest_feedback(self, user_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        with _scoped_openviking_user(user_id):
            return await self._ingest_feedback(user_id, event)

    async def _ingest_feedback(self, user_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(
            event.get("event_id")
            or event.get("idempotency_key")
            or event.get("id")
            or hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
        )
        uri = self.uris.feedback_signal(user_id, event_id)
        event = dict(event)
        event["event_id"] = event_id
        prior_signal_uris: List[str] = []
        if self._is_deleted_feedback_event(event):
            feedback_id = self._feedback_id(event)
            retracted_memory_ids: set[str] = set()
            if feedback_id:
                for signal in await self.client.list_json(self.uris.feedback_prefix(user_id)):
                    prior_event = self._feedback_event(signal)
                    if self._feedback_id(prior_event) != feedback_id:
                        continue
                    for memory_id in self._memory_ids_from_feedback_signal(signal):
                        retracted_memory_ids.add(memory_id)
                    signal_uri = str(signal.get("uri") or "")
                    prior_event_id = str(prior_event.get("event_id") or "")
                    if signal_uri and signal_uri != uri and prior_event_id != event_id:
                        prior_signal_uris.append(signal_uri)
            event = self._minimal_deleted_feedback_event(event, retracted_memory_ids)
        else:
            feedback_id = self._feedback_id(event)
            if feedback_id:
                superseding_delete = await self._find_superseding_deleted_feedback_event(
                    user_id,
                    feedback_id,
                    event,
                )
                if superseding_delete is not None:
                    purged = await self._purge_feedback_signal_uris([uri])
                    return {
                        "status": "stale_ignored",
                        "idempotent": True,
                        "event_id": event_id,
                        "uri": uri,
                        "superseded_by_event_id": str(
                            superseding_delete.get("event_id") or superseding_delete.get("id") or ""
                        ),
                        "purged_feedback_signals": purged,
                    }

        try:
            existing = await self.client.read_json(uri)
            purged = await self._purge_feedback_signal_uris(prior_signal_uris)
            return {
                "status": "duplicate",
                "idempotent": True,
                "event_id": event_id,
                "uri": uri,
                "existing": existing,
                "purged_feedback_signals": purged,
            }
        except OpenVikingNotFound:
            pass

        payload = {
            "kind": "openwebui.feedback",
            "user_id": user_id,
            "event_id": event_id,
            "received_at": self._now(),
            "event": event,
        }
        try:
            await self.client.write_json(uri, payload, create=True)
        except OpenVikingConflict:
            purged = await self._purge_feedback_signal_uris(prior_signal_uris)
            return {
                "status": "duplicate",
                "idempotent": True,
                "event_id": event_id,
                "uri": uri,
                "purged_feedback_signals": purged,
            }
        purged = await self._purge_feedback_signal_uris(prior_signal_uris)
        return {
            "status": "created",
            "idempotent": False,
            "event_id": event_id,
            "uri": uri,
            "purged_feedback_signals": purged,
        }

    async def _purge_feedback_signal_uris(self, signal_uris: Iterable[str]) -> int:
        purged = 0
        for signal_uri in sorted(set(signal_uris)):
            try:
                await self.client.delete_json(signal_uri)
                purged += 1
            except OpenVikingNotFound:
                continue
        return purged

    def _delete_event_supersedes(self, delete_signal: Dict[str, Any], delete_event: Dict[str, Any], event: Dict[str, Any]) -> bool:
        try:
            delete_version = int(delete_event.get("version"))
            event_version = int(event.get("version"))
        except (TypeError, ValueError):
            delete_version = event_version = -1
        if delete_version >= 0 and event_version >= 0:
            return delete_version >= event_version
        return self._feedback_timestamp(delete_signal, delete_event) >= self._feedback_timestamp({}, event)

    async def _find_superseding_deleted_feedback_event(
        self,
        user_id: str,
        feedback_id: str,
        event: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        best: Optional[tuple[tuple[int, int, int, str], Dict[str, Any]]] = None
        for signal in await self.client.list_json(self.uris.feedback_prefix(user_id)):
            candidate = self._feedback_event(signal)
            if self._feedback_id(candidate) != feedback_id or not self._is_deleted_feedback_event(candidate):
                continue
            if not self._delete_event_supersedes(signal, candidate, event):
                continue
            sort_key = self._feedback_event_sort_key(signal, candidate)
            if best is None or sort_key > best[0]:
                best = (sort_key, candidate)
        return best[1] if best is not None else None

    async def list_memories(
        self,
        user_id: str,
        *,
        memory_type: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with _scoped_openviking_user(user_id):
            return await self._list_memories(user_id, memory_type=memory_type, query=query, limit=limit)

    async def _list_memories(
        self,
        user_id: str,
        *,
        memory_type: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if memory_type is not None and memory_type not in MEMORY_TYPES:
            raise ValueError(f"unsupported memory type: {memory_type}")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        if query and query.strip():
            return await self._search_memories(
                user_id,
                query=query.strip(),
                memory_type=memory_type,
                limit=limit,
            )

        types = [memory_type] if memory_type else sorted(MEMORY_TYPES)
        tombstoned = await self._tombstone_ids(user_id)
        memories: List[Dict[str, Any]] = []
        for mtype in types:
            if mtype not in MEMORY_TYPES:
                continue
            for payload in await self.client.list_json(self.uris.memory_prefix(user_id, mtype)):
                memory_id = str(payload.get("id") or payload.get("memory_id") or "")
                if not memory_id or memory_id in tombstoned:
                    continue
                normalized = dict(payload)
                normalized.setdefault("id", memory_id)
                normalized.setdefault("user_id", user_id)
                normalized.setdefault("type", mtype)
                normalized.setdefault("confidence", 1.0)
                normalized.setdefault("created_at", normalized.get("updated_at", self._now()))
                normalized.setdefault("updated_at", normalized.get("created_at", self._now()))
                memories.append(normalized)
        memories.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return memories[:limit] if limit else memories

    async def _search_memories(
        self,
        user_id: str,
        *,
        query: str,
        memory_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        target_uri = self.uris.memory_prefix(user_id, memory_type) if memory_type else self.uris.memory_root(user_id)
        tombstoned = await self._tombstone_ids(user_id)
        results = await self.client.search_json(query, target_uri, limit=limit or 10)
        memories: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for payload in results:
            memory_id = str(payload.get("id") or payload.get("memory_id") or "")
            if not memory_id or memory_id in tombstoned or memory_id in seen:
                continue
            inferred_type = str(payload.get("type") or memory_type or "")
            if inferred_type not in MEMORY_TYPES:
                inferred_type = await self._find_memory_type(user_id, memory_id) or "imported"
            if memory_type and inferred_type != memory_type:
                continue
            normalized = dict(payload)
            normalized.setdefault("id", memory_id)
            normalized.setdefault("user_id", user_id)
            normalized.setdefault("type", inferred_type)
            normalized.setdefault("confidence", 1.0)
            normalized.setdefault("created_at", normalized.get("updated_at", self._now()))
            normalized.setdefault("updated_at", normalized.get("created_at", self._now()))
            memories.append(normalized)
            seen.add(memory_id)
        return memories[:limit] if limit else memories

    async def upsert_memory(self, user_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        with _scoped_openviking_user(user_id):
            return await self._upsert_memory(user_id, body)

    async def _upsert_memory(self, user_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        content = str(body.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        memory_id = str(body.get("id") or body.get("memory_id") or uuid.uuid4().hex)
        requested_type = body.get("type") or body.get("memory_type")
        if requested_type:
            memory_type = str(requested_type)
        else:
            memory_type = await self._find_memory_type(user_id, memory_id) or "imported"
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"unsupported memory type: {memory_type}")
        tombstoned = await self._tombstone_ids(user_id)
        if memory_id in tombstoned:
            raise MemoryRetracted(f"memory {memory_id} is retracted")
        existing_types = await self._find_memory_types(user_id, memory_id)
        if body.get("require_existing") and not existing_types:
            raise OpenVikingNotFound(f"memory {memory_id} not found")
        stale_types = [existing for existing in existing_types if existing != memory_type]
        now = self._now()
        created_at = body.get("created_at")
        if created_at is None:
            for existing_type in [memory_type, *stale_types]:
                try:
                    existing_payload = await self.client.read_json(
                        self.uris.memory(user_id, existing_type, memory_id)
                    )
                except OpenVikingNotFound:
                    continue
                created_at = existing_payload.get("created_at")
                if created_at is not None:
                    break
        payload = {
            "id": memory_id,
            "user_id": user_id,
            "type": memory_type,
            "content": content,
            "confidence": float(body.get("confidence", 1.0)),
            "source": body.get("source", "openwebui"),
            "created_at": int(created_at if created_at is not None else now),
            "updated_at": now,
        }
        if body.get("metadata") is not None:
            payload["metadata"] = body.get("metadata")
        await self.client.write_json(
            self.uris.memory(user_id, memory_type, memory_id),
            payload,
            create=False,
            replace_only=bool(body.get("require_existing")) and memory_type in existing_types,
        )
        deleted_stale = False
        try:
            for stale_type in stale_types:
                try:
                    await self.client.delete_json(self.uris.memory(user_id, stale_type, memory_id))
                    deleted_stale = True
                except OpenVikingNotFound:
                    pass
        finally:
            if deleted_stale:
                await self.client.refresh_uri(self.uris.memory_root(user_id), regenerate=True, wait=True)
        return payload

    async def _find_memory_types(self, user_id: str, memory_id: str) -> List[str]:
        found: List[str] = []
        for memory_type in sorted(MEMORY_TYPES):
            try:
                await self.client.read_json(self.uris.memory(user_id, memory_type, memory_id))
                found.append(memory_type)
            except OpenVikingNotFound:
                continue
        return found

    async def _find_memory_type(self, user_id: str, memory_id: str) -> Optional[str]:
        found = await self._find_memory_types(user_id, memory_id)
        return found[0] if found else None

    async def delete_memory(self, user_id: str, memory_id: str, *, reason: str = "user_delete") -> Dict[str, Any]:
        with _scoped_openviking_user(user_id):
            return await self._delete_memory(user_id, memory_id, reason=reason)

    async def _delete_memory(self, user_id: str, memory_id: str, *, reason: str = "user_delete") -> Dict[str, Any]:
        memory_types = await self._find_memory_types(user_id, memory_id)
        memory_uris = [self.uris.memory(user_id, memory_type, memory_id) for memory_type in memory_types]
        deleted_uris: List[str] = []
        failures: List[Exception] = []
        tombstone_uri = self.uris.tombstone(user_id, memory_id)
        if not memory_types and reason != "feedback_superseded":
            return {
                "status": "not_found",
                "memory_id": memory_id,
                "deleted": False,
                "deleted_uri": None,
                "deleted_uris": [],
                "tombstone_uri": None,
                "uri": None,
            }
        if reason != "feedback_superseded":
            tombstone_payload = {
                "id": memory_id,
                "memory_id": memory_id,
                "user_id": user_id,
                "reason": reason,
                "deleted_at": self._now(),
                "kind": "openwebui.memory.tombstone",
            }
            await self.client.write_json(tombstone_uri, tombstone_payload, create=False)
        try:
            for memory_uri in memory_uris:
                try:
                    await self.client.delete_json(memory_uri)
                    deleted_uris.append(memory_uri)
                except OpenVikingNotFound:
                    pass
                except Exception as exc:
                    failures.append(exc)
            if failures:
                pending_uris = [uri for uri in memory_uris if uri not in set(deleted_uris)]
                if reason != "feedback_superseded":
                    await self.client.write_json(
                        tombstone_uri,
                        {
                            **tombstone_payload,
                            "physical_cleanup": {
                                "status": "pending",
                                "pending_uris": pending_uris,
                                "deleted_uris": deleted_uris,
                                "error": str(failures[0]),
                                "recorded_at": self._now(),
                            },
                        },
                        create=False,
                    )
                return {
                    "status": "pending_physical_cleanup",
                    "memory_id": memory_id,
                    "deleted": True,
                    "physical_deleted": False,
                    "deleted_uri": deleted_uris[0] if deleted_uris else None,
                    "deleted_uris": deleted_uris,
                    "pending_uris": pending_uris,
                    "pending_physical_cleanup": True,
                    "error": str(failures[0]),
                    "tombstone_uri": tombstone_uri if reason != "feedback_superseded" else None,
                    "uri": tombstone_uri if reason != "feedback_superseded" else None,
                }
            status = "deleted" if deleted_uris else "superseded" if reason == "feedback_superseded" else "tombstoned"
            return {
                "status": status,
                "memory_id": memory_id,
                "deleted": bool(deleted_uris),
                "physical_deleted": bool(deleted_uris),
                "deleted_uri": deleted_uris[0] if deleted_uris else None,
                "deleted_uris": deleted_uris,
                "pending_uris": [],
                "pending_physical_cleanup": False,
                "tombstone_uri": tombstone_uri if reason != "feedback_superseded" else None,
                "uri": tombstone_uri if reason != "feedback_superseded" else None,
            }
        finally:
            if deleted_uris:
                await self.client.refresh_uri(self.uris.memory_root(user_id), regenerate=True, wait=True)

    @staticmethod
    def _feedback_event(signal: Dict[str, Any]) -> Dict[str, Any]:
        return signal.get("event") if isinstance(signal.get("event"), dict) else signal

    @staticmethod
    def _feedback_id(event: Dict[str, Any]) -> str:
        feedback = event.get("feedback") if isinstance(event.get("feedback"), dict) else {}
        previous = event.get("previous_feedback") if isinstance(event.get("previous_feedback"), dict) else {}
        return str(event.get("feedback_id") or feedback.get("id") or previous.get("id") or "")

    @staticmethod
    def _is_deleted_feedback_event(event: Dict[str, Any]) -> bool:
        return str(event.get("event_type") or "").lower() == "deleted"

    @staticmethod
    def _minimal_deleted_feedback_event(event: Dict[str, Any], retracted_memory_ids: Iterable[str]) -> Dict[str, Any]:
        keep_keys = {
            "event_id",
            "event_type",
            "feedback_id",
            "user_id",
            "actor_user_id",
            "actor_role",
            "version",
            "created_at",
            "updated_at",
        }
        minimal = {key: event[key] for key in keep_keys if key in event}
        minimal["event_type"] = "deleted"
        ids = sorted({str(memory_id) for memory_id in retracted_memory_ids if memory_id})
        if ids:
            minimal["retracted_memory_ids"] = ids
        minimal["redacted"] = True
        minimal["redaction_reason"] = "feedback_deleted"
        return minimal

    @staticmethod
    def _feedback_timestamp(signal: Dict[str, Any], event: Dict[str, Any]) -> int:
        return max(
            int(event.get("created_at") or 0),
            int(event.get("updated_at") or 0),
            int(signal.get("received_at") or 0),
        )

    @staticmethod
    def _feedback_event_sort_key(signal: Dict[str, Any], event: Dict[str, Any]) -> tuple[int, int, int, str]:
        version = int(event.get("version") or 0)
        updated_at = OpenWebUIBridgeService._feedback_timestamp(signal, event)
        received_at = int(signal.get("received_at") or 0)
        return (version, updated_at, received_at, str(event.get("event_id") or event.get("id") or ""))

    def _candidate_memories_from_feedback(
        self,
        signal: Dict[str, Any],
        *,
        since_ts: int,
        include_deleted: bool = False,
    ) -> List[Dict[str, str]]:
        event = signal.get("event") if isinstance(signal.get("event"), dict) else signal
        if not include_deleted and self._is_deleted_feedback_event(event):
            return []
        event_ts = self._feedback_timestamp(signal, event)
        if event_ts and event_ts < since_ts:
            return []

        candidates = event.get("memory_candidates")
        if isinstance(candidates, list):
            out = []
            for item in candidates:
                if isinstance(item, dict) and item.get("content"):
                    out.append({"type": str(item.get("type") or "needs"), "content": str(item["content"])})
            return out

        feedback = event.get("feedback") if isinstance(event.get("feedback"), dict) else {}
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not data and isinstance(feedback.get("data"), dict):
            data = feedback["data"]
        meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
        if not meta and isinstance(feedback.get("meta"), dict):
            meta = feedback["meta"]
        explicit_memory = data.get("memory") or data.get("preference") or event.get("content") or feedback.get("content")
        if explicit_memory:
            return [{"type": "preferences", "content": str(explicit_memory)}]
        task = meta.get("task") or data.get("task")
        if task:
            return [{"type": "recent_tasks", "content": str(task)}]
        need = meta.get("need") or data.get("need")
        if need:
            return [{"type": "needs", "content": str(need)}]
        feedback_text = data.get("comment") or data.get("reason")
        if feedback_text:
            return [{"type": "needs", "content": str(feedback_text)}]
        return []

    def _memory_ids_from_feedback_signal(self, signal: Dict[str, Any]) -> List[str]:
        memory_ids: List[str] = []
        for candidate in self._candidate_memories_from_feedback(
            signal,
            since_ts=0,
            include_deleted=True,
        ):
            memory_type = candidate.get("type", "needs")
            if memory_type not in DERIVED_MEMORY_TYPES:
                memory_type = "needs"
            content = candidate["content"].strip()
            if content:
                memory_ids.append(self._memory_id(memory_type, content))
        return memory_ids

    async def run_user_dreaming(self, user_id: str) -> Dict[str, Any]:
        with _scoped_openviking_user(user_id):
            return await self._run_user_dreaming(user_id)

    async def _run_user_dreaming(self, user_id: str) -> Dict[str, Any]:
        run_key = f"user:{user_id}"
        if run_key in self._running:
            return {"status": "already_running", "scope": "user", "user_id": user_id}
        self._running.add(run_key)
        run_id = f"dream-{uuid.uuid4().hex[:12]}"
        status = {
            "run_id": run_id,
            "scope": "user",
            "user_id": user_id,
            "phase": "running",
            "started_at": self._now(),
            "stage_counters": {},
        }
        self._status_by_user[user_id] = status
        try:
            since_ts = self._now() - 30 * 24 * 60 * 60
            signals = await self.client.list_json(self.uris.feedback_prefix(user_id))
            tombstoned = await self._tombstone_ids(user_id)
            latest_by_feedback_id: Dict[str, tuple[tuple[int, int, int, str], Dict[str, Any], Dict[str, Any]]] = {}
            for signal in signals:
                event = self._feedback_event(signal)
                feedback_id = self._feedback_id(event)
                if not feedback_id:
                    continue
                sort_key = self._feedback_event_sort_key(signal, event)
                if feedback_id not in latest_by_feedback_id or sort_key > latest_by_feedback_id[feedback_id][0]:
                    latest_by_feedback_id[feedback_id] = (sort_key, signal, event)

            superseded_memory_ids: set[str] = set()
            active_memory_ids: set[str] = set()
            if latest_by_feedback_id:
                for signal in signals:
                    event = self._feedback_event(signal)
                    feedback_id = self._feedback_id(event)
                    latest = latest_by_feedback_id.get(feedback_id)
                    is_current = latest is not None and event is latest[2]
                    if self._is_deleted_feedback_event(event):
                        retracted_ids = event.get("retracted_memory_ids")
                        if isinstance(retracted_ids, list):
                            for memory_id in retracted_ids:
                                if memory_id:
                                    superseded_memory_ids.add(str(memory_id))
                    for candidate in self._candidate_memories_from_feedback(
                        signal,
                        since_ts=0,
                        include_deleted=True,
                    ):
                        memory_type = candidate.get("type", "needs")
                        if memory_type not in DERIVED_MEMORY_TYPES:
                            memory_type = "needs"
                        content = candidate["content"].strip()
                        if not content:
                            continue
                        memory_id = self._memory_id(memory_type, content)
                        if is_current and not self._is_deleted_feedback_event(event):
                            active_memory_ids.add(memory_id)
                        else:
                            superseded_memory_ids.add(memory_id)
                retracted_memory_ids = superseded_memory_ids - active_memory_ids
                for memory_id in sorted(retracted_memory_ids):
                    await self.delete_memory(user_id, memory_id, reason="feedback_superseded")
            else:
                retracted_memory_ids = set()
            written = 0
            filtered = 0
            for signal in signals:
                event = self._feedback_event(signal)
                feedback_id = self._feedback_id(event)
                if feedback_id:
                    latest = latest_by_feedback_id.get(feedback_id)
                    if latest is not None and (event is not latest[2] or self._is_deleted_feedback_event(event)):
                        continue
                for candidate in self._candidate_memories_from_feedback(signal, since_ts=since_ts):
                    memory_type = candidate.get("type", "needs")
                    if memory_type not in DERIVED_MEMORY_TYPES:
                        memory_type = "needs"
                    content = candidate["content"].strip()
                    if not content:
                        continue
                    memory_id = self._memory_id(memory_type, content)
                    if memory_id in tombstoned:
                        filtered += 1
                        continue
                    await self.upsert_memory(
                        user_id,
                        {
                            "id": memory_id,
                            "type": memory_type,
                            "content": content,
                            "confidence": 0.72,
                            "source": "dreaming",
                        },
                    )
                    written += 1
            status.update(
                {
                    "phase": "complete",
                    "completed_at": self._now(),
                    "stage_counters": {
                        "raw_signals": len(signals),
                        "derived_memories_written": written,
                        "tombstone_filtered": filtered,
                        "feedback_retractions": len(retracted_memory_ids),
                    },
                }
            )
            return status
        except Exception as exc:
            status.update({"phase": "failed", "completed_at": self._now(), "error": str(exc)})
            raise
        finally:
            self._running.discard(run_key)

    async def run_org_aggregate(self) -> Dict[str, Any]:
        users = await self.client.list_user_ids()
        eligible_users = sorted({u for u in users if u})
        run_id = f"org-{uuid.uuid4().hex[:12]}"
        status = {
            "run_id": run_id,
            "scope": "org",
            "phase": "complete",
            "started_at": self._now(),
            "completed_at": self._now(),
            "distinct_users": len(eligible_users),
            "org_threshold_met": len(eligible_users) >= 2,
            "stage_counters": {},
        }
        if len(eligible_users) < 2:
            status["stage_counters"] = {"org_insights_written": 0}
            self._org_status = status
            return status

        identifier = datetime.now(ZoneInfo(self.window.timezone)).date().isoformat()
        payload = {
            "id": identifier,
            "kind": "hermes.org_insight",
            "distinct_users": len(eligible_users),
            "created_at": self._now(),
            "summary": "At least two distinct users produced memory/feedback signals in this window.",
        }
        await self.client.write_json(self.uris.org_insight(identifier), payload, create=False)
        status["insight_id"] = identifier
        status["stage_counters"] = {"org_insights_written": 1}
        self._org_status = status
        return status

    async def generate_skill_candidates(self, org_status: Dict[str, Any]) -> Dict[str, Any]:
        """Optionally materialize an agent-created skill candidate under HERMES_HOME.

        This deliberately writes only runtime data under HERMES_HOME.  It never
        patches hermes-agent, OpenWebUI, or OpenViking source; generated
        candidates carry provenance so the existing curator can later archive,
        reject, or consolidate them.
        """
        guard = self.skill_guard
        if guard is None or not guard.enabled:
            return {"status": "disabled", "skills_written": 0, "paths": []}
        if not org_status.get("org_threshold_met"):
            return {"status": "threshold_not_met", "skills_written": 0, "paths": []}

        insight_id = str(org_status.get("insight_id") or datetime.now(ZoneInfo(self.window.timezone)).date().isoformat())
        digest_seed = {"insight_id": insight_id, "distinct_users": org_status.get("distinct_users")}
        digest = hashlib.sha256(json.dumps(digest_seed, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        skill_id = f"agent-created-org-insight-{digest}"
        skill_dir = guard.ensure_allowed_path(Path("skills") / "agent-created" / skill_id)
        skill_dir.mkdir(parents=True, exist_ok=True)

        manifest = guard.with_agent_provenance(
            {
                "id": skill_id,
                "name": "Org Insight Follow-up",
                "source": "hermes-dreaming",
                "insight_id": insight_id,
                "created_at": self._now(),
                "behavior_check": {
                    "kind": "static",
                    "status": "pending_curator_review",
                    "expectation": "Candidate remains under HERMES_HOME and is never a source patch.",
                },
            }
        )
        manifest_path = guard.ensure_allowed_path(skill_dir / "manifest.json")
        skill_path = guard.ensure_allowed_path(skill_dir / "SKILL.md")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        skill_path.write_text(
            "\n".join(
                [
                    "---",
                    f"name: {skill_id}",
                    "description: Agent-created org insight follow-up candidate; pending curator review.",
                    "---",
                    "",
                    "# Org Insight Follow-up",
                    "",
                    "This candidate was generated from anonymized Hermes org insights and must be curated before use.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return {
            "status": "written",
            "skills_written": 1,
            "paths": [str(manifest_path), str(skill_path)],
            "candidate": manifest,
        }

    async def run_nightly_pipeline(
        self,
        *,
        before_phase: Optional[Callable[[str, Optional[str]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Run the v1 dreaming pipeline: all users first, then org aggregate."""
        users = await self.client.list_user_ids()
        eligible_users = sorted({u for u in users if u})
        user_runs = []
        user_failures = []
        for user_id in eligible_users:
            if before_phase:
                await before_phase("user", user_id)
            try:
                user_runs.append(await self.run_user_dreaming(user_id))
            except Exception as exc:
                failure = {
                    "scope": "user",
                    "phase": "failed",
                    "user_id": user_id,
                    "completed_at": self._now(),
                    "error": str(exc),
                }
                user_runs.append(failure)
                user_failures.append(failure)
        if before_phase:
            await before_phase("org", None)
        org_status = await self.run_org_aggregate()
        if before_phase:
            await before_phase("skills", None)
        skill_status = await self.generate_skill_candidates(org_status)
        return {
            "scope": "nightly",
            "phase": "partial" if user_failures else "complete",
            "distinct_users": len(eligible_users),
            "user_runs": user_runs,
            "org": org_status,
            "skills": skill_status,
            "stage_counters": {
                "users_processed": len(user_runs),
                "user_failures": len(user_failures),
                "org_insights_written": int(
                    (org_status.get("stage_counters") or {}).get("org_insights_written") or 0
                ),
                "skill_candidates_written": int(skill_status.get("skills_written") or 0),
            },
        }

    async def run_now(self, *, user_id: Optional[str] = None, org: bool = False) -> Dict[str, Any]:
        if org:
            return await self.run_org_aggregate()
        if not user_id:
            raise ValueError("user_id is required for per-user dreaming")
        return await self.run_user_dreaming(user_id)

    def status(self, *, user_id: Optional[str] = None, org: bool = False) -> Dict[str, Any]:
        if org:
            return dict(self._org_status or {"scope": "org", "phase": "never_run"})
        if not user_id:
            raise ValueError("user_id is required for per-user dreaming status")
        current = self._status_by_user.get(user_id)
        if not current:
            return {"scope": "user", "user_id": user_id, "phase": "never_run"}
        return dict(current)

    async def _read_nightly_ledger(self, local_date: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.client.read_json(self.uris.dreaming_run(local_date))
        except OpenVikingNotFound:
            return None

    async def _write_nightly_ledger(
        self,
        local_date: str,
        payload: Dict[str, Any],
        *,
        create: bool = False,
    ) -> None:
        await self.client.write_json(self.uris.dreaming_run(local_date), payload, create=create)

    async def _read_nightly_lock(self, local_date: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.client.read_json(self.uris.dreaming_run_lock(local_date))
        except OpenVikingNotFound:
            return None

    async def _delete_nightly_lock_if_owned(self, local_date: str, run_id: str) -> None:
        current = await self._read_nightly_lock(local_date)
        if current and current.get("run_id") == run_id:
            try:
                await self.client.delete_json(self.uris.dreaming_run_lock(local_date))
            except OpenVikingNotFound:
                pass

    def _nightly_lease_seconds(self) -> int:
        return max(1, int(os.getenv("HERMES_DREAMING_RUN_LEASE_SECONDS", "3600")))

    async def _renew_nightly_lease(self, local_date: str, run_id: str) -> Optional[Dict[str, Any]]:
        current_lock = await self._read_nightly_lock(local_date)
        if not current_lock or current_lock.get("run_id") != run_id:
            return None
        now = self._now()
        renewed_lock = {
            **current_lock,
            "lease_until": now + self._nightly_lease_seconds(),
            "renewed_at": now,
        }
        await self.client.write_json(self.uris.dreaming_run_lock(local_date), renewed_lock)
        current_ledger = await self._read_nightly_ledger(local_date)
        if not current_ledger or current_ledger.get("run_id") != run_id or current_ledger.get("phase") != "running":
            return None
        renewed_ledger = {
            **current_ledger,
            "lease_until": renewed_lock["lease_until"],
            "renewed_at": now,
        }
        await self._write_nightly_ledger(local_date, renewed_ledger)
        current_lock = await self._read_nightly_lock(local_date)
        if not current_lock or current_lock.get("run_id") != run_id:
            return None
        return renewed_ledger

    async def _acquire_nightly_lock(self, local_date: str, run_id: str, now: int) -> Optional[Dict[str, Any]]:
        lease_until = now + self._nightly_lease_seconds()
        lock = {
            "scope": "nightly",
            "phase": "running",
            "local_date": local_date,
            "run_id": run_id,
            "started_at": now,
            "lease_until": lease_until,
        }
        lock_uri = self.uris.dreaming_run_lock(local_date)
        try:
            await self.client.write_json(lock_uri, lock, create=True)
            return lock
        except OpenVikingConflict:
            current = await self._read_nightly_lock(local_date)
            if current and int(current.get("lease_until") or 0) > now:
                return None
            if current:
                try:
                    await self.client.delete_json(lock_uri)
                except OpenVikingNotFound:
                    pass
            try:
                await self.client.write_json(lock_uri, lock, create=True)
                return lock
            except OpenVikingConflict:
                return None

    async def run_due_nightly(self) -> Dict[str, Any]:
        """Run the built-in nightly aggregate at most once per local day."""
        if not self.window.is_within_window():
            return {"status": "outside_window", "timezone": self.window.timezone}
        async with self._nightly_lock:
            today = self.window.local_date_key
            if self._last_scheduler_date == today:
                return {"status": "already_ran", "local_date": today}
            now = self._now()
            existing = await self._read_nightly_ledger(today)
            if existing:
                phase = str(existing.get("phase") or "")
                if phase in {"complete", "partial"}:
                    self._last_scheduler_date = today
                    return {"status": "already_ran", "local_date": today, "ledger": existing}
                if phase == "running" and int(existing.get("lease_until") or 0) > now:
                    return {"status": "already_running", "local_date": today, "ledger": existing}
                if phase == "failed" and int(existing.get("next_attempt_at") or 0) > now:
                    return {"status": "retry_wait", "local_date": today, "ledger": existing}

            run_id = f"nightly-{uuid.uuid4().hex[:12]}"
            lock = await self._acquire_nightly_lock(today, run_id, now)
            if lock is None:
                current = await self._read_nightly_lock(today)
                return {"status": "already_running", "local_date": today, "ledger": current or existing}
            ledger = {
                "scope": "nightly",
                "phase": "running",
                "local_date": today,
                "run_id": run_id,
                "started_at": now,
                "lease_until": lock["lease_until"],
            }
            try:
                await self._write_nightly_ledger(today, ledger, create=existing is None)
            except OpenVikingConflict:
                await self._delete_nightly_lock_if_owned(today, run_id)
                current = await self._read_nightly_ledger(today)
                return {"status": "already_running", "local_date": today, "ledger": current or ledger}

            current_lock = await self._read_nightly_lock(today)
            if not current_lock or current_lock.get("run_id") != run_id:
                return {"status": "already_running", "local_date": today, "ledger": current_lock or ledger}
            current = await self._read_nightly_ledger(today)
            if not current or current.get("run_id") != run_id or current.get("phase") != "running":
                await self._delete_nightly_lock_if_owned(today, run_id)
                return {"status": "already_running", "local_date": today, "ledger": current or ledger}

            async def renew_before_phase(_phase: str, _user_id: Optional[str]) -> None:
                renewed = await self._renew_nightly_lease(today, run_id)
                if not renewed:
                    raise NightlyLeaseLost("nightly lease lost before phase")

            try:
                result = await self.run_nightly_pipeline(before_phase=renew_before_phase)
            except NightlyLeaseLost:
                current_lock = await self._read_nightly_lock(today)
                current = await self._read_nightly_ledger(today)
                return {"status": "already_running", "local_date": today, "ledger": current_lock or current or ledger}
            except Exception as exc:
                failure = {
                    **ledger,
                    "phase": "failed",
                    "completed_at": self._now(),
                    "lease_until": 0,
                    "next_attempt_at": self._now() + int(os.getenv("HERMES_DREAMING_RETRY_SECONDS", "900")),
                    "error": str(exc),
                }
                current_lock = await self._read_nightly_lock(today)
                current = await self._read_nightly_ledger(today)
                if current_lock and current_lock.get("run_id") == run_id and current and current.get("run_id") == run_id:
                    await self._write_nightly_ledger(today, failure)
                    await self._delete_nightly_lock_if_owned(today, run_id)
                self._org_status = failure
                raise

            completed = {
                **result,
                "local_date": today,
                "run_id": run_id,
                "completed_at": self._now(),
                "lease_until": 0,
            }
            current_lock = await self._read_nightly_lock(today)
            if not current_lock or current_lock.get("run_id") != run_id:
                return {"status": "already_running", "local_date": today, "ledger": current_lock or ledger}
            current = await self._read_nightly_ledger(today)
            if not current or current.get("run_id") != run_id:
                return {"status": "already_running", "local_date": today, "ledger": current or ledger}
            await self._write_nightly_ledger(today, completed)
            await self._delete_nightly_lock_if_owned(today, run_id)
            self._last_scheduler_date = today
            self._org_status = completed
            return completed

    def record_scheduler_failure(self, exc: Exception) -> Dict[str, Any]:
        status = {
            "scope": "nightly",
            "phase": "failed",
            "completed_at": self._now(),
            "error": str(exc),
        }
        self._org_status = status
        return status


async def dreaming_scheduler_loop(service: OpenWebUIBridgeService, *, poll_seconds: float = 300.0) -> None:
    """Built-in dreaming scheduler; intentionally not the generic /api/jobs API."""
    while True:
        try:
            await service.run_due_nightly()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            service.record_scheduler_failure(exc)
            logger.exception("OpenWebUI dreaming scheduler iteration failed")
        await asyncio.sleep(poll_seconds)
