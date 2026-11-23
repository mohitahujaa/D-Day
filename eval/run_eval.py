"""
End-to-end quality evaluation of the extraction pipeline against the REAL local model.

    python eval/run_eval.py                 # all cases
    python eval/run_eval.py --only gmail_sent_date postponed
    python eval/run_eval.py --model qwen3:4b --label qwen4b

Scores what matters for "minimal interaction":
  * recall      expected items found
  * exact       found AND auto-filled date + time correct (user only presses Save)
  * corrections edits the user would have to make: pick/fix a date or time (+1),
                skip a pre-selected false item (+1), add a missed item manually (+2)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extractor  # noqa: E402
import pipeline  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def matches(task, kws):
    title = (task.get("title") or "").lower()
    return any(k.lower() in title for k in kws)


def preselected(task):
    if "selected" in task:
        return bool(task["selected"])
    return task.get("applicability") == "explicit"


def score_case(case, tasks):
    remaining = list(tasks)
    rows, corrections = [], 0
    for exp in case.get("expect", []):
        hit = next((t for t in remaining if matches(t, exp["kw"])), None)
        if hit is None:
            rows.append({"kw": exp["kw"][0], "found": False})
            corrections += 2
            continue
        remaining.remove(hit)
        date_ok = hit.get("due_date") in exp["dates"]
        et = exp.get("time")
        if et == "any":
            time_ok = True
        elif et is None:
            time_ok = hit.get("due_time") is None
        else:
            time_ok = hit.get("due_time") == et
        end_ok = exp.get("end_time") is None or hit.get("end_time") == exp["end_time"]
        loc_ok = exp.get("location") is None or exp["location"].lower() in (hit.get("location") or "").lower()
        area_ok = exp.get("area") is None or hit.get("area") == exp["area"]
        fixes = (not date_ok) + (not time_ok) + (not end_ok) + (not area_ok)
        # A correct future item the user must still tick costs one click too.
        must_tick = not preselected(hit) and not hit.get("is_past")
        corrections += fixes + must_tick
        rows.append({"kw": exp["kw"][0], "found": True, "date": hit.get("due_date"), "time": hit.get("due_time"),
                     "date_ok": date_ok, "time_ok": time_ok, "end_ok": end_ok, "loc_ok": loc_ok,
                     "exact": fixes == 0, "title": hit.get("title"), "sel": preselected(hit),
                     "area": hit.get("area"), "area_ok": area_ok, "area_checked": exp.get("area") is not None})
    for opt in case.get("optional", []):
        hit = next((t for t in remaining if matches(t, opt["kw"])), None)
        if hit:
            remaining.remove(hit)
    false_pos = [t for t in remaining]
    intrusive = [t for t in false_pos if preselected(t)]
    corrections += len(intrusive)
    return rows, false_pos, intrusive, corrections


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--model")
    ap.add_argument("--label", default="run")
    ap.add_argument("--cases", default="cases.json", help="cases.json or holdout.json")
    args = ap.parse_args()
    if args.model:
        extractor.MODEL = args.model

    spec = json.load(open(os.path.join(HERE, args.cases), encoding="utf-8"))
    ref = datetime.fromisoformat(spec["reference_time"])
    cases = [c for c in spec["cases"] if not args.only or c["id"] in args.only]

    totals = {"expected": 0, "found": 0, "exact": 0, "loc_expected": 0, "loc_ok": 0,
              "false_pos": 0, "intrusive": 0, "corrections": 0, "latency": 0.0, "errors": 0}
    results = []
    for case in cases:
        t0 = time.perf_counter()
        try:
            out = pipeline.extract_tasks(case["text"], reference_datetime=ref, timezone="Asia/Kolkata")
            tasks, err = out["tasks"], None
        except Exception as exc:  # noqa: BLE001
            tasks, err = [], f"{type(exc).__name__}: {exc}"
            totals["errors"] += 1
        dt = time.perf_counter() - t0
        rows, fps, intrusive, corr = score_case(case, tasks)
        totals["expected"] += len(rows)
        totals["found"] += sum(r["found"] for r in rows)
        totals["exact"] += sum(r.get("exact", False) for r in rows)
        loc_rows = [r for r, e in zip(rows, case.get("expect", [])) if e.get("location") and r["found"]]
        totals["loc_expected"] += sum(1 for e in case.get("expect", []) if e.get("location"))
        totals["loc_ok"] += sum(r["loc_ok"] for r in loc_rows)
        totals["area_checked"] = totals.get("area_checked", 0) + sum(r.get("area_checked", False) for r in rows)
        totals["area_ok"] = totals.get("area_ok", 0) + sum(r.get("area_ok", False) and r.get("area_checked", False) for r in rows)
        totals["false_pos"] += len(fps)
        totals["intrusive"] += len(intrusive)
        totals["corrections"] += corr
        totals["latency"] += dt
        status = "OK " if corr == 0 and not err else "-- "
        print(f"{status}{case['id']:<22} {dt:5.1f}s corrections={corr} "
              + "; ".join(f"{r['kw']}:{'miss' if not r['found'] else (r['date'], r['time'], r.get('area'), 'ok' if r['exact'] else 'FIX')}" for r in rows)
              + (f" | FP: {[t['title'] for t in fps]}" if fps else "") + (f" | ERR {err}" if err else ""), flush=True)
        results.append({"id": case["id"], "rows": rows, "false_pos": [t.get("title") for t in fps],
                        "tasks": tasks, "error": err, "seconds": round(dt, 1)})

    n = max(totals["expected"], 1)
    summary = {
        "label": args.label, "model": extractor.MODEL, "cases": len(cases),
        "recall": round(totals["found"] / n, 3),
        "exact_no_edit": round(totals["exact"] / n, 3),
        "location": f"{totals['loc_ok']}/{totals['loc_expected']}",
        "category": f"{totals.get('area_ok', 0)}/{totals.get('area_checked', 0)}",
        "false_positives": totals["false_pos"],
        "preselected_false_positives": totals["intrusive"],
        "user_corrections": totals["corrections"],
        "errors": totals["errors"],
        "avg_seconds": round(totals["latency"] / max(len(cases), 1), 1),
    }
    print(json.dumps(summary, indent=2))
    out_dir = os.path.join(HERE, "results")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{args.label}.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
