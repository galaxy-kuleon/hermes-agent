import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gateway.config import PlatformConfig
from gateway.openwebui_bridge import (
    DreamingWindowConfig,
    OpenVikingConflict,
    OpenVikingHTTPClient,
    OpenVikingListingTruncated,
    OpenVikingNotFound,
    OpenVikingURIBuilder,
    OpenWebUIBridgeService,
    SkillEvolutionGuard,
    dreaming_scheduler_loop,
)
from gateway.platforms.api_server import APIServerAdapter


class FakeVikingClient:
    def __init__(self):
        self.store = {}
        self.search_calls = []
        self.refresh_calls = []

    async def read_json(self, uri):
        if uri not in self.store:
            raise OpenVikingNotFound(uri)
        return self.store[uri]

    async def write_json(self, uri, payload, *, create=False, replace_only=False):
        if create and uri in self.store:
            raise OpenVikingConflict(uri)
        if replace_only and uri not in self.store:
            raise OpenVikingNotFound(uri)
        self.store[uri] = payload
        return {"uri": uri}

    async def delete_json(self, uri):
        if uri not in self.store:
            raise OpenVikingNotFound(uri)
        del self.store[uri]
        return {"uri": uri}

    async def refresh_uri(self, uri, *, regenerate=True, wait=True):
        self.refresh_calls.append({"uri": uri, "regenerate": regenerate, "wait": wait})
        return {"uri": uri}

    async def list_json(self, prefix_uri):
        prefix = prefix_uri.rstrip('/') + '/'
        payloads = []
        for uri, payload in sorted(self.store.items()):
            if uri.startswith(prefix) and uri.endswith('.json'):
                item = dict(payload)
                item.setdefault("uri", uri)
                payloads.append(item)
        return payloads

    async def search_json(self, query, target_uri, *, limit=10):
        self.search_calls.append({"query": query, "target_uri": target_uri, "limit": limit})
        prefix = target_uri.rstrip('/') + '/'
        results = []
        for uri, payload in sorted(self.store.items()):
            if uri.startswith(prefix) and uri.endswith('.json') and '/tombstones/' not in uri:
                item = dict(payload)
                item["uri"] = uri
                item["score"] = 0.87
                results.append(item)
        return results[:limit]

    async def list_user_ids(self):
        users = set()
        for uri in self.store:
            if uri.startswith('viking://user/'):
                users.add(uri[len('viking://user/') :].split('/')[0])
        return sorted(users)


class FakeRequest:
    def __init__(self, headers=None, query=None, body=None):
        self.headers = headers or {}
        self.query = query or {}
        self._body = body if body is not None else {}
        self.can_read_body = body is not None
        self.match_info = {}

    async def json(self):
        return self._body


class FakeHTTPResponse:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.text)


class FakeHTTPClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, json=None, headers=None):
        self.calls.append({"method": "POST", "path": path, "json": json, "headers": headers})
        if path == "/api/v1/search/find":
            return FakeHTTPResponse(
                {
                    "result": {
                        "memories": [
                            {
                                "uri": "viking://user/alice/memories/preferences/m1.json",
                                "score": 0.42,
                                "abstract": "food preference",
                            }
                        ]
                    }
                }
            )
        return FakeHTTPResponse({"result": {"status": "ok", "path": path}})

    async def get(self, path, params=None, headers=None):
        self.calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
        if path == "/api/v1/content/read":
            return FakeHTTPResponse(
                {
                    "result": {
                        "id": "m1",
                        "user_id": "alice",
                        "type": "preferences",
                        "content": "likes sushi for lunch",
                    }
                }
            )
        return FakeHTTPResponse({"result": {"items": []}})

    async def delete(self, path, params=None, headers=None):
        self.calls.append({"method": "DELETE", "path": path, "params": params, "headers": headers})
        return FakeHTTPResponse({"result": {"deleted": True}})


def test_api_bridge_requires_user_scope_and_admin_role_gate():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))

    scope, err = adapter._require_owui_scope(FakeRequest())
    assert scope is None
    assert err.status == 401

    adapter._openwebui_bridge_key = 'bridge-secret'
    assert adapter._require_owui_scope(
        FakeRequest({'Authorization': 'Bearer generic-secret', 'X-OpenWebUI-User-Id': 'alice'})
    )[1].status == 401

    auth_headers = {'Authorization': 'Bearer bridge-secret'}

    scope, err = adapter._require_owui_scope(FakeRequest(auth_headers))
    assert scope is None
    assert err.status == 400

    scope, err = adapter._require_owui_scope(FakeRequest({**auth_headers, 'X-OpenWebUI-User-Id': '../alice'}))
    assert scope is None
    assert err.status == 400

    scope, err = adapter._require_owui_scope(FakeRequest({**auth_headers, 'X-OpenWebUI-User-Id': 'alice'}))
    assert err is None
    assert scope['user_id'] == 'alice'

    assert adapter._require_owui_admin(FakeRequest({**auth_headers, 'X-OpenWebUI-User-Role': 'user'})).status == 403
    assert adapter._require_owui_admin(FakeRequest({**auth_headers, 'X-OpenWebUI-User-Role': 'admin'})) is None


@pytest.mark.asyncio
async def test_internal_bridge_routes_require_configured_api_key():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    request = FakeRequest({'X-OpenWebUI-User-Id': 'alice'}, body={'event_id': 'evt-1'})

    response = await adapter._handle_openwebui_feedback_events(request)

    assert response.status == 401


@pytest.mark.asyncio
async def test_feedback_ingest_rejects_embedded_user_scope_mismatch():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._openwebui_bridge_key = 'bridge-secret'
    request = FakeRequest(
        {
            'Authorization': 'Bearer bridge-secret',
            'X-OpenWebUI-User-Id': 'alice',
        },
        body={
            'event_id': 'evt-1',
            'user_id': 'bob',
            'feedback': {'id': 'f1', 'user_id': 'bob'},
        },
    )

    response = await adapter._handle_openwebui_feedback_events(request)

    assert response.status == 400


@pytest.mark.asyncio
async def test_feedback_ingest_allows_distinct_actor_user_id_for_admin_actions():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._openwebui_bridge_key = 'bridge-secret'
    captured = {}

    class Bridge:
        async def ingest_feedback(self, user_id, body):
            captured['user_id'] = user_id
            captured['body'] = body
            return {'status': 'created'}

    adapter._openwebui_bridge_service = Bridge()
    request = FakeRequest(
        {
            'Authorization': 'Bearer bridge-secret',
            'X-OpenWebUI-User-Id': 'alice',
        },
        body={
            'event_id': 'evt-1',
            'user_id': 'alice',
            'actor_user_id': 'admin',
            'feedback': {'id': 'f1', 'user_id': 'alice'},
        },
    )

    response = await adapter._handle_openwebui_feedback_events(request)

    assert response.status == 200
    assert captured['user_id'] == 'alice'
    assert captured['body']['actor_user_id'] == 'admin'


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['abc', '0', '-1'])
async def test_memory_list_rejects_invalid_limit_before_bridge_call(limit):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._openwebui_bridge_key = 'bridge-secret'
    request = FakeRequest({'Authorization': 'Bearer bridge-secret', 'X-OpenWebUI-User-Id': 'alice'}, {'limit': limit})

    response = await adapter._handle_openwebui_list_memories(request)

    assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [{}, {'query': 'food'}])
async def test_memory_list_rejects_invalid_memory_type_as_bad_request(query):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._openwebui_bridge_key = 'bridge-secret'
    request = FakeRequest(
        {'Authorization': 'Bearer bridge-secret', 'X-OpenWebUI-User-Id': 'alice'},
        {'type': 'bad-type', **query},
    )

    response = await adapter._handle_openwebui_list_memories(request)

    assert response.status == 400


@pytest.mark.asyncio
async def test_memory_list_handler_returns_completeness_metadata_for_non_empty_list():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._openwebui_bridge_key = 'bridge-secret'

    class Bridge:
        async def list_memories(self, user_id, *, memory_type=None, query=None, limit=None):
            assert user_id == 'alice'
            assert memory_type is None
            assert query is None
            assert limit is None
            return [{'id': 'm1', 'content': 'likes tea'}]

    adapter._openwebui_bridge_service = Bridge()
    request = FakeRequest({'Authorization': 'Bearer bridge-secret', 'X-OpenWebUI-User-Id': 'alice'})

    response = await adapter._handle_openwebui_list_memories(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload == {
        'memories': [{'id': 'm1', 'content': 'likes tea'}],
        'total': 1,
        'has_more': False,
        'truncated': False,
    }


@pytest.mark.asyncio
async def test_memory_list_handler_marks_openviking_truncation_without_partial_ids():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._openwebui_bridge_key = 'bridge-secret'

    class Bridge:
        async def list_memories(self, user_id, *, memory_type=None, query=None, limit=None):
            raise OpenVikingListingTruncated('listing reached configured limit')

    adapter._openwebui_bridge_service = Bridge()
    request = FakeRequest({'Authorization': 'Bearer bridge-secret', 'X-OpenWebUI-User-Id': 'alice'})

    response = await adapter._handle_openwebui_list_memories(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload['memories'] == []
    assert payload['total'] == 0
    assert payload['has_more'] is True
    assert payload['truncated'] is True
    assert 'configured limit' in payload['error']


@pytest.mark.asyncio
async def test_openviking_http_client_uses_search_delete_and_maintenance_reindex_contract():
    fake_http = FakeHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="alice",
        agent="hermes",
        _client=fake_http,
    )

    hits = await client.search_json("food preferences", "viking://user/alice/memories", limit=7)
    await client.delete_json("viking://user/alice/memories/preferences/m1.json")
    await client.refresh_uri("viking://user/alice/memories", regenerate=True, wait=True)

    assert hits == [
        {
            "id": "m1",
            "user_id": "alice",
            "type": "preferences",
            "content": "likes sushi for lunch",
            "uri": "viking://user/alice/memories/preferences/m1.json",
            "score": 0.42,
            "abstract": "food preference",
        }
    ]
    assert fake_http.calls[0]["method"] == "POST"
    assert fake_http.calls[0]["path"] == "/api/v1/search/find"
    assert fake_http.calls[0]["json"] == {
        "query": "food preferences",
        "target_uri": "viking://user/alice/memories",
        "limit": 7,
        "include_provenance": False,
    }
    assert fake_http.calls[1]["method"] == "GET"
    assert fake_http.calls[1]["path"] == "/api/v1/content/read"
    assert fake_http.calls[1]["params"] == {"uri": "viking://user/alice/memories/preferences/m1.json"}
    assert fake_http.calls[2]["method"] == "DELETE"
    assert fake_http.calls[2]["path"] == "/api/v1/fs"
    assert fake_http.calls[2]["params"] == {
        "uri": "viking://user/alice/memories/preferences/m1.json",
        "recursive": "false",
    }
    assert fake_http.calls[3]["method"] == "POST"
    assert fake_http.calls[3]["path"] == "/api/v1/maintenance/reindex"
    assert fake_http.calls[3]["json"] == {
        "uri": "viking://user/alice/memories",
        "regenerate": True,
        "wait": True,
    }
    assert all(call["headers"]["Authorization"] == "Bearer secret" for call in fake_http.calls)
    assert all(call["headers"]["X-API-Key"] == "secret" for call in fake_http.calls)
    assert all(call["headers"]["X-OpenViking-User"] == "alice" for call in fake_http.calls)


@pytest.mark.asyncio
async def test_bridge_http_client_uses_scoped_openwebui_user_header_for_signal_writes():
    class NotFoundThenWriteHTTPClient:
        def __init__(self):
            self.calls = []

        async def get(self, path, params=None, headers=None):
            self.calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
            return FakeHTTPResponse({"error": "not found"}, status_code=404)

        async def post(self, path, json=None, headers=None):
            self.calls.append({"method": "POST", "path": path, "json": json, "headers": headers})
            return FakeHTTPResponse({"result": {"uri": json["uri"]}})

    fake_http = NotFoundThenWriteHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="default",
        agent="hermes",
        _client=fake_http,
    )
    service = OpenWebUIBridgeService(client)

    await service.ingest_feedback('alice', {'event_id': 'evt-1', 'feedback_id': 'f1'})

    assert any(call["method"] == "POST" for call in fake_http.calls)
    assert all(call["headers"]["X-OpenViking-User"] == "alice" for call in fake_http.calls)


@pytest.mark.asyncio
async def test_openviking_http_client_refuses_limit_sized_list_without_pagination():
    class LimitSizedListHTTPClient(FakeHTTPClient):
        async def get(self, path, params=None, headers=None):
            self.calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
            return FakeHTTPResponse(
                {
                    "result": {
                        "items": [
                            {"uri": "viking://user/alice/memories/preferences/a.json"},
                            {"uri": "viking://user/alice/memories/preferences/b.json"},
                        ]
                    }
                }
            )

    fake_http = LimitSizedListHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="alice",
        agent="hermes",
        list_limit=2,
        _client=fake_http,
    )

    with pytest.raises(OpenVikingListingTruncated):
        await client.list_json("viking://user/alice/memories/preferences")

    assert fake_http.calls[0]["params"]["limit"] == 2


@pytest.mark.asyncio
async def test_openviking_http_client_refuses_limit_sized_user_listing_without_pagination():
    class LimitSizedUserListHTTPClient(FakeHTTPClient):
        async def get(self, path, params=None, headers=None):
            self.calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
            return FakeHTTPResponse(
                {
                    "result": {
                        "items": [
                            {"uri": "viking://user/alice"},
                            {"uri": "viking://user/bob"},
                        ]
                    }
                }
            )

    fake_http = LimitSizedUserListHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="alice",
        agent="hermes",
        list_limit=2,
        _client=fake_http,
    )

    with pytest.raises(OpenVikingListingTruncated):
        await client.list_user_ids()

    assert fake_http.calls[0]["params"] == {
        "uri": "viking://user",
        "output": "original",
        "limit": 2,
    }


@pytest.mark.asyncio
async def test_openviking_http_client_list_json_propagates_non_404_read_failures():
    class FailingReadHTTPClient(FakeHTTPClient):
        async def get(self, path, params=None, headers=None):
            self.calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
            if path == "/api/v1/fs/ls":
                return FakeHTTPResponse(
                    {
                        "result": {
                            "items": [
                                {"uri": "viking://user/alice/memories/tombstones/m1.json"},
                            ]
                        }
                    }
                )
            return FakeHTTPResponse({"error": "openviking unavailable"}, status_code=500)

    fake_http = FailingReadHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="alice",
        agent="hermes",
        _client=fake_http,
    )

    with pytest.raises(RuntimeError, match="openviking unavailable"):
        await client.list_json("viking://user/alice/memories/tombstones")


@pytest.mark.asyncio
async def test_openviking_http_client_search_json_propagates_non_404_read_failures():
    class FailingSearchReadHTTPClient(FakeHTTPClient):
        async def get(self, path, params=None, headers=None):
            self.calls.append({"method": "GET", "path": path, "params": params, "headers": headers})
            return FakeHTTPResponse({"error": "read failed"}, status_code=500)

    fake_http = FailingSearchReadHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="alice",
        agent="hermes",
        _client=fake_http,
    )

    with pytest.raises(RuntimeError, match="read failed"):
        await client.search_json("food preferences", "viking://user/alice/memories")


@pytest.mark.asyncio
async def test_openviking_http_client_replace_only_write_does_not_fallback_to_create():
    class MissingReplaceHTTPClient(FakeHTTPClient):
        async def post(self, path, json=None, headers=None):
            self.calls.append({"method": "POST", "path": path, "json": json, "headers": headers})
            return FakeHTTPResponse({"error": "missing"}, status_code=404)

    fake_http = MissingReplaceHTTPClient()
    client = OpenVikingHTTPClient(
        "http://openviking:1933",
        api_key="secret",
        account="acct",
        user="alice",
        agent="hermes",
        _client=fake_http,
    )

    with pytest.raises(OpenVikingNotFound):
        await client.write_json(
            "viking://user/alice/memories/preferences/m1.json",
            {"id": "m1"},
            replace_only=True,
        )

    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["json"]["mode"] == "replace"


@pytest.mark.asyncio
async def test_dreaming_scheduler_starts_only_after_successful_api_start(monkeypatch):
    monkeypatch.delenv('API_SERVER_KEY', raising=False)
    monkeypatch.setenv('HERMES_DREAMING_ENABLED', 'true')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'host': '0.0.0.0'}))

    try:
        assert await adapter.connect() is False
        assert adapter._idle_commit_task is None
        assert adapter._sweep_task is None
        assert adapter._dreaming_scheduler_task is None
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize(
    'builder,args,expected',
    [
        (OpenVikingURIBuilder.feedback_signal, ('alice', 'evt-1'), 'viking://user/alice/signals/feedback/evt-1.json'),
        (OpenVikingURIBuilder.memory, ('alice', 'preferences', 'm1'), 'viking://user/alice/memories/preferences/m1.json'),
        (OpenVikingURIBuilder.tombstone, ('alice', 'm1'), 'viking://user/alice/memories/tombstones/m1.json'),
        (OpenVikingURIBuilder.org_insight, ('2026-05-10',), 'viking://resources/hermes/org-insights/2026-05-10.json'),
        (OpenVikingURIBuilder.dreaming_run, ('2026-05-10',), 'viking://resources/hermes/dreaming-runs/2026-05-10.json'),
    ],
)
def test_canonical_uri_builder(builder, args, expected):
    assert builder(*args) == expected


def test_uri_builder_rejects_traversal():
    with pytest.raises(ValueError):
        OpenVikingURIBuilder.feedback_signal('../alice', 'evt')
    with pytest.raises(ValueError):
        OpenVikingURIBuilder.memory('alice', 'preferences', 'bad/id')


@pytest.mark.asyncio
async def test_feedback_ingestion_is_idempotent_and_user_scoped():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)

    first = await service.ingest_feedback('alice', {'event_id': 'evt-1', 'feedback_id': 'f1'})
    second = await service.ingest_feedback('alice', {'event_id': 'evt-1', 'feedback_id': 'f1'})

    assert first['status'] == 'created'
    assert second['status'] == 'duplicate'
    assert second['idempotent'] is True
    assert list(fake.store) == ['viking://user/alice/signals/feedback/evt-1.json']


@pytest.mark.asyncio
async def test_deleted_feedback_signal_is_minimized_and_purges_prior_raw_feedback():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:0:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'version': 0,
            'updated_at': 100,
            'feedback': {
                'id': 'f1',
                'data': {'comment': 'please remember private detail'},
            },
        },
    )

    result = await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'version': 1,
            'updated_at': 200,
            'feedback': {
                'id': 'f1',
                'data': {'comment': 'please remember private detail'},
            },
        },
    )

    delete_uri = 'viking://user/alice/signals/feedback/feedback%3Af1%3A1%3Adeleted.json'
    assert result['purged_feedback_signals'] == 1
    assert 'viking://user/alice/signals/feedback/feedback%3Af1%3A0%3Acreated.json' not in fake.store
    assert delete_uri in fake.store
    stored_event = fake.store[delete_uri]['event']
    assert stored_event['event_type'] == 'deleted'
    assert stored_event['redacted'] is True
    assert 'feedback' not in stored_event
    assert stored_event['retracted_memory_ids'] == [
        service._memory_id('needs', 'please remember private detail')
    ]


@pytest.mark.asyncio
async def test_duplicate_deleted_feedback_signal_still_purges_prior_raw_feedback():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'version': 1,
            'updated_at': 200,
        },
    )
    fake.store['viking://user/alice/signals/feedback/feedback%3Af1%3A0%3Acreated.json'] = {
        'kind': 'openwebui.feedback',
        'event': {
            'event_id': 'feedback:f1:0:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'version': 0,
            'feedback': {'id': 'f1', 'data': {'comment': 'stale private detail'}},
        },
    }

    result = await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'version': 1,
            'updated_at': 200,
        },
    )

    assert result['status'] == 'duplicate'
    assert result['purged_feedback_signals'] == 1
    assert 'viking://user/alice/signals/feedback/feedback%3Af1%3A0%3Acreated.json' not in fake.store


@pytest.mark.asyncio
async def test_late_non_delete_feedback_event_is_ignored_after_delete_signal():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'version': 1,
            'updated_at': 200,
        },
    )
    fake.store['viking://user/alice/signals/feedback/feedback%3Af1%3A0%3Acreated.json'] = {
        'kind': 'openwebui.feedback',
        'event': {
            'event_id': 'feedback:f1:0:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'version': 0,
            'updated_at': 100,
            'feedback': {'id': 'f1', 'data': {'comment': 'late private detail'}},
        },
    }

    result = await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:0:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'version': 0,
            'updated_at': 100,
        },
    )

    assert result['status'] == 'stale_ignored'
    assert result['superseded_by_event_id'] == 'feedback:f1:1:deleted'
    assert result['purged_feedback_signals'] == 1
    assert 'viking://user/alice/signals/feedback/feedback%3Af1%3A0%3Acreated.json' not in fake.store


@pytest.mark.asyncio
async def test_tombstone_wins_over_existing_memory():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    memory = await service.upsert_memory('alice', {'id': 'm1', 'content': 'likes concise answers', 'type': 'preferences'})
    assert memory['id'] == 'm1'

    assert [m['id'] for m in await service.list_memories('alice')] == ['m1']
    tombstone = await service.delete_memory('alice', 'm1')

    assert tombstone['status'] == 'deleted'
    assert tombstone['deleted'] is True
    assert 'viking://user/alice/memories/preferences/m1.json' not in fake.store
    assert fake.refresh_calls == [{'uri': 'viking://user/alice/memories', 'regenerate': True, 'wait': True}]
    assert await service.list_memories('alice') == []


@pytest.mark.asyncio
async def test_delete_failure_returns_pending_physical_cleanup_after_logical_tombstone():
    class FailingDeleteClient(FakeVikingClient):
        async def delete_json(self, uri):
            raise RuntimeError('delete failed')

    fake = FailingDeleteClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'likes concise answers', 'type': 'preferences'})

    result = await service.delete_memory('alice', 'm1')

    assert result['status'] == 'pending_physical_cleanup'
    assert result['deleted'] is True
    assert result['physical_deleted'] is False
    assert result['pending_physical_cleanup'] is True
    assert result['pending_uris'] == ['viking://user/alice/memories/preferences/m1.json']
    assert result['error'] == 'delete failed'
    assert 'viking://user/alice/memories/preferences/m1.json' in fake.store
    tombstone = fake.store['viking://user/alice/memories/tombstones/m1.json']
    assert tombstone['reason'] == 'user_delete'
    assert tombstone['physical_cleanup']['status'] == 'pending'
    assert tombstone['physical_cleanup']['pending_uris'] == ['viking://user/alice/memories/preferences/m1.json']
    assert tombstone['physical_cleanup']['error'] == 'delete failed'
    assert await service.list_memories('alice') == []
    assert fake.refresh_calls == []


@pytest.mark.asyncio
async def test_delete_removes_duplicate_memory_ids_across_all_types_and_refreshes_once():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'prefers tea', 'type': 'preferences'})
    fake.store['viking://user/alice/memories/needs/m1.json'] = {
        'id': 'm1',
        'user_id': 'alice',
        'type': 'needs',
        'content': 'needs quiet focus time',
    }

    result = await service.delete_memory('alice', 'm1')

    assert result['status'] == 'deleted'
    assert set(result['deleted_uris']) == {
        'viking://user/alice/memories/preferences/m1.json',
        'viking://user/alice/memories/needs/m1.json',
    }
    assert 'viking://user/alice/memories/preferences/m1.json' not in fake.store
    assert 'viking://user/alice/memories/needs/m1.json' not in fake.store
    assert fake.refresh_calls == [{'uri': 'viking://user/alice/memories', 'regenerate': True, 'wait': True}]


@pytest.mark.asyncio
async def test_delete_partial_duplicate_failure_attempts_all_uris_and_refreshes_deleted_copy():
    class PartiallyFailingDeleteClient(FakeVikingClient):
        async def delete_json(self, uri):
            if uri.endswith('/needs/m1.json'):
                raise RuntimeError('delete needs failed')
            return await super().delete_json(uri)

    fake = PartiallyFailingDeleteClient()
    service = OpenWebUIBridgeService(fake)
    fake.store['viking://user/alice/memories/needs/m1.json'] = {
        'id': 'm1',
        'user_id': 'alice',
        'type': 'needs',
        'content': 'needs quiet focus time',
    }
    fake.store['viking://user/alice/memories/preferences/m1.json'] = {
        'id': 'm1',
        'user_id': 'alice',
        'type': 'preferences',
        'content': 'prefers tea',
    }

    result = await service.delete_memory('alice', 'm1')

    assert result['status'] == 'pending_physical_cleanup'
    assert result['deleted_uris'] == ['viking://user/alice/memories/preferences/m1.json']
    assert result['pending_uris'] == ['viking://user/alice/memories/needs/m1.json']
    assert result['error'] == 'delete needs failed'
    assert 'viking://user/alice/memories/needs/m1.json' in fake.store
    assert 'viking://user/alice/memories/preferences/m1.json' not in fake.store
    tombstone = fake.store['viking://user/alice/memories/tombstones/m1.json']
    assert tombstone['reason'] == 'user_delete'
    assert tombstone['physical_cleanup']['deleted_uris'] == ['viking://user/alice/memories/preferences/m1.json']
    assert tombstone['physical_cleanup']['pending_uris'] == ['viking://user/alice/memories/needs/m1.json']
    assert fake.refresh_calls == [{'uri': 'viking://user/alice/memories', 'regenerate': True, 'wait': True}]


@pytest.mark.asyncio
async def test_tombstone_write_failure_does_not_delete_memory():
    class FailingTombstoneClient(FakeVikingClient):
        async def write_json(self, uri, payload, *, create=False, replace_only=False):
            if '/tombstones/' in uri:
                raise RuntimeError('tombstone write failed')
            return await super().write_json(uri, payload, create=create, replace_only=replace_only)

    fake = FailingTombstoneClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'prefers tea', 'type': 'preferences'})

    with pytest.raises(RuntimeError, match='tombstone write failed'):
        await service.delete_memory('alice', 'm1')

    assert 'viking://user/alice/memories/preferences/m1.json' in fake.store
    assert fake.refresh_calls == []


@pytest.mark.asyncio
async def test_feedback_superseded_noop_delete_does_not_report_tombstoned():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)

    result = await service.delete_memory('alice', 'm1', reason='feedback_superseded')

    assert result['status'] == 'superseded'
    assert result['deleted'] is False
    assert result['tombstone_uri'] is None
    assert 'viking://user/alice/memories/tombstones/m1.json' not in fake.store
    assert fake.refresh_calls == []


@pytest.mark.asyncio
async def test_missing_user_delete_does_not_write_tombstone():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)

    result = await service.delete_memory('alice', 'missing')

    assert result['status'] == 'not_found'
    assert result['deleted'] is False
    assert result['tombstone_uri'] is None
    assert fake.store == {}
    assert fake.refresh_calls == []


@pytest.mark.asyncio
async def test_memory_query_uses_semantic_search_without_substring_filtering():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'likes sushi for lunch', 'type': 'preferences'})

    memories = await service.list_memories('alice', query='food preferences', limit=3)

    assert fake.search_calls == [
        {'query': 'food preferences', 'target_uri': 'viking://user/alice/memories', 'limit': 3}
    ]
    assert [memory['id'] for memory in memories] == ['m1']
    assert memories[0]['score'] == 0.87


@pytest.mark.asyncio
async def test_deleted_memory_is_absent_from_semantic_query_results():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'likes sushi for lunch', 'type': 'preferences'})
    await service.delete_memory('alice', 'm1')

    assert await service.list_memories('alice', query='food preferences') == []


@pytest.mark.asyncio
async def test_tombstone_listing_read_failure_fails_closed():
    class FailingTombstoneListClient(FakeVikingClient):
        async def list_json(self, prefix_uri):
            if prefix_uri == 'viking://user/alice/memories/tombstones':
                raise RuntimeError('tombstone read failed')
            return await super().list_json(prefix_uri)

    fake = FailingTombstoneListClient()
    service = OpenWebUIBridgeService(fake)
    fake.store['viking://user/alice/memories/preferences/m1.json'] = {
        'id': 'm1',
        'user_id': 'alice',
        'type': 'preferences',
        'content': 'likes sushi for lunch',
    }

    with pytest.raises(RuntimeError, match='tombstone read failed'):
        await service.list_memories('alice')


@pytest.mark.asyncio
async def test_feedback_signal_read_failure_fails_dreaming_without_writing_memory():
    class FailingSignalListClient(FakeVikingClient):
        async def list_json(self, prefix_uri):
            if prefix_uri == 'viking://user/alice/signals/feedback':
                raise RuntimeError('feedback signal read failed')
            return await super().list_json(prefix_uri)

    fake = FailingSignalListClient()
    service = OpenWebUIBridgeService(fake)

    with pytest.raises(RuntimeError, match='feedback signal read failed'):
        await service.run_now(user_id='alice')

    assert not any('/memories/' in uri for uri in fake.store)
    assert service.status(user_id='alice')['phase'] == 'failed'


@pytest.mark.asyncio
async def test_memory_update_preserves_existing_type_when_type_omitted():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'prefers short answers', 'type': 'preferences'})

    updated = await service.upsert_memory('alice', {'id': 'm1', 'content': 'prefers concise answers'})

    assert updated['type'] == 'preferences'
    assert updated['content'] == 'prefers concise answers'
    assert list(fake.store) == ['viking://user/alice/memories/preferences/m1.json']


@pytest.mark.asyncio
async def test_memory_update_requires_existing_memory_when_requested():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)

    with pytest.raises(OpenVikingNotFound):
        await service.upsert_memory(
            'alice',
            {
                'id': 'missing',
                'content': 'should not create a new memory',
                'require_existing': True,
            },
        )

    assert fake.store == {}


@pytest.mark.asyncio
async def test_memory_update_require_existing_uses_replace_only_for_existing_type():
    class RecordingVikingClient(FakeVikingClient):
        def __init__(self):
            super().__init__()
            self.write_calls = []

        async def write_json(self, uri, payload, *, create=False, replace_only=False):
            self.write_calls.append({"uri": uri, "create": create, "replace_only": replace_only})
            return await super().write_json(uri, payload, create=create, replace_only=replace_only)

    fake = RecordingVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory('alice', {'id': 'm1', 'content': 'prefers short answers', 'type': 'preferences'})

    await service.upsert_memory(
        'alice',
        {
            'id': 'm1',
            'content': 'prefers concise answers',
            'require_existing': True,
        },
    )

    assert fake.write_calls[-1] == {
        "uri": "viking://user/alice/memories/preferences/m1.json",
        "create": False,
        "replace_only": True,
    }


@pytest.mark.asyncio
async def test_memory_update_preserves_existing_created_at_when_omitted():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.upsert_memory(
        'alice',
        {
            'id': 'm1',
            'content': 'prefers short answers',
            'type': 'preferences',
            'created_at': 1_700_000_000,
        },
    )

    updated = await service.upsert_memory(
        'alice',
        {'id': 'm1', 'content': 'prefers concise answers', 'type': 'preferences'},
    )

    assert updated['created_at'] == 1_700_000_000
    assert updated['updated_at'] >= updated['created_at']


@pytest.mark.asyncio
async def test_memory_retag_writes_target_before_deleting_stale_type():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    fake.store['viking://user/alice/memories/preferences/m1.json'] = {
        'id': 'm1',
        'user_id': 'alice',
        'type': 'preferences',
        'content': 'prefers short answers',
    }

    updated = await service.upsert_memory('alice', {'id': 'm1', 'content': 'needs test evidence', 'type': 'needs'})

    assert updated['type'] == 'needs'
    assert 'viking://user/alice/memories/needs/m1.json' in fake.store
    assert 'viking://user/alice/memories/preferences/m1.json' not in fake.store
    assert fake.refresh_calls == [{'uri': 'viking://user/alice/memories', 'regenerate': True, 'wait': True}]


@pytest.mark.asyncio
async def test_memory_retag_write_failure_preserves_stale_existing_copy():
    class FailingTargetWriteClient(FakeVikingClient):
        async def write_json(self, uri, payload, *, create=False, replace_only=False):
            if uri.endswith('/needs/m1.json'):
                raise RuntimeError('target write failed')
            return await super().write_json(uri, payload, create=create, replace_only=replace_only)

    fake = FailingTargetWriteClient()
    service = OpenWebUIBridgeService(fake)
    fake.store['viking://user/alice/memories/preferences/m1.json'] = {
        'id': 'm1',
        'user_id': 'alice',
        'type': 'preferences',
        'content': 'prefers short answers',
    }

    with pytest.raises(RuntimeError, match='target write failed'):
        await service.upsert_memory('alice', {'id': 'm1', 'content': 'needs test evidence', 'type': 'needs'})

    assert 'viking://user/alice/memories/preferences/m1.json' in fake.store
    assert 'viking://user/alice/memories/needs/m1.json' not in fake.store
    assert fake.refresh_calls == []


@pytest.mark.asyncio
async def test_user_dreaming_writes_derived_memories_and_filters_tombstones():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'evt-1',
            'created_at': 1_800_000_000,
            'memory_candidates': [{'type': 'preferences', 'content': 'prefers reliability-first plans'}],
        },
    )

    first = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='preferences')
    assert first['phase'] == 'complete'
    assert first['stage_counters']['derived_memories_written'] == 1
    assert memories[0]['content'] == 'prefers reliability-first plans'

    await service.delete_memory('alice', memories[0]['id'])
    second = await service.run_now(user_id='alice')
    assert second['stage_counters']['tombstone_filtered'] == 1
    assert await service.list_memories('alice', memory_type='preferences') == []


@pytest.mark.asyncio
async def test_user_dreaming_reads_nested_openwebui_feedback_payload():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'evt-owui-1',
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'data': {'preference': 'prefers serious implementation waves'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )

    result = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='preferences')

    assert result['stage_counters']['derived_memories_written'] == 1
    assert memories[0]['content'] == 'prefers serious implementation waves'


@pytest.mark.asyncio
async def test_user_dreaming_reads_real_openwebui_feedback_comment_payload():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'evt-owui-comment',
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'reason': 'too vague', 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )

    result = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='needs')

    assert result['stage_counters']['derived_memories_written'] == 1
    assert memories[0]['content'] == 'needs concrete test evidence'


@pytest.mark.asyncio
async def test_user_dreaming_ignores_deleted_openwebui_feedback_payload():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:2:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'reason': 'too vague', 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )

    result = await service.run_now(user_id='alice')

    assert result['stage_counters']['derived_memories_written'] == 0
    assert await service.list_memories('alice', memory_type='needs') == []


@pytest.mark.asyncio
async def test_user_dreaming_suppresses_created_feedback_when_later_delete_exists():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:2:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'created_at': 1_800_000_001,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )

    result = await service.run_now(user_id='alice')

    assert result['stage_counters']['derived_memories_written'] == 0
    assert result['stage_counters']['feedback_retractions'] == 1
    assert await service.list_memories('alice', memory_type='needs') == []


@pytest.mark.asyncio
async def test_user_dreaming_later_delete_retracts_already_derived_feedback_memory():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )
    first = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='needs')
    assert first['stage_counters']['derived_memories_written'] == 1
    assert len(memories) == 1
    memory_id = memories[0]['id']

    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:2:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'created_at': 1_800_000_001,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )

    second = await service.run_now(user_id='alice')

    assert second['stage_counters']['derived_memories_written'] == 0
    assert second['stage_counters']['feedback_retractions'] == 1
    assert await service.list_memories('alice', memory_type='needs') == []
    assert f'viking://user/alice/memories/needs/{memory_id}.json' not in fake.store
    assert f'viking://user/alice/memories/tombstones/{memory_id}.json' not in fake.store


@pytest.mark.asyncio
async def test_user_dreaming_does_not_retract_memory_still_supported_by_active_feedback():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    shared_feedback = {
        'user_id': 'alice',
        'created_at': 1_800_000_000,
        'feedback': {
            'user_id': 'alice',
            'type': 'rating',
            'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
            'meta': {'chat_id': 'c1'},
        },
    }
    await service.ingest_feedback(
        'alice',
        {
            **shared_feedback,
            'event_id': 'feedback:f1:1:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'feedback': {**shared_feedback['feedback'], 'id': 'f1'},
        },
    )
    await service.ingest_feedback(
        'alice',
        {
            **shared_feedback,
            'event_id': 'feedback:f2:1:created',
            'event_type': 'created',
            'feedback_id': 'f2',
            'feedback': {**shared_feedback['feedback'], 'id': 'f2'},
        },
    )
    await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='needs')
    assert len(memories) == 1
    memory_id = memories[0]['id']

    await service.ingest_feedback(
        'alice',
        {
            **shared_feedback,
            'event_id': 'feedback:f1:2:deleted',
            'event_type': 'deleted',
            'feedback_id': 'f1',
            'created_at': 1_800_000_001,
            'feedback': {**shared_feedback['feedback'], 'id': 'f1'},
        },
    )

    result = await service.run_now(user_id='alice')

    assert result['stage_counters']['feedback_retractions'] == 0
    assert [memory['id'] for memory in await service.list_memories('alice', memory_type='needs')] == [memory_id]
    assert f'viking://user/alice/memories/tombstones/{memory_id}.json' not in fake.store


@pytest.mark.asyncio
async def test_user_dreaming_update_retracts_superseded_feedback_memory():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'version': 1,
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )
    await service.run_now(user_id='alice')
    first_memories = await service.list_memories('alice', memory_type='needs')
    assert [memory['content'] for memory in first_memories] == ['needs concrete test evidence']
    old_memory_id = first_memories[0]['id']

    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:2:updated',
            'event_type': 'updated',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'version': 2,
            'created_at': 1_800_000_000,
            'updated_at': 1_800_000_010,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs deployment evidence'},
                'meta': {'chat_id': 'c1'},
            },
            'previous_feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
                'meta': {'chat_id': 'c1'},
            },
        },
    )

    result = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='needs')

    assert result['stage_counters']['feedback_retractions'] == 1
    assert result['stage_counters']['derived_memories_written'] == 1
    assert [memory['content'] for memory in memories] == ['needs deployment evidence']
    assert f'viking://user/alice/memories/needs/{old_memory_id}.json' not in fake.store
    assert f'viking://user/alice/memories/tombstones/{old_memory_id}.json' not in fake.store


@pytest.mark.asyncio
async def test_user_dreaming_update_uses_recent_updated_at_for_old_feedback():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    old_created_at = service._now() - 60 * 24 * 60 * 60
    recent_updated_at = service._now()
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'version': 1,
            'created_at': old_created_at,
            'updated_at': old_created_at,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs old evidence'},
            },
        },
    )
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:2:updated',
            'event_type': 'updated',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'version': 2,
            'created_at': old_created_at,
            'updated_at': recent_updated_at,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs recent deployment evidence'},
            },
            'previous_feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs old evidence'},
            },
        },
    )

    result = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='needs')

    assert result['stage_counters']['derived_memories_written'] == 1
    assert [memory['content'] for memory in memories] == ['needs recent deployment evidence']


@pytest.mark.asyncio
async def test_user_dreaming_later_active_support_can_recreate_superseded_memory():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:1:created',
            'event_type': 'created',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'version': 1,
            'created_at': 1_800_000_000,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
            },
        },
    )
    await service.run_now(user_id='alice')
    first_memories = await service.list_memories('alice', memory_type='needs')
    memory_id = first_memories[0]['id']
    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f1:2:updated',
            'event_type': 'updated',
            'feedback_id': 'f1',
            'user_id': 'alice',
            'version': 2,
            'created_at': 1_800_000_000,
            'updated_at': 1_800_000_010,
            'feedback': {
                'id': 'f1',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs different evidence'},
            },
        },
    )
    await service.run_now(user_id='alice')
    assert f'viking://user/alice/memories/needs/{memory_id}.json' not in fake.store
    assert f'viking://user/alice/memories/tombstones/{memory_id}.json' not in fake.store

    await service.ingest_feedback(
        'alice',
        {
            'event_id': 'feedback:f2:1:created',
            'event_type': 'created',
            'feedback_id': 'f2',
            'user_id': 'alice',
            'version': 1,
            'created_at': 1_800_000_020,
            'feedback': {
                'id': 'f2',
                'user_id': 'alice',
                'type': 'rating',
                'data': {'rating': -1, 'comment': 'needs concrete test evidence'},
            },
        },
    )

    result = await service.run_now(user_id='alice')
    memories = await service.list_memories('alice', memory_type='needs')

    assert result['stage_counters']['derived_memories_written'] == 2
    assert 'needs concrete test evidence' in {memory['content'] for memory in memories}
    assert f'viking://user/alice/memories/needs/{memory_id}.json' in fake.store


@pytest.mark.asyncio
async def test_org_aggregate_requires_two_distinct_users():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback('alice', {'event_id': 'evt-a'})

    one_user = await service.run_now(org=True)
    assert one_user['org_threshold_met'] is False
    assert one_user['stage_counters']['org_insights_written'] == 0

    await service.ingest_feedback('bob', {'event_id': 'evt-b'})
    two_users = await service.run_now(org=True)
    assert two_users['org_threshold_met'] is True
    assert two_users['stage_counters']['org_insights_written'] == 1
    assert any(uri.startswith('viking://resources/hermes/org-insights/') for uri in fake.store)


@pytest.mark.asyncio
async def test_nightly_pipeline_processes_users_before_org_aggregate():
    fake = FakeVikingClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {'event_id': 'evt-a', 'created_at': 1_800_000_000, 'memory_candidates': [{'content': 'prefers stable delivery'}]},
    )
    await service.ingest_feedback(
        'bob',
        {'event_id': 'evt-b', 'created_at': 1_800_000_000, 'memory_candidates': [{'content': 'needs reproducible tests'}]},
    )

    result = await service.run_nightly_pipeline()

    assert result['stage_counters']['users_processed'] == 2
    assert result['stage_counters']['user_failures'] == 0
    assert result['org']['org_threshold_met'] is True
    assert result['stage_counters']['org_insights_written'] == 1
    assert any('/memories/needs/' in uri for uri in fake.store)


@pytest.mark.asyncio
async def test_nightly_pipeline_isolates_per_user_dreaming_failures():
    class FailingAliceSignalClient(FakeVikingClient):
        async def list_json(self, prefix_uri):
            if prefix_uri == 'viking://user/alice/signals/feedback':
                raise RuntimeError('alice feedback unavailable')
            return await super().list_json(prefix_uri)

    fake = FailingAliceSignalClient()
    service = OpenWebUIBridgeService(fake)
    await service.ingest_feedback(
        'alice',
        {'event_id': 'evt-a', 'created_at': 1_800_000_000, 'memory_candidates': [{'content': 'alice private'}]},
    )
    await service.ingest_feedback(
        'bob',
        {'event_id': 'evt-b', 'created_at': 1_800_000_000, 'memory_candidates': [{'content': 'bob needs tests'}]},
    )

    result = await service.run_nightly_pipeline()

    assert result['phase'] == 'partial'
    assert result['stage_counters']['users_processed'] == 2
    assert result['stage_counters']['user_failures'] == 1
    assert result['user_runs'][0]['phase'] == 'failed'
    assert 'alice feedback unavailable' in result['user_runs'][0]['error']
    assert any('/memories/needs/' in uri and 'bob' in uri for uri in fake.store)


@pytest.mark.asyncio
async def test_due_nightly_uses_durable_daily_ledger_across_service_restarts():
    fake = FakeVikingClient()
    window = DreamingWindowConfig(start_hour=0, end_hour=0)
    service = OpenWebUIBridgeService(fake, window=window)
    await service.ingest_feedback(
        'alice',
        {'event_id': 'evt-a', 'created_at': 1_800_000_000, 'memory_candidates': [{'content': 'prefers ledgers'}]},
    )
    await service.ingest_feedback(
        'bob',
        {'event_id': 'evt-b', 'created_at': 1_800_000_000, 'memory_candidates': [{'content': 'needs once daily'}]},
    )

    first = await service.run_due_nightly()
    today = window.local_date_key
    ledger_uri = OpenVikingURIBuilder.dreaming_run(today)
    restarted = OpenWebUIBridgeService(fake, window=window)
    second = await restarted.run_due_nightly()

    assert first['phase'] == 'complete'
    assert fake.store[ledger_uri]['phase'] == 'complete'
    assert second['status'] == 'already_ran'
    assert second['ledger']['local_date'] == today


@pytest.mark.asyncio
async def test_due_nightly_rechecks_ledger_ownership_before_running_pipeline():
    class StolenLeaseClient(FakeVikingClient):
        async def write_json(self, uri, payload, *, create=False, replace_only=False):
            if uri == OpenVikingURIBuilder.dreaming_run(window.local_date_key) and payload.get('phase') == 'running':
                stolen = dict(payload)
                stolen['run_id'] = 'other-run'
                self.store[uri] = stolen
                return {'uri': uri}
            return await super().write_json(uri, payload, create=create, replace_only=replace_only)

    class PipelineMustNotRunService(OpenWebUIBridgeService):
        async def run_nightly_pipeline(self):
            raise AssertionError('lost lease must not run nightly pipeline')

    window = DreamingWindowConfig(start_hour=0, end_hour=0)
    fake = StolenLeaseClient()
    service = PipelineMustNotRunService(fake, window=window)

    result = await service.run_due_nightly()

    assert result['status'] == 'already_running'
    assert result['ledger']['run_id'] == 'other-run'


@pytest.mark.asyncio
async def test_due_nightly_retry_respects_existing_attempt_lock():
    class PipelineMustNotRunService(OpenWebUIBridgeService):
        async def run_nightly_pipeline(self):
            raise AssertionError('active lock must prevent retry pipeline')

    fake = FakeVikingClient()
    window = DreamingWindowConfig(start_hour=0, end_hour=0)
    service = PipelineMustNotRunService(fake, window=window)
    today = window.local_date_key
    fake.store[OpenVikingURIBuilder.dreaming_run(today)] = {
        'scope': 'nightly',
        'phase': 'failed',
        'local_date': today,
        'run_id': 'failed-run',
        'next_attempt_at': 0,
    }
    fake.store[OpenVikingURIBuilder.dreaming_run_lock(today)] = {
        'scope': 'nightly',
        'phase': 'running',
        'local_date': today,
        'run_id': 'other-run',
        'lease_until': service._now() + 300,
    }

    result = await service.run_due_nightly()

    assert result['status'] == 'already_running'
    assert result['ledger']['run_id'] == 'other-run'


@pytest.mark.asyncio
async def test_due_nightly_renews_lease_before_each_pipeline_phase(monkeypatch):
    class SlowPhaseService(OpenWebUIBridgeService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.now = 1_800_000_000
            self.phase_leases = []

        def _now(self):
            self.now += 10
            return self.now

        async def run_user_dreaming(self, user_id):
            lock = fake.store[OpenVikingURIBuilder.dreaming_run_lock(window.local_date_key)]
            self.phase_leases.append(('user', user_id, self.now, lock['lease_until']))
            return {'scope': 'user', 'phase': 'complete', 'user_id': user_id}

        async def run_org_aggregate(self):
            lock = fake.store[OpenVikingURIBuilder.dreaming_run_lock(window.local_date_key)]
            self.phase_leases.append(('org', None, self.now, lock['lease_until']))
            return {'scope': 'org', 'phase': 'complete', 'stage_counters': {'org_insights_written': 0}}

        async def generate_skill_candidates(self, org_status):
            lock = fake.store[OpenVikingURIBuilder.dreaming_run_lock(window.local_date_key)]
            self.phase_leases.append(('skills', None, self.now, lock['lease_until']))
            return {'status': 'disabled', 'skills_written': 0}

    monkeypatch.setenv('HERMES_DREAMING_RUN_LEASE_SECONDS', '5')
    fake = FakeVikingClient()
    window = DreamingWindowConfig(start_hour=0, end_hour=0)
    fake.store['viking://user/alice/signals/feedback/evt-a.json'] = {'event_id': 'evt-a'}
    service = SlowPhaseService(fake, window=window)

    result = await service.run_due_nightly()

    assert result['phase'] == 'complete'
    assert [phase for phase, *_ in service.phase_leases] == ['user', 'org', 'skills']
    assert all(lease_until > observed_now for _phase, _user_id, observed_now, lease_until in service.phase_leases)


@pytest.mark.asyncio
async def test_due_nightly_stops_when_lease_is_lost_before_next_phase():
    class LeaseStealingService(OpenWebUIBridgeService):
        async def run_user_dreaming(self, user_id):
            lock_uri = OpenVikingURIBuilder.dreaming_run_lock(window.local_date_key)
            ledger_uri = OpenVikingURIBuilder.dreaming_run(window.local_date_key)
            fake.store[lock_uri] = {**fake.store[lock_uri], 'run_id': 'other-run'}
            fake.store[ledger_uri] = {**fake.store[ledger_uri], 'run_id': 'other-run'}
            return {'scope': 'user', 'phase': 'complete', 'user_id': user_id}

        async def run_org_aggregate(self):
            raise AssertionError('lost lease must stop before org aggregate')

    fake = FakeVikingClient()
    window = DreamingWindowConfig(start_hour=0, end_hour=0)
    fake.store['viking://user/alice/signals/feedback/evt-a.json'] = {'event_id': 'evt-a'}
    service = LeaseStealingService(fake, window=window)

    result = await service.run_due_nightly()

    assert result['status'] == 'already_running'
    assert result['ledger']['run_id'] == 'other-run'


@pytest.mark.asyncio
async def test_enabled_skill_evolution_writes_agent_created_candidate_under_hermes_home(tmp_path):
    fake = FakeVikingClient()
    guard = SkillEvolutionGuard(tmp_path, enabled=True)
    service = OpenWebUIBridgeService(fake, skill_guard=guard)
    await service.ingest_feedback('alice', {'event_id': 'evt-a'})
    await service.ingest_feedback('bob', {'event_id': 'evt-b'})

    result = await service.run_nightly_pipeline()

    assert result['stage_counters']['skill_candidates_written'] == 1
    paths = [Path(path) for path in result['skills']['paths']]
    assert paths
    assert all(path.is_relative_to((tmp_path / 'skills').resolve()) for path in paths)
    manifest = next(path for path in paths if path.name == 'manifest.json')
    assert '"agent_created": true' in manifest.read_text()


@pytest.mark.asyncio
async def test_due_nightly_retries_later_when_pipeline_fails():
    class FailingUserListClient(FakeVikingClient):
        async def list_user_ids(self):
            raise RuntimeError('openviking unavailable')

    fake = FailingUserListClient()
    window = DreamingWindowConfig(start_hour=0, end_hour=0)
    service = OpenWebUIBridgeService(fake, window=window)

    with pytest.raises(RuntimeError):
        await service.run_due_nightly()

    ledger = fake.store[OpenVikingURIBuilder.dreaming_run(window.local_date_key)]
    assert ledger['phase'] == 'failed'
    assert ledger['next_attempt_at'] > ledger['completed_at']
    retry = await service.run_due_nightly()
    assert retry['status'] == 'retry_wait'
    assert service._last_scheduler_date is None


@pytest.mark.asyncio
async def test_dreaming_scheduler_records_failures_without_advancing_run_date(monkeypatch, caplog):
    class FailingUserListClient(FakeVikingClient):
        async def list_user_ids(self):
            raise RuntimeError('openviking unavailable')

    async def stop_after_iteration(_delay):
        raise asyncio.CancelledError

    service = OpenWebUIBridgeService(FailingUserListClient(), window=DreamingWindowConfig(start_hour=0, end_hour=0))
    monkeypatch.setattr(asyncio, 'sleep', stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        await dreaming_scheduler_loop(service, poll_seconds=0)

    status = service.status(org=True)
    assert status['scope'] == 'nightly'
    assert status['phase'] == 'failed'
    assert 'openviking unavailable' in status['error']
    assert service._last_scheduler_date is None
    assert 'OpenWebUI dreaming scheduler iteration failed' in caplog.text


def test_dreaming_window_uses_asia_taipei_hours():
    window = DreamingWindowConfig(timezone='Asia/Taipei', start_hour=2, end_hour=8)
    assert window.is_within_window(datetime(2026, 5, 10, 18, 0, tzinfo=ZoneInfo('UTC'))) is True  # 02:00 +08
    assert window.is_within_window(datetime(2026, 5, 10, 1, 0, tzinfo=ZoneInfo('UTC'))) is False  # 09:00 +08


def test_skill_evolution_guard_allows_only_hermes_home_runtime_paths(tmp_path):
    guard = SkillEvolutionGuard(tmp_path, enabled=True)
    assert guard.ensure_allowed_path('skills/generated/SKILL.md').is_relative_to((tmp_path / 'skills').resolve())
    with pytest.raises(ValueError):
        guard.ensure_allowed_path(Path('/tmp/not-hermes/source.patch'))
    payload = guard.with_agent_provenance({'name': 'generated'})
    assert payload['provenance']['agent_created'] is True
