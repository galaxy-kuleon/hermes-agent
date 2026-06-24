# DrawingML MCP Server Pattern for Any-PDF-to-DOCX

Session learning from building a simple PDF→DOCX MCP server around python-docx, low-level DrawingML/OOXML, and local q7.

## When to use

Use this pattern when the user wants a local MCP server/tool that converts arbitrary PDFs into editable DOCX drafts with strong content-preservation gates.

## Architecture

- MCP tool layer exposes:
  - `pdf_locate_test_corpus`: returns curated test PDFs only; do not expose arbitrary filesystem scans by default.
  - `pdf_convert_any_to_docx`: deterministic export first, optional q7 OCR and q7 render QA second.
- Deterministic compiler:
  - PyMuPDF extracts native PDF text lines and image instances.
  - `python-docx` creates the `.docx` package.
  - Low-level OOXML/DrawingML injection creates:
    - editable absolutely positioned `wps:wsp` text boxes for each text/OCR line;
    - anchored `wp:anchor` images for localized or page image instances.
- q7 roles:
  - OCR fallback on image-backed/scanned pages with weak/no native text.
  - Render QA after DOCX generation: source PDF→PNG vs DOCX→PDF→PNG.

## Local q7 safety

Use q7 as a local-only tool unless the user explicitly approves a remote endpoint.

Recommended defaults:

```text
Q7_BASE_URL=http://localhost:11234/v1
Q7_MODEL=qwen3.6-35b-a3b-q7
Q7_API_KEY=<local-q7-api-key>
```

Implementation guard:

- accept only `localhost`, `127.0.0.1`, `::1`, or `host.docker.internal` by default;
- require an explicit opt-in such as `Q7_ALLOW_REMOTE=1` for non-local endpoints;
- never hardcode the actual local API key into source files; use env vars or `.env.example` placeholders.

## Content correctness gates

Strict mode must count content even when it is intentionally excluded:

- If `include_full_page_images=false`, skipped full-page raster images still count as source image instances.
- Strict QA should fail when source images are skipped: `image_instances_written < image_instances_source`.
- Full-page raster preservation can be used as a visual reference/content-preservation fallback, but keep status as `review`; do not call it final editable reconstruction.
- q7 OCR text boxes can make scanned content editable, but if the page still depends on a full-page image, label it as review/partial rather than client-ready.

## Coordinate and layout pitfalls

- PyMuPDF page rotation matters. `page.rect` is the displayed page, while `page.mediabox`/image bbox evidence can reflect the unrotated coordinate system.
- Apply rotation-aware coordinate transforms to **both** native text bboxes and image bboxes before anchoring DrawingML objects.
- For rotated full-page images, use rendered page clips rather than raw `extract_image(xref)` streams; raw image streams are stored before page rotation and can appear sideways in DOCX.
- Mixed page sizes/orientations need separate DOCX sections. Do not mutate only `doc.sections[0]` for every page; create a new section/page setup when page dimensions change or for each page.

## q7 render QA fail-closed rule

Aggregate q7 verdicts conservatively:

- Overall `PASS` only if every judged page returns explicit `PASS`.
- Any `FAIL` ⇒ overall `FAIL`.
- Any `PARTIAL` ⇒ overall `PARTIAL`.
- Missing, malformed, unknown, or absent verdicts ⇒ overall `UNKNOWN` or a non-pass review state.

Do not let malformed VLM JSON or `UNKNOWN` page verdicts become `PASS`.

## Git/workflow discipline

Before committing a new converter/MCP tool:

1. Run focused tests and then the full suite.
2. Render generated DOCX with LibreOffice/soffice.
3. Run a simple staged-diff secret/danger scan.
4. Use an independent reviewer subagent; fail closed on security or logic findings.
5. Fix review blockers and add regression tests before committing.
6. If adding work under an existing non-canonical folder (e.g. a discovered prototype repo), explicitly explain why that repo was chosen and offer to migrate to the canonical project before further investment.
