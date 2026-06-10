import json
from datetime import datetime
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tools.local_document_export_tool import (
    build_artifact_signature,
    owner_hash_for_scope,
)


def _write_manifest(root: Path, *, key: str, expires_at: str = "2999-01-01T00:00:00Z"):
    user_id = "user-1"
    chat_id = "chat-1"
    artifact_id = "a" * 32
    filename = "memo.docx"
    owner_hash = owner_hash_for_scope(user_id, chat_id)
    artifact_dir = root / owner_hash / artifact_id
    artifact_dir.mkdir(parents=True)
    file_path = artifact_dir / filename
    data = b"docx bytes"
    file_path.write_bytes(data)
    import hashlib

    sha256 = hashlib.sha256(data).hexdigest()
    sig = build_artifact_signature(
        key=key,
        version="v1",
        user_id=user_id,
        chat_id=chat_id,
        artifact_id=artifact_id,
        filename=filename,
        sha256=sha256,
        expires_at=expires_at,
    )
    manifest = {
        "artifact_id": artifact_id,
        "signature_version": "v1",
        "owner": {"user_id": user_id, "chat_id": chat_id, "owner_hash": owner_hash},
        "expires_at": expires_at,
        "artifacts": [
            {
                "format": "docx",
                "filename": filename,
                "size": len(data),
                "sha256": sha256,
                "expires_at": expires_at,
                "signature": sig,
            }
        ],
    }
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return artifact_id, filename, expires_at, sig


def _expires_epoch(expires_at: str) -> int:
    return int(datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp())


def _add_artifact_routes(app: web.Application, adapter: APIServerAdapter) -> None:
    app.router.add_get(
        "/v1/artifacts/{artifact_id}/{filename}", adapter._handle_artifact_download
    )
    app.router.add_get(
        "/v1/artifacts/{artifact_id}/{filename}/download/{expires_epoch}/{sig}",
        adapter._handle_artifact_download,
    )


@pytest.mark.asyncio
async def test_artifact_route_serves_signed_download_without_bearer_auth(monkeypatch, tmp_path):
    key = "secret"
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", key)
    artifact_id, filename, expires_at, sig = _write_manifest(tmp_path, key=key)

    adapter = APIServerAdapter(PlatformConfig())
    app = web.Application()
    _add_artifact_routes(app, adapter)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(
            f"/v1/artifacts/{artifact_id}/{filename}?expires={expires_at}&sig={sig}"
        )
        body = await resp.read()
    finally:
        await client.close()

    assert resp.status == 200
    assert body == b"docx bytes"
    assert resp.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.headers["Cache-Control"] == "no-store, no-cache"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_artifact_route_rejects_missing_key(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.delenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", raising=False)

    adapter = APIServerAdapter(PlatformConfig())
    app = web.Application()
    _add_artifact_routes(app, adapter)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(f"/v1/artifacts/{'a' * 32}/manifest.json?expires=x&sig=y")
    finally:
        await client.close()

    assert resp.status == 503


@pytest.mark.asyncio
async def test_artifact_route_blocks_manifest_and_source_md_with_valid_key(
    monkeypatch, tmp_path
):
    key = "secret"
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", key)
    artifact_id, _filename, expires_at, sig = _write_manifest(tmp_path, key=key)

    adapter = APIServerAdapter(PlatformConfig())
    app = web.Application()
    _add_artifact_routes(app, adapter)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        manifest_resp = await client.get(
            f"/v1/artifacts/{artifact_id}/manifest.json?expires={expires_at}&sig={sig}"
        )
        source_resp = await client.get(
            f"/v1/artifacts/{artifact_id}/export.source.md?expires={expires_at}&sig={sig}"
        )
    finally:
        await client.close()

    assert manifest_resp.status == 403
    assert source_resp.status == 403


@pytest.mark.asyncio
async def test_artifact_route_accepts_html_escaped_sig_query_fallback(
    monkeypatch, tmp_path
):
    key = "secret"
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", key)
    artifact_id, filename, expires_at, sig = _write_manifest(tmp_path, key=key)

    adapter = APIServerAdapter(PlatformConfig())
    app = web.Application()
    _add_artifact_routes(app, adapter)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        escaped_resp = await client.get(
            f"/v1/artifacts/{artifact_id}/{filename}?expires={expires_at}&amp;sig={sig}"
        )
        escaped_body = await escaped_resp.read()
        missing_resp = await client.get(
            f"/v1/artifacts/{artifact_id}/{filename}?expires={expires_at}"
        )
        invalid_resp = await client.get(
            f"/v1/artifacts/{artifact_id}/{filename}?expires={expires_at}&amp;sig=bad"
        )
    finally:
        await client.close()

    assert escaped_resp.status == 200
    assert escaped_body == b"docx bytes"
    assert missing_resp.status == 403
    assert invalid_resp.status == 403


@pytest.mark.asyncio
async def test_artifact_route_serves_path_style_signed_download(monkeypatch, tmp_path):
    key = "secret"
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", key)
    artifact_id, filename, expires_at, sig = _write_manifest(tmp_path, key=key)

    adapter = APIServerAdapter(PlatformConfig())
    app = web.Application()
    _add_artifact_routes(app, adapter)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(
            f"/v1/artifacts/{artifact_id}/{filename}/download/{_expires_epoch(expires_at)}/{sig}"
        )
        body = await resp.read()
    finally:
        await client.close()

    assert resp.status == 200
    assert body == b"docx bytes"
    assert resp.headers["Cache-Control"] == "no-store, no-cache"


@pytest.mark.asyncio
async def test_artifact_route_rejects_invalid_path_style_signature(monkeypatch, tmp_path):
    key = "secret"
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", key)
    artifact_id, filename, expires_at, _sig = _write_manifest(tmp_path, key=key)

    adapter = APIServerAdapter(PlatformConfig())
    app = web.Application()
    _add_artifact_routes(app, adapter)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(
            f"/v1/artifacts/{artifact_id}/{filename}/download/{_expires_epoch(expires_at)}/bad"
        )
    finally:
        await client.close()

    assert resp.status == 403
