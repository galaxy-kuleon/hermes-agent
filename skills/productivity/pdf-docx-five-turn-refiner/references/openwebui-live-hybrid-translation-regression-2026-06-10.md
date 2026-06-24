# OpenWebUI live hybrid-PDF translation regression (2026-06-10)

## Class of failure

A live OpenWebUI 8083 / Origin Agent run can pass the first PDF preflight (`hybrid_image_backed_pdf → route=xb`) and still produce a false-success DOCX later if the agent falls back to old manual OCR/translation scripts.

This is not the same as the original XA/pdf2docx misroute. It is a second-stage failure:

```text
correct detect → manual pdftoppm + vision summary → partial text reconstruction → wrong translator script → downloadable but untranslated DOCX
```

## Observed symptoms

In the live regression chat, the agent correctly detected:

```json
{
  "classification": "hybrid_image_backed_pdf",
  "route": "xb",
  "digital_pages": 0,
  "image_backed_pages": 2,
  "max_image_area_ratio": 0.9976
}
```

Then it drifted:

1. Used `pdftoppm + vision_analyze` manually instead of `pdfdocx5.py convert --path xb --ocr-engine auto`.
2. Treated a VLM response that contained summary/truncated transcription as complete OCR.
3. Created a shortened Chinese DOCX from hand-written text.
4. Used `translate_docx_simple.py ... English`, which was a tiny English→Korean fallback and did not translate Chinese to English.
5. Exported a DOCX because file creation succeeded.

Structural QA of the exported DOCX showed:

```text
text_nodes: 34
chars: 1069
CJK chars: 714
Latin chars: 83
media_count: 0
```

The output was editable, but still Chinese and incomplete.

## Durable rule

For hybrid/scanned PDF → English DOCX tasks, detection is necessary but not sufficient. Require all of these before success:

1. `pdfdocx5.py detect` classifies route.
2. Hybrid/scanned inputs use a real OCR/reconstruction path (`pdfdocx5.py convert --path xb --ocr-engine auto`) or an equally evidenced OCR pipeline with per-page raw text counts.
3. DOCX translation uses `docx-translation-json` or another real translator, not `translate_docx_simple.py` or a zero-change fallback.
4. Run residual-language QA before export/claim: for English target, excessive CJK or unchanged text is FAIL.
5. Render the final DOCX and inspect from human POV; a download URL/HTTP 200 is only transport success.

## Suggested fail-fast checks

```bash
python3 /home/hermes/skills/software-development/anything-to-docx/scripts/verify_translated_docx.py \
  source.docx output.docx English
```

Expected false-success failure shape:

```text
same_text: true
output_cjk: large
FAIL: output appears unchanged from source
```

If this checker is not available in the environment, implement equivalent checks directly against `word/document.xml`: compare text equality/hash and count residual CJK for English targets.
