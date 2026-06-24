# hifi-pdf2docx clean-scan MCP slice — 2026-06-12

## Trigger

Use this reference when improving or operating a PDF→DOCX MCP/server workflow for scanned, photoed, or hybrid image-backed PDFs, especially when a previous output looks like stacked/duplicated text.

## User correction captured

Noel corrected two workflow issues:

1. When he says “attachment”, run the actual uploaded attachment/file, not a synthetic or nearby corpus PDF unless explicitly asked.
2. For scanned/photoed pages, do **not** preserve the dirty page background by default. Humans expect a clean white A4 page. Preserve only localized visual assets such as logos, charts/photos/diagrams, signatures, stamps/chops/seals, QR/barcodes, intentional watermarks, and necessary form controls.

## Failure pattern observed

The old `scan_tbx` strategy could produce visually unusable DOCX renders:

```text
source raster/ink background
+ OCR text boxes
+ residual ink/graphics overlay
= stacked text / duplicate-looking content
```

Mechanical checks such as page count, alignment, or low-confidence-line WARN can pass while the human-visible result is not client-usable. Treat visual side-by-side review as required before success claims.

## Better contract

For scanned/photoed/hybrid pages, default to:

```text
clean white page
+ editable OCR/native text
+ native tables/lines/shapes where reliable
+ localized crop_keep visual assets only
+ QA/report for unresolved low-confidence content
```

Do not include:

- full-page raster body as a success path
- grey/dirty paper, shadows, wrinkles, JPEG noise
- original black text ink already converted to editable text
- residual ink blobs from imperfect masks

## Ledger shape

Use explicit ledgers so agents and QA can audit why something became text, shape, crop, or dropped noise:

```json
{
  "page_route": "digital|scanned|hybrid_image_backed|mixed",
  "text_lines": [
    {"page": 1, "bbox": [x0, y0, x1, y1], "text": "...", "confidence": 0.93, "source": "native_pdf|mac_vision|glm_ocr|q7_adjudicated", "status": "verified|review|unreadable"}
  ],
  "visual_obligations": [
    {"page": 1, "bbox": [x0, y0, x1, y1], "kind": "signature|stamp|logo|chart|qr|watermark|table_rule|form_line|noise|text_ink|paper_background", "action": "crop_keep|reconstruct_shape|drop_noise|ocr_text", "reason": "..."}
  ]
}
```

Only `crop_keep` regions should become DOCX images. `table_rule`/`form_line` should prefer native Word/OOXML line/table reconstruction. `noise`, `text_ink`, and `paper_background` should not enter the final DOCX.

## MCP/server tool pattern

Prefer explicit small tools over one opaque `convert_pdf` step:

```text
preflight_pdf
extract_ocr
detect_visual_obligations
convert_pdf
review_flagged
review_assets
render_qa
get_report
explain_failure
```

Keep `convert_pdf` backward-compatible, but require it to write ledgers and honest QA results.

## QA gates

A “nice”/production result should include:

- route evidence: digital vs scanned vs hybrid image-backed
- no full-page raster fallback gate
- editable text coverage gate
- visual obligation coverage gate
- DOCX→PDF render with isolated LibreOffice profile
- contact sheet / side-by-side visual evidence
- final status: `PASS | NEEDS_REVIEW | FAIL`

Do not let `WARN/PASS` mechanical checks hide a visually stacked or screenshot-backed output.

## Verified vertical slice from the session

A Codex tmux implementation slice added:

- ledger dataclasses for page routes, OCR lines, visual obligations
- page-level classifier including `hybrid_image_backed`
- `scan_clean` default for scanned/photoed pages
- `scan_tbx_legacy` for old ink-overlay behavior
- `no_full_page_raster` and contact-sheet QA gates
- new MCP tools: `preflight_pdf`, `extract_ocr`, `detect_visual_obligations`, `render_qa`, `explain_failure`

Verification evidence in that session:

```text
Host tests: 8 passed
Host py_compile: PASS
MCP import smoke: PASS
Container py_compile: PASS
hermes mcp test hifi_pdf2docx: 10 tools discovered
Live Hermes API preflight smoke: scanned route, max_image_area_ratio 0.9706
Direct container clean-scan conversion smoke: PASS, no_full_page_raster PASS, contact_sheet PASS
```

Remaining gap from the slice: `reconstruct_shape` obligations were detected but not yet compiled into native Word line/shape OOXML.
