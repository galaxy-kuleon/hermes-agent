# OpenWebUI 8083 PDF→DOCX behavior vs local CLI — 2026-06-12

## Why this reference exists

A local macOS CLI battle test of `galaxy-kuleon/pdf-to-docx-research` was initially discussed alongside OpenWebUI 8083 behavior. Noel correctly challenged that local CLI results are **not** OpenWebUI 8083 behavior verification. Future PDF→DOCX work must report these as separate evidence classes.

## Local CLI evidence class

Local repo tested:

```text
/Users/admin/Works/pdf-to-docx-research
```

Representative command family:

```bash
.venv/bin/hifi-pdf2docx positioned-convert input.pdf --out output.docx --workdir <run>/work-positioned
.venv/bin/hifi-pdf2docx validate output.docx --workdir <run>/validate-positioned/<stem>
```

This validates the host/local Python code path, fonts, LibreOffice render, DOCX XML, page counts, and contact sheets. It does **not** prove the OpenWebUI user-facing runtime works.

The font patch work showed:

- Homebrew fonts installed under `/Users/admin/Library/Fonts`.
- `positioned.py` needed `fc-match`/fontconfig and macOS `~/Library/Fonts` discovery; installing fonts alone was insufficient if code searched only Linux font paths.
- `tests/test_positioned_font_fit.py` and full `pytest` passed after patch.

## OpenWebUI 8083 behavior evidence class

For user-facing OpenWebUI verification, run the live/equivalent 8083 path:

```text
OpenWebUI 8083 API/UI
→ POST /api/v1/files/?process=false
→ POST /api/chat/completions model=hermes-agent
→ Path B /handoff original PDF only
→ Hermes container runtime tool/session
→ /handoff/exports artifact
→ authenticated GET /api/exports/... returns valid bytes
→ DB/log/task/session checks
```

In the 2026-06-12 run:

- Stack health was green: `owui`, `hermes`, `openviking`, `docling`, `soffice` healthy/ready.
- `hermes-agent` row was active and `skip_rag: true`.
- OWUI logs showed `hermes-handoff: Path B activated` and `(no markdown conversion)`.
- Negative assertions: no Path A/Docling/markdown conversion/fallback loader in OWUI logs; no Docling non-health requests.
- Hermes session showed a real `mcp_hifi_pdf2docx_convert_pdf` tool call.
- Exported DOCX downloaded through `/api/exports/...` with HTTP 200, DOCX content type, ZIP magic `PK\x03\x04`.

## Critical distinction

The 8083 behavior run used the runtime/container stack:

```text
/home/hermes/hifi-pdf2docx
mcp_hifi_pdf2docx_convert_pdf
```

It did **not** necessarily execute the host-local `pdf-to-docx-research/src/hifi_pdf2docx/positioned.py` patch unless that code is explicitly imported/deployed into the Hermes container runtime. Never imply local CLI fixes are live in 8083 without a deployment/import checksum or runtime behavior test.

## Observed 8083 output quality issue

The payslip conversion produced a downloadable, renderable DOCX, but the agent/tool reported:

```text
status: PARTIAL
qa: 1 page preserved, text intact, render QA failed
errors: no_full_page_raster FAIL — page 1 area_ratio=1.0
```

Independent DOCX inspection confirmed:

- valid DOCX ZIP
- `word/document.xml` present
- editable text exists (~694 chars)
- no native tables
- media present
- one full-page/large DrawingML extent
- LibreOffice render OK and visually close to source

Interpretation: **plumbing PASS, product-quality/anti-cheat PARTIAL/FAIL**. The artifact is downloadable and visually close because it uses a page-scale raster/image layer, which violates the editable/no-full-page-screenshot standard.

## Required reporting pattern

When reporting PDF→DOCX results, always label the evidence class:

| Evidence | What it proves | What it does not prove |
|---|---|---|
| Local CLI conversion/render QA | Host engine/code behavior | OpenWebUI 8083 user-facing behavior |
| Docker `docker exec` tool smoke | Runtime tool can work in container | OpenWebUI upload/chat/export loop works |
| 8083 API/UI E2E | User-facing stack behavior | General quality across corpus |
| `/api/exports` HTTP 200 + ZIP | Download route and file validity | DOCX is editable/non-cheating/high fidelity |

## Minimum 8083 PDF→DOCX behavior gate

- Health: `owui`, `hermes`, `openviking`, relevant sidecars healthy.
- Model: `hermes-agent` active and `skip_rag: true`.
- Upload: `process=false`.
- Path B logs: original `/handoff/...pdf`, `(no markdown conversion)`.
- Negative logs: no Path A, no Docling, no fallback loader, no markdown hydration for the uploaded PDF.
- Hermes session: real tool/MCP/terminal call, not just a simulated response.
- Artifact: under `/handoff/exports/<user>/<chat>/...`.
- Download: authenticated `/api/exports/...` HTTP 200, correct content type, valid bytes/ZIP.
- Task cleanup: `/api/tasks/chat/<chat_id>` empty.
- Quality: inspect DOCX XML and render; fail/partial if full-page raster, no native tables where required, blank pages, missing OCR ledger, or visual QA unacceptable.

## Pitfall

If the user asks “did you verify on 8083?”, do not answer from local CLI artifacts. Either run the 8083 API/UI behavior gate or explicitly say it has not been done yet.