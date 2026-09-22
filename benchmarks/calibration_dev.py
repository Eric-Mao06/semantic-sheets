"""Held-out check of the threshold calibration rule (engine/calibrate.py) on rows the benchmark never sees.

Why this exists: the calibration rule was written knowing how the benchmark scenarios behave, so a fair test must
show that it does something sensible on *other* rows before it is scored on the benchmark rows. This script draws a
disjoint sample from the same raw sources (different messages, complaints and reviews), runs the exact plans a
previous run produced (`--plan-from`), and records only unsupervised diagnostics: where the cut landed, what the
score distribution looks like, how many rows fall on each side and how many are flagged. No gold labels are used
and nothing here feeds back into the rule; the constants in calibrate.py are fixed.

    python benchmarks/calibration_dev.py --plan-from planner-deepseek-v4.1-flash

Writes benchmarks/results/calibration_dev.json."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import BENCH_DATA_DIR, RESULTS_DIR, ROOT, dump_json, estimate_tokens  # noqa: E402

DEV_SEED = 11  # different from scenarios.SEED on purpose
DEV_DIR = BENCH_DATA_DIR / "calibration_dev"


def _dev_tables():
    import pandas as pd

    from common import SAMPLES_DIR
    from scenarios import Prepared, Table, _AIRBNB_ROWS, _csv

    def write(df: pd.DataFrame, name: str) -> Table:
        DEV_DIR.mkdir(parents=True, exist_ok=True)
        out = DEV_DIR / f"{name}.csv"
        df.to_csv(out, index=False)
        return Table(name=name, path=out, row_count=len(df), columns=list(df.columns), est_tokens=estimate_tokens(out.read_text(encoding="utf-8")))

    out = {}
    # support: 3,000 Bitext messages not in the benchmark's 3,000
    bench = set(_csv("bitext_support_3000.csv")["instruction"])
    full = _csv("bitext_support_full.csv")
    rest = full[~full["instruction"].isin(bench)].sample(n=3000, random_state=DEV_SEED).reset_index(drop=True)
    rest.insert(0, "row_id", range(1, len(rest) + 1))
    out["support_requests"] = (Prepared([write(rest[["row_id", "flags", "instruction"]], "support_requests")]), f"{len(rest)} Bitext messages disjoint from the benchmark sample (of {len(full) - len(bench)} available)")

    # cfpb: same 4,000 other + 1,000 credit-reporting recipe as the demo sample, from complaints not in it
    bench_ids = set(_csv("cfpb_complaints_5000.csv")["Complaint ID"])
    big = pd.read_csv(SAMPLES_DIR / "cfpb_complaints_100k.csv", dtype=str, keep_default_na=False)
    big = big[~big["Complaint ID"].isin(bench_ids)]
    credit = big[big["Product"].str.contains("Credit reporting", case=False)]
    other = big.drop(credit.index)
    df = pd.concat([other.sample(n=4000, random_state=DEV_SEED), credit.sample(n=1000, random_state=DEV_SEED)]).sample(frac=1.0, random_state=DEV_SEED).reset_index(drop=True)
    df.insert(0, "row_id", range(1, len(df) + 1))
    cols = ["row_id", "Date received", "Product", "Sub-product", "Issue", "Sub-issue", "Company", "State"]
    out["cfpb_complaints"] = (Prepared([write(df[cols], "consumer_complaints")]), f"{len(df)} CFPB complaints disjoint from the benchmark sample, same 4,000 non-credit-reporting + 1,000 credit-reporting mix")

    # airbnb: reviews not in the benchmark's 1,200. The benchmark took every review mentioning wifi/internet, so this
    # split has none with the keyword -- a deliberate stress test of the one-cluster guard.
    from scenarios import _WIFI

    full = _csv("airbnb_reviews_5000.csv")
    mentions = full[full["comments"].str.contains(_WIFI)]
    rest_pool = full.drop(mentions.index)
    bench_rest = rest_pool.sample(n=_AIRBNB_ROWS - len(mentions), random_state=7)  # what prep_airbnb drew
    rest = rest_pool.drop(bench_rest.index).sample(n=_AIRBNB_ROWS, random_state=DEV_SEED).reset_index(drop=True)
    rest.insert(0, "row_id", range(1, len(rest) + 1))
    cols = ["row_id", "listing_id", "listing_name", "neighbourhood", "borough", "room_type", "nightly_price", "comments"]
    out["airbnb_reviews"] = (Prepared([write(rest[cols], "airbnb_reviews")]), f"{len(rest)} reviews disjoint from the benchmark sample; none contains the Wi-Fi keyword (the benchmark took all of those)")
    return out


def _score_summary(scores: list[float]) -> dict:
    s = sorted(scores)
    n = len(s)
    if not n:
        return {}
    pct = lambda p: round(s[min(n - 1, int(p * n))], 3)  # noqa: E731
    hist = {f"{i / 10:.1f}-{(i + 1) / 10:.1f}": 0 for i in range(10)}
    for x in s:
        hist[f"{min(int(x * 10), 9) / 10:.1f}-{(min(int(x * 10), 9) + 1) / 10:.1f}"] += 1
    return {"n": n, "min": round(s[0], 3), "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "max": round(s[-1], 3), "histogram": hist}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-from", required=True)
    ap.add_argument("-s", "--scenarios", default="support_requests,cfpb_complaints,airbnb_reviews")
    args = ap.parse_args()
    os.environ.setdefault("SEMSHEET_DATA_DIR", str(ROOT / "data" / "benchmark_store"))
    os.environ.setdefault("SEMSHEET_WORKSPACE_BUDGET_USD", "50")

    from pipeline import run_pipeline
    from scenarios import SCENARIOS

    tables = _dev_tables()
    report = {"plan_from": args.plan_from, "dev_seed": DEV_SEED, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scenarios": {}}
    for key in [k for k in args.scenarios.split(",") if k]:
        prep, note = tables[key]
        meta_path = ROOT / "data" / "benchmark" / "raw_outputs" / args.plan_from / key / "pipeline" / "meta.json"
        meta = {**json.loads(meta_path.read_text(encoding="utf-8")), "run": args.plan_from}
        print(f"[{key}] {note}; running plan from {args.plan_from}")
        t0 = time.time()
        pr = run_pipeline(SCENARIOS[key], prep, plan_from=meta)
        entry = {"note": note, "rows": prep.primary.row_count, "job_state": pr.job.get("state"), "error": pr.error, "seconds": round(time.time() - t0, 1),
                 "jev_usd": (pr.cost or {}).get("jev_usd_from_measured_tokens"), "questions": {}}
        for step in pr.plan.get("steps", []):
            if step.get("op") != "semantic_annotate":
                continue
            df = pr.steps.get(step["id"])
            for qn in step.get("questions", []):
                if qn.get("kind") != "boolean" or df is None or f"{qn['name']}.score" not in df.columns:
                    continue
                scores = [float(x) for x, st in zip(df[f"{qn['name']}.score"], df[f"{qn['name']}.status"]) if x == x and st == "ok"]
                cal = ((pr.calibration or {}).get(step["id"]) or {}).get(qn["name"]) or {}
                th = qn.get("thresholds") or {}
                tmin, fmax = float(th.get("true_min", 0.85)), float(th.get("false_max", 0.15))
                cut = cal.get("cut")
                entry["questions"][qn["name"]] = {
                    "instruction": qn.get("instruction"), "calibration": cal, "scores": _score_summary(scores),
                    "under_calibrated_cut": None if cut is None else {"true": sum(1 for s in scores if s >= cut), "false": sum(1 for s in scores if s < cut),
                                                                       "flagged_near_cut": sum(1 for s in scores if abs(s - cut) <= cal.get("flag_margin", 0) + 1e-12)},
                    "under_planner_thresholds": {"true": sum(1 for s in scores if s >= tmin), "uncertain": sum(1 for s in scores if fmax < s < tmin), "false": sum(1 for s in scores if s <= fmax)},
                }
                print(f"  {qn['name']}: n={len(scores)} cut={cut} raw={cal.get('raw_cut')} clamped={cal.get('clamped')} -> {entry['questions'][qn['name']]['under_calibrated_cut']} | planner {entry['questions'][qn['name']]['under_planner_thresholds']}")
        report["scenarios"][key] = entry
        dump_json(RESULTS_DIR / "calibration_dev.json", report)
    print(f"wrote {RESULTS_DIR / 'calibration_dev.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
