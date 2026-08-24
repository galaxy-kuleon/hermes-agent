import json

from agent.artifact_delivery import ensure_export_links_in_terminal_answer


def _export(markdown: str, *, success: bool = True) -> dict:
    return {
        "role": "tool",
        "name": "local_document_export",
        "tool_call_id": "export-1",
        "content": json.dumps({"success": success, "markdown": markdown}),
    }


def test_appends_latest_successful_export_link_to_terminal_answer():
    messages = [
        {"role": "user", "content": "Create the document"},
        _export("[Download first.docx](http://example/first)"),
        _export("[Download final.docx](http://example/final)"),
    ]

    answer, suffix = ensure_export_links_in_terminal_answer(
        "文件已完成。", messages, current_turn_user_idx=0
    )

    assert "first.docx" not in answer
    assert answer.endswith("[Download final.docx](http://example/final)")
    assert suffix == "\n\n下載檔案：\n[Download final.docx](http://example/final)"


def test_does_not_duplicate_link_already_in_terminal_answer():
    link = "[Download final.docx](http://example/final)"
    messages = [{"role": "user", "content": "Create it"}, _export(link)]

    answer, suffix = ensure_export_links_in_terminal_answer(
        f"文件已完成。\n\n{link}", messages, current_turn_user_idx=0
    )

    assert answer.count(link) == 1
    assert suffix == ""


def test_ignores_failed_malformed_and_previous_turn_exports():
    messages = [
        {"role": "user", "content": "Earlier request"},
        _export("[Download old.docx](http://example/old)"),
        {"role": "assistant", "content": "Done"},
        {"role": "user", "content": "Current request"},
        _export("[Download failed.docx](http://example/failed)", success=False),
        {
            "role": "tool",
            "name": "local_document_export",
            "content": "not-json",
        },
    ]

    answer, suffix = ensure_export_links_in_terminal_answer(
        "No artifact was produced.", messages, current_turn_user_idx=3
    )

    assert answer == "No artifact was produced."
    assert suffix == ""


def test_appends_only_missing_link_when_export_has_multiple_formats():
    docx = "[Download final.docx](http://example/final.docx)"
    pdf = "[Download final.pdf](http://example/final.pdf)"
    messages = [
        {"role": "user", "content": "Create both"},
        _export(f"{docx}\n{pdf}"),
    ]

    answer, suffix = ensure_export_links_in_terminal_answer(
        f"文件已完成。\n\n{docx}", messages, current_turn_user_idx=0
    )

    assert answer.count(docx) == 1
    assert answer.count(pdf) == 1
    assert suffix.endswith(pdf)
