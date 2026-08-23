import json

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import document_extract_cache


def test_cache_follows_hermes_profile_not_process_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "container-home"))
    source_sha256 = "a" * 64
    first_home = tmp_path / "profile-one"
    second_home = tmp_path / "profile-two"

    token = set_hermes_home_override(first_home)
    try:
        assert document_extract_cache.remember(
            source_sha256,
            ".pdf",
            text="完整抽取內容",
            file_size=123,
            gaps=["page_images_not_returned"],
        )
        assert document_extract_cache.lookup(source_sha256, ".pdf")["text"] == "完整抽取內容"
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(second_home)
    try:
        assert document_extract_cache.lookup(source_sha256, ".pdf") is None
    finally:
        reset_hermes_home_override(token)

    assert not (tmp_path / "container-home" / "cache").exists()


def test_invalid_cache_entry_is_a_miss_not_a_document_failure(tmp_path):
    source_sha256 = "b" * 64
    token = set_hermes_home_override(tmp_path)
    try:
        path = document_extract_cache._cache_path(source_sha256, ".pdf")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"text": "untrusted without identity"}), encoding="utf-8")
        assert document_extract_cache.lookup(source_sha256, ".pdf") is None
        assert document_extract_cache.remember(
            source_sha256,
            ".pdf",
            text="recovered extraction",
            file_size=456,
        )
        assert document_extract_cache.lookup(source_sha256, ".pdf")["text"] == "recovered extraction"
    finally:
        reset_hermes_home_override(token)
