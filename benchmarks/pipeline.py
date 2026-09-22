"""Drive the Semantic Sheet pipeline in-process: import -> planner (gpt-6-astra) -> Jev job -> export every step.

`SEMSHEET_DATA_DIR` must be set before this module is imported (run.py does that) so the benchmark uses its
own store instead of the demo workspace."""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from common import ROOT, jev_cost, planner_cost

sys.path.insert(0, str(ROOT / "server"))

from semsheet.config import settings  # noqa: E402
from semsheet.db import get_db  # noqa: E402
from semsheet.engine.executor import JobRunner  # noqa: E402
from semsheet.engine.jev import JevClient  # noqa: E402
from semsheet.services import ServiceError, Services  # noqa: E402

from scenarios import Prepared, Scenario  # noqa: E402


@dataclass
class PipelineResult:
    plan: dict[str, Any] = field(default_factory=dict)
    planner: dict[str, Any] = field(default_factory=dict)
    estimate: dict[str, Any] = field(default_factory=dict)
    job: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, pd.DataFrame] = field(default_factory=dict)
    datasets: dict[str, dict[str, Any]] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "plan": self.plan, "planner": self.planner, "estimate": self.estimate,
            "job": {k: self.job.get(k) for k in ("job_id", "state", "terminal_reason", "usage", "progress", "error_summary", "errors")},
            "step_rows": {k: int(len(v)) for k, v in self.steps.items()},
            "datasets": {k: {kk: vv for kk, vv in v.items() if kk in ("dataset_id", "version_id", "row_count")} for k, v in self.datasets.items()},
            "timings_seconds": self.timings, "cost": self.cost, "error": self.error,
        }


def run_pipeline(scenario: Scenario, prep: Prepared, spend_target_usd: float = 5.0, deadline_seconds: int = 1800) -> PipelineResult:
    res = PipelineResult()
    db = get_db()
    svc = Services(db)
    ws = svc.workspace_from_token(settings.demo_workspace_token)
    t0 = time.time()
    try:
        for t in prep.tables:
            out = svc.datasets_import(ws, None, t.name, {}, source_path=t.path)
            ds = out["dataset"]
            ver = db.one("SELECT data_path FROM dataset_versions WHERE id=?", (ds["version_id"],))
            frame = pd.read_parquet(ver["data_path"])
            row_id_map = {int(a): int(b) for a, b in zip(frame["_row_id"], frame["row_id"])} if "row_id" in frame.columns else {}
            res.datasets[t.name] = {"dataset_id": ds["dataset_id"], "version_id": ds["version_id"], "row_count": ds["row_count"], "parquet": ver["data_path"], "frame": frame, "row_id_map": row_id_map}
        res.timings["import"] = round(time.time() - t0, 2)

        t1 = time.time()
        primary = res.datasets[prep.primary.name]
        compiled = svc.plans_compile(ws, primary["dataset_id"], primary["version_id"], scenario.prompt)
        res.timings["planner"] = round(time.time() - t1, 2)
        res.plan = compiled["plan"]
        res.estimate = compiled["estimate"]
        usage = compiled["planner"].get("usage") or {}
        res.planner = {**compiled["planner"], "attempts": compiled.get("attempts"), "title": compiled.get("title"), "description": compiled.get("description"),
                       "cost": planner_cost(compiled["planner"]["model"], usage)}

        t2 = time.time()
        limits = {"max_source_rows": max(t.row_count for t in prep.tables), "max_provider_requests": 20_000, "spend_target_usd": spend_target_usd, "deadline_seconds": deadline_seconds}
        job = svc.jobs_submit(ws, compiled["plan_hash"], None, limits, None)

        async def _run() -> None:
            async with JevClient() as client:
                await JobRunner(db, client, "benchmark").run(job["job_id"])

        asyncio.run(_run())
        res.timings["job"] = round(time.time() - t2, 2)
        res.job = svc.jobs_get(ws, job["job_id"])
        rv_id = res.job["result_version_id"]

        for step in res.plan["steps"]:
            try:
                exp = svc.results_export(ws, rv_id, "parquet", step=step["id"], raw=True)
                path, _ = svc.export_file(ws, exp["export_id"])
                res.steps[step["id"]] = pd.read_parquet(path)
            except ServiceError as e:
                res.steps[step["id"]] = pd.DataFrame()
                res.error = (res.error or "") + f"export {step['id']}: {e.message}; "
        ju = res.job.get("usage") or {}
        jev_usd = jev_cost(int(ju.get("input_tokens") or 0))
        res.cost = {
            "planner_usd": res.planner["cost"]["usd"],
            "jev_usd_from_measured_tokens": jev_usd,
            "jev_usd_reported_by_job": ju.get("spent_usd"),
            "jev_input_tokens": ju.get("input_tokens"), "jev_requests": ju.get("provider_requests"), "jev_cache_hits": ju.get("cache_hits"),
            "jev_route_requests": ju.get("route_requests") or {},
            "total_usd": round(res.planner["cost"]["usd"] + jev_usd, 6),
        }
    except ServiceError as e:
        res.error = f"{e.code}: {e.message} {e.details or ''}"[:2000]
    except Exception as e:  # noqa: BLE001
        res.error = f"{type(e).__name__}: {e}"[:2000]
    res.timings["total"] = round(time.time() - t0, 2)
    return res


def store_dir() -> Path:
    return settings.data_dir
