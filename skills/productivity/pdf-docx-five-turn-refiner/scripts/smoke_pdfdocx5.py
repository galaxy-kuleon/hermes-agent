#!/usr/bin/env python3
"""Regression smoke runner for pdf-docx-five-turn-refiner.

Runs quick local checks without q7 by default:
- digital route detection
- XA conversion/render QA
- bounded multi-page XA + compact overflow + table normalization
- contact sheet generation

Use --include-xb for the slower scanned/OCR smoke.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "pdfdocx5.py"
SKILL_DIR = SCRIPT.parent.parent
DEFAULT_WORK = Path("/tmp/pdfdocx5-regression-smoke")
DEFAULT_CORPUS = SKILL_DIR / "references" / "battle-test-corpus.json"
DIGITAL = Path("/Users/admin/Works/pdf2docx/test/samples/demo-text.pdf")
TABLE_SAMPLE = Path("/Users/admin/Works/pdf2docx/test/samples/demo-table.pdf")
LARGE = Path("/Users/admin/Works/pdf-to-docx-v5/examples/large.pdf")
SCANNED = Path("/Users/admin/Works/docling/tests/data_scanned/ocr_test.pdf")


def parse_json_lenient(text: str):
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def run(args: list[str], timeout: int = 600):
    cmd = ["uv", "run", str(SCRIPT)] + args
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return parse_json_lenient(proc.stdout)


def assert_true(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


def load_corpus(path: Path, include_slow: bool = False) -> list[dict[str, object]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for item in data.get("items", []):
        p = Path(str(item.get("path", "")))
        if not p.exists():
            continue
        if item.get("slow") and not include_slow:
            continue
        items.append(item)
    return items


def append_history(history_dir: Path, records: list[dict[str, object]]) -> dict[str, str]:
    history_dir.mkdir(parents=True, exist_ok=True)
    jsonl = history_dir / "metrics-history.jsonl"
    csv_path = history_dir / "metrics-history.csv"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = []
    for rec in records:
        metric = rec.get("metric") or {}
        row = {
            "timestamp": ts,
            "id": rec.get("id") or "",
            "pdf": rec.get("pdf") or "",
            "route": rec.get("route") or "",
            "render_ok": rec.get("render_ok"),
            "page_count_delta": rec.get("page_count_delta"),
            "table_count_delta": rec.get("table_count_delta"),
            "rmse_rgb_delta": metric.get("rmse_rgb_delta") if isinstance(metric, dict) else None,
            "mean_abs_rgb_delta": metric.get("mean_abs_rgb_delta") if isinstance(metric, dict) else None,
            "changed_channel_fraction_gt24": metric.get("changed_channel_fraction_gt24") if isinstance(metric, dict) else None,
            "status": "ok" if rec.get("render_ok") and rec.get("page_count_delta") == 0 else "warn",
        }
        rows.append(row)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_header = not csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["timestamp"])
        if write_header and rows:
            w.writeheader()
        for row in rows:
            w.writerow(row)
    return {"jsonl": str(jsonl), "csv": str(csv_path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(DEFAULT_WORK))
    ap.add_argument("--include-xb", action="store_true", help="Run slower scanned/OCR smoke")
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="Curated battle-test corpus manifest")
    ap.add_argument("--history-dir", default=None, help="Append stable metrics history JSONL/CSV here")
    ap.add_argument("--strict-table-geometry", action="store_true", help="Fail if table_geometry_status is not ok for table samples")
    args = ap.parse_args()

    work = Path(args.work_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    results: dict[str, object] = {"work_dir": str(work), "checks": [], "corpus": str(args.corpus)}
    history_records: list[dict[str, object]] = []

    det = run(["detect", str(DIGITAL)], timeout=120)
    assert_true(det["route"] == "xa", f"digital detect route expected xa, got {det.get('route')}")
    results["checks"].append({"name": "detect_digital", "route": det["route"], "pages": det["pages"]})

    xa_docx = work / "demo-text.smoke.docx"
    xa = run(["convert", str(DIGITAL), "-o", str(xa_docx), "--path", "xa", "--turns", "2", "--work-dir", str(work / "xa")], timeout=600)
    qa = xa["refine"]["turns"][-1]["qa"]
    assert_true(qa["render_ok"], "XA render failed")
    assert_true(qa["anti_cheating"]["has_visible_text"], "XA has no visible editable text")
    assert_true(qa["anti_cheating"]["full_page_raster_risk"] != "high", "XA raster cheating risk high")
    results["checks"].append({"name": "xa_convert", "render_ok": qa["render_ok"], "page_count_delta": qa.get("page_count_delta")})

    large_docx = work / "large-3p.compact-table.docx"
    large = run([
        "convert", str(LARGE), "-o", str(large_docx), "--path", "xa", "--turns", "5", "--max-pages", "3",
        "--work-dir", str(work / "large3"), "--compact-on-overflow", "--normalize-tables",
    ], timeout=600)
    qas = [t["qa"] for t in large["refine"]["turns"]]
    assert_true(qas[-1]["render_ok"], "large render failed")
    assert_true(qas[-1].get("page_count_delta") == 0, f"large page_count_delta expected 0, got {qas[-1].get('page_count_delta')}")
    assert_true(qas[-1]["table_qa"]["table_count_delta"] == 0, f"table_count_delta expected 0, got {qas[-1]['table_qa']['table_count_delta']}")
    if args.strict_table_geometry and qas[-1].get("table_geometry_status"):
        assert_true(qas[-1]["table_geometry_status"].get("ok", False), f"table_geometry_status failed: {qas[-1]['table_geometry_status']}")
    results["checks"].append({"name": "large_compact_table", "turns": len(qas), "final_page_count_delta": qas[-1].get("page_count_delta"), "table_qa": qas[-1]["table_qa"], "table_geometry_status": qas[-1].get("table_geometry_status")})

    contact = run(["qa", str(work / "large3" / "qa_source_subset.pdf"), str(large_docx), "--work-dir", str(work / "contact"), "--contact-sheet", "--judge-pages", "1"], timeout=300)
    sheet = Path(contact.get("contact_sheet", ""))
    assert_true(sheet.exists(), f"contact sheet missing: {sheet}")
    assert_true(contact.get("visual_diff_metrics"), "visual diff metrics missing")
    results["checks"].append({"name": "contact_sheet", "path": str(sheet), "bytes": sheet.stat().st_size, "diff": contact["visual_diff_metrics"][0]})

    before_docx = work / "large3" / "refine" / "turn_1.docx"
    compare = run(["compare", str(work / "large3" / "qa_source_subset.pdf"), str(before_docx), str(large_docx), "--work-dir", str(work / "compare"), "--judge-pages", "1"], timeout=300)
    three = Path(compare.get("before_after_contact_sheet", ""))
    assert_true(three.exists(), f"three-way contact sheet missing: {three}")
    assert_true(compare.get("visual_diff_metrics"), "three-way visual metrics missing")
    results["checks"].append({"name": "compare_three_way", "path": str(three), "bytes": three.stat().st_size, "metric": compare["visual_diff_metrics"][0]})

    # Mocked q7 suggestion coverage without live q7: apply a synthetic table_col_width suggestion.
    mock_report = work / "mock-q7-table.report.json"
    mock_report.write_text(json.dumps({"q7_judges": [{"normalized": {"fix_suggestions": [{"patch_type": "table_col_width", "target": "all native tables", "risk": "low"}]}}]}, indent=2), encoding="utf-8")
    patched = work / "large-3p.mockpatched.docx"
    patch = run(["apply-suggestions", str(work / "large3" / "qa_source_subset.pdf"), str(large_docx), str(mock_report), "-o", str(patched)], timeout=180)
    assert_true(patch["applied"], "mock q7 table suggestion did not apply")
    results["checks"].append({"name": "mock_q7_apply_suggestions", "applied": [x["result"]["patch_type"] for x in patch["applied"]]})

    mock_final_docx = work / "large-3p.mock-final.docx"
    mock_final = run([
        "refine", str(work / "large3" / "qa_source_subset.pdf"), str(large_docx), "-o", str(mock_final_docx),
        "--work-dir", str(work / "mock-final"), "--turns", "2", "--apply-q7-suggestions", "--mock-q7-report", str(mock_report),
    ], timeout=300)
    last_turn = mock_final["turns"][-1]
    assert_true("post_q7_patch_qa" in last_turn, "mock final-turn q7 did not trigger post_q7_patch_qa")
    assert_true(last_turn["post_q7_patch_qa"]["render_ok"], "mock final-turn post patch render failed")
    results["checks"].append({"name": "mock_q7_final_turn_refine", "post_q7_patch_qa": True, "final_stats": mock_final["final_stats"]})

    trend = []
    corpus_items = load_corpus(Path(args.corpus), include_slow=args.include_xb)
    fast_items = [x for x in corpus_items if x.get("route") == "xa"][:4] or [
        {"id": "demo_text", "path": str(DIGITAL), "route": "xa", "max_pages": 1},
        {"id": "demo_table", "path": str(TABLE_SAMPLE), "route": "xa", "max_pages": 1},
    ]
    for item in fast_items:
        pdf = Path(str(item["path"]))
        out_docx = work / "trend" / f"{item.get('id', pdf.stem)}.docx"
        max_pages = str(int(item.get("max_pages") or 1))
        cmd = ["convert", str(pdf), "-o", str(out_docx), "--path", str(item.get("route", "xa")), "--turns", "5", "--max-pages", max_pages, "--work-dir", str(work / "trend" / str(item.get("id", pdf.stem))), "--contact-sheet", "--judge-pages", "1"]
        if item.get("requires_compact_on_overflow"):
            cmd.append("--compact-on-overflow")
        if int(item.get("expect_tables_min") or 0) > 0:
            cmd.append("--normalize-tables")
        tr = run(cmd, timeout=600)
        final_qa = tr["refine"]["turns"][-1]["qa"]
        metric = (final_qa.get("visual_diff_metrics") or [{}])[0].get("source_vs_rendered")
        rec = {"id": item.get("id"), "pdf": str(pdf), "route": item.get("route", "xa"), "render_ok": final_qa["render_ok"], "page_count_delta": final_qa.get("page_count_delta"), "table_count_delta": final_qa.get("table_qa", {}).get("table_count_delta"), "metric": metric}
        trend.append(rec)
        history_records.append(rec)
    assert_true(all(x["render_ok"] for x in trend), "trend smoke render failure")
    results["checks"].append({"name": "multi_pdf_visual_trend", "items": trend})

    if args.history_dir:
        results["metrics_history"] = append_history(Path(args.history_dir), history_records)

    if args.include_xb:
        xb_docx = work / "ocr-test.smoke.docx"
        xb = run(["convert", str(SCANNED), "-o", str(xb_docx), "--path", "xb", "--turns", "2", "--max-pages", "1", "--work-dir", str(work / "xb"), "--ocr-engine", "sdk"], timeout=600)
        xbqa = xb["refine"]["turns"][-1]["qa"]
        assert_true(xbqa["render_ok"], "XB render failed")
        assert_true(xbqa["anti_cheating"]["has_visible_text"], "XB no editable OCR text")
        results["checks"].append({"name": "xb_sdk", "render_ok": xbqa["render_ok"], "text_chars": xb["conversion"].get("text_chars")})

    out = work / "smoke-result.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "result": str(out), **results}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
