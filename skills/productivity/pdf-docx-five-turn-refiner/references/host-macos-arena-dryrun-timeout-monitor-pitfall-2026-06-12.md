# Host macOS PDF→DOCX arena pitfall: dry-run false completion, timeout policy, and monitor truth

## Trigger

Use this when supervising a host-macOS PDF→DOCX arena across multiple converter projects, especially the 12 PDFs × 3 projects = 36-slot pattern.

## What went wrong

A tmux-launched Hermes/q7 worker created an arena harness and initially recorded `36/36` rows, but they were all `SKIPPED (dry-run)` with zero DOCX files. A later partial rerun executed only `pdf_to_docx_research` instead of all three projects. The monitor initially counted filesystem `.docx` files and could have been confused by stale outputs; the user explicitly wanted notification only when 36 real DOCX files were ready.

## Durable workflow corrections

1. **Never count attempt rows as completion unless they are real attempts.** Treat `SKIPPED`, `dry-run`, and `output_docx_exists=false` as not ready.
2. **Monitor from the current result manifest, not loose filesystem globbing.** Count only rows whose current `output_docx` exists and whose `output_docx_exists` is true. Stale DOCX files from old/dry runs must not trigger READY.
3. **Make the runner order explicit and observable.** For a 3-project arena, log and enforce project-major ordering:
   - project A all PDFs
   - project B all PDFs
   - project C all PDFs
   or whatever ordering the user requested. Do not rely on an agent promise.
4. **Use page-aware generous slot timeouts.** A durable host-side default:
   - digital: `1800 + 300s/page` minimum 30 min
   - scanned/OCR: `3600 + 600s/page` minimum 60 min
   - q7/layout-heavy project: add about `300s/page`
   - cap each slot around 4h unless the user asks otherwise
   - q7/OCR per-request timeout should also scale by page count and be at least 900s.
5. **If an off-contract runner is active, stop it before starting the corrected runner.** Kill the scoped tmux session and scoped child process group, then reset monitor notification state. Do not leave competing runners writing the same result files.
6. **Archive or suffix old reports before a real rerun.** Keep old `dryrun-*` evidence but make the current `arena_results.jsonl`/CSV represent the active run only.
7. **Report “36 slots” separately from “36 actual DOCX files”.** A slot can be PASS/FAIL/TIMEOUT/PARTIAL; the user’s notification condition may be stricter: 36 actual DOCX artifacts ready.

## Minimal monitor logic

```python
rows = [json.loads(line) for line in results_jsonl.read_text().splitlines() if line.strip()]
ready_docx = {
    row['output_docx']
    for row in rows
    if row.get('output_docx_exists') and row.get('output_docx') and Path(row['output_docx']).exists()
}
if len(ready_docx) >= 36:
    print('READY')
```

Also emit one blocked/partial notification if the runner exits before the requested artifact count is reached.

## Action-ledger expectation

When correcting a live arena, report:

- observed only: pane captures, result counts, logs inspected
- controlled: tmux sessions killed/started, scripts patched, monitor updated, process waiter created
- exact session/process IDs
- current real progress: attempts, actual DOCX count, status counts, pipeline currently running
