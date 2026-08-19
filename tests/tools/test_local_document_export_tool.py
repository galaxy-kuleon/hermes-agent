import io
import json
import zipfile
from pathlib import Path

import pytest

from tools import local_document_export_tool as tool


def _valid_docx_bytes(text: str = "Client Memo") -> bytes:
    buf = io.BytesIO()
    document_xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>"
        + text
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _valid_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n" + (b"0" * 1024) + b"\n%%EOF\n"


@pytest.fixture(autouse=True)
def clean_export_context(monkeypatch):
    token = tool.set_trusted_export_context({})
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    yield
    tool.reset_trusted_export_context(token)


def test_sanitize_filename_stem_blocks_traversal_controls_bidi_markdown_and_reserved_names():
    assert tool.sanitize_filename_stem("../CON [bad](x) ; rm -rf \u202ecod.exe") == "export"
    assert tool.sanitize_filename_stem("client:memo|draft*2026") == "client-memo-draft-2026"
    assert len(tool.sanitize_filename_stem("a" * 300)) <= 80


def test_export_fails_closed_without_trusted_user_chat_context(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", "secret")

    result = json.loads(
        tool.local_document_export(
            {
                "content_markdown": "# Memo",
                "formats": ["docx"],
                "user_id": "llm-supplied",
                "chat_id": "llm-supplied",
            }
        )
    )

    assert result["success"] is False
    assert "trusted user/chat context" in result["error"]


def test_export_fails_closed_without_signing_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    token = tool.set_trusted_export_context(
        {
            "platform": "api_server",
            "user_id": "u1",
            "chat_id": "c1",
            "gateway_session_key": "session-secret",
        }
    )
    try:
        result = json.loads(
            tool.local_document_export(
                {"content_markdown": "# Memo", "formats": ["docx"]}
            )
        )
    finally:
        tool.reset_trusted_export_context(token)

    assert result["success"] is False
    assert "LOCAL_EXPORT_ARTIFACT_SIGNING_KEY" in result["error"]
    assert not any(
        p.name == "manifest.json" or p.suffix in {".docx", ".pdf", ".md"}
        for p in tmp_path.rglob("*")
    )


def test_export_success_writes_scoped_manifest_links_and_removes_source(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", "secret")
    monkeypatch.setenv("LOCAL_EXPORT_PUBLIC_BASE_URL", "http://localhost:8642")
    monkeypatch.setenv("LOCAL_EXPORT_TTL_HOURS", "2")
    monkeypatch.setattr(tool, "_post_markdown_to_docx", lambda markdown: _valid_docx_bytes())
    monkeypatch.setattr(tool, "_post_docx_to_pdf", lambda docx: _valid_pdf_bytes())
    token = tool.set_trusted_export_context(
        {"platform": "api_server", "user_id": "u1", "chat_id": "c1"}
    )
    try:
        result = json.loads(
            tool.local_document_export(
                {
                    "content_markdown": "# Client Memo\n\n**Important:** recap",
                    "formats": ["docx", "pdf"],
                    "filename_stem": "../Client Memo",
                }
            )
        )
    finally:
        tool.reset_trusted_export_context(token)

    assert result["success"] is True
    assert result["artifact_id"]
    assert result["markdown"].count("[") == 2
    assert all(a["url"].startswith("http://localhost:8642/v1/artifacts/") for a in result["artifacts"])
    artifact_dirs = [p for p in tmp_path.rglob(result["artifact_id"]) if p.is_dir()]
    assert len(artifact_dirs) == 1
    artifact_dir = artifact_dirs[0]
    assert not list(artifact_dir.glob("*.md"))
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["owner"]["user_id"] == "u1"
    assert manifest["owner"]["chat_id"] == "c1"
    assert "gateway_session_key" not in manifest["owner"]
    assert manifest["signature_version"] == "v1"
    assert {a["format"] for a in manifest["artifacts"]} == {"docx", "pdf"}


def test_artifact_url_uses_path_style_signature_without_query_or_amp(monkeypatch):
    monkeypatch.setenv("LOCAL_EXPORT_PUBLIC_BASE_URL", "http://localhost:8642")

    url = tool._artifact_url(
        "a" * 32,
        "memo.pdf",
        "2026-06-06T15:20:04Z",
        "b" * 64,
    )

    assert url == (
        "http://localhost:8642/v1/artifacts/"
        f"{'a' * 32}/memo.pdf/download/1780759204/{'b' * 64}"
    )
    assert "?" not in url
    assert "&" not in url
    assert "%3A" not in url


def test_artifact_url_supports_same_origin_proxy_base(monkeypatch):
    monkeypatch.setenv("LOCAL_EXPORT_PUBLIC_BASE_URL", "/api/hermes")

    url = tool._artifact_url(
        "a" * 32,
        "memo 中文.pdf",
        "2026-06-06T15:20:04Z",
        "b" * 64,
    )

    assert url == (
        "/api/hermes/v1/artifacts/"
        f"{'a' * 32}/memo%20%E4%B8%AD%E6%96%87.pdf/download/1780759204/{'b' * 64}"
    )


def test_export_success_uses_path_style_artifact_links(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_EXPORT_ARTIFACT_SIGNING_KEY", "secret")
    monkeypatch.setenv("LOCAL_EXPORT_PUBLIC_BASE_URL", "http://localhost:8642")
    monkeypatch.setattr(tool, "_post_markdown_to_docx", lambda markdown: _valid_docx_bytes())
    token = tool.set_trusted_export_context(
        {"platform": "api_server", "user_id": "u1", "chat_id": "c1"}
    )
    try:
        result = json.loads(
            tool.local_document_export(
                {
                    "content_markdown": "# Client Memo",
                    "formats": ["docx"],
                    "filename_stem": "client memo",
                }
            )
        )
    finally:
        tool.reset_trusted_export_context(token)

    assert result["success"] is True
    url = result["artifacts"][0]["url"]
    assert "/download/" in url
    assert "?" not in url
    assert "&" not in url
    assert "%3A" not in url
    assert result["markdown"] == f"[Download client-memo.docx]({url})"


def test_validation_rejects_fake_docx_and_pdf():
    with pytest.raises(ValueError):
        tool.validate_docx_bytes(b"not-a-zip")
    with pytest.raises(ValueError):
        tool.validate_pdf_bytes(b"%PDF")
