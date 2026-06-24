# Host Mac q7-Hermes PDF→DOCX Arena Pattern

Use this when Noel asks to run a PDF→DOCX arena outside OpenWebUI 8083, on the macOS host, with a local OpenAI-compatible model as the **main agent brain**.

## Core correction from session

When Noel says:

> tmux + hermes agent + localhost:1234/v1 model qwen3.6-35b-a3b-q7-mtp is the main AI agent

Do **not** treat the Discord/macOS Hermes session as the direct converter and do not merely call q7 as a side judge. The intended topology is:

```text
Discord Hermes (orchestrator/supervisor)
  └── tmux session
        └── Hermes Agent worker
              ├── agent brain: http://localhost:1234/v1, qwen3.6-35b-a3b-q7-mtp
              ├── skills/tools loaded for PDF→DOCX work
              └── runs/compares project pipelines on the host Mac
```

The parent Hermes should set up, supervise, verify, and summarize. The tmux Hermes worker is the active agent doing the PDF→DOCX pipeline work.

## Arena shape

For three project pipelines and a 12-PDF corpus, report **36 attempts/output slots**:

```text
12 PDFs × 3 project pipelines = 36 attempts
```

Keep attempts distinct from successful DOCX files. If a pipeline crashes, times out, or correctly fail-fasts, record a `FAIL`/`TIMEOUT`/`OCR_REQUIRED` report rather than fabricating a DOCX.

Typical project roots from the session:

```text
/Users/admin/Works/pdf-to-docx-research
/Users/admin/Works/hifi-pdf2docx
/Users/admin/Works/atdv4/pdf-to-docx-mcp-server   # actual pyproject under atdv4
```

Typical corpus root:

```text
/Users/admin/Tmp/test_pdfs
```

## Recommended pre-start questions

Ask compact ABCDE-style questions before launching the arena:

1. Worker topology: one q7 Hermes worker sequentially vs multiple parallel workers.
2. Profile/config: dedicated profile for q7 worker vs command/env override.
3. Mutation policy: read-only, venv/deps only, small patches, or branch/commit.
4. Run mode: smoke `1 PDF × 3 pipelines` first vs direct full `12×3`.
5. QA depth: DOCX only, structural OOXML checks, LibreOffice render/contact sheets, or q7 visual judge.
6. Output root.
7. Confirm `atdv4` target path is the nested `pdf-to-docx-mcp-server` project.

Suggested default if Noel says “use defaults”:

```text
one q7 Hermes worker, dedicated q7 profile, venv/deps only, smoke first,
structural QA + LibreOffice render/contact sheets, output under
/Users/admin/Tmp/test_pdfs/arena_outputs/<timestamp>/
```

## Verification expectations

For every attempt, write a machine-readable manifest plus concise Markdown summary:

- source PDF path and digital/scanned classification if known
- project/pipeline name
- command/path used
- status: `PASS`, `PARTIAL`, `FAIL`, `TIMEOUT`, `BLOCKED`, `OCR_REQUIRED`, etc.
- output DOCX path if produced
- structural QA: visible editable text count, native table count, media/full-page-raster risk
- render QA: LibreOffice DOCX→PDF/PNG success, page-count comparison, contact sheet path when available
- anti-cheating result: flag full-page screenshot/raster-backed DOCX separately from editable reconstruction

Never report “36 DOCX files” unless 36 files were actually produced and verified to exist. Prefer “36 attempts/output slots” until verified.