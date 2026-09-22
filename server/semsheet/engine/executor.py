"""Job executor. Runs the semantic stages of a plan chunk by chunk.

Guarantees implemented here:
* each committed chunk is a Parquet file named by (job, stage, chunk) and recorded transactionally, so a retry
  or worker restart never duplicates rows and reuses everything already committed
* spend is reserved before dispatch and reconciled from provider usage after each response
* cancellation, deadline and budget checks happen between packets; a stopped job keeps its partial result
* cache hits are a subset of successes and are counted separately from inference attempts
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from rapidfuzz import fuzz, process

from ..config import settings
from ..db import Database, dumps, loads, now
from ..models import JobLimits, Plan, Question, SemanticAnnotateStep, SemanticMatchStep
from ..storage import build_context, derived_dir
from . import calibrate, jev
from .exact import ROW_ID, compile_plan, connect, lit, q

log = logging.getLogger("semsheet.executor")


NOT_DISPATCHED = object()


def _annotate_row_counts(table: pa.Table) -> dict[str, int]:
    """Classify each row of a semantic_annotate chunk by the statuses of its questions."""
    status_cols = [c for c in table.column_names if c.endswith(".status")]
    near_cols = [c for c in table.column_names if c.endswith(".near")]
    st_lists = [table.column(c).to_pylist() for c in status_cols]
    near_lists = [table.column(c).to_pylist() for c in near_cols]
    counts = {"succeeded": 0, "uncertain": 0, "flagged": 0, "missing": 0, "failed": 0, "skipped": 0}
    for i in range(table.num_rows):
        sts = [s[i] for s in st_lists]
        if any(s in (jev.FAILED, jev.TOO_LONG) for s in sts):
            counts["failed"] += 1
        elif all(s == "skipped" for s in sts):
            counts["skipped"] += 1
        elif all(s == jev.MISSING for s in sts):
            counts["missing"] += 1
        elif any(s == jev.UNCERTAIN for s in sts):
            counts["uncertain"] += 1
        else:
            counts["succeeded"] += 1
            if any(bool(nl[i]) for nl in near_lists):
                counts["flagged"] += 1
    return counts


def _match_row_counts(table: pa.Table, name: str) -> dict[str, int]:
    sts = table.column(f"{name}.status").to_pylist()
    near = table.column(f"{name}.near").to_pylist() if f"{name}.near" in table.column_names else [False] * len(sts)
    return {
        "failed": sum(1 for s in sts if s == jev.FAILED),
        "uncertain": sum(1 for s in sts if s == "uncertain"),
        "missing": sum(1 for s in sts if s == "no_candidates"),
        "succeeded": sum(1 for s in sts if s in ("matched", "unmatched")),
        "flagged": sum(1 for s, nr in zip(sts, near) if s in ("matched", "unmatched") and nr),
    }


def _replace_column(table: pa.Table, name: str, values: list[Any], typ: pa.DataType) -> pa.Table:
    idx = table.schema.get_field_index(name)
    arr = pa.array(values, typ)
    return table.set_column(idx, pa.field(name, typ), arr) if idx >= 0 else table.append_column(pa.field(name, typ), arr)


class StopJob(Exception):
    def __init__(self, reason: str, state: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state


@dataclass
class StageProgress:
    rows_total: int = 0
    rows_succeeded: int = 0
    rows_uncertain: int = 0
    rows_flagged: int = 0  # answered, but within the flag margin of a calibrated cut (listed in review views)
    rows_missing: int = 0
    rows_failed: int = 0
    rows_skipped: int = 0
    rows_pending: int = 0
    cache_hits: int = 0
    inference_attempts: int = 0
    provider_requests: int = 0
    pairs: int = 0
    chunks_committed: int = 0
    chunks_total: int = 0
    rows_beyond_cap: int = 0
    complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    provider_requests: int = 0
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    cache_hits: int = 0
    ambiguous_attempts: int = 0
    route_requests: dict[str, int] = field(default_factory=dict)
    route_failures: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["spent_usd"] = round(d["spent_usd"], 6)
        d["reserved_usd"] = round(d["reserved_usd"], 6)
        return d


class JobRunner:
    def __init__(self, db: Database, client: jev.JevClient, worker_id: str) -> None:
        self.db = db
        self.client = client
        self.worker_id = worker_id
        self._stop_after_commit: StopJob | None = None

    # ------------------------------------------------------------------------------------------
    async def run(self, job_id: str) -> None:
        job = dict(self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))
        plan = Plan.model_validate(loads(self.db.one("SELECT plan_json FROM plans WHERE id=?", (job["plan_id"],))["plan_json"]))
        limits = JobLimits.model_validate(loads(job["limits_json"]))
        ver = dict(self.db.one("SELECT * FROM dataset_versions WHERE id=?", (job["dataset_version_id"],)))
        rv_id = job["result_version_id"]
        ws_id = job["workspace_id"]
        progress = loads(job["progress_json"], {}) or {}
        stages: dict[str, StageProgress] = {k: StageProgress(**v) for k, v in (progress.get("stages") or {}).items()}
        usage = Usage(**{k: v for k, v in (loads(job["usage_json"], {}) or {}).items() if k in Usage.__dataclass_fields__})
        started_at = job["started_at"] or now()
        self.db.execute("UPDATE jobs SET state='running', started_at=?, worker_id=?, heartbeat_at=? WHERE id=?", (started_at, self.worker_id, now(), job_id))
        self.db.append_event(job_id, "running", {"job_id": job_id, "worker_id": self.worker_id, "resumed": bool(job["started_at"])})
        deadline = started_at + limits.deadline_seconds
        manifest = loads(self.db.one("SELECT manifest_json FROM result_versions WHERE id=?", (rv_id,))["manifest_json"], {})
        state = "succeeded"
        reason: str | None = None
        error_summary: str | None = None
        try:
            semantic_steps = [s for s in plan.steps if isinstance(s, (SemanticAnnotateStep, SemanticMatchStep))]
            for step in semantic_steps:
                sp = stages.setdefault(step.id, StageProgress())
                if sp.complete:
                    continue
                self.db.append_event(job_id, "stage_started", {"stage": step.id, "op": step.op})
                ctx = build_context(self.db, ws_id, plan, ver, rv_id, manifest)
                compiled = compile_plan(plan, ctx)
                input_sql = compiled.sql_for(step.input)
                if isinstance(step, SemanticAnnotateStep):
                    await self._run_annotate(job_id, ws_id, rv_id, plan, step, input_sql, limits, deadline, sp, usage, stages, manifest)
                else:
                    await self._run_match(job_id, ws_id, rv_id, plan, step, input_sql, ctx, limits, deadline, sp, usage, stages, manifest)
                if sp.rows_pending == 0:
                    self._calibrate_stage(job_id, rv_id, step, sp, manifest)
                sp.complete = sp.rows_pending == 0 and sp.rows_failed == 0 and sp.rows_beyond_cap == 0
                manifest.setdefault("steps", {})[step.id] = sp.to_dict()
                self._save(job_id, rv_id, stages, usage, manifest, bump=False)
                self.db.append_event(job_id, "stage_completed", {"stage": step.id, "progress": sp.to_dict()})
                if sp.rows_failed:
                    state = "partial"
                    reason = reason or "provider_failures"
                if sp.rows_beyond_cap:
                    state = "partial"
                    reason = reason or "max_source_rows"
        except StopJob as s:
            state, reason = s.state, s.reason
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            state, reason, error_summary = "failed", "internal_error", f"{type(e).__name__}: {e}"[:500]
            committed = self.db.one("SELECT count(*) c FROM job_chunks WHERE job_id=? AND state='committed'", (job_id,))["c"]
            if committed:
                state = "partial"
        finally:
            if state == "succeeded" and any(not sp.complete for sp in stages.values()):
                state = "partial"
                reason = reason or "incomplete"
            manifest["status"] = state
            manifest["steps"] = {k: v.to_dict() for k, v in stages.items()}
            manifest["finished_at"] = now()
            t = now()
            with self.db.connect() as conn:
                conn.execute("UPDATE jobs SET state=?, terminal_reason=?, finished_at=?, progress_json=?, usage_json=?, error_summary=?, heartbeat_at=? WHERE id=?",
                             (state, reason, t, dumps(self._progress(stages)), dumps(usage.to_dict()), error_summary, t, job_id))
                conn.execute("UPDATE result_versions SET status=?, manifest_json=?, updated_at=?, revision=revision+1 WHERE id=?", (state, dumps(manifest), t, rv_id))
            rev = self.db.one("SELECT revision FROM result_versions WHERE id=?", (rv_id,))["revision"]
            self.db.append_event(job_id, "finished", {"job_id": job_id, "state": state, "terminal_reason": reason, "result_version_id": rv_id, "revision": rev, "usage": usage.to_dict(), "progress": self._progress(stages)})

    # ------------------------------------------------------------------------------------------
    def _progress(self, stages: dict[str, StageProgress]) -> dict[str, Any]:
        examined = sum(s.rows_succeeded + s.rows_uncertain + s.rows_missing + s.rows_failed + s.rows_skipped for s in stages.values())
        remaining = sum(s.rows_pending for s in stages.values())
        return {"stages": {k: v.to_dict() for k, v in stages.items()}, "rows_examined": examined, "rows_remaining": remaining,
                "errors": sum(s.rows_failed for s in stages.values())}

    def _save(self, job_id: str, rv_id: str, stages: dict[str, StageProgress], usage: Usage, manifest: dict[str, Any], bump: bool) -> int:
        t = now()
        manifest["steps"] = {k: v.to_dict() for k, v in stages.items()}
        with self.db.connect() as conn:
            conn.execute("UPDATE jobs SET progress_json=?, usage_json=?, heartbeat_at=? WHERE id=?", (dumps(self._progress(stages)), dumps(usage.to_dict()), t, job_id))
            if bump:
                conn.execute("UPDATE result_versions SET revision=revision+1, manifest_json=?, updated_at=? WHERE id=?", (dumps(manifest), t, rv_id))
            else:
                conn.execute("UPDATE result_versions SET manifest_json=?, updated_at=? WHERE id=?", (dumps(manifest), t, rv_id))
            rev = conn.execute("SELECT revision FROM result_versions WHERE id=?", (rv_id,)).fetchone()["revision"]
        return int(rev)

    def _check_stop(self, job_id: str, deadline: float, limits: JobLimits, usage: Usage) -> None:
        row = self.db.one("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
        if row and row["cancel_requested"]:
            raise StopJob("cancelled", "cancelled")
        if now() > deadline:
            raise StopJob("deadline", "partial")
        if usage.provider_requests >= limits.max_provider_requests:
            raise StopJob("max_provider_requests", "partial")

    def _reserve(self, ws_id: str, limits: JobLimits, usage: Usage, est_tokens: int) -> None:
        est = jev.cost_usd(est_tokens)
        if usage.spent_usd + usage.reserved_usd + est > limits.spend_target_usd:
            raise StopJob("budget", "partial")
        spend = self.db.workspace_spend(ws_id)
        if spend["spent_usd"] + est > spend["budget_usd"]:
            raise StopJob("workspace_budget", "partial")
        usage.reserved_usd += est

    def _settle(self, ws_id: str, job_id: str, usage: Usage, est_tokens: int, result: jev.JevResult | None) -> None:
        usage.reserved_usd = max(0.0, usage.reserved_usd - jev.cost_usd(est_tokens))
        if result is not None:
            usage.input_tokens += result.input_tokens
            usage.output_tokens += result.output_tokens
            usage.provider_requests += 1
            cost = jev.cost_usd(result.input_tokens)
            usage.spent_usd += cost
            self.db.execute("INSERT INTO spend_ledger(workspace_id,job_id,kind,input_tokens,usd,created_at) VALUES(?,?,?,?,?,?)", (ws_id, job_id, "jev", result.input_tokens, cost, now()))

    # ------------------------------------------------------------------------------------------
    # Stage: semantic_annotate
    # ------------------------------------------------------------------------------------------
    async def _run_annotate(self, job_id: str, ws_id: str, rv_id: str, plan: Plan, step: SemanticAnnotateStep, input_sql: str, limits: JobLimits, deadline: float,
                            sp: StageProgress, usage: Usage, stages: dict[str, StageProgress], manifest: dict[str, Any]) -> None:
        cols = [ROW_ID] + step.columns
        with connect() as con:
            tbl = con.execute(f"SELECT {', '.join(q(c) for c in cols)} FROM ({input_sql}) t ORDER BY {q(ROW_ID)} LIMIT {int(limits.max_source_rows)}").to_arrow_table()
            total_input = int(con.execute(f"SELECT count(*) FROM ({input_sql}) t").fetchone()[0])
        rows = tbl.to_pylist()
        if total_input > limits.max_source_rows:
            manifest.setdefault("caps", {})[step.id] = {"input_rows": total_input, "processed_cap": limits.max_source_rows}
            sp.rows_beyond_cap = total_input - limits.max_source_rows
        chunks = _chunk_bounds(len(rows))
        sp.rows_total = len(rows)
        sp.chunks_total = len(chunks)
        committed = {r["chunk_index"] for r in self.db.query("SELECT chunk_index FROM job_chunks WHERE job_id=? AND stage_id=? AND state='committed'", (job_id, step.id))}
        sp.rows_pending = sum(e - s for i, (s, e) in enumerate(chunks) if i not in committed)
        out_dir = derived_dir(rv_id, step.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        rows_per_request = limits.rows_per_request or settings.jev_rows_per_request
        for idx, (s, e) in enumerate(chunks):
            if idx in committed:
                continue
            self._check_stop(job_id, deadline, limits, usage)
            chunk_rows = rows[s:e]
            table, stats = await self._annotate_chunk(job_id, ws_id, plan.model, step, chunk_rows, rows_per_request, limits, usage, deadline)
            self._commit_chunk(job_id, rv_id, step.id, idx, out_dir, table, _annotate_row_counts(table), stats, sp, usage, stages, manifest, chunk_rows[0][ROW_ID], chunk_rows[-1][ROW_ID])

    async def _annotate_chunk(self, job_id: str, ws_id: str, model: str, step: SemanticAnnotateStep, rows: list[dict[str, Any]], rows_per_request: int,
                              limits: JobLimits, usage: Usage, deadline: float) -> tuple[pa.Table, dict[str, int]]:
        qs = step.questions
        raw: dict[tuple[int, str], dict[str, Any] | None] = {}
        status_override: dict[tuple[int, str], str] = {}
        payloads: dict[int, dict[str, Any]] = {}
        keys: dict[tuple[int, str], str] = {}
        for r in rows:
            rid = int(r[ROW_ID])
            payload = jev.row_payload(r, step.columns)
            if payload is None:
                for qn in qs:
                    status_override[(rid, qn.name)] = "skipped" if step.on_missing == "skip" else jev.MISSING
                continue
            payloads[rid] = payload
            for qn in qs:
                keys[(rid, qn.name)] = jev.cache_key(ws_id, model, qn, payload)
        # cache lookup
        stats = {"cache_hits": 0, "attempts": 0, "requests": 0, "failed": 0}
        hits = self._cache_lookup(list(keys.values()))
        items: list[jev.PacketItem] = []
        for rid, payload in payloads.items():
            misses = []
            for qn in qs:
                k = keys[(rid, qn.name)]
                if k in hits:
                    raw[(rid, qn.name)] = hits[k]
                    stats["cache_hits"] += 1
                else:
                    misses.append(qn)
            if misses:
                items.append(jev.PacketItem(row_id=rid, payload=payload, questions=misses))
        packets, too_long = jev.pack(items, rows_per_request)
        for it in too_long:
            for qn in it.questions:
                status_override[(it.row_id, qn.name)] = jev.TOO_LONG
        results = await self._dispatch(job_id, ws_id, model, packets, limits, usage, deadline, stats)
        new_cache: list[tuple[str, dict[str, Any], int]] = []
        not_dispatched: set[int] = set()
        for pkt, res in zip(packets, results):
            if res is NOT_DISPATCHED:
                not_dispatched.update(it.row_id for it in pkt.items)
            elif isinstance(res, jev.JevResult):
                alloc = jev.allocate_usage(pkt, res.input_tokens)
                for it in pkt.items:
                    for qn in it.questions:
                        ans = res.answers.get(f"{it.ref}__{qn.name}")
                        raw[(it.row_id, qn.name)] = ans
                        stats["attempts"] += 1
                        if ans is not None:
                            new_cache.append((keys[(it.row_id, qn.name)], ans, alloc.get(it.row_id, 0) // max(1, len(it.questions))))
                        else:
                            stats["failed"] += 1
            else:
                for it in pkt.items:
                    for qn in it.questions:
                        raw[(it.row_id, qn.name)] = None
                        status_override[(it.row_id, qn.name)] = jev.FAILED
                        stats["attempts"] += 1
                        stats["failed"] += 1
        self._cache_store(ws_id, model, new_cache)
        # Build the chunk table
        arrays: dict[str, list[Any]] = {ROW_ID: []}
        for qn in qs:
            for name, _ in qn.output_columns():
                arrays[name] = []
            arrays[f"{qn.name}.raw"] = []
        for r in rows:
            rid = int(r[ROW_ID])
            if rid in not_dispatched:
                continue  # stays pending; no derived row is written for it
            arrays[ROW_ID].append(rid)
            for qn in qs:
                st = status_override.get((rid, qn.name))
                if st is not None:
                    interp = {"value": None, "score": None, "confidence": None, "status": st}
                    rraw = None
                else:
                    rraw = raw.get((rid, qn.name))
                    interp = jev.interpret(qn, rraw)
                arrays[f"{qn.name}.value"].append(interp["value"])
                arrays[f"{qn.name}.score"].append(interp["score"])
                if qn.kind in ("category", "score"):
                    arrays[f"{qn.name}.confidence"].append(interp["confidence"])
                else:
                    arrays[f"{qn.name}.near"].append(False)
                arrays[f"{qn.name}.status"].append(interp["status"])
                arrays[f"{qn.name}.raw"].append(None if rraw is None else json.dumps(rraw, separators=(",", ":")))
        typed: list[tuple[str, pa.DataType]] = [(ROW_ID, pa.int64())]
        for qn in qs:
            typed.append((f"{qn.name}.value", pa.bool_() if qn.kind == "boolean" else pa.string()))
            typed.append((f"{qn.name}.score", pa.float64()))
            if qn.kind in ("category", "score"):
                typed.append((f"{qn.name}.confidence", pa.float64()))
            else:
                typed.append((f"{qn.name}.near", pa.bool_()))
            typed.append((f"{qn.name}.status", pa.string()))
            typed.append((f"{qn.name}.raw", pa.string()))
        table = pa.Table.from_arrays([pa.array(arrays[name], t) for name, t in typed], schema=pa.schema([pa.field(name, t) for name, t in typed]))
        return table, stats

    async def _dispatch(self, job_id: str, ws_id: str, model: str, packets: list[jev.Packet], limits: JobLimits, usage: Usage, deadline: float, stats: dict[str, int]) -> list[Any]:
        results: list[Any] = [None] * len(packets)

        async def one(i: int, pkt: jev.Packet) -> None:
            try:
                res = await self.client.evaluate(pkt, model)
                results[i] = res
            except jev.JevError as e:
                results[i] = e
            finally:
                self._settle(ws_id, job_id, usage, pkt.estimated_tokens, results[i] if isinstance(results[i], jev.JevResult) else None)
                stats["requests"] += 1
                usage.ambiguous_attempts = self.client.ambiguous_attempts
                usage.route_requests = dict(getattr(self.client, "route_requests", None) or {})
                usage.route_failures = dict(getattr(self.client, "route_failures", None) or {})

        # Keep enough packets in flight to fill every route's concurrency (the client bounds each route itself).
        max_pending = settings.jev_concurrency * max(2, len(getattr(self.client, "routes", None) or ()))
        pending: list[asyncio.Task] = []
        for i, pkt in enumerate(packets):
            # Between packets: cancellation, deadline, request cap and spend reservation.
            try:
                self._check_stop(job_id, deadline, limits, usage)
                self._reserve(ws_id, limits, usage, pkt.estimated_tokens)
            except StopJob as s:
                # Let in-flight requests finish, then mark the rest failed with the stop reason and re-raise after commit.
                if pending:
                    await asyncio.gather(*pending)
                for j in range(i, len(packets)):
                    results[j] = NOT_DISPATCHED
                self._stop_after_commit = s
                return results
            pending.append(asyncio.create_task(one(i, pkt)))
            if len(pending) >= max_pending:
                done, still = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                pending = list(still)
        if pending:
            await asyncio.gather(*pending)
        return results

    def _commit_chunk(self, job_id: str, rv_id: str, stage_id: str, idx: int, out_dir: Path, table: pa.Table, counts: dict[str, int], stats: dict[str, int],
                      sp: StageProgress, usage: Usage, stages: dict[str, StageProgress], manifest: dict[str, Any], first_row: int, last_row: int) -> None:
        """Write one chunk atomically (tmp file + rename), fold its row counts into the stage progress, record it in
        job_chunks and bump the result revision. Raises the pending StopJob, if any, once the chunk is durable."""
        tmp = out_dir / f".chunk_{idx:05d}.parquet.tmp"
        final = out_dir / f"chunk_{idx:05d}.parquet"
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, final)
        n = table.num_rows
        sp.rows_succeeded += counts.get("succeeded", 0)
        sp.rows_uncertain += counts.get("uncertain", 0)
        sp.rows_flagged += counts.get("flagged", 0)
        sp.rows_missing += counts.get("missing", 0)
        sp.rows_failed += counts.get("failed", 0)
        sp.rows_skipped += counts.get("skipped", 0)
        sp.rows_pending = max(0, sp.rows_pending - n)
        sp.cache_hits += stats.get("cache_hits", 0)
        sp.inference_attempts += stats.get("attempts", 0)
        sp.provider_requests += stats.get("requests", 0)
        sp.pairs += stats.get("pairs", 0)
        sp.chunks_committed += 1
        usage.cache_hits += stats.get("cache_hits", 0)
        with self.db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO job_chunks(job_id,stage_id,chunk_index,state,row_count,usage_json,path,error,committed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (job_id, stage_id, idx, "committed", n, dumps(stats), str(final), None, now()))
        rev = self._save(job_id, rv_id, stages, usage, manifest, bump=True)
        self.db.append_event(job_id, "chunk_committed", {"stage": stage_id, "chunk_index": idx, "rows": n, "row_range": [int(first_row), int(last_row)], "revision": rev,
                                                         "progress": self._progress(stages), "usage": usage.to_dict()})
        if self._stop_after_commit is not None:
            stop, self._stop_after_commit = self._stop_after_commit, None
            raise stop

    # ------------------------------------------------------------------------------------------
    # Post-stage: threshold calibration
    # ------------------------------------------------------------------------------------------
    def _calibrate_stage(self, job_id: str, rv_id: str, step: SemanticAnnotateStep | SemanticMatchStep, sp: StageProgress, manifest: dict[str, Any]) -> None:
        """Once every chunk of a stage is committed, place each boolean cut on the observed score distribution
        (see calibrate.py) and rewrite value/near/status in the chunk files from the stored scores.

        Idempotent: everything is derived from <q>.score (and the candidate list for matches), never from the
        previous value, so a resumed job can run it again safely."""
        out_dir = derived_dir(rv_id, step.id)
        files = sorted(out_dir.glob("chunk_*.parquet"))
        if not files:
            return
        tables = [pq.read_table(f) for f in files]
        cal_out = manifest.setdefault("calibration", {}).setdefault(step.id, {})
        changed = False
        if isinstance(step, SemanticAnnotateStep):
            for qn in step.questions:
                if qn.kind != "boolean":
                    continue
                scored = [(s, st) for t in tables for s, st in zip(t.column(f"{qn.name}.score").to_pylist(), t.column(f"{qn.name}.status").to_pylist())
                          if s is not None and st in (jev.OK, jev.UNCERTAIN)]
                cal = calibrate.calibrate([s for s, _ in scored], qn.thresholds.true_min, qn.thresholds.false_max, qn.thresholds.mode)
                cal_out[qn.name] = cal.to_dict()
                if cal.mode != "auto":
                    continue
                changed = True
                for i, t in enumerate(tables):
                    scores = t.column(f"{qn.name}.score").to_pylist()
                    statuses = t.column(f"{qn.name}.status").to_pylist()
                    values = t.column(f"{qn.name}.value").to_pylist()
                    near = [False] * len(scores)
                    for r, (s, st) in enumerate(zip(scores, statuses)):
                        if s is None or st not in (jev.OK, jev.UNCERTAIN):
                            continue
                        values[r], near[r] = calibrate.decide(s, cal)
                        statuses[r] = jev.OK
                    t = _replace_column(t, f"{qn.name}.value", values, pa.bool_())
                    t = _replace_column(t, f"{qn.name}.near", near, pa.bool_())
                    tables[i] = _replace_column(t, f"{qn.name}.status", statuses, pa.string())
        else:
            n = step.name
            scored = [s for t in tables for s, st in zip(t.column(f"{n}.score").to_pylist(), t.column(f"{n}.status").to_pylist())
                      if s is not None and st in ("matched", "uncertain", "unmatched")]
            cal = calibrate.calibrate(scored, step.accept_min, step.reject_max, step.threshold_mode)
            cal_out[n] = cal.to_dict()
            if cal.mode == "auto":
                changed = True
                for i, t in enumerate(tables):
                    scores = t.column(f"{n}.score").to_pylist()
                    statuses = t.column(f"{n}.status").to_pylist()
                    rids = t.column(f"{n}.right_row_id").to_pylist()
                    cands = t.column(f"{n}.candidates").to_pylist()
                    near = [False] * len(scores)
                    for r, (s, st) in enumerate(zip(scores, statuses)):
                        if s is None or st not in ("matched", "uncertain", "unmatched"):
                            continue
                        cl = json.loads(cands[r]) if cands[r] else []
                        scored_c = [c for c in cl if c.get("p_same") is not None]
                        best = max(scored_c, key=lambda c: c["p_same"]) if scored_c else None
                        is_match, near[r] = calibrate.decide(s, cal)
                        if is_match:
                            statuses[r] = "matched"
                            rids[r] = best["right_row_id"] if best else rids[r]
                        else:
                            # a rejected row with an unscored candidate is still undecidable
                            statuses[r] = "uncertain" if len(scored_c) < len(cl) else "unmatched"
                            rids[r] = None
                    t = _replace_column(t, f"{n}.right_row_id", rids, pa.int64())
                    t = _replace_column(t, f"{n}.near", near, pa.bool_())
                    tables[i] = _replace_column(t, f"{n}.status", statuses, pa.string())
        if not changed:
            return
        for f, t in zip(files, tables):
            tmp = f.with_name("." + f.name + ".tmp")
            pq.write_table(t, tmp, compression="zstd")
            os.replace(tmp, f)
        # recount the stage from the rewritten chunks
        totals = {"succeeded": 0, "uncertain": 0, "flagged": 0, "missing": 0, "failed": 0, "skipped": 0}
        for t in tables:
            c = _annotate_row_counts(t) if isinstance(step, SemanticAnnotateStep) else _match_row_counts(t, step.name)
            for k, v in c.items():
                totals[k] += v
        sp.rows_succeeded, sp.rows_uncertain, sp.rows_flagged = totals["succeeded"], totals["uncertain"], totals["flagged"]
        sp.rows_missing, sp.rows_failed = totals["missing"], totals["failed"]
        if isinstance(step, SemanticAnnotateStep):
            sp.rows_skipped = totals["skipped"]
        self.db.append_event(job_id, "stage_calibrated", {"stage": step.id, "calibration": cal_out, "progress": sp.to_dict()})

    # ------------------------------------------------------------------------------------------
    # Stage: semantic_match
    # ------------------------------------------------------------------------------------------
    async def _run_match(self, job_id: str, ws_id: str, rv_id: str, plan: Plan, step: SemanticMatchStep, input_sql: str, ctx: Any, limits: JobLimits, deadline: float,
                         sp: StageProgress, usage: Usage, stages: dict[str, StageProgress], manifest: dict[str, Any]) -> None:
        rpath, rschema = ctx.datasets[step.right.dataset_id]
        rcols = [ROW_ID] + list(dict.fromkeys(step.right_columns + ([step.blocking.right] if step.blocking else [])))
        lcols = [ROW_ID] + list(dict.fromkeys(step.left_columns + ([step.blocking.left] if step.blocking else [])))
        with connect() as con:
            right = con.execute(f"SELECT {', '.join(q(c) for c in rcols)} FROM read_parquet({lit(str(rpath))}) ORDER BY {q(ROW_ID)}").to_arrow_table().to_pylist()
            left = con.execute(f"SELECT {', '.join(q(c) for c in lcols)} FROM ({input_sql}) t ORDER BY {q(ROW_ID)} LIMIT {int(limits.max_source_rows)}").to_arrow_table().to_pylist()
        if len(right) > settings.match_max_right_rows:
            raise StopJob("right_table_too_large", "failed")
        with connect() as con:
            total_left = int(con.execute(f"SELECT count(*) FROM ({input_sql}) t").fetchone()[0])
        if total_left > limits.max_source_rows:
            manifest.setdefault("caps", {})[step.id] = {"input_rows": total_left, "processed_cap": limits.max_source_rows}
            sp.rows_beyond_cap = total_left - limits.max_source_rows
        right_text = [" | ".join(str(r.get(c) or "") for c in step.right_columns) for r in right]
        blocks: dict[Any, list[int]] = {}
        if step.blocking:
            for i, r in enumerate(right):
                blocks.setdefault(jev.normalize_cell(r.get(step.blocking.right)), []).append(i)
        chunks = _chunk_bounds(len(left))
        sp.rows_total = len(left)
        sp.chunks_total = len(chunks)
        committed = {r["chunk_index"] for r in self.db.query("SELECT chunk_index FROM job_chunks WHERE job_id=? AND stage_id=? AND state='committed'", (job_id, step.id))}
        sp.rows_pending = sum(e - s for i, (s, e) in enumerate(chunks) if i not in committed)
        out_dir = derived_dir(rv_id, step.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        rows_per_request = limits.rows_per_request or settings.jev_rows_per_request
        manifest.setdefault("matching", {})[step.id] = {"retrieval": settings.retrieval_version, "candidates_per_row": step.candidates_per_row, "right_rows": len(right),
                                                        "blocking": step.blocking.model_dump() if step.blocking else None, "note": "no match among candidates is not proof that no match exists"}
        pair_question = Question(name="same", kind="boolean", instruction=step.instruction, criteria=step.criteria, thresholds={"true_min": step.accept_min, "false_max": step.reject_max})
        pairs_budget = settings.match_max_pairs
        for idx, (s, e) in enumerate(chunks):
            if idx in committed:
                continue
            self._check_stop(job_id, deadline, limits, usage)
            chunk = left[s:e]
            # candidate generation: exact blocking then lexical retrieval
            cand: dict[int, list[tuple[int, float]]] = {}
            for left_row in chunk:
                query = " | ".join(str(left_row.get(c) or "") for c in step.left_columns)
                pool_idx = blocks.get(jev.normalize_cell(left_row.get(step.blocking.left)), []) if step.blocking else None
                if pool_idx is not None and not pool_idx:
                    cand[int(left_row[ROW_ID])] = []
                    continue
                if pool_idx is None:
                    found = process.extract(query, right_text, scorer=fuzz.token_set_ratio, limit=step.candidates_per_row)
                    cand[int(left_row[ROW_ID])] = [(int(right[i][ROW_ID]), float(score) / 100.0) for _, score, i in found]
                else:
                    sub = {i: right_text[i] for i in pool_idx}
                    found = process.extract(query, sub, scorer=fuzz.token_set_ratio, limit=step.candidates_per_row)
                    cand[int(left_row[ROW_ID])] = [(int(right[i][ROW_ID]), float(score) / 100.0) for _, score, i in found]
            right_by_id = {int(r[ROW_ID]): r for r in right}
            items: list[jev.PacketItem] = []
            keys: dict[tuple[int, int], str] = {}
            payload_of: dict[tuple[int, int], dict[str, Any]] = {}
            for left_row in chunk:
                lid = int(left_row[ROW_ID])
                for rid, _ in cand[lid]:
                    payload = {"left_record": {c: (jev.normalize_cell(left_row.get(c)) or "") for c in step.left_columns},
                               "right_record": {c: (jev.normalize_cell(right_by_id[rid].get(c)) or "") for c in step.right_columns}}
                    payload_of[(lid, rid)] = payload
                    keys[(lid, rid)] = jev.cache_key(ws_id, plan.model, pair_question, payload)
            total_pairs_so_far = sp.pairs + len(keys)
            if total_pairs_so_far > pairs_budget:
                raise StopJob("pair_budget", "partial")
            stats = {"cache_hits": 0, "attempts": 0, "requests": 0, "failed": 0, "pairs": len(keys)}
            hits = self._cache_lookup(list(keys.values()))
            raw: dict[tuple[int, int], dict[str, Any] | None] = {}
            for (lid, rid), k in keys.items():
                if k in hits:
                    raw[(lid, rid)] = hits[k]
                    stats["cache_hits"] += 1
                else:
                    items.append(jev.PacketItem(row_id=lid * 1_000_000_007 + rid, payload=payload_of[(lid, rid)], questions=[pair_question]))
            packets, too_long = jev.pack(items, rows_per_request)
            results = await self._dispatch(job_id, ws_id, plan.model, packets, limits, usage, deadline, stats)
            new_cache = []
            item_pair = {lid * 1_000_000_007 + rid: (lid, rid) for (lid, rid) in keys}
            not_dispatched_left: set[int] = set()
            for pkt, res in zip(packets, results):
                for it in pkt.items:
                    pair = item_pair[it.row_id]
                    if res is NOT_DISPATCHED:
                        not_dispatched_left.add(pair[0])
                        continue
                    stats["attempts"] += 1
                    if isinstance(res, jev.JevResult):
                        ans = res.answers.get(f"{it.ref}__same")
                        raw[pair] = ans
                        if ans is not None:
                            new_cache.append((keys[pair], ans, 0))
                        else:
                            stats["failed"] += 1
                    else:
                        raw[pair] = None
                        stats["failed"] += 1
            for it in too_long:
                raw[item_pair[it.row_id]] = None
            self._cache_store(ws_id, plan.model, new_cache)
            # decide per left row
            out = {ROW_ID: [], f"{step.name}.right_row_id": [], f"{step.name}.score": [], f"{step.name}.status": [], f"{step.name}.candidates": []}
            for left_row in chunk:
                lid = int(left_row[ROW_ID])
                if lid in not_dispatched_left:
                    continue  # stays pending
                cands = []
                any_failed = False
                for rid, retr in cand[lid]:
                    a = raw.get((lid, rid))
                    p = None if a is None else float(a.get("noul", 0.0))
                    if a is None:
                        any_failed = True
                    cands.append({"right_row_id": rid, "p_same": p, "retrieval_score": round(retr, 3)})
                scored = [c for c in cands if c["p_same"] is not None]
                out[ROW_ID].append(lid)
                out[f"{step.name}.candidates"].append(json.dumps(cands, separators=(",", ":")))
                if not cands or not scored:
                    out[f"{step.name}.right_row_id"].append(None)
                    out[f"{step.name}.score"].append(None)
                    out[f"{step.name}.status"].append("no_candidates" if not cands else jev.FAILED)
                else:
                    best = max(scored, key=lambda c: c["p_same"])
                    p = best["p_same"]
                    if p >= step.accept_min:
                        st = "matched"
                    elif p <= step.reject_max:
                        st = "unmatched"
                    else:
                        st = "uncertain"
                    if any_failed and st == "unmatched":
                        st = "uncertain"
                    out[f"{step.name}.right_row_id"].append(best["right_row_id"] if st != "unmatched" else None)
                    out[f"{step.name}.score"].append(p)
                    out[f"{step.name}.status"].append(st)
            table = pa.table({
                ROW_ID: pa.array(out[ROW_ID], pa.int64()),
                f"{step.name}.right_row_id": pa.array(out[f"{step.name}.right_row_id"], pa.int64()),
                f"{step.name}.score": pa.array(out[f"{step.name}.score"], pa.float64()),
                f"{step.name}.near": pa.array([False] * len(out[ROW_ID]), pa.bool_()),
                f"{step.name}.status": pa.array(out[f"{step.name}.status"], pa.string()),
                f"{step.name}.candidates": pa.array(out[f"{step.name}.candidates"], pa.string()),
            })
            self._commit_chunk(job_id, rv_id, step.id, idx, out_dir, table, _match_row_counts(table, step.name), stats, sp, usage, stages, manifest, chunk[0][ROW_ID], chunk[-1][ROW_ID])

    # ------------------------------------------------------------------------------------------
    # Prediction cache
    # ------------------------------------------------------------------------------------------
    def _cache_lookup(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(keys), 500):
            batch = keys[i : i + 500]
            marks = ",".join("?" * len(batch))
            for r in self.db.query(f"SELECT cache_key, raw_json FROM prediction_cache WHERE cache_key IN ({marks})", batch):
                out[r["cache_key"]] = json.loads(r["raw_json"])
        return out

    def _cache_store(self, ws_id: str, model: str, entries: list[tuple[str, dict[str, Any], int]]) -> None:
        if not entries:
            return
        t = now()
        with self.db.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO prediction_cache(cache_key,workspace_id,model,raw_json,input_tokens,created_at) VALUES(?,?,?,?,?,?)",
                             [(k, ws_id, model, json.dumps(raw, separators=(",", ":")), int(tok), t) for k, raw, tok in entries])


def _chunk_bounds(n: int) -> list[tuple[int, int]]:
    """First chunk is small so the first committed result appears quickly; later chunks use the configured size."""
    bounds: list[tuple[int, int]] = []
    first = min(n, max(20, settings.chunk_rows // 4))
    pos = 0
    if n:
        bounds.append((0, first))
        pos = first
    while pos < n:
        e = min(n, pos + settings.chunk_rows)
        bounds.append((pos, e))
        pos = e
    return bounds
