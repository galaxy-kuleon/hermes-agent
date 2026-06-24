# hifi-pdf2docx MCP: clean scanned/photoed output pattern

Session lesson from integrating `galaxy-kuleon/hifi-pdf2docx` into the kg-openwebui-stack-v2 Hermes container and running a Mac Vision OCR scanned-page smoke.

## Problem observed

A scanned/image-only CJK page converted through the current `scan_tbx` style pipeline produced a DOCX render where too many layers were stacked:

```text
source raster / residual black ink
+ OCR text boxes
+ graphics overlays from "ink minus text mask"
= duplicated/ghosted/overlaid text that is not client-usable
```

Mechanical checks such as page count, alignment, and low-confidence WARN can pass while the visual output is obviously unusable. Treat this as a false-success class.

## Product rule

For scanned/photoed PDFs, the default human expectation is a clean white A4/page background. Dirty paper, shadows, wrinkles, scan noise, JPEG artifacts, and text ink that has already been OCR'ed are not document obligations.

Default reconstruction should be:

```text
clean white DOCX page
+ editable OCR/native text
+ native tables/lines/shapes where reliable
+ localized image crops only for real visual obligations
+ QA appendix/report for unresolved or low-confidence content
```

## Keep as localized image crops only when needed

Whitelist visual obligations:

- logo / letterhead mark
- chart, diagram, photo, illustration
- handwritten signature
- stamp, chop, seal
- QR code / barcode
- intentional watermark
- highlight/callout only when not safely reconstructable as a Word shape
- form controls only when not safely reconstructable as Word shapes

## Do not preserve as image by default

- full-page raster background
- grey/dirty scanned paper
- camera shadow or page curl/wrinkle
- JPEG/compression noise
- black text ink already represented as editable OCR text
- residual ink blobs from imperfect text masks
- full-page screenshot fallback as a success path

## Recommended implementation contract

Move from implicit layer compositing to ledgers:

1. **Page route ledger** — digital/scanned/hybrid_image_backed per page, with native-text and image-area evidence.
2. **Text/content ledger** — each line has bbox, text, confidence, source (`native_pdf`, `mac_vision`, `glm_ocr`, `q7_adjudicated`), crop evidence, and status (`verified`, `review`, `unreadable`).
3. **Visual obligation ledger** — each non-text region has kind and action:
   - `crop_keep` for signatures/stamps/logos/charts/photos/QR/watermarks
   - `reconstruct_shape` for table rules/form lines/highlights when possible
   - `drop_noise` for paper/background/shadows/dirt
   - `ocr_text` for text ink regions

Only `crop_keep` regions should be inserted as images. Large near-page-sized crops are forbidden as final success artifacts except in explicit debug/reference mode.

## MCP/tooling implication

The MCP server should expose small auditable steps rather than one opaque conversion:

- `preflight_pdf`
- `extract_ocr`
- `detect_visual_obligations`
- `convert_pdf`
- `review_flagged` (bounded q7 crop review)
- `review_assets` (bounded ambiguous-asset review)
- `render_qa`
- `get_report`
- `explain_failure`

`convert_pdf` may still orchestrate the default flow, but it must emit ledgers and cannot return final success unless visual/editability/content gates pass.

## QA gates to add

A high-fidelity claim requires at least:

- no full-page raster / screenshot-backed output
- editable text coverage gate
- visual obligation coverage gate
- DOCX→PDF render with private LibreOffice profile
- contact sheet for human side-by-side review
- low-confidence legal text handled as `NEEDS_REVIEW`, not `PASS`

Use `PASS | NEEDS_REVIEW | FAIL` rather than optimistic `PASS/WARN` when visual review or content truth is not final.
