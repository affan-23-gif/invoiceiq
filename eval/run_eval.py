"""Evaluate extraction + decisions on the 18 sample invoices.

For each extractor (default: gemini and the regex baseline) it measures:
- header field accuracy (vendor, number, dates, PO, currency, subtotal, tax, total)
- line-item recall and precision (a line counts when qty, unit price and amount all match)
- decision accuracy (auto-approve / review / reject) and whether each planted problem was caught
- false alarms (issues raised on an invoice that should have none of them)
- time and cost

    python -m eval.run_eval                       # gemini + regex
    python -m eval.run_eval --extractors regex    # offline
Writes eval/results.md and eval/results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_tmp = Path(tempfile.mkdtemp(prefix="invoiceiq-eval-"))
os.environ["DB_PATH"] = str(_tmp / "eval.db")
os.environ["UPLOAD_DIR"] = str(_tmp / "uploads")
sys.path.insert(0, str(HERE.parent))

from rapidfuzz import fuzz  # noqa: E402

from app import checks  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.extract import DailyQuotaExceeded, Extraction, FakeExtractor, GeminiExtractor, RegexExtractor  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

HEADER = ["vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "currency", "subtotal", "tax", "total"]


def field_ok(name: str, got, want) -> bool:
    if want in (None, "") and got in (None, ""):
        return True
    if got in (None, "") or want in (None, ""):
        return False
    if name in {"subtotal", "tax", "total"}:
        return abs(float(got) - float(want)) <= 0.01
    if name == "vendor_name":
        return fuzz.token_sort_ratio(checks.norm_name(got), checks.norm_name(want)) >= 90
    if name == "po_number":
        return checks.norm_po(got) == checks.norm_po(want)
    if name == "invoice_number":
        return checks.norm_invoice_number(got) == checks.norm_invoice_number(want)
    return str(got).strip().upper() == str(want).strip().upper()


def lines_score(got: list, want: list) -> tuple[int, int, int]:
    """(matched, predicted, expected) using qty, unit price and amount."""
    pool = list(got)
    hit = 0
    for w in want:
        for i, g in enumerate(pool):
            if all(g.get(k) is not None and abs(float(g[k]) - float(w[k])) <= 0.01 for k in ("quantity", "unit_price", "amount")):
                hit += 1
                pool.pop(i)
                break
    return hit, len(got), len(want)


class CachingExtractor:
    """Wraps an extractor so a file is only sent to the model once per run."""

    def __init__(self, inner):
        self.inner, self.name, self.cache = inner, inner.name, {}

    def extract(self, content: bytes, mime: str) -> Extraction:
        key = hash(content)
        if key not in self.cache:
            self.cache[key] = self.inner.extract(content, mime)
        return self.cache[key]


def run(extractor_name: str, cases: list[dict]) -> dict:
    inner = {"gemini": lambda: GeminiExtractor(settings.gemini_api_key, settings.extract_model, settings.gemini_rpm),
             "regex": RegexExtractor, "fake": FakeExtractor}[extractor_name]()
    ex = CachingExtractor(inner)
    rows = []
    for case in cases:
        db = DB(settings.db_path)
        db.seed_reference()
        pipe = Pipeline(db, ex)
        try:
            for req in case["requires"]:
                r = next(c for c in cases if c["id"] == req)
                pipe.process((settings.samples_dir / r["file"]).read_bytes(), r["file"])
            content = (settings.samples_dir / case["file"]).read_bytes()
            t0 = time.perf_counter()
            inv = pipe.process(content, case["file"])
            secs = time.perf_counter() - t0
        except DailyQuotaExceeded as exc:
            print(f"\nStopped: {exc}")
            db.close()
            break
        db.close()
        truth, data = case["truth"], inv["data"] or {}
        fields = {f: field_ok(f, data.get(f), truth.get(f)) for f in HEADER}
        hit, pred, exp = lines_score(data.get("line_items") or [], truth["line_items"])
        got_codes = sorted({i["code"] for i in inv["issues"] if i["severity"] != "info"})
        want = case["expect"]
        caught = set(want["issues"]) <= set(got_codes)
        false_alarms = sorted(set(got_codes) - set(want["issues"]))
        row = {"id": case["id"], "kind": case["kind"], "layout": case["layout"], "note": case["note"],
               "fields_ok": sum(fields.values()), "fields_total": len(HEADER),
               "wrong_fields": [f for f, ok in fields.items() if not ok],
               "lines_hit": hit, "lines_pred": pred, "lines_expected": exp,
               "decision": inv["status"], "expected_decision": want["decision"], "decision_ok": inv["status"] == want["decision"],
               "issues": got_codes, "expected_issues": want["issues"], "problem_caught": caught, "false_alarms": false_alarms,
               "seconds": round(secs, 2), "cost_usd": inv.get("cost_usd", 0)}
        if inv["status"] == "failed":
            row["error"] = inv["issues"][0]["message"]
        rows.append(row)
        print(f"  {case['id']} {case['kind']:<5} fields {row['fields_ok']}/{len(HEADER)}  lines {hit}/{exp}  "
              f"{'OK ' if row['decision_ok'] else 'BAD'} {inv['status']:<13} {got_codes}"
              + (f"\n      error: {row['error']}" if row.get("error") else
                 (f"  wrong: {row['wrong_fields']}" if row["wrong_fields"] else "")))
    n = len(rows)
    if not n:
        return {"extractor": ex.name, "rows": []}
    seeded = [r for r in rows if r["expected_issues"]]
    clean = [r for r in rows if not r["expected_issues"]]
    scans = [r for r in rows if r["kind"] == "image"]
    lp, le = sum(r["lines_pred"] for r in rows), sum(r["lines_expected"] for r in rows)
    lh = sum(r["lines_hit"] for r in rows)
    return {
        "extractor": ex.name, "n": n, "rows": rows,
        "field_accuracy": sum(r["fields_ok"] for r in rows) / (n * len(HEADER)),
        "scan_field_accuracy": (sum(r["fields_ok"] for r in scans) / (len(scans) * len(HEADER))) if scans else None,
        "line_recall": lh / le if le else 0, "line_precision": lh / lp if lp else 0,
        "decision_accuracy": sum(r["decision_ok"] for r in rows) / n,
        "problems_caught": sum(r["problem_caught"] for r in seeded), "problems_total": len(seeded),
        "clean_auto_approved": sum(r["decision"] == "auto_approved" for r in clean), "clean_total": len(clean),
        "avg_seconds": sum(r["seconds"] for r in rows) / n, "total_cost": sum(r["cost_usd"] for r in rows),
    }


def report(results: list[dict]) -> str:
    res = [r for r in results if r.get("rows")]
    pct = lambda x: "–" if x is None else f"{x:.0%}"  # noqa: E731
    lines = ["# InvoiceIQ eval results", "",
             "18 invoices: 3 layouts, 4 scanned images, US and EU number/date formats, 10 clean and 8 with a planted "
             "problem. Each invoice starts from a fresh database.", "",
             "| Metric | " + " | ".join(f"`{r['extractor']}`" for r in res) + " |",
             "|---|" + "---|" * len(res)]
    metrics = [
        ("Header fields correct (9 per invoice)", lambda r: pct(r["field_accuracy"])),
        ("…on scanned images only", lambda r: pct(r["scan_field_accuracy"])),
        ("Line items found (recall)", lambda r: pct(r["line_recall"])),
        ("Line items correct (precision)", lambda r: pct(r["line_precision"])),
        ("Final decision correct", lambda r: pct(r["decision_accuracy"])),
        ("Planted problems caught", lambda r: f"{r['problems_caught']}/{r['problems_total']}"),
        ("Clean invoices auto-approved", lambda r: f"{r['clean_auto_approved']}/{r['clean_total']}"),
        ("Avg time per invoice", lambda r: f"{r['avg_seconds']:.1f}s"),
        ("Total cost (18 invoices)", lambda r: f"${r['total_cost']:.4f}"),
    ]
    for label, fn in metrics:
        lines.append(f"| {label} | " + " | ".join(fn(r) for r in res) + " |")
    for r in res:
        lines += ["", f"## `{r['extractor']}` per invoice", "",
                  "| # | Type | Fields | Lines | Decision | Issues raised | Notes |", "|---|---|---|---|---|---|---|"]
        for row in r["rows"]:
            notes = []
            if row["wrong_fields"]:
                notes.append("wrong: " + ", ".join(row["wrong_fields"]))
            if row["false_alarms"]:
                notes.append("extra: " + ", ".join(row["false_alarms"]))
            if row.get("error"):
                notes.append("extraction failed: " + row["error"][:120].replace("|", "/"))
            if not row["problem_caught"]:
                notes.append("missed: " + ", ".join(sorted(set(row["expected_issues"]) - set(row["issues"]))))
            lines.append(f"| {row['id']} | {row['kind']}, {row['layout']} | {row['fields_ok']}/{row['fields_total']} | "
                         f"{row['lines_hit']}/{row['lines_expected']} | {'✅' if row['decision_ok'] else '❌'} {row['decision']} | "
                         f"{', '.join(row['issues']) or '–'} | {'; '.join(notes)} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractors", default="gemini,regex")
    ap.add_argument("--only", default="", help="comma-separated case ids")
    args = ap.parse_args()
    cases = [json.loads(p.read_text()) for p in sorted(settings.samples_dir.glob("c*.json"))]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep or any(c["id"] in x["requires"] for x in cases if x["id"] in keep)]
    results = []
    for name in args.extractors.split(","):
        print(f"\n== {name}")
        results.append(run(name.strip(), cases))
    (HERE / "results.json").write_text(json.dumps(results, indent=2, default=str))
    (HERE / "results.md").write_text(report(results))
    for r in results:
        if r.get("rows"):
            print(f"\n{r['extractor']}: fields {r['field_accuracy']:.0%} · lines {r['line_recall']:.0%} · decisions "
                  f"{r['decision_accuracy']:.0%} · problems caught {r['problems_caught']}/{r['problems_total']} · "
                  f"${r['total_cost']:.4f}")
    print("see eval/results.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
