"""Classification accuracy on the Bitext support sample with its own 27-intent taxonomy (labels held out).

Usage: TYPESAFE_API_KEY=... SS_DATA_DIR=... python scripts/eval_bitext.py --csv samples/bitext_support.csv [--rows 2000]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb

from semantic_sheets.services import datasets, jobs, results, workspaces
from semantic_sheets import worker


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--rows", type=int, default=0)
    args = ap.parse_args()
    ws, _ = workspaces.ensure_default_workspace()
    con = duckdb.connect()
    intents = [r[0] for r in con.execute(f"SELECT DISTINCT intent FROM read_csv('{args.csv}') ORDER BY 1").fetchall()]
    print(f"{len(intents)} intents:", intents)
    ds = datasets.import_local_file(ws.id, Path(args.csv), "bitext eval", {"max_rows": args.rows} if args.rows else None)
    plan = {"plan_version": "1", "source": {"dataset_id": ds.id}, "steps": [
        {"id": "labels", "op": "semantic_annotate", "input": "source", "columns": ["instruction"], "questions": [
            {"name": "intent_pred", "kind": "category", "instruction": "Which support intent does this customer message express?",
             "labels": [{"name": i, "description": i.replace("_", " ")} for i in intents], "add_default_labels": False}]}], "output": "labels"}
    job, _ = jobs.submit(ws.id, plan=plan, limits={"spend_target_usd": 2.0})
    t0 = time.time(); state = worker.run_once(); dt = time.time() - t0
    j = jobs.describe(jobs.get(ws.id, job.id))
    print("job", state, f"{dt:.1f}s", j["usage"])
    agg = results.query(ws.id, j["result_version_id"], {"aggregate": {"group_by": ["intent", "intent_pred.value"], "metrics": [{"fn": "count", "name": "n"}]}, "limit": 2000}, max_rows=2000, max_bytes=10_000_000)
    rows = agg["rows"]
    total = sum(n for _, _, n in rows); correct = sum(n for a, b, n in rows if a == b)
    print(f"accuracy: {correct}/{total} = {correct/total:.1%}")
    conf = results.query(ws.id, j["result_version_id"], {"aggregate": {"group_by": ["intent"], "metrics": [{"fn": "count", "name": "n"}]}, "limit": 50}, max_rows=200, max_bytes=1_000_000)
    per = {}
    for a, b, n in rows:
        per.setdefault(a, [0, 0]); per[a][1] += n
        if a == b: per[a][0] += n
    for k, (c, t) in sorted(per.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        print(f"  {k:32s} {c:5d}/{t:5d} = {c/t:6.1%}")
    wrong = sorted([(n, a, b) for a, b, n in rows if a != b], reverse=True)[:8]
    print("top confusions:", wrong)
    # threshold on confidence: precision/coverage of auto-accepted decisions
    for mc in (0.0, 0.5, 0.7, 0.9):
        r = results.query(ws.id, j["result_version_id"], {"where": {"column": "intent_pred.confidence", "operator": "gte", "value": mc},
                          "aggregate": {"group_by": ["intent", "intent_pred.value"], "metrics": [{"fn": "count", "name": "n"}]}, "limit": 2000}, max_rows=2000, max_bytes=10_000_000)
        t = sum(n for _, _, n in r["rows"]); c = sum(n for a, b, n in r["rows"] if a == b)
        print(f"  confidence >= {mc}: coverage {t/total:.1%}, precision {c/max(1,t):.1%}")


if __name__ == "__main__":
    main()
