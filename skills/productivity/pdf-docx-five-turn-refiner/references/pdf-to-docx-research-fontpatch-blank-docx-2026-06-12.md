# pdf-to-docx-research font patch + blank DOCX diagnosis (2026-06-12)

## Context

Repo: `/Users/admin/Works/pdf-to-docx-research` (`galaxy-kuleon/pdf-to-docx-research`).
Task class: evaluate/fix experimental PDF→DOCX conversion after installing CJK/Latin fonts and running full-PDF battle tests.

## Durable learnings

### 1. macOS font installation alone is not enough

Installed Homebrew casks:

```bash
brew install --cask \
  font-noto-sans-cjk-tc \
  font-noto-serif-cjk-tc \
  font-noto-sans-mono-cjk-tc \
  font-liberation \
  font-source-han-sans-vf \
  font-source-han-serif-vf
```

Fonts land under `/Users/admin/Library/Fonts/`. `fc-match` resolves:

```text
Noto Sans CJK TC          -> NotoSansCJKtc-Regular.otf
Noto Serif CJK TC         -> NotoSerifCJKtc-Regular.otf
Noto Sans Mono CJK TC     -> NotoSansMonoCJKtc-Regular.otf
Liberation Sans/Serif/Mono -> Liberation*.ttf
Source Han Sans TC VF     -> SourceHanSans-VF.otf.ttc
Source Han Serif TC VF    -> SourceHanSerif-VF.otf.ttc
```

But `positioned.py` originally only searched Linux font paths, so the CJK fit test still failed after install. Fix pattern:

- resolve via `fc-match`/fontconfig when available;
- reject generic fallback matches such as Verdana by checking matched family;
- expand `~/Library/Fonts` paths;
- include macOS fallback assets like PingFang/Hiragino only after real Noto/Homebrew paths;
- recognize Source Han family names with `TC VF` suffix.

Verification after patch:

```bash
cd /Users/admin/Works/pdf-to-docx-research
.venv/bin/pytest -q tests/test_positioned_font_fit.py
.venv/bin/pytest -q
.venv/bin/ruff check src/hifi_pdf2docx/positioned.py tests/test_positioned_font_fit.py
```

Expected: targeted test and full suite pass. In the observed run, `Noto Sans CJK TC` measured a dense legal CJK line as wider than the available box and fit from `10.8` down to about `9.04`, proving fallback-width measurement was fixed.

### 2. Blank DOCX is usually an anti-cheating failure mode, not a font failure

For scanned/image-backed PDFs, `positioned-convert` can produce page-count-correct but visually blank DOCX because:

```text
PDF page body is a full-page raster image
+ include_page_backgrounds == False
+ q7_ocr == "none"
+ converter deliberately skips page-sized images to avoid screenshot-backed DOCX
= Word pages/sections exist, but no editable text or images are emitted
```

The relevant code path is the full-page image skip in `positioned.py`:

```python
if not policy.include_page_backgrounds and _is_page_sized(block["bbox"], page.rect):
    continue
```

This is desirable anti-cheating behavior, but must not be reported as success. For scanned/signed/hybrid image-backed PDFs, a page-count match with `visible_text_chars == 0` is a hard fail.

### 3. Product fix: route, fail fast, then build scan_clean

Do not try to fix blank scanned DOCX with fonts. Correct repair sequence:

1. Add an OCR-required fail-fast guard to `positioned-convert` or a high-level wrapper:
   - classify PDF first;
   - if pages are `scanned_image`, `signed_overlay`, or hybrid image-backed;
   - and `--page-backgrounds` is false and OCR is disabled;
   - raise/return `OCR_REQUIRED` instead of writing a blank DOCX.
2. Introduce a high-level `auto-convert`/router:
   - `digital_text` → positioned reconstruction;
   - `scanned_image` / `signed_overlay` / hybrid image-backed → `scan_clean` OCR reconstruction;
   - `image_heavy_slide` → localized image-preservation route plus OCR/text QA.
3. Build `scan_clean` as content-truth-first, not screenshot-backed:
   - white page background;
   - editable OCR text lines/paragraphs/tables with bbox evidence;
   - preserve only localized visual obligations (logo, signature, stamp/chop/seal, QR/barcode, chart/photo, necessary form controls);
   - never preserve full-page dirty scan/ink as the main content layer.

### 4. Black logo/image rectangles are a separate image extraction bug

Non-page-sized logos/images can render as black rectangles when raw PDF image blocks are extracted without masks/alpha/soft masks. Do not conflate this with blank scanned pages or OCR. Fix pattern:

- for localized visual obligations, prefer rendering a clipped page region/crop from the PDF page rather than using raw embedded image bytes;
- enforce a max area threshold so localized crops cannot become full-page screenshot cheating;
- add regression: payslip/logo must not become a black rectangle.

### 5. Battle-test gate shape

When re-testing this repo or similar converters, use a compact run artifact with:

- all repo-root PDFs or a representative full-PDF corpus, not page-1-only;
- DOCX outputs;
- LibreOffice DOCX→PDF renders;
- DOCX XML stats (`visible_text_chars`, paragraphs, native tables, drawings/media);
- page-count delta;
- source-vs-render contact sheets;
- hard verdict taxonomy such as `MECH_PASS`, `FAIL_TEXT_MISSING`, `FAIL_OCR_REQUIRED`, `REVIEW_PAGE_DELTA`.

Observed representative failures after the font patch:

```text
scanned employment contract: 15 -> 15 pages, 0 visible chars, FAIL_TEXT_MISSING
signed-overlay MNDA: 2 -> 2 pages, only DocuSign/signature text, FAIL_OCR_REQUIRED
payslip digital: renderable with text, but logo/footer black artifacts remain
slide-heavy deck: 45 -> 45 pages, renderable, but fidelity still not commercial-grade
```

Use these as symptom patterns, not fixed file-specific truths.
