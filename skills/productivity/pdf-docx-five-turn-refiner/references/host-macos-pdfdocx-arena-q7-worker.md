# Host macOS PDF→DOCX Arena with tmux Hermes q7 Worker

Use this pattern when Noel wants to compare multiple PDF→DOCX projects/pipelines outside OpenWebUI 8083 on the macOS host, with a local q7 Hermes worker as the main agent.

## Core role split

- **Main arena agent brain:** tmux-launched Hermes Agent using LM Studio/OpenAI-compatible endpoint `http://localhost:1234/v1` and model `qwen3.6-35b-a3b-q7-mtp`.
- **Project/VLM model:** existing PDF→DOCX projects should use the separate visual/VLM endpoint `http://localhost:11234/v1`, API key `change-me-local-key`, model `qwen3.6-35b-a3b-q7` for OCR/layout/visual judging where supported.
- **Supervisor Hermes:** the current chat agent should configure, launch, monitor, and independently verify; it should not be the main arena executor.

Do not conflate these endpoints. The 1234 q7-mtp endpoint is the worker's agent brain; 11234 q7 is the pipeline/VLM judge/advisor endpoint.

## Setup pattern

1. Create a dedicated Hermes profile so the q7-mtp worker does not mutate the default profile:

```bash
hermes profile create pdfdocxarenaq7 --clone \
  --description 'Host macOS PDF-to-DOCX arena worker using LM Studio qwen3.6-35b-a3b-q7-mtp as Hermes agent brain.'
hermes --profile pdfdocxarenaq7 config set model.provider lmstudio
hermes --profile pdfdocxarenaq7 config set model.default qwen3.6-35b-a3b-q7-mtp
hermes --profile pdfdocxarenaq7 config set model.base_url http://localhost:1234/v1
```

2. Set profile `.env` values for both layers:

```text
LM_API_KEY=change-me-local-key
LM_BASE_URL=http://localhost:1234/v1
PDFDOCX_ARENA_VLM_BASE_URL=http://localhost:11234/v1
PDFDOCX_ARENA_VLM_MODEL=qwen3.6-35b-a3b-q7
PDFDOCX_ARENA_VLM_API_KEY=change-me-local-key
```

3. Smoke-test the worker brain before launching a long arena:

```bash
hermes --profile pdfdocxarenaq7 chat -Q \
  --provider lmstudio -m qwen3.6-35b-a3b-q7-mtp \
  -q 'Reply exactly: READY_Q7_MTP' --max-turns 1
```

4. Probe `/v1/models` on both endpoints and record the exact model IDs available.

## Pre-arena safety

Before allowing H3-level small patches:

- Record `git status --short`, branch, and HEAD for every repo.
- Save `git diff --binary` for tracked changes.
- Snapshot untracked files with a null-delimited file list and tarball.
- For non-git or unclear nested repos, create a source snapshot excluding `.venv`, caches, and outputs.

Keep these under the run directory, e.g. `arena_outputs/<timestamp>/backups/<project>/`.

## Run directory shape

```text
arena_outputs/<timestamp>/
  inputs/pdf_manifest.json
  inputs/pdf_manifest.csv
  worker_prompt.md
  run_worker.py
  monitor_arena.py
  backups/<project>/...
  outputs/<pipeline_id>/<pdf_id>__<safe_stem>/
  reports/progress.md
  reports/arena_results.jsonl
  reports/arena_results.csv
  reports/final_report.md
  logs/worker_hermes_stdout.log
```

Use a manifest that distinguishes expected attempt slots from actual `.docx` files. For 12 PDFs × 3 pipelines, report 36 attempts even if some slots fail/timeout and produce no DOCX.

## Worker prompt essentials

The tmux Hermes worker prompt should be self-contained and include:

- The three project paths and exact corpus manifest path.
- Sequential execution requirement.
- Timeout policy: digital ≈20 min; scanned/OCR up to 30–60 min if useful.
- Project VLM endpoint env vars for 11234 q7.
- H3 mutation policy: project-local venvs/dependencies and small source patches allowed; no commit/push/reset; record every patch.
- Anti-cheating gate: full-page raster/screenshot-backed DOCX is `FAIL`.
- Full conversion of every PDF page; q7 visual QA may sample pages, but conversion itself must not be page-1-only.
- Required output fields per PDF×pipeline attempt.

## Launch pattern

Use tmux so the q7 worker is observable and durable:

```bash
tmux new-session -d -s pdfdocx-arena-<HHMMSS> -x 200 -y 60 \
  "zsh -lc 'cd /Users/admin && /usr/bin/env python3 <run_dir>/run_worker.py; tmux wait-for -S pdfdocx-arena-<HHMMSS>-done'"
```

Inside `run_worker.py`, call:

```bash
hermes --profile pdfdocxarenaq7 chat --cli -Q --yolo \
  --provider lmstudio -m qwen3.6-35b-a3b-q7-mtp \
  --toolsets terminal,file,skills,todo,vision \
  --skills ocr-and-documents,pdf-docx-five-turn-refiner \
  --max-turns 300 -q "$(cat worker_prompt.md)"
```

Log stdout to `logs/worker_hermes_stdout.log`. A background parent process can wait on the tmux signal and notify on completion.

## Monitoring pattern

Create a low-noise script-only cron monitor under `~/.hermes/scripts/` (cron script paths must be relative to that directory). The monitor should inspect only:

- result JSONL row count vs expected attempts,
- `.docx` count under outputs,
- status counts,
- tmux session presence,
- worker exit code and final report existence.

Emit Discord output only on progress/change or a long heartbeat; otherwise be silent. This prevents long gateway messages and token waste.

## Result taxonomy

Each row should include:

- `pipeline_id`, `project_path`, `pdf_id`, `pdf_path`, `source_pages`, `input_classification`
- `attempt_status`: `PASS | PARTIAL | FAIL | TIMEOUT | SKIPPED`
- `output_docx`, `output_docx_exists`
- `render_ok`, `rendered_pages`
- `no_full_page_raster`: `PASS | FAIL | UNKNOWN`
- `editable_text_chars`, `native_table_count`, `media_count`
- `q7_judged`, `q7_human_acceptability`, `q7_manual_rework`
- `contact_sheet`, `error_summary`, `notes`

Never fabricate missing DOCX outputs; failed/timeout slots remain slots with failure rows.
