---
name: pdf-docx-five-turn-refiner
description: "Hermes-compatible PDF→DOCX workflow: route digital PDFs through pdf2docx (Path XA), route scanned/non-digital PDFs through pdftocairo + GLM-OCR/VLM into a content-correct DOCX (Path XB), then refine the candidate DOCX with deterministic Python/lxml/OOXML and render QA within five turns. Use whenever Noel asks for pragmatic PDF to DOCX conversion, XA/XB/XC paths, five-turn DOCX fidelity repair, pdf2docx, GLM-OCR, or local q7 visual judging."
tags: [pdf, docx, pdf2docx, glm-ocr, vlm, ooxml, lxml, five-turns, hermes-compatible]
version: 0.1.0
created_by: agent
---

# PDF→DOCX Five-Turn Refiner

## Goal

Build a pragmatic, Hermes-compatible PDF→DOCX workflow that avoids the previous trap of trying to solve high fidelity in one monolithic conversion step.

Use three paths:

```text
PDF
├── Path XA: digital/born-digital PDF → pdf2docx → editable candidate DOCX
├── Path XB: scanned/image PDF → pdftocairo PNG → GLM-OCR/Ollama + VLM hints → content-correct DOCX
└── Path XC: candidate DOCX → Python/lxml/OOXML refiner → render QA → ≤5 repair turns → final DOCX
```

The governing principle:

```text
XA/XB = produce an editable candidate
XC    = make it look right
QA    = prove it is not cheating and not useless
```

Do **not** let XA/XB chase perfect layout. XA/XB should create a stable, editable candidate that the deterministic refiner can improve.

## Default Local Toolchain

Prefer this stack on Noel's machine:

- `uv` for Python dependency isolation and script execution
- Python 3
- Poppler utils: `pdftocairo`, `pdfinfo`, optionally `pdftotext`
- `pdf2docx` for digital PDF Path XA
- `PyMuPDF` / `fitz` for PDF text/layout evidence and digital/scanned detection
- `python-docx` for DOCX construction and coarse editing
- `lxml` for OOXML patching inside the DOCX zip
- LibreOffice `soffice` for DOCX→PDF render verification
- q7 VLM judge:
  - host/macOS default: `http://localhost:11234/v1`
  - Docker/8083 Hermes container default: `http://host.docker.internal:11234/v1`
  - model `qwen3.6-35b-a3b-q7`, API key `change-me-local-key`
  - override with `PDFDOCX5_Q7_BASE_URL`, `PDFDOCX5_Q7_MODEL`, `PDFDOCX5_Q7_API_KEY`
- GLM-OCR via Ollama:
  - host/macOS default: `http://localhost:11434`
  - Docker/8083 Hermes container default: `http://host.docker.internal:11434`
  - preferred model names: `glm-ocr:latest`, fallback `glm-ocr`
  - override with `PDFDOCX5_OLLAMA_URL`, `PDFDOCX5_GLM_MODELS`
- Optional macOS Vision OCR bridge from `~/hifi-vision-bridge`:
  - host endpoint: `http://127.0.0.1:18765` (`GET /health`, `POST /ocr?langs=zh-Hant,en-US`)
  - returns line boxes in rendered-image pixels; convert back to page points using `scale=dpi/72`
  - keep local-only by default; remote OCR endpoints can exfiltrate confidential PDFs
  - use as bounded content-truth/backstop and geometry evidence, especially for CJK; do not blindly trust low-confidence English legal lines (`A ward`, `Panticipant` artifacts were observed)
  - when using Vision boxes to shrink q7 layout textboxes, require exact normalized text coverage; partial Vision coverage may be useful as QA evidence but must not shrink a q7 paragraph, or rendered DOCX can truncate legal text

Run the bundled helper with uv:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py --help
```

## Test Corpus Discovery

Noel's machine has many useful PDFs under `~/Works`. Start there, but do not inherit old assumptions from previous high-fidelity experiments.

Good initial sample families:

```text
~/Works/pdf2docx/test/samples/*.pdf
~/Works/docling/tests/data/vlm_docx_battle/*.pdf
~/Works/docling/tests/data_scanned/*.pdf
~/Works/docling/tests/data/pdf/*.pdf
~/Works/GLM-OCR/ir-schema/tests/*.pdf
```

A curated local manifest is maintained at:

```text
~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/references/battle-test-corpus.json
```

It currently covers small digital text, digital tables, multi-page overflow/table behavior, scanned OCR, and lattice-table fixtures. Smoke scripts skip missing paths gracefully and skip `slow: true` items unless `--include-xb` is used.

Use the bundled sampler:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py sample --root ~/Works --limit 30
```

Pick at least:

- one small born-digital text PDF
- one table-heavy digital PDF
- one scanned/OCR-required PDF
- one multi-page PDF
- one adversarial/mixed PDF if time allows

## Path Selection

Use page-level detection, not just document-level detection, because real PDFs can be mixed.

Detection heuristic:

1. Open with PyMuPDF.
2. For each page, inspect `page.get_text("text").strip()` **and image placement area** via `page.get_images(full=True)` + `page.get_image_rects(xref)`.
3. If most pages have meaningful native text and no page-sized raster body, route to XA.
4. If most pages have little/no text, route to XB.
5. If a page has a page-sized image (≈65%+ page area) plus only sparse selectable text, classify it as `hybrid_image_backed_pdf` and route to XB. Typical trap: DocuSign IDs, signature names/titles, or dates are selectable, but the contract body is a full-page image.
6. If mixed, split or process page ranges separately; default to XB when translation/editability is requested unless the user explicitly accepts an image-backed DOCX.

Rule of thumb:

```text
page-sized image + sparse text layer → hybrid_image_backed_pdf → XB/OCR-VLM
digital_ratio >= 0.70 and no page-image trap → XA
0.20 <= digital_ratio < 0.70 → mixed; prefer XB for translation/editability
otherwise → XB
```

Never treat `pdftotext` returning *some* text as proof that the PDF body is born-digital. Run `pdfdocx5.py detect` and inspect `classification`, `image_backed_pages`, `max_image_area_ratio`, and `warnings` before choosing XA.

## Path XA: Digital PDF → pdf2docx

XA is for born-digital PDFs with native text.

Command:

```bash
uv run --with pdf2docx --with pymupdf --with python-docx --with lxml \
  ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  convert input.pdf -o output.docx --path xa --turns 5
```

Acceptance criteria for XA candidate:

- DOCX opens in LibreOffice/Word
- visible text exists in `word/document.xml`
- page count is plausible
- no full-page screenshot-only cheating
- tables/images may be rough; do not over-optimize here

## Path XB: Scanned/Image PDF → PNG/SDK OCR → Deterministic Editable DOCX

XB is for scanned/non-digital/hybrid image-backed PDFs. Its first goal is content correctness with editable text; high-fidelity layout is improved by deterministic refiner passes and bounded VLM suggestions.

**Deterministic-first rule:** for non-digital PDFs, keep the pipeline structured and validator-driven. Render pages, collect layout/OCR evidence, build an IR/candidate DOCX deterministically, then use q7/122B only for OCR/defect reports/bounded repair suggestions. Do not let an agent improvise with `vision_analyze` summaries, generic paragraph scripts, or file-existence success. See `references/deterministic-non-digital-pdf-docx.md`.

Two OCR engines are supported:

```text
--ocr-engine auto   # default: try upstream GLM-OCR SDK first, fallback to direct page-image OCR if weak
--ocr-engine sdk    # force upstream GLM-OCR SDK parse via ~/Works/GLM-OCR, --extra layout, selfhosted Ollama
--ocr-engine direct # render pages with pdftocairo, call Ollama /api/generate per image, then clean loops
```

Preferred flow:

```text
PDF → GLM-OCR SDK parse (layout CPU + glm-ocr:latest via Ollama) → Markdown/JSON → simple DOCX
```

Direct fallback flow:

```text
PDF → pdftocairo -png → page PNGs → GLM-OCR/Ollama or q7 fallback → simple structured text/table IR → python-docx DOCX
```

Command:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  convert scanned.pdf -o output.docx --path xb --turns 5 --ocr-engine auto
```

For smoke tests on large/multi-page scanned PDFs, bound work explicitly:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  convert scanned.pdf -o output.docx --path xb --max-pages 1 --ocr-engine sdk
```

XB acceptance criteria:

- OCR text is present and editable in DOCX
- page breaks are preserved
- tables are represented as native tables when reliably detected; otherwise use paragraphs first
- optional page thumbnails may be included for visual reference only, but do not claim that as editable output
- output must be renderable by LibreOffice

## Path XC: Five-Turn DOCX Refiner

XC receives a candidate DOCX from XA or XB and improves it using deterministic OOXML operations plus render feedback.

Maximum five repair turns:

1. **Normalize OOXML / document shell**
   - unpack DOCX
   - set page size and margins from source PDF
   - ensure sane `styles.xml`
   - remove broken/invalid XML patterns where possible
   - verify LibreOffice can render

2. **Repair page geometry**
   - compare source PDF page count/size with rendered DOCX
   - patch section properties, margins, page breaks, obvious orientation errors

3. **Repair typography/tables/images**
   - normalize font family/size if wildly wrong
   - set paragraph spacing/line spacing
   - patch table grid widths, borders, cell margins
   - preserve image relationships and adjust sizes/anchors where deterministic evidence exists

4. **VLM defect-targeted patching**
   - render source PDF and current DOCX to PNG
   - ask q7 to produce structured human-visible defects
   - only apply whitelisted patch types; do not let the VLM write arbitrary OOXML

5. **Final QA / anti-cheating gate**
   - verify visible editable text exists
   - check for screenshot-only output
   - render final DOCX to PDF/PNG
   - write a defect report explaining acceptability and remaining work

The refiner may stop early if render QA passes.

## Anti-Cheating QA

Never call a DOCX successful just because the file exists.

Check at minimum:

- `word/document.xml` contains visible text
- output is not only a full-page raster image
- tables use native `w:tbl` where table evidence exists
- DOCX→PDF render works through LibreOffice
- source/rendered page counts are plausible
- human-POV defect report exists for high-fidelity claims

For scanned PDFs, it is acceptable for the first XB candidate to be visually rough if OCR text is correct. It is **not** acceptable to silently hide all content inside page screenshots and call it editable.

### Clean scanned/photoed default

For scanned, photoed, or hybrid image-backed pages, treat a clean white A4/page background as the default. Do **not** preserve dirty paper, grey scan background, shadows, wrinkles, JPEG noise, original text ink, or residual OCR-mask blobs. Preserve only localized visual obligations as images (logo, chart/photo/diagram, signature, stamp/chop/seal, QR/barcode, intentional watermark, or necessary form controls). Prefer native Word/OOXML tables, borders, and line shapes for form/table rules. If an output looks like OCR text boxes stacked over source ink/background, mark it `FAIL` or `NEEDS_REVIEW` even when page-count/alignment checks pass.

Use the ledger-driven pattern in `references/hifi-clean-scan-mcp-2026-06-12.md` when implementing/operating MCP-backed PDF→DOCX tools: preflight route evidence, OCR text ledger, visual-obligation ledger, `no_full_page_raster` gate, contact-sheet render QA, and final `PASS | NEEDS_REVIEW | FAIL` status.

### Translation false-success gate

For PDF→translated-DOCX tasks, add language/content QA on top of the normal render/editability checks:

- Correct PDF route detection is not enough. A live OpenWebUI regression showed `hybrid_image_backed_pdf → xb` detection succeeded, but the agent later drifted into manual `pdftoppm + vision_analyze` summary extraction and exported an untranslated DOCX.
- Do not treat a VLM summary as complete OCR. XB must preserve per-page raw OCR evidence (`raw_text_chars`, `text_chars`, OCR engine/model) or an equivalent audit trail.
- Do not use old simple fallback scripts for unrelated targets, especially `translate_docx_simple.py ... English`; zero-change or dictionary-fallback translation is a hard fail.
- For English targets, inspect `word/document.xml` for residual CJK and compare source/output text. If the output is unchanged or still mostly CJK, report `FAILED_QA` even if the DOCX downloads and renders.
- If available, run a fail-fast checker such as `verify_translated_docx.py source.docx output.docx English` before exporting.

See `references/openwebui-live-hybrid-translation-regression-2026-06-10.md` for the concrete live regression and guard pattern. See also `references/mnda-hybrid-qa-pass-vector-border-2026-06-10.md` for the later QA-pass repair pattern: direct GLM-OCR content extraction, vector `w:pgBorders` scan-boundary reconstruction instead of screenshot backing, isolated LibreOffice render profiles, and translation-gate verification. For the OpenWebUI 8083 browser-like E2E variant (file upload API → Path B handoff → Hermes container conversion/translation → `/api/exports` verification), see `references/mnda-hybrid-openwebui-e2e-2026-06-10.md`.

## Multi-Page Policy

Many PDFs have many pages. Do not send every page image to the VLM unless the user explicitly asks for exhaustive QA.

Default policy:

- process all pages for deterministic XA/XB conversion where feasible
- render all pages if cheap
- VLM-judge sampled pages by default: first page, last page, and a middle page
- use `--judge-pages sample`, `--judge-pages risk`, `--judge-pages all`, or explicit `--judge-pages 1,3-5`
- `risk` adds highest-risk pages by cheap PyMuPDF evidence: tables, images, drawings, and low native-text coverage
- expose `--max-pages` for bounded smoke tests; when `--max-pages N` is used, the helper creates a `qa_source_subset.pdf` so page-count QA compares against the same bounded source subset instead of the full original PDF
- always report whether QA was sampled or exhaustive through `q7_judge_policy`

Example:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py qa \
  source.pdf output.docx --judge-q7 --judge-pages sample
```

## Deterministic Patch Types Added So Far

- `--dedupe-paragraphs`: conservative exact duplicate paragraph removal gated by source text evidence. It only removes repeated candidate paragraphs when the same normalized paragraph appears fewer times in the source text. It is off by default because deletion patches must be conservative.
- `--compact-on-overflow`: when rendered DOCX has more pages than the QA source, retry up to five turns with progressively tighter margins, paragraph spacing, line spacing, font-size/image scaling, and stepwise empty layout paragraph removal. Round11 changed this to avoid stacked compaction and to choose the best-scored turn instead of blindly returning the last turn; this directly targets `page_count_delta > 0` without using VLM edits.
- `apply-suggestions`: applies q7 `fix_suggestions` through whitelisted deterministic adapters (`normalize_indentation`, `change_font`, source-gated `delete_paragraphs`, `normalize_tables`, `text_replacement`).
- `--normalize-tables`: normalizes native DOCX tables with OOXML-backed width, borders, cell margins, and paragraph spacing; useful before/after table_col_width/table_cell_border q7 suggestions.
- `--recover-missing-text`: when QA detects `editable_text_coverage_low|medium`, add compact editable source text as a fallback for pdf2docx overlap-loss cases. If coverage is very low, Round16 inserts source page-1 text before the first explicit page/section break (`page1_inline_plus_remaining_fallback`) rather than only appending at the end; this can turn a visually empty first page into a content-filled page while still reporting changed-pixel failures. Round17 makes recovered overflow jump directly to empty-paragraph-removal compact profiles so the five-turn budget can restore page-count parity. Round19 fixed the insertion anchor to ignore ordinary `<w:br/>` line breaks and only use true page/section boundaries (`w:sectPr`, page breaks, `lastRenderedPageBreak`). It is explicitly not a high-fidelity layout repair. Auto battle enables this for XA cases so overlap text loss is surfaced as recovered-content/visual-layout failure rather than silent content loss.
- `--xa-line-overlap-threshold 0.99`: Round20 experimental XA root-fix lane for overlap-heavy forms. It passes pdf2docx `line_overlap_threshold=0.99`, recovering much more native text and reducing `visual_changed_fraction_extreme` without plaintext fallback, but may introduce content-table delta that still needs repair.
- `--preserve-candidate-layout`: Round20 refiner mode that skips the generic compact profile at compact level 0. Use with overlap-tolerant XA when raw pdf2docx geometry is better than the normalizer; generic profile can collapse table/ink placement on these forms.
- `--xa-strategy auto|default|overlap099`: Round22 XA lane chooser. `auto` runs `default_recovery` (normal pdf2docx + table normalization + missing-text recovery) and `overlap099` (`line_overlap_threshold=0.99` + preserve layout, no plaintext recovery), then picks the lower QA score. Use this for autonomous battle runs so overlap-heavy forms can choose the native-layout lane while ordinary documents keep the safer default lane.
- editable token coverage QA: Round21 adds `editable_token_coverage` and prefers token occurrence coverage over raw visible-char ratio for digital PDFs. This avoids false medium coverage penalties when pdf2docx preserves source words but OOXML/PDF whitespace and duplicated form labels differ.
- visual table-alignment relaxation: Round21 keeps table-geometry detector violations in the report but does not score them as hard failures when `content_table_count_delta == 0`, sampled visual ink bbox alignment is tight (`max_abs_ink_bbox_delta <= 0.05`), and changed fraction is not extreme (`<0.25`). This handles PyMuPDF rendered-PDF table pairing artifacts on dense government forms.
- document-shell normalization: page size from PDF, 0.75 inch margins, default Normal font, paragraph spacing/line spacing sanity unless `--preserve-candidate-layout` is active.
- image-backed scan boundary repair: for PDFs with page-sized raster bodies, the refiner may add an OOXML vector page border (`w:pgBorders`) to reconstruct scanned/DocuSign page-edge ink without embedding a full-page screenshot. This fixes honest ink-bbox layout QA for hybrid PDFs while keeping the DOCX editable and non-raster-backed.
- LibreOffice render isolation: `docx_to_pdf` should pass a per-render `-env:UserInstallation=file://...` profile. In the 8083 Hermes container, stale/root-owned `~/.config/libreoffice` state can hang `soffice` for the `hermes` user; isolated profiles make QA deterministic.

## Visual QA Contact Sheets

Use `--contact-sheet` to produce a human-reviewable side-by-side PNG artifact showing selected source PDF pages next to rendered DOCX pages:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py qa \
  source.pdf output.docx --contact-sheet --judge-pages risk
```

The report includes:

```json
"contact_sheet": "/path/to/contact_sheet.png",
"visual_diff_metrics": [
  {
    "page": 1,
    "source_vs_rendered": {
      "mean_abs_rgb_delta": 15.739,
      "rmse_rgb_delta": 41.822,
      "changed_channel_fraction_gt24": 0.16085
    }
  }
]
```

For patch workflows, use `compare` to create a three-column source/before/after contact sheet:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py compare \
  source.pdf before.docx after.docx --work-dir /tmp/pdfdocx5-compare --judge-pages risk
```

The compare report includes:

```json
"before_after_contact_sheet": "/path/to/before_after_contact_sheet.png",
"visual_diff_metrics": [
  {
    "source_vs_before": {...},
    "source_vs_after": {...},
    "before_vs_after": {...}
  }
],
"diff_heatmaps": [
  {"page": 1, "kind": "source_vs_after", "path": "/path/to/source_vs_after_p1.png"}
]
```

When tables are detected, contact sheets draw table bbox overlays:

```text
red     = source table bbox
orange  = before DOCX rendered table bbox
blue    = after/rendered DOCX table bbox
```

This is intentionally separate from q7: humans can inspect the visual delta without paying VLM time. It uses the same `--judge-pages sample|risk|all|1,3-5` selector.

## Page Risk and Table QA

QA also emits a composite `failure_score` so low pixel-diff RMSE cannot hide editability/page/table failures:

```json
"failure_score": {
  "score": 35,
  "severity": "minor",
  "reasons": [
    {"type": "table_count_delta", "points": 15},
    {"type": "table_geometry_status", "points": 12}
  ]
}
```

Score ingredients include page count delta, content-table count delta, layout-table overuse, table geometry status, editable-text coverage, visual ink/layout coverage, changed-pixel fraction, raster risk, render failure, and high visual RMSE. Raw DOCX table count is split into `docx_content_tables` and `docx_layout_tables` because pdf2docx often emits 1-row header/footer/section-title tables that should not be counted as semantic table drift. Government/IP forms add extra furniture rules for one-row title banners, general notes, personal-data notices, `Note N` blocks, and long text-field tables such as novelty/confidential-disclosure statements. Round14 adds ink-bbox/height/changed-fraction penalties so table parity cannot hide a visually empty or content-shifted rendered page. Round18 treats `changed_channel_fraction_gt24 >= 0.25` as `visual_changed_fraction_extreme` (24 pts) because human inspection showed dense plaintext fallback can otherwise be misclassified as `ok` despite obvious layout defects.

The helper collects cheap PyMuPDF page evidence during QA:

```json
pdf_evidence_summary: {
  pages_sampled,
  source_tables_detected,
  source_images_detected,
  max_risk_score,
  top_risk_pages
}
table_qa: {
  source_tables_detected,
  docx_native_tables,
  table_count_delta,
  has_native_tables_when_source_tables_detected
}
table_geometry_qa: {
  source_tables,
  rendered_tables,
  table_count_delta,
  source_geometry,
  rendered_geometry,
  paired_geometry
}
```

`table_geometry_qa` compares detected source PDF table bbox/row/column estimates against tables detected in the rendered DOCX PDF. It is still heuristic, but useful for flagging table count and rough geometry drift.

QA also computes `table_geometry_status` with threshold violations:

```json
"table_geometry_status": {
  "ok": true,
  "bbox_tolerance": 0.18,
  "violations": []
}
```

Control tolerance with:

```bash
--table-bbox-tolerance 0.18
```

Use `--judge-pages risk` to ask q7 to inspect first/last/middle plus the highest-risk pages by tables/images/drawings/low text.

## q7 Image + OOXML Repair Suggestions

Yes: q7 can compare the input PDF page PNG, rendered output DOCX page PNG, and compact DOCX OOXML excerpts to suggest concrete fixes. For a reified MCP-server pattern that combines python-docx, direct DrawingML/OOXML text boxes/images, local q7 OCR, q7 render QA, strict content gates, rotation handling, and fail-closed review, see `references/drawingml-mcp-server-pattern.md`.

Use:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py qa \
  source.pdf output.docx --judge-q7 --judge-pages 1 --q7-xml-context \
  > q7.report.json
```

This sends source page image + rendered DOCX page image + selected `word/document.xml`/`styles.xml` snippets: section properties, first tables/drawings, paragraph properties/text summaries, and Normal style. q7 returns both `defects` and `fix_suggestions` with `patch_type`, `target`, `rationale`, `ooxml_hint`, and `risk`.

### Reified q7 Suggestion Adapters

q7 suggestions can now be applied through whitelisted deterministic adapters:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py apply-suggestions \
  source.pdf input.docx q7.report.json -o patched.docx
```

Supported patch adapters:

```text
normalize_indentation  # OOXML-backed clamp for targeted absurd paragraph indents
change_font            # sets ascii/hAnsi/eastAsia/cs run fonts such as Courier New on targeted paragraphs
delete_paragraphs      # source-gated duplicate deletion; skips unless candidate/source evidence permits
normalize_tables       # table width/borders/cell margins/spacing normalization for native DOCX tables
text_replacement       # targeted low-risk text cleanup, including spaces before punctuation
```

There is also an in-loop option:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py refine \
  source.pdf input.docx -o refined.docx --judge-q7 --q7-xml-context \
  --apply-q7-suggestions --turns 5
```

If q7 produces suggestions on the final requested turn and the five-turn budget is not exhausted, the helper can apply one deterministic q7 patch and immediately run a post-patch QA (`post_q7_patch_qa`) instead of discarding the final-turn suggestion.

For offline regression tests without live q7, inject a mock q7-style report:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py refine \
  source.pdf input.docx -o refined.docx \
  --turns 2 --apply-q7-suggestions --mock-q7-report mock-q7.json
```

`mock-q7.json` may contain either:

```json
{"fix_suggestions": [{"patch_type": "table_col_width", "target": "all native tables", "risk": "low"}]}
```

or a normal nested q7 report with `q7_judges[].normalized.fix_suggestions`.

Important boundary: q7 suggests; deterministic code applies. Do not let q7 write arbitrary OOXML directly. Map suggestions only to whitelisted patch types. High-risk deletions require both source-evidence gating and explicit `--allow-high-risk-delete`.

## q7 Judge Normalization

The helper now preserves both `parsed` and `normalized` q7 judge output. This matters because q7 may violate schema in small ways, e.g. returning `human_acceptability: false` instead of the requested enum. Normalization maps common variants into:

```text
human_acceptability: acceptable | needs_minor_touchup | needs_major_rework | unusable
manual_rework: none | minutes | hours | rebuild_from_scratch
```

Check `schema_warnings` when normalization had to repair malformed judge output.

## q7 Judge Prompt Shape

Ask q7 for structured defects from a human office-worker/lawyer point of view:

```json
{
  "human_acceptability": "acceptable | needs_minor_touchup | needs_major_rework | unusable",
  "manual_rework": "none | minutes | hours | rebuild_from_scratch",
  "top_differences": ["..."],
  "defects": [
    {
      "page": 1,
      "severity": "minor | major | critical",
      "area": "text | layout | table | image | header_footer | typography | page_geometry",
      "description": "what a human would notice",
      "impact": "why it matters",
      "suggested_patch_type": "page_margin | paragraph_spacing | font_size | table_col_width | image_resize | none"
    }
  ]
}
```

Use `max_tokens >= 4096` because q7 reasoning models can otherwise return empty content.

### Threshold-Triggered q7

To save VLM calls, let cheap QA decide whether q7 should run:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py qa \
  source.pdf output.docx \
  --contact-sheet --judge-pages risk \
  --judge-q7-on-threshold \
  --visual-rmse-threshold 55 \
  --table-bbox-tolerance 0.18 \
  --q7-xml-context
```

The report includes:

```json
"q7_threshold_policy": {
  "enabled": true,
  "triggered": true,
  "reasons": [
    {"type": "visual_rmse_threshold", "page": 1, "rmse_rgb_delta": 41.822, "threshold": 1.0}
  ]
}
```

If triggered, `q7_judge_policy.trigger` is `"threshold"`; otherwise no q7 call is made.

## Repository-Level Experimental Converter Battle Tests

When evaluating an experimental PDF→DOCX repo rather than the bundled `pdfdocx5.py` helper, still use this skill's QA philosophy: full-PDF conversion, DOCX XML/editability inspection, LibreOffice render verification, page-count comparison, and source-vs-render contact sheets. Do not call a converter successful because it produced `.docx` files.

For a host-macOS multi-project arena where the main executor is a tmux-launched Hermes Agent using LM Studio `http://localhost:1234/v1` model `qwen3.6-35b-a3b-q7-mtp`, while the PDF→DOCX projects themselves use the separate `http://localhost:11234/v1` q7 endpoint as VLM/visual judge, follow `references/host-macos-pdfdocx-arena-q7-worker.md`. It captures the endpoint role split, dedicated profile setup, pre-arena git/diff backups, run-directory shape, worker prompt contract, low-noise monitor cron pattern, and 36-attempt slot vs actual-DOCX accounting.

When Noel asks for a host-mac **arena** using `tmux + hermes agent + localhost:1234/v1` model `qwen3.6-35b-a3b-q7-mtp`, treat the tmux-launched Hermes worker as the **main active PDF→DOCX agent**, not merely as a side judge. The parent/Discord Hermes should supervise, verify, and summarize while the q7 Hermes worker uses skills/tools to run the project pipelines. See `references/host-mac-q7-hermes-arena.md` for the topology, pre-start questions, 12×3=36 attempt accounting, and verification manifest expectations.

Arena supervision pitfall: do not let `SKIPPED (dry-run)` rows, stale DOCX files, or a partial one-project rerun satisfy “36 DOCX files ready.” Count only current-manifest rows with `output_docx_exists=true` and an existing `output_docx`, enforce/log the requested project order, and use generous page-aware timeouts instead of fixed 1200s caps. If a worker is off-contract, stop the scoped tmux session/process group, archive old dry-run reports, reset monitor notification state, and start a corrected runner. See `references/host-macos-arena-dryrun-timeout-monitor-pitfall-2026-06-12.md`.

For the concrete `galaxy-kuleon/pdf-to-docx-research` / `hifi-pdf2docx` repo, see `references/pdf-to-docx-research-battle-test-2026-06-12.md`. It records the Python 3.12/uv setup pattern, semantic vs positioned vs Ollama-OCR lanes, a reusable verdict taxonomy (`MECH_PASS`, `REVIEW_PAGE_DELTA`, `FAIL_OCR_REQUIRED`, etc.), and observed pitfalls such as blank scanned outputs without OCR, black image-background artifacts, zero native table counts, and non-legal-grade GLM-OCR CJK drift.

When the work touches OpenWebUI 8083 / Origin Agent user-facing conversion, do **not** conflate local CLI battle tests with live 8083 behavior. See `references/openwebui-8083-pdfdocx-behavior-vs-local-cli-2026-06-12.md`: local CLI validates the host repo; 8083 behavior requires upload with `process=false`, Path B `/handoff` logs, Hermes session/tool evidence, `/handoff/exports` artifact, authenticated `/api/exports` download, task cleanup, and DOCX XML/render anti-cheat checks. A valid downloadable DOCX can still be `PARTIAL/FAIL` if it contains a full-page raster layer or lacks native/editable structure. For the later macOS font-discovery patch and why scanned PDFs produce blank DOCX under the honest non-raster lane, see `references/pdf-to-docx-research-fontpatch-blank-docx-2026-06-12.md`; use its `OCR_REQUIRED` fail-fast pattern before reporting positioned-convert results as success.

For the follow-up macOS font discovery and retest, see `references/pdf-to-docx-research-font-discovery-2026-06-12.md`. It records the Homebrew font casks, correct Source Han `* TC VF` family names, the `fc-match`/candidate-path patch pattern for `positioned.py`, verification commands, and the lesson that font fitting can pass while OCR/image/table fidelity remain separate failures.

### macOS font installation and discovery for PDF→DOCX rendering

For deterministic CJK/Latin text measurement and LibreOffice/Word render parity on Noel's macOS host, install the open font set documented in `references/macos-fonts-for-pdf-docx-rendering.md`:

```bash
brew install --cask \
  font-noto-sans-cjk-tc \
  font-noto-serif-cjk-tc \
  font-noto-sans-mono-cjk-tc \
  font-liberation
brew install --cask font-source-han-sans-vf font-source-han-serif-vf
```

Then verify with `fc-match`. Important pitfall: `Source Han Sans` may not match the installed variable font; use `Source Han Sans TC VF` and `Source Han Serif TC VF`. Also, installing fonts is not enough if the converter hardcodes Linux-only `/usr/share/fonts/...` paths; patch font discovery to check `~/Library/Fonts`, macOS system font assets, or `fc-match` before rerunning font-fit/layout tests.

## Recommended First Battle Test

```bash
# Digital smoke test
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  convert ~/Works/pdf2docx/test/samples/demo-text.pdf \
  -o /tmp/pdfdocx5-demo-text.docx --path auto --turns 2

# Scanned/OCR smoke test
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  convert ~/Works/docling/tests/data_scanned/ocr_test.pdf \
  -o /tmp/pdfdocx5-ocr-test.docx --path auto --turns 2

# QA only
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  qa ~/Works/pdf2docx/test/samples/demo-text.pdf /tmp/pdfdocx5-demo-text.docx
```

## Implementation Notes

- Use `uv run` instead of mutating the global Python environment.
- Keep the script dependency list in `scripts/pdfdocx5.py` PEP 723 metadata.
- Keep q7 and GLM-OCR endpoints configurable via CLI flags/env vars.
- Prefer deterministic patches. VLM output should diagnose and suggest, not directly author arbitrary XML.
- When touching OOXML, always unpack to a temp directory, patch with `lxml`, repack, then render with LibreOffice.
- Use `pdftocairo`, not ad hoc screenshot tooling, for PDF page images.
- For MCP/server implementations, prefer deterministic conversion first and q7 as a bounded OCR/QA tool second. Keep q7 endpoints local-only by default, fail closed on malformed q7 verdicts, and add regression tests for rotated pages, mixed page sizes, skipped full-page rasters, and unknown q7 verdicts. See `references/drawingml-mcp-server-pattern.md`.
- For scanned/photoed PDF→DOCX, default to a clean white page; do **not** preserve dirty paper/background/text ink as a blanket image layer. Preserve only localized visual obligations (logo, chart/photo, signature, stamp/chop/seal, QR/barcode, intentional watermark, or unreconstructable callouts/form controls). The hifi-pdf2docx MCP session showed that `ink minus text_mask` overlays can stack residual source ink under OCR text boxes and produce visually unusable DOCX despite mechanical WARN/PASS checks. Use ledgers and strict visual/editability gates; see `references/hifi-pdf2docx-mcp-clean-scanned-output.md`.
- When adding q7 layout/IR reconstruction for image-backed pages, never treat q7's partial layout as source truth. If a full-page raster is decomposed/suppressed, downgrade to `review` (or fail) until render QA/human review confirms completeness. Add a regression where q7 returns only one paragraph from an image-backed page; strict QA must not return `ok`.
- When a user refers to an uploaded attachment, run the actual attachment path/URL they provided. Do not substitute a synthetic PDF, nearby corpus PDF, or prior smoke fixture unless the user explicitly asks for a bounded synthetic smoke. If you need to create a synthetic repro, label it as synthetic and keep it separate from attachment verification.
- For q7 layout tables, native Word tables can still paginate in LibreOffice even with `w:tblpPr`. Use compact cell margins, small emergency font, exact `w:trHeight`, and a safety compression factor so the table bottom does not sit exactly on the page boundary. Verify DOCX→PDF page count and check that last-row text is on the intended page.
- For q7 layout paragraphs, DrawingML text boxes must wrap (`wps:bodyPr wrap="square"`) or long legal paragraphs clip at the right edge. Combine q7 layout with q7 OCR backstop: append only OCR lines whose normalized text is not already covered by layout/table text.
- For `galaxy-kuleon/pdf-to-docx-research` positioned text fitting on macOS, installing fonts is not sufficient if code only searches Linux paths. Ensure `positioned.py` resolves fonts through `fc-match`/fontconfig when available, expands `~/Library/Fonts`, and includes Homebrew cask font paths such as `NotoSansCJKtc-Regular.otf`, `NotoSerifCJKtc-Regular.otf`, `NotoSansMonoCJKtc-Regular.otf`, `LiberationSans-Regular.ttf`, and Source Han `*TC VF` family names. Verify with `.venv/bin/pytest -q tests/test_positioned_font_fit.py`.
- Do not run monolithic all-page q7 layout/OCR batches on large/image-heavy PDFs. A real 8-PDF/105-page battle test hung on the first `demo-issue-346` q7 layout request before any checkpoint. Use page-level subprocess/request timeouts, checkpoint after each page/PDF, and keep q7 visual QA separate from deterministic conversion so one VLM hang cannot block the corpus.
- When evaluating async API wrappers around PDF→DOCX reconstruction, do not trust `status=succeeded` or a green service QA alone. Also inspect the packaged/local `manifest.json` for `scan.timed_out`, `timed_out_index`, and `page_count_scanned_ok < pages_total`; inspect rendered pages/contact sheets for placeholders such as `[Page N - No geometry data available]`; and verify `GET /jobs/{id}/artifact` actually downloads. A port-8010 run on `complex_english_cjk_10page.pdf` returned `succeeded` with `qa_report.ok=true` and 10 rendered pages even though scanning stopped at page 5 and pages 5/10 rendered as placeholder text, while the artifact endpoint returned 404.
- If you choose an existing prototype repo as the implementation location, explain why it was chosen and ask/offer to migrate to the canonical repo before the work becomes durable.

## First Observed Pitfall: Direct GLM-OCR Repetition Loops

During the first smoke test on `~/Works/docling/tests/data_scanned/ocr_test.pdf`, direct Ollama `/api/generate` with `glm-ocr:latest` returned a repeated Markdown-fenced sentence loop: ~57k raw characters for a one-page source and a 14-page DOCX render. The helper now runs `clean_ocr_text()` to remove code fences, collapse exact repeated word chunks, and truncate suspiciously long per-page OCR with an explicit warning.

Do not trust XB OCR blindly. Always inspect `*.pdfdocx5.report.json` fields:

```json
raw_text_chars
text_chars
ocr_clean_warnings
rendered_pages vs source_pages
```

If cleaned OCR is still suspicious, rerun with q7 fallback/judge or use the upstream GLM-OCR SDK parse path instead of direct image prompting.

## Regression Smoke Runner

Run the no-q7 regression smoke suite after editing the helper:

```bash
~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/smoke_pdfdocx5.py \
  --work-dir /tmp/pdfdocx5-regression-smoke
```

For autonomous discovery + battle testing over `~/Works`, use:

```bash
uv run ~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/auto_battle_pdfdocx5.py \
  --root ~/Works --probe-limit 300 --run-limit 3 \
  --out /tmp/pdfdocx5-auto-battle
```

It discovers candidate PDFs, runs bounded conversion/refinement, ranks failures by composite QA score, and writes `auto-battle-report.json`.

Default checks:

- digital route detection
- XA conversion + render QA
- bounded 3-page `large.pdf` conversion with `--compact-on-overflow --normalize-tables`
- two-column contact sheet generation with visual diff metrics and heatmaps
- three-column source/before/after compare contact sheet with table bbox overlays
- mocked q7 suggestion application through `apply-suggestions` without live q7
- mocked final-turn q7 injection through `refine --mock-q7-report` and `post_q7_patch_qa`
- curated-corpus multi-PDF visual trend smoke via `references/battle-test-corpus.json`
- optional table geometry gate via `--strict-table-geometry`
- optional metrics history append via `--history-dir`, writing `metrics-history.jsonl` and `metrics-history.csv`

Optional slower OCR smoke:

```bash
~/.hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/smoke_pdfdocx5.py --include-xb
```

## Current Limitations of v0.1

This skill is intentionally a seed. The first helper script supports smoke-testable XA/XB/XC scaffolding, not a finished commercial converter. Grow it by adding small deterministic patch types and preserving real battle-test evidence after each improvement.
