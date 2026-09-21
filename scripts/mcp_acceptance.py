"""MCP acceptance scenario (design §16): 100,000 rows, one bulk plan, bounded retrieval, no raw-table output.

Starts a dedicated API+worker instance on its own data directory, imports a 100k-row sample through the
upload path, then drives everything through the MCP endpoint with the official client. Reports serialized
response bytes per tool call and an estimated frontier-visible token count (chars / 4, no tokenizer
dependency), provider usage, wall time and throughput.

Usage: TYPESAFE_API_KEY=... python scripts/mcp_acceptance.py --csv data/samples/online_retail_100k.csv [--port 8010]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def scenario(base: str, key: str, csv: Path, spend: float) -> dict:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=120)
    bytes_by_tool: dict[str, int] = {}
    calls = 0

    async def call(s: ClientSession, name: str, args: dict):
        nonlocal calls
        res = await s.call_tool(name, args)
        calls += 1
        text = res.content[0].text if res.content else ""
        bytes_by_tool[name] = bytes_by_tool.get(name, 0) + len(text.encode())
        data = res.structured_content
        if data.get("error"):
            raise RuntimeError(f"{name}: {data['error']}")
        return data["data"]

    async with streamable_http_client(f"{base}/mcp/", http_client=http) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            t_all = time.time()
            up = await call(s, "uploads_prepare", {"filename": csv.name})
            # The host uploads bytes directly; they never pass through tool arguments.
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=600) as h:
                with open(csv, "rb") as f:
                    resp = await h.put(up["upload_url"], content=f.read())
                    resp.raise_for_status()
            t0 = time.time()
            ds = await call(s, "datasets_import", {"upload_id": up["upload_id"], "name": "acceptance-100k", "wait": True})
            import_s = time.time() - t0
            assert ds["status"] == "ready", ds
            desc = await call(s, "datasets_describe", {"dataset_id": ds["dataset_id"], "sample": True, "sample_rows": 5})
            text_col = next(c["name"] for c in desc["schema"] if c["type"] == "text" and c.get("max_length", 0) > 15)
            plan = {"plan_version": "1", "source": {"dataset_id": ds["dataset_id"]}, "steps": [
                {"id": "labels", "op": "semantic_annotate", "input": "source", "columns": [text_col], "questions": [
                    {"name": "gift", "kind": "boolean", "instruction": f"Is the product described in {text_col} something typically bought as a gift or decoration rather than a practical household supply?"},
                    {"name": "gift_kind", "kind": "category", "instruction": "Which gift category fits the product best?",
                     "labels": [{"name": "home_decor"}, {"name": "kitchen"}, {"name": "toys_and_games"}, {"name": "stationery"}, {"name": "jewellery_and_accessories"}, {"name": "seasonal"}, {"name": "garden"}, {"name": "bags_and_storage"}]}]},
                {"id": "gifts", "op": "filter", "input": "labels", "where": {"column": "gift.value", "operator": "eq", "value": True}},
                {"id": "by_kind", "op": "aggregate", "input": "gifts", "group_by": ["gift_kind.value"], "metrics": [{"name": "rows", "fn": "count"}]},
            ], "output": "by_kind"}
            v = await call(s, "plans_validate", {"plan": plan})
            j = await call(s, "jobs_submit", {"plan_hash": v["plan_hash"], "idempotency_key": "acceptance-100k-1",
                                              "limits": {"spend_target_usd": spend, "max_source_rows": 100000, "deadline_seconds": 3600}})
            t_job = time.time()
            polls = 0
            first_chunk_at = None
            while True:
                await asyncio.sleep(max(1, j.get("suggested_poll_seconds") or 2))
                j = await call(s, "jobs_get", {"job_id": j["job_id"]})
                polls += 1
                if first_chunk_at is None and (j["progress"].get("succeeded") or 0) > 0:
                    first_chunk_at = time.time() - t_job
                if j["state"] not in ("queued", "running"):
                    break
            job_s = time.time() - t_job
            agg = await call(s, "results_query", {"result_version_id": j["result_version_id"], "spec": {"limit": 50}})
            top = await call(s, "results_query", {"result_version_id": j["result_version_id"], "spec": {"scope": {"step": "gifts"}, "limit": 20, "columns": [text_col, "gift.p", "gift_kind.value"]}})
            exp = await call(s, "results_export", {"result_version_id": j["result_version_id"], "format": "parquet", "scope": {"step": "labels"}})
            total_s = time.time() - t_all
    total_bytes = sum(bytes_by_tool.values())
    return {
        "rows": desc["row_count"], "import_seconds": round(import_s, 1), "job_state": j["state"], "terminal_reason": j["terminal_reason"],
        "job_seconds": round(job_s, 1), "rows_per_second": round(desc["row_count"] / job_s, 1), "first_committed_chunk_seconds": first_chunk_at,
        "progress": j["progress"], "usage": j["usage"], "estimate": v["estimate"], "polls": polls, "tool_calls": calls,
        "serialized_response_bytes": total_bytes, "bytes_by_tool": bytes_by_tool, "estimated_frontier_tokens": total_bytes // 4,
        "aggregate_rows": agg["rows"], "top_rows": top["rows"][:5], "top_bytes": top["bytes"], "export": {k: exp[k] for k in ("bytes", "row_count", "format")},
        "export_complete": exp["manifest"]["complete"], "total_seconds": round(total_s, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--spend", type=float, default=2.0)
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    data_dir = args.data_dir or tempfile.mkdtemp(prefix="ss_accept_")
    key = "acceptance-key"
    env = {**os.environ, "SS_DATA_DIR": data_dir, "SS_DEV_WORKSPACE_KEY": key, "SS_INLINE_WORKER": "1", "SS_FAKE_JEV": os.environ.get("SS_FAKE_JEV", "0"),
           "SS_PUBLIC_BASE_URL": f"http://127.0.0.1:{args.port}", "SS_WORKSPACE_BUDGET_USD": "50"}
    server_dir = Path(__file__).resolve().parents[1] / "server"
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "semantic_sheets.api.app:app", "--port", str(args.port), "--log-level", "warning"],
                            cwd=server_dir, env=env)
    base = f"http://127.0.0.1:{args.port}"
    try:
        for _ in range(60):
            try:
                if httpx2.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        report = asyncio.run(scenario(base, key, Path(args.csv), args.spend))
        print(json.dumps(report, indent=2))
    finally:
        proc.terminate()
        proc.wait(timeout=20)


if __name__ == "__main__":
    main()
