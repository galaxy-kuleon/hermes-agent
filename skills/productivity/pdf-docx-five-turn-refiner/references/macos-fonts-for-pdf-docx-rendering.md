# macOS Fonts for Deterministic PDF→DOCX Rendering

Session: 2026-06-12, `galaxy-kuleon/pdf-to-docx-research` / `hifi-pdf2docx`.

## Problem observed

The repo's positioned DOCX font-fit test failed even after PDF→DOCX conversion was otherwise runnable:

```text
test_fit_font_size_shrinks_dense_cjk_line_to_box_width
assert 10.8 < 10.8
```

Root cause: the converter expected `Noto Sans CJK TC` and `Liberation Sans/Serif`, but macOS initially resolved them incorrectly:

```text
fc-match "Noto Sans CJK TC" -> Verdana.ttf
fc-match "Liberation Sans"  -> Arial.ttf
```

The code also only listed Linux font paths under `/usr/share/fonts/...`, so installing fonts alone may not fix measurement unless the code can discover macOS `~/Library/Fonts` paths or use fontconfig/CoreText.

## Recommended macOS install

Install deterministic open fonts used across macOS and Linux render/measurement paths:

```bash
brew install --cask \
  font-noto-sans-cjk-tc \
  font-noto-serif-cjk-tc \
  font-noto-sans-mono-cjk-tc \
  font-liberation

brew install --cask font-source-han-sans-vf font-source-han-serif-vf
```

Homebrew installs these under:

```text
~/Library/Fonts/
```

Expected files include:

```text
~/Library/Fonts/NotoSansCJKtc-Regular.otf
~/Library/Fonts/NotoSerifCJKtc-Regular.otf
~/Library/Fonts/NotoSansMonoCJKtc-Regular.otf
~/Library/Fonts/LiberationSans-Regular.ttf
~/Library/Fonts/LiberationSerif-Regular.ttf
~/Library/Fonts/LiberationMono-Regular.ttf
~/Library/Fonts/SourceHanSans-VF.otf.ttc
~/Library/Fonts/SourceHanSerif-VF.otf.ttc
```

Refresh fontconfig if available:

```bash
fc-cache -f
```

Verify:

```bash
fc-match "Noto Sans CJK TC"
fc-match "Noto Serif CJK TC"
fc-match "Noto Sans Mono CJK TC"
fc-match "Liberation Sans"
fc-match "Liberation Serif"
fc-match "Liberation Mono"
fc-match "Source Han Sans TC VF"
fc-match "Source Han Serif TC VF"
```

Note: Source Han variable font family names are locale-specific. `fc-match "Source Han Sans"` may still fall back to Verdana; use:

```text
Source Han Sans TC VF
Source Han Serif TC VF
```

## Recommended mapping

| Use | Primary | Fallback |
| --- | --- | --- |
| Traditional Chinese sans | `Noto Sans CJK TC` | `PingFang TC`, `Heiti TC`, `Source Han Sans TC VF` |
| Traditional Chinese serif | `Noto Serif CJK TC` | `Songti TC`, `Source Han Serif TC VF` |
| CJK mono / tables | `Noto Sans Mono CJK TC` | `Noto Sans CJK TC` |
| Latin sans | `Liberation Sans` | `Arial`, `Helvetica` |
| Latin serif | `Liberation Serif` | `Times New Roman` |
| Latin mono | `Liberation Mono` | `Courier New`, `Menlo` |

## Code-discovery fix pattern

Do not rely on Linux-only font paths. For macOS, font discovery should check at least:

```text
~/Library/Fonts/NotoSansCJKtc-Regular.otf
~/Library/Fonts/NotoSerifCJKtc-Regular.otf
~/Library/Fonts/NotoSansMonoCJKtc-Regular.otf
~/Library/Fonts/LiberationSans-Regular.ttf
~/Library/Fonts/LiberationSerif-Regular.ttf
~/Library/Fonts/LiberationMono-Regular.ttf
/System/Library/AssetsV2/com_apple_MobileAsset_Font7/*/AssetData/PingFang.ttc
/System/Library/AssetsV2/com_apple_MobileAsset_Font7/*/AssetData/Hiragino_Sans_TC.ttc
/System/Library/Fonts/STHeiti Medium.ttc
/System/Library/Fonts/Supplemental/Songti.ttc
```

Better: use `fc-match` when available and fall back to explicit paths. For `.ttc` collections, verify PIL/Pillow can measure the selected family correctly; if not, prefer installed OTFs such as `NotoSansCJKtc-Regular.otf`.

## Container install

For Docker/Linux render parity:

```bash
apt-get update && apt-get install -y \
  fonts-noto-cjk \
  fonts-noto-cjk-extra \
  fonts-liberation \
  fonts-dejavu-core \
  fontconfig
fc-cache -f -v
```

Common Linux paths:

```text
/usr/share/fonts/opentype/noto/
/usr/share/fonts/truetype/liberation/
/usr/share/fonts/truetype/dejavu/
```

## QA gate after font install

After installing fonts and patching discovery, rerun the focused test and at least one render comparison:

```bash
cd /Users/admin/Works/pdf-to-docx-research
.venv/bin/pytest -q tests/test_positioned_font_fit.py
# then rerun positioned conversion + LibreOffice render on a CJK-heavy PDF
```

If the font-fit test still fails, inspect `_font_file_for()` before assuming the fonts are missing; the likely failure is path discovery, not installation.
