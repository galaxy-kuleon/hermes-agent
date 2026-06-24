# pdf-to-docx-research battle-test notes (2026-06-12)

Use this reference when Noel asks to try or evaluate `galaxy-kuleon/pdf-to-docx-research` or a similar experimental PDF→DOCX repo.

## Repo / environment

- Repo path observed: `/Users/admin/Works/pdf-to-docx-research`.
- Upstream: `https://github.com/galaxy-kuleon/pdf-to-docx-research`.
- Package name / CLI: `hifi-pdf2docx`.
- Requires Python `>=3.12`.
- Under Hermes sessions whose active interpreter is Python 3.11, do **not** rely on plain `uv pip install -e ...`; it may resolve against the active Hermes venv. Use:

```bash
cd /Users/admin/Works/pdf-to-docx-research
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/hifi-pdf2docx doctor
```

Run repo scripts with `.venv/bin/python ...` when they import the package; repo scripts using `/usr/bin/env python3` may otherwise hit macOS system Python and fail on Python-3.12-only features such as `enum.StrEnum`.

## Commands that produced useful evidence

Run smoke/regression first, but do not let a single failing unit test stop empirical conversion unless the converter still runs:

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/check_baseline.py
.venv/bin/python scripts/check_adversarial_docx.py
```

Classify all root PDFs:

```bash
RUN_ROOT=conversion-runs/hermes-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RUN_ROOT"/{docx,work,validate,logs}
.venv/bin/hifi-pdf2docx classify ./*.pdf --workdir "$RUN_ROOT/work" | tee "$RUN_ROOT/logs/classify.txt"
```

Semantic conversion over full PDFs:

```bash
for pdf in ./*.pdf; do
  stem=$(python3 - "$pdf" <<'PY'
import re, sys
from pathlib import Path
print(re.sub(r'[^A-Za-z0-9._-]+', '_', Path(sys.argv[1]).stem).strip('._') or 'document')
PY
)
  out="$RUN_ROOT/docx/${stem}.semantic.docx"
  .venv/bin/hifi-pdf2docx convert "$pdf" --out "$out" --workdir "$RUN_ROOT/work"
done
```

Positioned conversion over full PDFs:

```bash
for pdf in ./*.pdf; do
  stem=$(python3 - "$pdf" <<'PY'
import re, sys
from pathlib import Path
print(re.sub(r'[^A-Za-z0-9._-]+', '_', Path(sys.argv[1]).stem).strip('._') or 'document')
PY
)
  out="$RUN_ROOT/docx/${stem}.positioned.docx"
  .venv/bin/hifi-pdf2docx positioned-convert "$pdf" --out "$out" --workdir "$RUN_ROOT/work-positioned"
done
```

Render every DOCX through LibreOffice and record page counts:

```bash
for docx in "$RUN_ROOT"/docx/*.docx; do
  stem=$(basename "$docx" .docx)
  .venv/bin/hifi-pdf2docx validate "$docx" --workdir "$RUN_ROOT/validate/$stem"
done
```

For signed-overlay / hybrid image-backed PDFs, test OCR as a separate lane rather than treating no-OCR output as failure of the whole repo:

```bash
.venv/bin/hifi-pdf2docx convert input.pdf \
  --out "$RUN_ROOT/docx/input.ollama-ocr.docx" \
  --workdir "$RUN_ROOT/work-ocr" \
  --ocr-engine ollama --ocr-dpi 144
.venv/bin/hifi-pdf2docx validate "$RUN_ROOT/docx/input.ollama-ocr.docx" \
  --workdir "$RUN_ROOT/validate-ocr/input"
```

## QA gates used in the session

For each generated DOCX, inspect `word/document.xml` and package parts:

- visible text chars (excluding `w:vanish` runs)
- paragraphs
- native `w:tbl` count
- drawings and `word/media/*` count
- DOCX→PDF render success
- rendered page count vs source page count
- source-vs-render contact sheet

Minimum verdict taxonomy used:

```text
MECH_PASS              # DOCX exists, visible text present, render works, page count plausible
REVIEW_PAGE_DELTA      # render works but page count differs
FAIL_TEXT_MISSING      # DOCX/render exists but visible editable text is absent/near-empty
FAIL_OCR_REQUIRED      # scanned/signed/image-heavy source has no reconstructed body text
OCR_TEXT_REVIEW        # OCR produced editable text, but requires legal/content/fidelity review
CONVERT_FAIL / RENDER_FAIL
```

Important: `MECH_PASS` is **not** a client-deliverable verdict. It only means the mechanical gates passed. Always inspect contact sheets before claiming usability.

## Observed results / pitfalls

- `semantic convert` can produce DOCX and render through LibreOffice, but scanned/signed-overlay sources often become blank or single-page outputs with no body text. Digital PDFs become rough text flow; tables/logos/layout are frequently lost.
- `positioned-convert` is the more promising visual lane for digital and slide-heavy PDFs. It preserved page count and many image/text positions better, but outputs are still not normally editable Word reconstructions: many objects are textboxes/drawings, native table count was often zero, and visual artifacts such as black logo/image backgrounds appeared.
- For scanned PDFs without OCR, `positioned-convert` correctly avoids full-page raster cheating but may render as blank pages. Treat that as `FAIL_OCR_REQUIRED`, not success.
- Ollama GLM-OCR on a 2-page MNDA/signed-overlay PDF produced editable text and matching page count, but legal-grade content was not acceptable: CJK OCR drift included examples such as `下稿`, `管業`, `笛栗`, `合约`, and the output collapsed layout into ordinary text flow with signatures/stamps not faithfully preserved.
- A repo unit test failed on macOS for CJK font fitting because font candidates were Linux-oriented (`Noto Sans CJK TC` paths) and fallback measurement was too optimistic. Durable fix direction: add macOS CJK font lookup (e.g. PingFang / system font paths) or a platform-aware font resolver before relying on `_fit_font_size_to_width` tests.

## Recommended next repair targets

1. Add platform-aware CJK font file lookup so positioned text measurement is deterministic on macOS and Linux.
2. Reify the QA harness: DOCX XML anti-cheat, render page-count gate, blank-page fail, and source-vs-render contact sheets.
3. Fix positioned image transparency/background handling; black logo/image backgrounds are highly visible.
4. For scanned/signed-overlay sources, implement content-truth-first OCR: OCR candidates → line bboxes/crops → correction ledger/adjudication → deterministic DOCX compiler. Do not directly trust OCR Markdown as final legal text.
5. Add native table reconstruction metrics. Zero `w:tbl` on table-like PDFs should be review/fail depending on source evidence.

## Reporting pattern for Noel

Keep Discord output concise. Put full details in `QA_SUMMARY.all.md`, `QA_SUMMARY.all.json`, logs, and a zip. Report:

- run root
- zip path and SHA256
- count of DOCX/contact sheets
- short verdict per lane
- blockers and next repair targets

Avoid claiming success from file existence or LibreOffice render alone.
