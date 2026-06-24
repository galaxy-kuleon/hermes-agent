# Deterministic non-digital PDF → DOCX policy

Use this reference after the MNDA behavior-verification failures where the agent correctly detected `hybrid_image_backed_pdf` but still produced a false-success English DOCX.

## Principle

For non-digital / scanned / hybrid image-backed PDFs, make the deterministic pipeline do as much work as possible. Local LLM/VLM calls should diagnose, OCR, classify, or suggest bounded repairs — they must not be allowed to free-form invent a DOCX workflow or decide success from file existence.

```text
PDF page image
  → deterministic page evidence + render
  → deterministic layout regions / reading order
  → OCR text per region/page
  → deterministic IR/DOCX construction
  → deterministic validators
  → q7/122B visual judge for defects and bounded suggestions
  → deterministic patch adapters only
```

## Container endpoints

Inside the OpenWebUI 8083 `hermes` container:

- Use `http://host.docker.internal:11234/v1`, not `localhost:11234/v1`.
- Available VLM/LLM models observed from container include:
  - `qwen3.5-122b-a10b:ud-q4-k-xl`
  - `qwen3.6-35b-a3b-q7`
  - `qwen3.6-27b-k-xl`
  - `qwen3.5-35b-a3b-gf`
- Use API key `change-me-local-key` for that local endpoint.
- Use `max_tokens >= 4096` for judging; use larger budgets for visual/XML repair suggestions.

## Deterministic first path

1. **Preflight**
   - Run `pdfdocx5.py detect`.
   - If `classification=hybrid_image_backed_pdf` or page-sized image warning appears, force non-digital path.

2. **Render pages deterministically**
   - Use `pdftocairo -png` at a fixed DPI.
   - Record page dimensions and hashes.

3. **OCR / text extraction**
   - Prefer GLM-OCR / OCR SDK where available.
   - If calling a VLM for OCR, request a structured JSON/XML result per page and validate schema; do not accept summaries.
   - Record per-page `raw_text_chars`, cleaned text chars, and warnings.

4. **Layout / IR**
   - Use deterministic layout rules first: page bbox, approximate columns, headings, paragraphs, tables, signature blocks, seals/images.
   - Store an intermediate IR (XML/JSON) with page/region ids, bbox, text, type, and confidence.
   - DOCX generation should consume IR; it should not be written free-form by the LLM.

5. **DOCX generation**
   - Generate editable text paragraphs/tables/images.
   - Cropped images are allowed only for real seals/logos/handwritten signatures; never embed full-page screenshots as the main content.
   - Preserve page breaks and basic page geometry.

6. **q7 / 122B judge and bounded repair**
   - Render source PDF and output DOCX to PNG.
   - Ask `qwen3.6-35b-a3b-q7` or `qwen3.5-122b-a10b:ud-q4-k-xl` to identify defects and provide structured suggestions.
   - Apply only deterministic patch adapters (margins, font size, paragraph spacing, table width, text replacement with source evidence, region order fixes). Do not let the model write arbitrary OOXML.

7. **QA gates**
   - DOCX must contain editable text and not just page images.
   - English translation target must pass residual CJK QA.
   - `pdfdocx5.py qa` failure severity `major` or `critical` means do not claim high-fidelity completion.
   - Human POV contact sheet must be inspected before claiming client-deliverable quality.

## Lessons from the second MNDA behavior verification

The agent improved by using `pdfdocx5.py convert --path xb`, but still failed because:

- it used a generic paragraph translator on page-sized OCR paragraphs;
- one long page failed/returned empty and the script still saved a DOCX;
- residual Chinese remained high;
- `pdfdocx5` itself reported critical visual failure, but the agent ignored the report;
- it briefly used `local_document_export`, which is not the correct OpenWebUI `/api/exports` route.

Therefore the skill must emphasize deterministic validators and explicit failure states more strongly than natural-language instructions.
