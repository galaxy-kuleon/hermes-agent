# Smoke Test Log — 2026-06-09

Initial cultivation run for `pdf-docx-five-turn-refiner` on Noel's macOS Hermes host.

## Dependency Probe

Available:

```text
uv: /opt/homebrew/bin/uv
python3: /usr/bin/python3
pdftocairo: /opt/homebrew/bin/pdftocairo
pdfinfo: /opt/homebrew/bin/pdfinfo
soffice: /opt/homebrew/bin/soffice
PyMuPDF/fitz: installed
python-docx: installed
lxml: installed
Pillow: installed
requests: installed
```

`pdf2docx` was not globally installed, but `uv run` installed it from PEP 723 script metadata successfully.

## Corpus Discovery

Command:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  sample --root /Users/admin/Works --limit 12
```

Found useful PDF families immediately:

```text
/Users/admin/Works/pdf2docx/test/samples/*.pdf
/Users/admin/Works/docling/tests/data/vlm_docx_battle/*.pdf
/Users/admin/Works/docling/tests/data_scanned/*.pdf
/Users/admin/Works/docling/tests/data/pdf/*.pdf
/Users/admin/Works/GLM-OCR/input/*.pdf
/Users/admin/Works/pdf-to-docx-v5/examples/*.pdf
```

Notable multi-page stress candidate:

```text
/Users/admin/Works/pdf-to-docx-v5/examples/large.pdf — 80 pages, digital_ratio 1.0
```

## Route Detection Smoke Tests

Digital:

```bash
uv run .../pdfdocx5.py detect /Users/admin/Works/pdf2docx/test/samples/demo-text.pdf
```

Result:

```json
{
  "pages": 1,
  "digital_pages": 1,
  "digital_ratio": 1.0,
  "route": "xa",
  "total_text_chars": 1810
}
```

Scanned/OCR:

```bash
uv run .../pdfdocx5.py detect /Users/admin/Works/docling/tests/data_scanned/ocr_test.pdf
```

Result:

```json
{
  "pages": 1,
  "digital_pages": 0,
  "digital_ratio": 0.0,
  "route": "xb",
  "total_text_chars": 0
}
```

## XA Smoke Test

Command:

```bash
uv run .../pdfdocx5.py convert \
  /Users/admin/Works/pdf2docx/test/samples/demo-text.pdf \
  -o /tmp/pdfdocx5-smoke/demo-text.final.docx \
  --path auto --turns 2 \
  --work-dir /tmp/pdfdocx5-smoke/xa-work
```

Result:

```json
{
  "selected_route": "xa",
  "tool": "pdf2docx",
  "render_ok": true,
  "rendered_pages": 1,
  "source_pages": 1,
  "visible_text_chars": 1776,
  "paragraphs": 22,
  "tables": 0,
  "images": 0,
  "full_page_raster_risk": "low"
}
```

Output:

```text
/tmp/pdfdocx5-smoke/demo-text.final.docx
/tmp/pdfdocx5-smoke/demo-text.final.pdfdocx5.report.json
```

## XB Smoke Test and First Pitfall

First run on:

```text
/Users/admin/Works/docling/tests/data_scanned/ocr_test.pdf
```

Direct Ollama `glm-ocr:latest` returned a repeated Markdown-fenced sentence loop: ~57k chars for one source page, causing a 14-page rendered DOCX. This is not acceptable for the content-correct path.

Patch added:

- remove Markdown code fences
- collapse exact repeated word chunks
- warn through `ocr_clean_warnings`
- truncate suspiciously long per-page OCR with explicit marker

Retest command:

```bash
uv run .../pdfdocx5.py convert \
  /Users/admin/Works/docling/tests/data_scanned/ocr_test.pdf \
  -o /tmp/pdfdocx5-smoke/ocr-test.cleaned.final.docx \
  --path auto --turns 2 --max-pages 1 \
  --work-dir /tmp/pdfdocx5-smoke/xb-work2
```

Result:

```json
{
  "selected_route": "xb",
  "raw_text_chars": 57276,
  "text_chars": 532,
  "ocr_clean_warnings": ["collapsed_repetition:55830->532"],
  "render_ok": true,
  "rendered_pages": 1,
  "source_pages": 1,
  "visible_text_chars": 541,
  "paragraphs": 7,
  "images": 0,
  "full_page_raster_risk": "low"
}
```

Output:

```text
/tmp/pdfdocx5-smoke/ocr-test.cleaned.final.docx
/tmp/pdfdocx5-smoke/ocr-test.cleaned.final.pdfdocx5.report.json
```

## q7 Judge Smoke Test

Command:

```bash
uv run .../pdfdocx5.py qa \
  /Users/admin/Works/pdf2docx/test/samples/demo-text.pdf \
  /tmp/pdfdocx5-smoke/demo-text.final.docx \
  --work-dir /tmp/pdfdocx5-smoke/q7-qa \
  --judge-q7
```

Result:

- q7 endpoint worked with model `qwen3.6-35b-a3b-q7` and API key `change-me-local-key`.
- It returned structured JSON-like output, but used `human_acceptability: false` instead of the requested enum, so future code should normalize/validate q7 judge schemas.
- q7 flagged duplicate-body-text and formatting defects in the pdf2docx output. This confirms the judge is useful for adversarial/human-POV review.

## Next Improvements

1. Add upstream GLM-OCR SDK mode as an XB option; direct `/api/generate` is useful but can loop.
2. Add q7 judge schema normalization and validation.
3. Add deterministic patch type: duplicate paragraph removal gated by source text evidence.
4. Add multi-page sampling policy: first/last + risk pages for q7, all pages for deterministic QA.
5. Add DOCX XML patch modules for table grid widths and image anchors.
