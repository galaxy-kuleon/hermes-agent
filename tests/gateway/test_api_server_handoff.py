import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter


def _scope():
    return {"user_id": "user-1", "chat_id": "chat-1"}


def _prepare_handoff(monkeypatch, tmp_path):
    handoff_dir = tmp_path / "handoff"
    monkeypatch.setattr(api_server, "HANDOFF_DIR", handoff_dir)
    monkeypatch.setattr(api_server, "HANDOFF_SIGNING_KEY", "test-secret")
    original = (
        handoff_dir
        / "user"
        / "user-1"
        / "chat"
        / "chat-1"
        / "message"
        / "msg-1"
        / "report.pdf"
    )
    original.parent.mkdir(parents=True)
    original.write_bytes(b"%PDF original")
    return original


class _FakeRequest:
    def __init__(self, body, headers):
        self._body = body
        self.headers = headers

    async def json(self):
        return self._body


def test_handoff_context_accepts_minimal_signed_path_metadata(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    sig = api_server._sign_handoff_entry("user-1", "chat-1", str(original))
    message = (
        "please inspect\n"
        f'<files><file original="{original}" sig="{sig}"/></files>'
    )

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert "<files>" not in out
    assert '<attached_files source="openwebui-skip-rag-handoff">' in out
    # Files are addressed by short handle. The signed path must NOT reach the
    # model at all: it is ~190 characters the model can only get wrong, and
    # every path it invented in the live incident was a variation on one it
    # had been shown.
    assert '<file id="F01" name="report.pdf"' in out
    # `original` stays until every deployed skill consumer has migrated to ids:
    # anything-to-docx still requires the literal /handoff path for SOC.
    assert f'original="{original}"' in out
    assert 'markdown="' not in out
    assert "%PDF original" not in out


def test_handoff_context_preserves_validated_file_id_and_sha256_metadata(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    sha256 = "83c00fb2636ab7323daef872ba0c786f57e833b44361abde31befa08d623829d"
    sig = api_server._sign_handoff_entry(
        "user-1",
        "chat-1",
        str(original),
        file_id="owui-file-1",
        sha256=sha256,
    )
    message = (
        "please inspect\n"
        f'<files><file original="{original}" file_id="owui-file-1" '
        f'sha256="{sha256}" sig="{sig}"/></files>'
    )

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert "<files>" not in out
    assert '<file id="F01" name="report.pdf" file_id="owui-file-1"' in out
    # sha256 is verified on the way in and is useless to the model, so it is
    # not re-emitted: 64 chars per file over a 46-file audit is pure noise.
    assert sha256 not in out
    assert f'original="{original}"' in out
    assert "%PDF original" not in out


def test_cross_user_replay_is_rejected(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    sha256 = "83c00fb2636ab7323daef872ba0c786f57e833b44361abde31befa08d623829d"
    sig = api_server._sign_handoff_entry(
        "user-1",
        "chat-1",
        str(original),
        file_id="owui-file-1",
        sha256=sha256,
    )
    message = (
        "please inspect\n"
        f'<files><file original="{original}" file_id="owui-file-1" '
        f'sha256="{sha256}" sig="{sig}"/></files>'
    )

    out = api_server._augment_message_with_handoff_context(
        message,
        {"user_id": "user-2", "chat_id": "chat-1"},
    )

    assert out == "please inspect"
    assert "<files>" not in out
    assert "<attached_files" not in out
    assert str(original) not in out
    assert "owui-file-1" not in out
    assert sha256 not in out


def test_handoff_context_rejects_tampered_sha256_metadata(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    good_sha256 = "83c00fb2636ab7323daef872ba0c786f57e833b44361abde31befa08d623829d"
    bad_sha256 = "0" * 64
    sig = api_server._sign_handoff_entry(
        "user-1",
        "chat-1",
        str(original),
        file_id="owui-file-1",
        sha256=good_sha256,
    )
    message = (
        "please inspect\n"
        f'<files><file original="{original}" file_id="owui-file-1" '
        f'sha256="{bad_sha256}" sig="{sig}"/></files>'
    )

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert out == "please inspect"
    assert "<files>" not in out
    assert str(original) not in out
    assert "owui-file-1" not in out


def test_legacy_markdown_signature_is_accepted_without_hydrating_markdown(
    monkeypatch,
    tmp_path,
):
    original = _prepare_handoff(monkeypatch, tmp_path)
    markdown = original.with_suffix(".pdf.md")
    markdown.write_text("SECRET MARKDOWN CONTENT", encoding="utf-8")
    sig = api_server._sign_legacy_handoff_entry(
        "user-1",
        "chat-1",
        str(original),
        str(markdown),
    )
    message = (
        "please inspect\n"
        f'<files><file name="report.pdf" user="user-1" chat="chat-1" '
        f'original="{original}" markdown="{markdown}" sig="{sig}"/></files>'
    )

    def fail_read(*args, **kwargs):
        raise AssertionError("handoff augmentation must not read file contents")

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    monkeypatch.setattr(Path, "read_text", fail_read)

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert "<files>" not in out
    assert '<file id="F01" name="report.pdf"' in out
    assert 'markdown="' not in out
    assert "SECRET MARKDOWN CONTENT" not in out


def test_invalid_handoff_block_is_stripped_without_exposing_path(monkeypatch, tmp_path):
    _prepare_handoff(monkeypatch, tmp_path)
    message = (
        "please inspect\n"
        '<files><file original="/etc/passwd" sig="fake"/></files>'
    )

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert out == "please inspect"
    assert "<files>" not in out
    assert "/etc/passwd" not in out
    assert "<attached_files" not in out


def test_multimodal_text_part_handoff_is_stripped_and_augmented(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    sig = api_server._sign_handoff_entry("user-1", "chat-1", str(original))
    content = [
        {
            "type": "text",
            "text": (
                f'look at this<files><file original="{original}" '
                f'sig="{sig}"/></files>'
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/image.png"},
        },
    ]

    out = api_server._augment_message_with_handoff_context(content, _scope())

    assert isinstance(out, list)
    assert out[1] == content[1]
    text = out[0]["text"]
    assert "<files>" not in text
    assert '<attached_files source="openwebui-skip-rag-handoff">' in text
    assert '<file id="F01" name="report.pdf"' in text
    assert 'markdown="' not in text


def test_empty_signing_key_strips_handoff_without_exposing_path(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    monkeypatch.setattr(api_server, "HANDOFF_SIGNING_KEY", "")
    message = (
        "please inspect\n"
        f'<files><file original="{original}" sig=""/></files>'
    )

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert out == "please inspect"
    assert "<files>" not in out
    assert str(original) not in out
    assert "<attached_files" not in out


@pytest.mark.asyncio
async def test_runs_api_handoff_input_becomes_path_only_metadata(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    sig = api_server._sign_handoff_entry("user-1", "chat-1", str(original))
    captured = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def run_conversation(
            self,
            user_message=None,
            conversation_history=None,
            task_id=None,
        ):
            captured["user_message"] = user_message
            from tools.file_grants import file_grant_error

            captured["grant_error"] = file_grant_error(
                str(original),
                task_id=task_id,
                operation="read",
            )
            captured["ungranted_error"] = file_grant_error(
                str(original.with_name("other.pdf")),
                task_id=task_id,
                operation="read",
            )
            return {"final_response": "ok"}

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())
    monkeypatch.setattr(
        api_server,
        "web",
        SimpleNamespace(
            json_response=lambda data, status=200, headers=None: SimpleNamespace(
                status=status,
                text=json.dumps(data),
                headers=headers or {},
            ),
        ),
    )
    request = _FakeRequest(
        {
            "input": (
                f'run this<files><file original="{original}" '
                f'sig="{sig}"/></files>'
            ),
        },
        {
            "X-OpenWebUI-User-Id": "user-1",
            "X-OpenWebUI-Chat-Id": "chat-1",
        },
    )

    response = await adapter._handle_runs(request)
    assert response.status == 202
    assert json.loads(response.text)["status"] == "started"

    for _ in range(50):
        if "user_message" in captured:
            break
        await asyncio.sleep(0.01)

    assert "user_message" in captured
    user_message = captured["user_message"]
    assert "<files>" not in user_message
    assert '<attached_files source="openwebui-skip-rag-handoff">' in user_message
    assert '<file id="F01" name="report.pdf"' in user_message
    assert 'markdown="' not in user_message
    assert captured["grant_error"] is None
    assert "not granted" in captured["ungranted_error"].lower()


@pytest.mark.asyncio
async def test_shared_api_executor_binds_exact_handoff_grants(monkeypatch, tmp_path):
    original = _prepare_handoff(monkeypatch, tmp_path)
    captured = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def run_conversation(self, user_message, conversation_history, task_id):
            from tools.file_grants import file_grant_error

            captured["allowed"] = file_grant_error(
                str(original),
                task_id=task_id,
                operation="read",
            )
            captured["denied"] = file_grant_error(
                str(original.with_name("other.pdf")),
                task_id=task_id,
                operation="read",
            )
            # The handle the <attached_files> block showed the model must
            # authorize the same file, and a handle that was never issued
            # must be denied like any other ungranted path.
            captured["handle_allowed"] = file_grant_error(
                "F01", task_id=task_id, operation="read"
            )
            captured["handle_denied"] = file_grant_error(
                "F99", task_id=task_id, operation="read"
            )
            from tools import request_file_cache

            captured["cache_scope_open"] = request_file_cache.is_active(task_id)
            return {"final_response": "ok"}

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_create_agent", lambda **kwargs: FakeAgent())

    result, _usage = await adapter._run_agent(
        user_message="inspect",
        conversation_history=[],
        session_id="session-1",
        user_id="user-1",
        chat_id="chat-1",
        granted_file_paths=[str(original)],
    )

    assert result["final_response"] == "ok"
    assert captured["allowed"] is None
    assert "not granted" in captured["denied"].lower()
    assert captured["handle_allowed"] is None
    assert "not granted" in captured["handle_denied"].lower()
    # The denial must name the handles the model may actually use.
    assert "F01" in captured["handle_denied"]
    assert captured["cache_scope_open"] is True


def test_handoff_guidance_refuses_names_from_outside_the_list(monkeypatch, tmp_path):
    """The block must tell the model where filenames may legitimately come from.

    The live incident's `read_file("…example-never-attached.docx")` was
    not degeneration: the caller's persistent USER.md said "use simplified file
    names (not full /handoff paths). Format: YYYYMMDD_Description.ext (e.g.
    "example-never-attached.docx" …)", and that memory is embedded
    verbatim into every session's system prompt. The model followed a naming
    instruction and had no authoritative list to name from.
    """
    original = _prepare_handoff(monkeypatch, tmp_path)
    sig = api_server._sign_handoff_entry("user-1", "chat-1", str(original))
    message = f'audit these<files><file original="{original}" sig="{sig}"/></files>'

    out = api_server._augment_message_with_handoff_context(message, _scope())

    assert "never invent a filename" in out
    assert "instruction, example" in out
    assert "the file was not attached" in out
    # And the roster is the authority the guidance points at.
    assert '<file id="F01" name="report.pdf"' in out
    assert "attachments()" in out
