# Hybrid image-backed PDF false-success regression (MNDA / feefa045)

## Incident

OpenWebUI 8083 chat `feefa045-ead6-494b-a839-8792ddb369db` was asked to use `anything-to-docx` to translate a PDF MNDA to English. The agent reported success and produced a downloadable DOCX, but human/render QA showed the body remained Chinese page images; only DocuSign/signature fields were translated.

## Root cause

The PDF was hybrid/image-backed:

- `pdftotext` returned some selectable text: DocuSign ID, a Chinese name/title, and date.
- Each PDF page also contained a page-sized raster image covering ≈99.8% of the page.
- The actual contract body was inside those images and was not in the text layer.

The old detector counted any page with `>=30` text chars as digital, so it routed to XA (`pdf2docx`). `pdf2docx` embedded the page images and preserved only the tiny text layer. The translation script saw only five editable paragraphs and translated those, then the agent verified only `doc.paragraphs` and file existence.

## Required fix

Route these PDFs to XB/OCR-VLM, not XA.

Detection must inspect both native text and image area:

```text
page-sized image >= 0.65 page area + sparse text chars < 600
  => classification = hybrid_image_backed_pdf
  => route = xb
```

`pdfdocx5.py detect` must expose:

- `classification`
- `image_backed_pages`
- `page_image_pages`
- `max_image_area_ratio`
- per-page `page_evidence[]`
- warnings explaining that `pdftotext` text is not enough evidence.

## QA lesson

Do not claim translation success from:

- valid DOCX ZIP;
- `/api/exports/...` returning 200;
- small `doc.paragraphs` sample being translated.

For PDF→translated DOCX, inspect/editability + render output:

- `word/document.xml` text node count and editable text length;
- `word/media/*` large page images;
- DOCX→PDF→PNG render;
- residual source-language body text from human POV or OCR/VLM.

## Regression expectation

For the MNDA-like hybrid case, `pdfdocx5.py detect` should return roughly:

```json
{
  "classification": "hybrid_image_backed_pdf",
  "route": "xb",
  "digital_pages": 0,
  "image_backed_pages": 2,
  "page_image_pages": 2,
  "max_image_area_ratio": 0.9976
}
```
