from tools.document_text_safety import (
    INLINE_IMAGE_PLACEHOLDER,
    strip_inline_base64_images,
)


def test_strips_markdown_inline_base64_without_losing_text(caplog):
    payload = "A" * 2_000_000
    source = f"before\n![page](data:image/png;base64,{payload})\nafter"

    cleaned = strip_inline_base64_images(source, source="claim.pdf")

    assert "before" in cleaned and "after" in cleaned
    assert payload not in cleaned
    assert "base64," not in cleaned
    assert INLINE_IMAGE_PLACEHOLDER in cleaned
    assert "occurrences=1" in caplog.text
    assert payload[:100] not in caplog.text


def test_strips_html_and_raw_inline_base64_uris():
    source = (
        '<img src="data:image/jpeg;base64,QUJDRA==">\n'
        "raw=data:image/webp;base64,RUZHSA=="
    )

    cleaned = strip_inline_base64_images(source, source="docling.md")

    assert cleaned.count(INLINE_IMAGE_PLACEHOLDER) == 2
    assert "base64," not in cleaned


def test_non_image_base64_and_normal_text_are_unchanged():
    source = "download=data:application/pdf;base64,QUJD\nnormal text"
    assert strip_inline_base64_images(source) == source
