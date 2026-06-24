# pdf-to-docx-research macOS font discovery patch (2026-06-12)

## Context

Repo: `/Users/admin/Works/pdf-to-docx-research` (`galaxy-kuleon/pdf-to-docx-research`).

The positioned converter's CJK font-fit test failed on macOS even after installing CJK fonts because `src/hifi_pdf2docx/positioned.py` only searched Linux font paths. The code fell back to PyMuPDF Helvetica-style measurement, making dense CJK text look narrower than it really was:

```text
tests/test_positioned_font_fit.py::test_fit_font_size_shrinks_dense_cjk_line_to_box_width
FAILED: assert 10.8 < 10.8
```

## Fonts installed on macOS host

Installed with Homebrew casks:

```bash
brew install --cask \
  font-noto-sans-cjk-tc \
  font-noto-serif-cjk-tc \
  font-noto-sans-mono-cjk-tc \
  font-liberation

brew install --cask font-source-han-sans-vf font-source-han-serif-vf
```

Installed location:

```text
/Users/admin/Library/Fonts/
```

Important family names verified with `fc-match`:

```text
Noto Sans CJK TC           -> NotoSansCJKtc-Regular.otf
Noto Serif CJK TC          -> NotoSerifCJKtc-Regular.otf
Noto Sans Mono CJK TC      -> NotoSansMonoCJKtc-Regular.otf
Liberation Sans            -> LiberationSans-Regular.ttf
Liberation Serif           -> LiberationSerif-Regular.ttf
Liberation Mono            -> LiberationMono-Regular.ttf
Source Han Sans TC VF      -> SourceHanSans-VF.otf.ttc
Source Han Serif TC VF     -> SourceHanSerif-VF.otf.ttc
```

Do not query generic `Source Han Sans` / `Source Han Serif` on this host; those fall back to Verdana. Use the `* TC VF` family names.

## Code fix pattern

Patch `positioned.py` font discovery to:

1. Use `fc-match`/fontconfig when available.
2. Reject false fallback matches by comparing normalized family names (e.g. avoid accepting Verdana for `Source Han Sans`).
3. Expand `~/Library/Fonts` and `/Library/Fonts` candidates.
4. Include Linux candidates for container compatibility.
5. Include macOS system/mobile-asset fallback candidates such as `PingFang.ttc` and `Hiragino_Sans_TC.ttc`.
6. Use `glob.glob()` for wildcards that appear above the filename component, e.g. `/System/Library/AssetsV2/.../*/AssetData/PingFang.ttc`.

Core helper shape:

```python
def _font_file_for(font_name: str) -> Path | None:
    family = _font_family_for(font_name)
    fontconfig_path = _fontconfig_file_for(family)
    if fontconfig_path:
        return fontconfig_path
    for candidate in FONT_FILE_CANDIDATES.get(family, []):
        for path in _expand_font_candidate(candidate):
            if path.exists():
                return path
    return None
```

Family mapping used:

```text
CJK mono       -> Noto Sans Mono CJK TC
CJK serif      -> Noto Serif CJK TC
CJK sans       -> Noto Sans CJK TC
Source Han     -> Source Han Sans/Serif TC VF
Latin sans     -> Liberation Sans
Latin serif    -> Liberation Serif
Latin mono     -> Liberation Mono
```

## Verification commands

Run before/after tests to prove the fix:

```bash
cd /Users/admin/Works/pdf-to-docx-research
.venv/bin/pytest -q tests/test_positioned_font_fit.py::test_fit_font_size_shrinks_dense_cjk_line_to_box_width
```

After patch, verify font resolution and measured width:

```bash
.venv/bin/python - <<'PY'
from hifi_pdf2docx.positioned import _font_file_for, _measure_text_width, _fit_font_size_to_width
text=("本保密合約(下稱「本合約」)自西元 2026 年 06 月 01 日起生效""(下稱「生效日」)，由群聯電子股份有限公司，其主要營業地址位於苗栗縣竹南")
for name in ["Noto Sans CJK TC", "Noto Serif CJK TC", "Noto Sans Mono CJK TC", "Liberation Sans", "Liberation Serif", "Source Han Sans TC VF"]:
    print(name, '->', _font_file_for(name))
print('measured@10.8=', _measure_text_width(text, 'Noto Sans CJK TC', 10.8))
print('fit=', _fit_font_size_to_width(text, 'Noto Sans CJK TC', max_font_size=10.8, target_width=583.3))
print('available=', 583.3 * 0.96)
PY
```

Observed good output:

```text
Noto Sans CJK TC -> /Users/admin/Library/Fonts/NotoSansCJKtc-Regular.otf
Noto Serif CJK TC -> /Users/admin/Library/Fonts/NotoSerifCJKtc-Regular.otf
Noto Sans Mono CJK TC -> /Users/admin/Library/Fonts/NotoSansMonoCJKtc-Regular.otf
Liberation Sans -> /Users/admin/Library/Fonts/LiberationSans-Regular.ttf
Liberation Serif -> /Users/admin/Library/Fonts/LiberationSerif-Regular.ttf
Source Han Sans TC VF -> /Users/admin/Library/Fonts/SourceHanSans-VF.otf.ttc
measured@10.8= 669.25
fit= 9.036465296974225
available= 559.968
```

Then run:

```bash
.venv/bin/pytest -q tests/test_positioned_font_fit.py
.venv/bin/pytest -q
.venv/bin/ruff check src/hifi_pdf2docx/positioned.py tests/test_positioned_font_fit.py
```

Observed after patch:

```text
2 passed
9 passed
All checks passed
```

## PDF battle retest after font patch

Use a fresh run directory and full PDFs, not page-1 smoke:

```bash
RUN_ROOT="conversion-runs/font-patch-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_ROOT"/{docx,work-positioned,validate-positioned,logs,contact-sheets,contact-renders}
.venv/bin/hifi-pdf2docx classify ./*.pdf --workdir "$RUN_ROOT/work-positioned" | tee "$RUN_ROOT/logs/classify.txt"
for pdf in ./*.pdf; do
  stem=$(python3 - "$pdf" <<'PY'
import re,sys
from pathlib import Path
print(re.sub(r'[^A-Za-z0-9._-]+','_',Path(sys.argv[1]).stem).strip('._') or 'document')
PY
)
  out="$RUN_ROOT/docx/${stem}.positioned.fontpatch.docx"
  .venv/bin/hifi-pdf2docx positioned-convert "$pdf" --out "$out" --workdir "$RUN_ROOT/work-positioned"
done
```

Then render every DOCX with `hifi-pdf2docx validate`, inspect DOCX XML, compare page counts, and create source-vs-render contact sheets.

Observed high-level result after font patch:

```text
8 PDFs converted
105 source pages total
8 DOCX rendered by LibreOffice
105 rendered pages total
```

The font patch fixed CJK measuring and made all tests green, but it did not make the converter commercial-grade:

- Digital/slide PDFs had correct page counts and better text fitting.
- Scanned/image-backed PDFs still rendered blank/near-blank because positioned-convert does not perform OCR.
- Signed-overlay MNDA still needed OCR/content-truth routing.
- Image transparency/black-background artifacts remained in logos, slide elements, and footer graphics.
- Native table count remained zero for the tested outputs.

## Decision lesson

Treat font discovery as a prerequisite reliability fix, not a product-quality fix. After font patching, immediately run the full PDF battle harness because typography changes can improve text fit while leaving OCR, image transparency, tables, and layout fidelity as separate failures.
