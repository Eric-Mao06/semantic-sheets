"""Packing gate: does packing several rows into one Jev request change the answers vs isolated rows?

Runs the same boolean + category questions over N rows with packet sizes 1, 5, 10, 20 and reports
agreement with the isolated (pack=1) answers, mean |Δp| for the boolean, throughput, and tokens/row.

Usage: TYPESAFE_API_KEY=... python scripts/benchmark_packing.py --csv samples/bitext_support.csv --column instruction --rows 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import duckdb

from semantic_sheets.jev.client import JevClient
from semantic_sheets.jev.packing import Packet, build_request, project_row, row_tokens
from semantic_sheets.plan.schema import BooleanQuestion, CategoryQuestion, Label

QUESTIONS = [
    BooleanQuestion(name="cancel_afford", instruction="Is the customer trying to cancel an order because they cannot afford it?"),
    CategoryQuestion(name="intent", instruction="What is the customer trying to do?", labels=[Label(name=n) for n in [
        "cancel_order", "track_order", "get_refund", "change_shipping_address", "contact_customer_service", "check_invoice",
        "delivery_options", "newsletter_subscription", "review", "payment_issue", "create_account", "recover_password"]]),
]


async def run(rows: list[tuple[int, dict]], pack: int, concurrency: int) -> tuple[dict, float, int, int]:
    packets: list[Packet] = []
    for i in range(0, len(rows), pack):
        chunk = rows[i:i + pack]
        packets.append(Packet(rows=chunk, est_tokens=sum(row_tokens(p) for _, p in chunk) + 200))
    answers: dict = {}
    tokens = 0
    sem = asyncio.Semaphore(concurrency)
    async with JevClient() as client:
        async def one(p: Packet):
            nonlocal tokens
            state, qs, key_map = build_request(p, QUESTIONS)
            async with sem:
                resp = await client.system_one(state, qs, est_tokens=p.est_tokens)
            tokens += resp.input_tokens
            for key, (rid, qname) in key_map.items():
                answers[(rid, qname)] = resp.answers.get(key)
        t0 = time.monotonic()
        await asyncio.gather(*[one(p) for p in packets])
        dt = time.monotonic() - t0
    return answers, dt, tokens, len(packets)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--column", default="instruction")
    ap.add_argument("--rows", type=int, default=300)
    ap.add_argument("--packs", default="1,5,10,20")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    con = duckdb.connect()
    data = con.execute(f"SELECT row_number() OVER () - 1 AS rid, \"{args.column}\" FROM read_csv('{args.csv}') USING SAMPLE reservoir({args.rows} ROWS) REPEATABLE (1)").fetchall()
    rows = [(int(r[0]), project_row({args.column: r[1]}, [args.column])) for r in data]
    rows = [(rid, p) for rid, p in rows if p[args.column] is not None]
    base = None
    report = []
    for pack in [int(x) for x in args.packs.split(",")]:
        answers, dt, tokens, n_req = asyncio.run(run(rows, pack, args.concurrency))
        if base is None:
            base = answers
        agree_bool = agree_cat = 0
        dps = []
        for rid, _ in rows:
            a, b = base.get((rid, "cancel_afford")), answers.get((rid, "cancel_afford"))
            if a and b:
                dps.append(abs(a["noul"] - b["noul"]))
                if (a["noul"] >= 0.85) == (b["noul"] >= 0.85):
                    agree_bool += 1
            a, b = base.get((rid, "intent")), answers.get((rid, "intent"))
            if a and b and a["choice"] == b["choice"]:
                agree_cat += 1
        n = len(rows)
        rep = {"pack": pack, "rows": n, "requests": n_req, "seconds": round(dt, 2), "rows_per_s": round(n / dt, 1),
               "input_tokens": tokens, "tokens_per_row": round(tokens / n, 1),
               "bool_decision_agreement": round(agree_bool / n, 4), "bool_mean_abs_dp": round(statistics.mean(dps), 4) if dps else None,
               "category_agreement": round(agree_cat / n, 4)}
        report.append(rep)
        print(json.dumps(rep))
    print("\nGate: <= 1 percentage point loss vs isolated rows on decision agreement is the proposed threshold.")


if __name__ == "__main__":
    main()
