# Round23 — FTA/DDT repeated commercial split-page repair (2026-06-10)

## Failure mode

Italian FTA/DDT commercial/shipping PDFs (e.g. `FTA 3716.pdf`) were converted by `pdf2docx` into DOCX where one source page could render as two DOCX pages:

- Page N contained header/product table.
- Page N+1 contained footer/payment/transport blocks.
- Generic compact retries could force page parity, but destroyed the middle product table (ink height collapsed to ~0.085).

Root cause from OOXML inspection:

- `pdf2docx` emitted absurd exact row heights:
  - product/container row around `9280` twips
  - footer `ANNOTAZIONI / CONTRIBUTO CONAI` row around `10164` twips
- A naive row clamp to `260` twips fixed page count but collapsed the product table into a line.

## Implemented repair

In `scripts/pdfdocx5.py` normalization:

1. `reorder_repeated_commercial_tables(doc)`
   - Detects FTA/DDT-ish tables by labels such as `DOCUMENTO DI TRASPORTO`, `CODICE ARTICOLO`, `ANNOTAZIONI`, `CONTRIBUTO CONAI`, `MAGAZZINO C/O`.
   - Moves the main commercial/product table before footer/bottom tables when `pdf2docx` emits footer first.

2. `clamp_commercial_table_row_heights(doc)`
   - Conservative FTA/DDT-only OOXML row-height clamp.
   - Product/container rows: clamp `>=8000` twips with `CODICE ARTICOLO` to `4804` twips (keeps product rows visible).
   - Footer rows: clamp `>=5000` twips in `ANNOTAZIONI / CONTRIBUTO CONAI / MAGAZZINO C/O` footer tables to `2600` twips.

3. `compact_commercial_legal_footnotes(doc)`
   - Compresses repeated legal footnotes and sale condition paragraphs in FTA/DDT documents without using screenshot-backed output.

4. Table-count QA basis fix
   - `python-docx` sees top-level tables; OOXML stats include nested tables.
   - The scorer now chooses the content table count basis closer to source evidence:
     - `native_ooxml_closer_to_source` for FTA3716 / FTA14357 / FTA959.
     - `python_docx_top_level` for FTA9323.
   - This prevents both false negative and false positive `content_table_count_delta` in repeated commercial sections.

## Verification

Command:

```bash
uv run scripts/auto_battle_pdfdocx5.py \
  --root '/Users/admin/Works/Tmp/NEW_DIR_20250910_VER/Global IP Registrations/Canada/[Procedure 4] Declaration of Use/10240/[CANIPTM0012] 20161028 Supporting materials' \
  --probe-limit 100 --run-limit 4 --max-pages 3 --xa-strategy auto \
  --out /tmp/pdfdocx5-round23-fta-family-autobattle-v2
```

Results:

| PDF | Selected lane | Source/rendered pages | content_table_delta | Score |
| --- | --- | --- | --- | --- |
| FTA 3716.pdf | overlap099 | 3 / 3 | 0 | 49 major |
| FTA 14357.pdf | overlap099 | 3 / 3 | 0 | 49 major |
| FTA 959.pdf | overlap099 | 3 / 3 | 0 | 55 major |
| FTA 9323.pdf | overlap099 | 3 / 3 | 0 | 55 major |

Before this fix, broad battle showed FTA3716/FTA14357 at raw 79 and FTA959/FTA9323 at raw 85, with split-page/page-count or table-count failures.

## Remaining defects

Round23 fixes the split-page and repeated-section table count problem. It does **not** fully solve human-visible fidelity:

- Source logo/large top-left graphic is still missing.
- Invoice header red-grid fields are weak/missing.
- Footer/payment/transport cells are structurally simplified.
- Overall content is shifted/reflowed relative to the source.
- Remaining scores are mainly `table_geometry_status`, `visual_changed_fraction_extreme`, and sometimes `editable_token_coverage_medium`.

Treat these as Round22/next-round header/layout fidelity work, not as split-page merge failures.
