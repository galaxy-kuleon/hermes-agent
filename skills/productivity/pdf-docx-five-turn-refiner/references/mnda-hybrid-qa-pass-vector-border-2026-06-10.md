# MNDA hybrid PDF QA-pass repair: vector border + isolated LibreOffice

## When this applies

Use this pattern for hybrid/image-backed PDFs such as DocuSign contracts where:

- `pdfdocx5.py detect` reports `hybrid_image_backed_pdf` or page-sized raster bodies.
- The source has sparse selectable text but the body is a scanned/page image.
- XB direct OCR produces usable editable text, but source-vs-rendered QA fails with ink-bbox penalties such as:
  - `visual_ink_bbox_shift`
  - `visual_ink_height_ratio_bad`
  - source ink bbox spans nearly the full page while DOCX render ink does not.

## Durable lesson

Do **not** fix this by embedding a full-page screenshot/background image into the DOCX. That violates Noel's editable-DOCX requirement.

The safe deterministic repair is to reconstruct only the page-edge scan boundary as OOXML vector geometry:

- Add `w:pgBorders` to sections for page-image-backed sources.
- Keep OCR text as editable DOCX text.
- Keep `media_count=0` or only small real asset crops (logos, signatures, seals) when justified by source regions.

This can move a truthful but visually sparse XB output from `major/critical` to `minor/ok` without screenshot cheating.

## Implementation points

In `scripts/pdfdocx5.py`:

1. Add a helper like `source_needs_vector_page_border(pdf)` using `probe_pdf(pdf)`:
   - true when `page_image_pages > 0` and `max_image_area_ratio >= 0.65`.
2. Add `add_vector_page_border(section)`:
   - writes `w:sectPr/w:pgBorders` with `w:offsetFrom="page"`.
   - add top/left/bottom/right `w:val="single"`, e.g. `w:sz="18"`, `w:space="0"`, `w:color="000000"`.
3. In `normalize_docx()`, after source page-size normalization, apply the border to each section when `source_needs_vector_page_border(pdf)` is true.
4. Record a patch flag such as `vector_page_border_for_page_image_source=true` in the refine report.

## LibreOffice render pitfall

Inside the 8083 Hermes container, `soffice` may hang under the `hermes` user if the default LibreOffice profile under `/home/hermes/.config/libreoffice` is stale or root-owned from earlier root runs.

Fix the render helper instead of relying on global profile state:

```python
profile_dir = Path(tempfile.mkdtemp(prefix="lo-profile-", dir=str(out_dir)))
cmd = [
    "soffice",
    "--headless",
    f"-env:UserInstallation={profile_dir.as_uri()}",
    "--convert-to", "pdf",
    "--outdir", str(out_dir),
    str(docx),
]
```

Clean up the temp profile after conversion.

## Verification recipe

Run from inside the `hermes` container as user `hermes`:

```bash
uv run /home/hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py detect source.pdf
uv run /home/hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py convert source.pdf -o /tmp/out.docx --path xb --turns 2 --ocr-engine direct --work-dir /tmp/pdfdocx5-work
uv run /home/hermes/skills/productivity/pdf-docx-five-turn-refiner/scripts/pdfdocx5.py qa source.pdf /tmp/out.docx --contact-sheet --judge-pages risk --work-dir /tmp/pdfdocx5-qa
```

For translated DOCX tasks, then run the hard gate:

```bash
python3 /home/hermes/skills/software-development/anything-to-docx/scripts/translate_docx_weak_llm_gate.py \
  /tmp/out.docx /tmp/out_en.docx \
  --source-pdf source.pdf \
  --target-language English \
  --source-qa-timeout 360
```

Expected pass shape for the MNDA-style case:

- source-layout QA may remain `minor` due to changed text geometry, but must not be `major` or `critical`.
- translation gate returns `PASS`.
- final DOCX has very low/no residual CJK for English target.
- final DOCX is not image-backed (`media_count` should be 0 unless small legitimate crops are present).

## 8083 behavior-verification distinction

A direct container run proves the runtime tool works. It does **not** prove OpenWebUI 8083 invoked the skill correctly.

For 8083 behavior evidence, create or use an OpenWebUI chat through `/api/chat/completions` with model `hermes-agent`, ask the agent to run exact `pdfdocx5.py detect/qa` commands, then verify:

- `/api/tasks/chat/<chat_id>` is empty after completion.
- the assistant message contains the expected compact result.
- `/home/hermes/sessions/session_api-*-chat-<chat_id>.json` contains the terminal/tool call and the expected `pdfdocx5.py` output.
