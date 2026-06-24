#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pymupdf>=1.24.0",
#   "pdf2docx>=0.5.8",
#   "python-docx>=1.1.0",
#   "lxml>=5.0.0",
#   "pillow>=10.0.0",
#   "requests>=2.31.0",
# ]
# ///
"""Autonomous PDF→DOCX battle runner.

Find PDFs under ~/Works, select high-value candidates, run bounded pdfdocx5
conversion/refinement, and rank failures by composite QA score.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent / "pdfdocx5.py"
SKILL_DIR = SCRIPT.parent.parent
DEFAULT_CORPUS = SKILL_DIR / "references" / "battle-test-corpus.json"


def load_pdfdocx5():
    spec = importlib.util.spec_from_file_location("pdfdocx5", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pdfdocx5"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def corpus_paths(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x.get("path")) for x in data.get("items", []) if x.get("path")}
    except Exception:
        return set()


def discover_candidates(root: Path, limit_probe: int, include_existing: bool, corpus: Path, include_xb: bool = False) -> list[dict[str, Any]]:
    m = load_pdfdocx5()
    known = corpus_paths(corpus)
    priority_terms = [
        "NEW_DIR_20250910",
        "anything-to-docx-v3/test-inputs",
        "glm-ocr-latest-test/input",
        "vlm_docx_battle",
        "data_scanned",
        "docling/tests/data/pdf",
    ]
    pdfs = []
    for p in root.rglob("*.pdf"):
        sp = str(p)
        if not include_existing and sp in known:
            continue
        if "/.venv/" in sp or "/node_modules/" in sp:
            continue
        pri = 0 if any(t in sp for t in priority_terms) else 1
        pdfs.append((pri, len(sp), p))
    pdfs = [p for _, _, p in sorted(pdfs)[:limit_probe]]
    out = []
    for p in pdfs:
        try:
            probe = m.probe_pdf(p)
            ev = m.pdf_page_evidence(p, max_pages=5)
            summ = m.summarize_pdf_evidence(ev)
            if probe.route == "xb" and not include_xb:
                continue
            source_table_count = int(summ.get("source_tables_detected", 0))
            source_image_count = int(summ.get("source_images_detected", 0))
            # Battle selection should prefer semantically table-heavy PDFs; otherwise image-heavy slide decks
            # dominate the queue and do not advance table-grid reconstruction.
            score = source_table_count * 40 + min(source_image_count, 20) * 2 + int(summ.get("max_risk_score", 0))
            if source_table_count == 0:
                score -= 80
            if probe.pages and probe.pages >= 3:
                score += 6
            if probe.route in {"xb", "mixed"}:
                score += 10
            out.append({
                "path": str(p),
                "name": p.name,
                "route": probe.route,
                "pages": probe.pages,
                "digital_ratio": probe.digital_ratio,
                "text_chars": probe.total_text_chars,
                "risk_score": score,
                "evidence": summ,
            })
        except Exception as exc:
            out.append({"path": str(p), "error": repr(exc), "risk_score": -1})
    return sorted([x for x in out if "error" not in x], key=lambda x: x["risk_score"], reverse=True)


def run_case(item: dict[str, Any], out_root: Path, max_pages: int, overlap_tolerant_xa: bool = False, xa_strategy: str = "default") -> dict[str, Any]:
    cid = Path(item["path"]).stem.replace(" ", "_").replace("/", "_")[:80]
    work = out_root / cid
    out_docx = out_root / f"{cid}.docx"
    route = "xa" if item.get("route") in {"xa", "mixed"} else "xb"
    cmd = [
        "uv", "run", str(SCRIPT), "convert", item["path"],
        "-o", str(out_docx), "--path", route,
        "--turns", "5", "--max-pages", str(min(max_pages, int(item.get("pages") or max_pages))),
        "--work-dir", str(work), "--judge-pages", "1", "--contact-sheet",
    ]
    if route == "xa":
        cmd += ["--compact-on-overflow"]
        if xa_strategy in {"auto", "overlap099"}:
            cmd += ["--xa-strategy", xa_strategy]
        if overlap_tolerant_xa:
            # Alternative XA lane for overlap-heavy forms: preserve pdf2docx geometry and relax overlap
            # filtering instead of adding plaintext recovery fallback.
            cmd += ["--xa-strategy", "overlap099"]
        elif xa_strategy == "default":
            cmd += ["--normalize-tables", "--recover-missing-text"]
        else:
            # Auto/overlap lanes manage per-lane table normalization/recovery internally.
            pass
    else:
        cmd += ["--ocr-engine", "auto"]
    start = time.time()
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=900)
    dur = round(time.time() - start, 1)
    if proc.returncode != 0:
        return {"path": item["path"], "ok": False, "duration_s": dur, "returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:]}
    data = json.loads(proc.stdout[proc.stdout.find("{"):proc.stdout.rfind("}") + 1])
    refine = data.get("refine", {})
    turns = refine.get("turns", [])
    best_turn = refine.get("best_turn")
    if isinstance(best_turn, int) and 1 <= best_turn <= len(turns):
        qa = turns[best_turn - 1].get("qa", {})
    else:
        qa = turns[-1].get("qa", {}) if turns else {}
    return {
        "path": item["path"], "ok": True, "duration_s": dur, "route": data.get("selected_route"),
        "report_path": data.get("report_path"), "final_docx": data.get("refine", {}).get("final_docx"),
        "best_turn": data.get("refine", {}).get("best_turn"), "best_failure_score": data.get("refine", {}).get("best_failure_score"),
        "render_ok": qa.get("render_ok"), "page_count_delta": qa.get("page_count_delta"),
        "table_qa": qa.get("table_qa"), "table_geometry_status": qa.get("table_geometry_status"),
        "failure_score": qa.get("failure_score"), "contact_sheet": qa.get("contact_sheet"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/Users/admin/Works")
    ap.add_argument("--out", default="/tmp/pdfdocx5-auto-battle")
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--probe-limit", type=int, default=300)
    ap.add_argument("--run-limit", type=int, default=3)
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--include-existing", action="store_true")
    ap.add_argument("--include-xb", action="store_true", help="Allow slow scanned/OCR XB candidates; default prefers fast XA/mixed PDFs")
    ap.add_argument("--overlap-tolerant-xa", action="store_true", help="XA battle lane: use pdf2docx line_overlap_threshold=0.99 and preserve candidate geometry instead of plaintext recovery")
    ap.add_argument("--xa-strategy", choices=["default", "overlap099", "auto"], default="default", help="XA strategy to pass to pdfdocx5 convert")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    candidates = discover_candidates(Path(args.root), args.probe_limit, args.include_existing, Path(args.corpus), include_xb=args.include_xb)
    selected = candidates[: args.run_limit]
    results = []
    for item in selected:
        results.append(run_case(item, out_root, args.max_pages, overlap_tolerant_xa=args.overlap_tolerant_xa, xa_strategy=args.xa_strategy))
    ranked = sorted(results, key=lambda x: int(((x.get("failure_score") or x.get("best_failure_score") or {}).get("score", 999)) if x.get("ok") else 999), reverse=True)
    report = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "selected": selected, "results": results, "ranked_failures": ranked}
    path = out_root / "auto-battle-report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "report": str(path), "selected_count": len(selected), "results": results}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
