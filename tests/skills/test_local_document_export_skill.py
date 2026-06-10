import re
from pathlib import Path


SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "productivity"
    / "local-document-export"
    / "SKILL.md"
)


def test_skill_frontmatter_and_description_contract():
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert re.search(r"^name: local-document-export$", text, re.MULTILINE)
    description = re.search(r"^description: (.*)$", text, re.MULTILINE).group(1)
    assert description.endswith(".")
    assert len(description) <= 60
    assert "local_document_export" in text


def test_skill_documents_local_only_openwebui_and_clean_reexport_warning():
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "Google Workspace" in text
    assert "OpenWebUI" in text
    assert "markdown links" in text
    assert "new clean document" in text
    assert "not a layout-preserving" in text


def test_skill_requires_verbatim_markdown_links_without_html_escaping():
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "Paste the exact" in text
    assert "markdown" in text
    assert "verbatim" in text
    assert "&amp;" in text
    assert "]( URL)" in text


def test_skill_requires_ascii_apostrophes_for_exact_legal_strings():
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "ASCII" in text
    assert "U+0027" in text
    assert "U+2019" in text
    assert "content_markdown" in text
