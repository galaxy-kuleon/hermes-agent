# MNDA hybrid PDF QA-pass and OpenWebUI 8083 E2E notes

This records the durable technique from an MNDA/DocuSign-style hybrid PDF that had sparse selectable text but a page-sized scanned body.

## Correct route

`pdfdocx5.py detect` should classify this class as `hybrid_image_backed_pdf`, not born-digital:

- `digital_pages=0`
- `image_backed_pages=2`
- `page_image_pages=2`
- `max_image_area_ratio≈0.9976`
- warning: page-sized image with sparse text layer; `pdftotext` text does not prove the body is editable/digital.

Route to XB with OCR:

```bash
uv run /home/hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py \
  convert "$SRC" -o /tmp/intermediate.docx \
  --path xb --ocr-engine direct --turns 2 \
  --work-dir /tmp/pdfdocx5-work
```

Direct full-page GLM-OCR through Ollama was more robust for this case than an unmounted/partial DocIR experiment. DocIR remains promising for layout IR, but the pass condition came from direct OCR + deterministic refiner/QA.

## Vector page-border repair

For image-backed scanned bodies, source ink may include page-edge scan/border ink. If the editable DOCX lacks that boundary, honest ink-bbox QA may score a severe visual shift even though the content is editable. The accepted repair is to add OOXML vector page borders (`w:pgBorders`) during normalization for page-image-backed PDFs.

This is not screenshot cheating: it reconstructs the page boundary as editable/vector document geometry and keeps `media_count=0` / low raster risk.

## LibreOffice QA isolation

Render QA should call `soffice` with a per-render isolated profile:

```bash
soffice --headless -env:UserInstallation=file:///tmp/.../lo-profile-* --convert-to pdf ...
```

This avoids stale or root-owned LibreOffice profile state in the Hermes container and keeps render QA deterministic.

## Translation gate

For English output, use the weak-LLM gate; do not use old ad-hoc translation scripts or direct OpenWebUI `/v1/chat/completions` calls:

```bash
python3 /home/hermes/skills/software-development/anything-to-docx/scripts/translate_docx_weak_llm_gate.py \
  /tmp/intermediate.docx /tmp/final_en.docx \
  --source-pdf "$SRC" \
  --workdir /tmp/translate-work \
  --target-language English \
  --source-qa-timeout 360
```

After deterministic party-name or terminology repair, rerun `verify_translated_docx.py` and inspect the final exported DOCX.

## Observed pass shape

A good pass for this class may still have minor visual pixel differences:

- source/intermediate layout QA: `failure_score=32`, `severity=minor`
- page count matches source
- anti-cheating gate passes: visible editable text, low/full-page-raster risk, `media_count=0`
- translation gate: `PASS`
- final English DOCX: `cjk=0`, high Latin count, no full-page raster media

Do not require pixel-perfect render parity for translated editable DOCX; block major/critical layout failures and false-success translation, not every residual RMSE delta.
