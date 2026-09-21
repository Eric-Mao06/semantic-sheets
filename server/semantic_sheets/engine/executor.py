"""Run a job: freeze inputs, execute semantic steps in chunks, checkpoint each chunk, account for spend.

Chunks are committed as Parquet files named by index; a restarted worker skips committed chunks. Every
prediction records the row ID, question, model, cache key, raw response, status and token allocation.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from ..config import settings
from ..db import Job, JobEvent, PlanVersion, Prediction, Workspace, session, utcnow
from ..jev.client import JevClient, JevError, make_client
from ..jev.packing import (
    Packet,
    build_match_request,
    build_request,
    cache_key,
    interpret,
    make_packets,
    match_pair_input,
    match_spec,
    project_row,
    question_spec,
    row_is_missing,
    row_tokens,
)
from ..plan.compile import ROW_ID, CompiledPlan, StepInfo, compile_plan, parse_plan
from ..plan.expr import quote_ident as q
from ..plan.schema import SemanticAnnotate, SemanticMatch, match_columns, question_columns
from ..services.catalog import WorkspaceCatalog
from ..storage import (
    sql_str,
    step_chunk_files,
    step_chunk_glob,
    step_chunk_parquet,
    step_dir,
    step_final_parquet,
    step_input_parquet,
)
from .exact import DUCK_TYPES, build_sql


class StopJob(Exception):
    def __init__(self, reason: str, state: str = "partial"):
        super().__init__(reason)
        self.reason = reason
        self.state = state


@dataclass
class StepProgress:
    step_id: str
    source_rows: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    uncertain: int = 0
    cache_hits: int = 0
    pairs: int = 0
    chunks_total: int = 0
    chunks_done: int = 0

    @property
    def pending(self) -> int:
        return max(0, self.source_rows - self.succeeded - self.failed - self.skipped)

    def to_dict(self) -> dict[str, Any]:
        return {"source_rows": self.source_rows, "succeeded": self.succeeded, "failed": self.failed,
                "skipped": self.skipped, "pending": self.pending, "uncertain": self.uncertain,
                "cache_hits": self.cache_hits, "pairs": self.pairs, "chunks_total": self.chunks_total,
                "chunks_done": self.chunks_done, "complete": self.pending == 0 and self.chunks_done >= self.chunks_total}


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provider_errors: int = 0
    retries: int = 0
    reserved_usd: float = 0.0  # in-flight reservation

    def to_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost_usd, 6), "provider_errors": self.provider_errors, "retries": self.retries}


class JobRunner:
    def __init__(self, job_id: str, worker_id: str, client: JevClient | None = None):
        self.job_id = job_id
        self.worker_id = worker_id
        self.cfg = settings()
        self.client = client
        self.usage = Usage()
        self.steps: dict[str, StepProgress] = {}
        self.errors: list[dict[str, Any]] = []
        self.seq = 0
        self.model = self.cfg.jev_model
        self.limits: dict[str, Any] = {}
        self.deadline: dt.datetime | None = None
        self._last_lease = 0.0
        self._sem: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None

    # ---- lifecycle ---------------------------------------------------------------------------------------

    async def run(self) -> str:
        with session() as s:
            job = s.get(Job, self.job_id)
            pv = s.get(PlanVersion, job.plan_version_id)
            self.limits = dict(job.limits_json or {})
            self.model = job.model
            self.deadline = job.deadline_at
            self.usage = Usage(**{k: v for k, v in (job.usage_json or {}).items() if k in Usage.__dataclass_fields__ and k != "reserved_usd"})
            self.seq = s.scalar(select(JobEvent.seq).where(JobEvent.job_id == self.job_id).order_by(JobEvent.seq.desc()).limit(1)) or 0
            self.errors = list(job.error_summary_json or [])
            workspace_id = job.workspace_id
            plan_json = pv.plan_json
            job.state = "running"
            job.started_at = job.started_at or utcnow()
            s.commit()
        self.emit("running", {"attempt": job.attempts})
        catalog = WorkspaceCatalog(workspace_id)
        state = "succeeded"
        reason: str | None = None
        try:
            plan = parse_plan(plan_json)
            compiled = compile_plan(plan, catalog)
            self._sem = asyncio.Semaphore(self.cfg.jev_max_in_flight)
            self._lock = asyncio.Lock()
            if self.client is None:
                self.client = make_client()
            async with self.client:
                for sid in compiled.ancestors(plan.output):
                    info = compiled.steps[sid]
                    if not info.semantic:
                        continue
                    step = next(st for st in plan.steps if st.id == sid)
                    if isinstance(step, SemanticAnnotate):
                        await self.run_annotate(compiled, step, info, catalog, workspace_id)
                    elif isinstance(step, SemanticMatch):
                        await self.run_match(compiled, step, info, catalog, workspace_id)
            if any(p.failed for p in self.steps.values()):
                state, reason = "partial", "row_failures"
        except StopJob as e:
            state, reason = e.state, e.reason
        except JevError as e:
            state, reason = "failed", f"provider_failure: {e.message}"
            self.record_error("provider", e.message)
        except Exception as e:  # noqa: BLE001
            state, reason = "failed", f"internal_error: {type(e).__name__}: {e}"
            self.record_error("internal", f"{type(e).__name__}: {e}", traceback.format_exc()[-2000:])
        self.finish(state, reason)
        return state

    def finish(self, state: str, reason: str | None) -> None:
        with session() as s:
            job = s.get(Job, self.job_id)
            if job.cancel_requested and state in ("partial", "succeeded"):
                state = "cancelled" if reason == "cancelled" else state
            job.state = state
            job.terminal_reason = reason
            job.finished_at = utcnow()
            job.progress_json = self.progress_dict()
            job.usage_json = self.usage.to_dict()
            job.error_summary_json = self.errors[:50]
            job.lease_owner = None
            job.lease_until = None
            ws = s.get(Workspace, job.workspace_id)
            ws.reserved_usd = max(0.0, (ws.reserved_usd or 0.0) - (job.reserved_usd or 0.0))
            ws.spent_usd = (ws.spent_usd or 0.0) + self.usage.cost_usd
            job.reserved_usd = 0.0
            job.result_revision = (job.result_revision or 0) + 1
            s.commit()
        self.emit("finished", {"state": state, "reason": reason, "progress": self.progress_dict(), "usage": self.usage.to_dict()})

    def progress_dict(self) -> dict[str, Any]:
        steps = {k: v.to_dict() for k, v in self.steps.items()}
        tot = StepProgress("total")
        for p in self.steps.values():
            tot.source_rows += p.source_rows
            tot.succeeded += p.succeeded
            tot.failed += p.failed
            tot.skipped += p.skipped
            tot.uncertain += p.uncertain
            tot.cache_hits += p.cache_hits
            tot.pairs += p.pairs
            tot.chunks_total += p.chunks_total
            tot.chunks_done += p.chunks_done
        d = tot.to_dict()
        d["steps"] = steps
        return d

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        with session() as s:
            s.add(JobEvent(job_id=self.job_id, seq=self.seq, kind=kind, payload_json=payload))
            s.commit()

    def record_error(self, kind: str, message: str, detail: str | None = None) -> None:
        if len(self.errors) < 50:
            self.errors.append({"kind": kind, "message": message[:500], "detail": (detail or "")[:2000], "at": utcnow().isoformat()})

    def checkpoint(self, *, changed_rows: tuple[int, int] | None, step_id: str) -> None:
        """Persist progress and usage after a committed chunk; bump the result revision; renew the lease."""
        with session() as s:
            job = s.get(Job, self.job_id)
            job.progress_json = self.progress_dict()
            job.usage_json = self.usage.to_dict()
            job.error_summary_json = self.errors[:50]
            job.result_revision = (job.result_revision or 0) + 1
            job.lease_until = utcnow() + dt.timedelta(seconds=self.cfg.worker_lease_seconds)
            rev = job.result_revision
            cancel = job.cancel_requested
            s.commit()
        self.emit("progress", {"step": step_id, "result_revision": rev, "changed_rows": changed_rows,
                               "progress": self.progress_dict(), "usage": self.usage.to_dict()})
        if cancel:
            raise StopJob("cancelled", "cancelled")

    def check_guards(self, next_est_cost: float, next_requests: int) -> None:
        if self.deadline and utcnow() > self.deadline:
            raise StopJob("deadline", "partial")
        target = float(self.limits.get("spend_target_usd") or self.cfg.default_spend_target_usd)
        if self.usage.cost_usd + self.usage.reserved_usd + next_est_cost > target:
            raise StopJob("budget", "partial")
        max_req = int(self.limits.get("max_provider_requests") or self.cfg.default_max_provider_requests)
        if self.usage.requests + next_requests > max_req:
            raise StopJob("request_cap", "partial")
        with session() as s:
            job = s.get(Job, self.job_id)
            if job.cancel_requested:
                raise StopJob("cancelled", "cancelled")

    def cost_of(self, tokens: int) -> float:
        return tokens / 1e6 * self.cfg.jev_price_per_million_input

    # ---- shared provider call -----------------------------------------------------------------------------

    async def call(self, state: Any, questions: dict[str, dict[str, Any]], est_tokens: int) -> tuple[dict[str, Any] | None, str | None]:
        """One provider request with a reservation. Returns (answers, error)."""
        assert self._sem is not None and self._lock is not None
        est_cost = self.cost_of(est_tokens)
        async with self._lock:
            self.usage.reserved_usd += est_cost
        try:
            async with self._sem:
                try:
                    resp = await self.client.system_one(state, questions, est_tokens=est_tokens, model=self.model)
                except JevError as e:
                    async with self._lock:
                        self.usage.provider_errors += 1
                        if e.status in (401, 403):
                            raise
                        # A timeout may still have been billed; keep the reservation as spend to stay conservative.
                        if e.retryable:
                            self.usage.cost_usd += est_cost
                    self.record_error("provider", e.message)
                    return None, e.message
            async with self._lock:
                self.usage.requests += 1
                self.usage.input_tokens += resp.input_tokens
                self.usage.output_tokens += resp.output_tokens
                self.usage.cost_usd += self.cost_of(resp.input_tokens)
                self.usage.retries += max(0, resp.attempts - 1)
            return resp.answers, None
        finally:
            async with self._lock:
                self.usage.reserved_usd = max(0.0, self.usage.reserved_usd - est_cost)

    # ---- semantic_annotate ---------------------------------------------------------------------------------

    async def run_annotate(self, compiled: CompiledPlan, step: SemanticAnnotate, info: StepInfo,
                           catalog: WorkspaceCatalog, workspace_id: str) -> None:
        prog = self.steps.setdefault(step.id, StepProgress(step.id))
        input_path = step_input_parquet(self.job_id, step.id)
        if not input_path.exists():
            self.materialize_input(compiled, step.input, [ROW_ID] + step.columns, catalog, input_path)
        con = duckdb.connect()
        try:
            total = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(input_path)})").fetchone()[0]
        finally:
            con.close()
        max_rows = int(self.limits.get("max_source_rows") or self.cfg.default_max_source_rows)
        if total > max_rows:
            raise StopJob(f"row_limit: {total} input rows exceed max_source_rows={max_rows}", "failed")
        chunk_rows = self.cfg.chunk_rows
        prog.source_rows = int(total)
        prog.chunks_total = math.ceil(total / chunk_rows) if total else 0
        # Recover progress from committed chunks (worker restart).
        done = {int(p.stem.split("_")[1]) for p in step_chunk_files(self.job_id, step.id)}
        self.recount_from_files(prog, step)
        self.emit("step_started", {"step": step.id, "source_rows": prog.source_rows, "chunks": prog.chunks_total,
                                   "resumed_chunks": len(done)})
        specs = {qn.name: question_spec(qn) for qn in step.questions}
        q_tokens = sum(len(json.dumps(spec)) for spec in specs.values()) // 3 + 12 * len(specs)
        schema = self.annotate_schema(step)
        pending = [i for i in range(prog.chunks_total) if i not in done]
        # Two chunks in flight keep the provider busy while a chunk commits.
        window = 2
        for start in range(0, len(pending), window):
            batch = pending[start:start + window]
            est_tokens = sum(self.chunk_token_estimate(input_path, i, chunk_rows, step.columns, q_tokens) for i in batch)
            self.check_guards(self.cost_of(est_tokens), math.ceil(chunk_rows * len(batch) / max(1, self.cfg.jev_pack_rows)))
            results = await asyncio.gather(*[
                self.process_annotate_chunk(i, input_path, chunk_rows, step, specs, q_tokens, workspace_id, schema)
                for i in batch])
            for i, (n_ok, n_fail, n_skip, n_unc, n_hits, rows_range) in zip(batch, results):
                prog.succeeded += n_ok
                prog.failed += n_fail
                prog.skipped += n_skip
                prog.uncertain += n_unc
                prog.cache_hits += n_hits
                prog.chunks_done += 1
                self.checkpoint(changed_rows=rows_range, step_id=step.id)
        self.compact(step.id)
        self.emit("step_finished", {"step": step.id, "progress": prog.to_dict()})

    def annotate_schema(self, step: SemanticAnnotate) -> pa.Schema:
        fields = [pa.field(ROW_ID, pa.int64())]
        for qn in step.questions:
            for name, t in question_columns(qn):
                fields.append(pa.field(name, ARROW_TYPES[t]))
        return pa.schema(fields)

    def chunk_token_estimate(self, input_path: Path, index: int, chunk_rows: int, columns: list[str], q_tokens: int) -> int:
        # Cheap upper-bound estimate: sample the chunk's byte size via a quick scan of lengths.
        con = duckdb.connect()
        try:
            sel = " + ".join(f"coalesce(length(CAST({q(c)} AS VARCHAR)), 0)" for c in columns) or "0"
            row = con.execute(f"SELECT count(*), coalesce(sum({sel}), 0) FROM (SELECT * FROM read_parquet({sql_str(input_path)}) "
                              f"ORDER BY {q(ROW_ID)} LIMIT {chunk_rows} OFFSET {index * chunk_rows}) c").fetchone()
            n, chars = int(row[0] or 0), int(row[1] or 0)
        finally:
            con.close()
        return int(chars * 0.3) + n * (24 + q_tokens) + 60 * math.ceil(n / max(1, self.cfg.jev_pack_rows))

    def read_chunk(self, input_path: Path, index: int, chunk_rows: int) -> tuple[list[str], list[tuple]]:
        con = duckdb.connect()
        try:
            cur = con.execute(f"SELECT * FROM read_parquet({sql_str(input_path)}) ORDER BY {q(ROW_ID)} "
                              f"LIMIT {chunk_rows} OFFSET {index * chunk_rows}")
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()
        finally:
            con.close()

    async def process_annotate_chunk(self, index: int, input_path: Path, chunk_rows: int, step: SemanticAnnotate,
                                     specs: dict[str, dict[str, Any]], q_tokens: int, workspace_id: str,
                                     schema: pa.Schema) -> tuple[int, int, int, int, int, tuple[int, int] | None]:
        cols, raw_rows = self.read_chunk(input_path, index, chunk_rows)
        if not raw_rows:
            self.write_chunk(step.id, index, schema, [])
            return 0, 0, 0, 0, 0, None
        rid_idx = cols.index(ROW_ID)
        out: dict[int, dict[str, Any]] = {}
        todo: list[tuple[int, dict[str, Any]]] = []
        keys: dict[tuple[int, str], str] = {}
        n_skip = 0
        for r in raw_rows:
            row = dict(zip(cols, r))
            rid = int(row[ROW_ID])
            proj = project_row(row, step.columns)
            if row_is_missing(proj):
                d: dict[str, Any] = {}
                for qn in step.questions:
                    d.update(interpret(qn, None, status="missing_input" if step.on_missing == "unknown" else "skipped"))
                out[rid] = d
                n_skip += 1
                continue
            for qn in step.questions:
                keys[(rid, qn.name)] = cache_key(workspace_id, self.model, specs[qn.name], proj)
            todo.append((rid, proj))
        # Cache lookup
        cached = self.lookup_cache(list(keys.values()))
        n_hits = 0
        raw_answers: dict[tuple[int, str], dict[str, Any] | None] = {}
        errors: dict[tuple[int, str], str] = {}
        need: list[tuple[int, dict[str, Any]]] = []
        for rid, proj in todo:
            missing = False
            for qn in step.questions:
                k = keys[(rid, qn.name)]
                if k in cached:
                    raw_answers[(rid, qn.name)] = cached[k]
                    n_hits += 1
                else:
                    missing = True
            if missing:
                need.append((rid, proj))
        # Provider calls for uncached rows (all questions re-asked together for simplicity of packing).
        packets, too_long = make_packets(need, q_tokens, self.cfg.jev_pack_rows, self.cfg.jev_state_token_budget,
                                         self.cfg.jev_total_token_budget)
        for rid in too_long:
            for qn in step.questions:
                errors[(rid, qn.name)] = "input_too_long: the row exceeds the provider context budget and was not truncated"
        new_predictions: list[tuple[str, dict[str, Any], int]] = []

        async def run_packet(p: Packet) -> None:
            state, qs, key_map = build_request(p, step.questions)
            answers, err = await self.call(state, qs, p.est_tokens)
            per_q_tokens = (p.est_tokens // max(1, len(key_map))) if answers is not None else 0
            for key, (rid, qname) in key_map.items():
                if answers is None:
                    errors[(rid, qname)] = err or "provider error"
                    continue
                a = answers.get(key)
                if not isinstance(a, dict):
                    errors[(rid, qname)] = "missing answer in provider response"
                    continue
                raw_answers[(rid, qname)] = a
                new_predictions.append((keys[(rid, qname)], a, per_q_tokens))

        await asyncio.gather(*[run_packet(p) for p in packets])
        self.store_cache(workspace_id, new_predictions)
        n_ok = n_fail = n_unc = 0
        for rid, proj in todo:
            d = {}
            failed = False
            unc = False
            for qn in step.questions:
                k = (rid, qn.name)
                if k in raw_answers:
                    vals = interpret(qn, raw_answers[k])
                    if not failed and vals.get(f"{qn.name}.status") == "uncertain":
                        unc = True
                else:
                    vals = interpret(qn, None, error=errors.get(k, "unknown"))
                    failed = True
                d.update(vals)
            out[rid] = d
            if failed:
                n_fail += 1
            else:
                n_ok += 1
                if unc:
                    n_unc += 1
        rows_sorted = sorted(out.items())
        self.write_chunk(step.id, index, schema, [{ROW_ID: rid, **d} for rid, d in rows_sorted])
        rng = (rows_sorted[0][0], rows_sorted[-1][0]) if rows_sorted else None
        return n_ok, n_fail, n_skip, n_unc, n_hits, rng

    # ---- semantic_match ------------------------------------------------------------------------------------

    async def run_match(self, compiled: CompiledPlan, step: SemanticMatch, info: StepInfo, catalog: WorkspaceCatalog,
                        workspace_id: str) -> None:
        prog = self.steps.setdefault(step.id, StepProgress(step.id))
        input_path = step_input_parquet(self.job_id, step.id)
        cand_path = step_dir(self.job_id, step.id) / "candidates.parquet"
        left_cols = list(dict.fromkeys(step.left_columns + [b.left for b in step.blocking]))
        if not input_path.exists():
            self.materialize_input(compiled, step.input, [ROW_ID] + left_cols, catalog, input_path)
        right_cols = list(dict.fromkeys(step.right_columns + [b.right for b in step.blocking] + (step.show_right_columns or [])))
        right_sql = catalog.source_sql(step.right.dataset_id, step.right.version_id)
        if not cand_path.exists():
            generate_candidates(input_path, right_sql, step, left_cols, right_cols, cand_path)
        con = duckdb.connect()
        try:
            total = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(input_path)})").fetchone()[0]
            pairs = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(cand_path)})").fetchone()[0]
        finally:
            con.close()
        max_rows = int(self.limits.get("max_source_rows") or self.cfg.default_max_source_rows)
        if total > max_rows:
            raise StopJob(f"row_limit: {total} left rows exceed max_source_rows={max_rows}", "failed")
        chunk_rows = max(20, self.cfg.chunk_rows // 4)
        prog.source_rows = int(total)
        prog.pairs = int(pairs)
        prog.chunks_total = math.ceil(total / chunk_rows) if total else 0
        done = {int(p.stem.split("_")[1]) for p in step_chunk_files(self.job_id, step.id)}
        self.recount_from_files(prog, step)
        self.emit("step_started", {"step": step.id, "source_rows": prog.source_rows, "pairs": prog.pairs,
                                   "chunks": prog.chunks_total, "resumed_chunks": len(done)})
        spec = match_spec(step)
        schema = pa.schema([pa.field(ROW_ID, pa.int64()), pa.field("match.right_row_id", pa.int64()),
                            pa.field("match.p", pa.float64()), pa.field("match.status", pa.string()),
                            pa.field("match.candidate_count", pa.int64()), pa.field("match.candidates", pa.string()),
                            pa.field("match.raw", pa.string())])
        for i in range(prog.chunks_total):
            if i in done:
                continue
            est = self.cost_of(chunk_rows * step.candidates_per_row * 120)
            self.check_guards(est, chunk_rows)
            n_ok, n_fail, n_skip, n_unc, n_hits, rng = await self.process_match_chunk(
                i, input_path, cand_path, chunk_rows, step, spec, workspace_id, schema, left_cols)
            prog.succeeded += n_ok
            prog.failed += n_fail
            prog.skipped += n_skip
            prog.uncertain += n_unc
            prog.cache_hits += n_hits
            prog.chunks_done += 1
            self.checkpoint(changed_rows=rng, step_id=step.id)
        self.compact(step.id)
        self.emit("step_finished", {"step": step.id, "progress": prog.to_dict()})

    async def process_match_chunk(self, index: int, input_path: Path, cand_path: Path, chunk_rows: int,
                                  step: SemanticMatch, spec: dict[str, Any], workspace_id: str, schema: pa.Schema,
                                  left_cols: list[str]) -> tuple[int, int, int, int, int, tuple[int, int] | None]:
        cols, left_rows = self.read_chunk(input_path, index, chunk_rows)
        if not left_rows:
            self.write_chunk(step.id, index, schema, [])
            return 0, 0, 0, 0, 0, None
        lefts = {int(r[cols.index(ROW_ID)]): dict(zip(cols, r)) for r in left_rows}
        lo, hi = min(lefts), max(lefts)
        con = duckdb.connect()
        try:
            cur = con.execute(f"SELECT * FROM read_parquet({sql_str(cand_path)}) WHERE left_row_id BETWEEN {lo} AND {hi} "
                              "ORDER BY left_row_id, retrieval_score DESC, right_row_id")
            ccols = [d[0] for d in cur.description]
            cands = [dict(zip(ccols, r)) for r in cur.fetchall()]
        finally:
            con.close()
        by_left: dict[int, list[dict[str, Any]]] = {}
        for c in cands:
            by_left.setdefault(int(c["left_row_id"]), []).append(c)
        pair_inputs: dict[tuple[int, int], dict[str, Any]] = {}
        keys: dict[tuple[int, int], str] = {}
        n_skip = 0
        skipped: set[int] = set()
        for lid, lrow in lefts.items():
            lproj = project_row(lrow, step.left_columns)
            if row_is_missing(lproj):
                skipped.add(lid)
                n_skip += 1
                continue
            for c in by_left.get(lid, []):
                rproj = {rc: c.get("r_" + rc) for rc in step.right_columns}
                rproj = project_row(rproj, step.right_columns)
                inp = match_pair_input(lproj, rproj)
                pair_inputs[(lid, int(c["right_row_id"]))] = inp
                keys[(lid, int(c["right_row_id"]))] = cache_key(workspace_id, self.model, spec, inp)
        cached = self.lookup_cache(list(keys.values()))
        answers: dict[tuple[int, int], float] = {}
        errors: dict[tuple[int, int], str] = {}
        n_hits = 0
        need: list[tuple[tuple[int, int], dict[str, Any]]] = []
        for pk, inp in pair_inputs.items():
            k = keys[pk]
            if k in cached:
                answers[pk] = float(cached[k].get("noul", 0.0))
                n_hits += 1
            else:
                need.append((pk, inp))
        instr_tokens = int(len(step.instruction) * 0.3) + 20
        packets: list[list[tuple[tuple[int, int], dict[str, Any]]]] = []
        cur_p: list[tuple[tuple[int, int], dict[str, Any]]] = []
        cur_t = 0
        for pk, inp in need:
            t = row_tokens(inp) + instr_tokens
            if cur_p and (len(cur_p) >= self.cfg.jev_pack_rows or cur_t + t > self.cfg.jev_state_token_budget):
                packets.append(cur_p)
                cur_p, cur_t = [], 0
            cur_p.append((pk, inp))
            cur_t += t
        if cur_p:
            packets.append(cur_p)
        new_predictions: list[tuple[str, dict[str, Any], int]] = []

        async def run_packet(p: list[tuple[tuple[int, int], dict[str, Any]]]) -> None:
            state, qs = build_match_request([(f"{a}:{b}", inp) for (a, b), inp in p], step.instruction)
            est = sum(row_tokens(inp) + instr_tokens for _, inp in p) + 60
            ans, err = await self.call(state, qs, est)
            for i, (pk, inp) in enumerate(p):
                key = f"P{i}"
                if ans is None or not isinstance(ans.get(key), dict) or ans[key].get("noul") is None:
                    errors[pk] = err or "missing answer"
                    continue
                answers[pk] = float(ans[key]["noul"])
                new_predictions.append((keys[pk], ans[key], est // max(1, len(p))))

        await asyncio.gather(*[run_packet(p) for p in packets])
        self.store_cache(workspace_id, new_predictions)
        out_rows: list[dict[str, Any]] = []
        n_ok = n_fail = n_unc = 0
        for lid in sorted(lefts):
            if lid in skipped:
                out_rows.append({ROW_ID: lid, "match.right_row_id": None, "match.p": None, "match.status": "missing_input",
                                 "match.candidate_count": 0, "match.candidates": None, "match.raw": None})
                continue
            cl = by_left.get(lid, [])
            scored = []
            failed = False
            for c in cl:
                pk = (lid, int(c["right_row_id"]))
                if pk in answers:
                    scored.append({"right_row_id": pk[1], "p": answers[pk], "retrieval_score": round(float(c["retrieval_score"]), 4)})
                else:
                    failed = True
                    scored.append({"right_row_id": pk[1], "p": None, "error": errors.get(pk, "unknown"),
                                   "retrieval_score": round(float(c["retrieval_score"]), 4)})
            scored.sort(key=lambda d: (-(d["p"] if d["p"] is not None else -1), d["right_row_id"]))
            best = next((d for d in scored if d["p"] is not None), None)
            if failed and best is None and cl:
                status, n_fail = "failed", n_fail + 1
            elif not cl:
                status, n_ok = "no_candidates", n_ok + 1
            elif best["p"] >= step.thresholds.match_min:
                status, n_ok = "match", n_ok + 1
            elif best["p"] <= step.thresholds.nonmatch_max:
                status, n_ok = "non_match", n_ok + 1
            else:
                status, n_ok, n_unc = "uncertain", n_ok + 1, n_unc + 1
            raw = json.dumps({"retrieval": "exact_blocking+lexical_v1", "candidates": scored}, ensure_ascii=False)
            if step.mode == "all" and status == "match":
                for d in scored:
                    if d["p"] is not None and d["p"] >= step.thresholds.match_min:
                        out_rows.append({ROW_ID: lid, "match.right_row_id": d["right_row_id"], "match.p": d["p"],
                                         "match.status": "match", "match.candidate_count": len(cl),
                                         "match.candidates": json.dumps(scored), "match.raw": raw})
            else:
                out_rows.append({ROW_ID: lid, "match.right_row_id": best["right_row_id"] if best and status in ("match", "uncertain") else None,
                                 "match.p": best["p"] if best else None, "match.status": status,
                                 "match.candidate_count": len(cl), "match.candidates": json.dumps(scored), "match.raw": raw})
        self.write_chunk(step.id, index, schema, out_rows)
        return n_ok, n_fail, n_skip, n_unc, n_hits, (lo, hi)

    # ---- helpers ------------------------------------------------------------------------------------------

    def materialize_input(self, compiled: CompiledPlan, input_step: str, columns: list[str], catalog: WorkspaceCatalog,
                          out: Path) -> None:
        sql = build_sql(compiled, input_step, catalog.source_sql, lambda sid: self.semantic_sql(sid))
        sel = ", ".join(q(c) for c in columns)
        tmp = out.with_suffix(".tmp")
        con = duckdb.connect()
        try:
            con.execute(f"COPY (SELECT {sel} FROM ({sql}) t ORDER BY {q(ROW_ID)}) TO {sql_str(tmp)} (FORMAT PARQUET)")
        finally:
            con.close()
        os.replace(tmp, out)

    def semantic_sql(self, step_id: str) -> str | None:
        return semantic_results_sql(self.job_id, step_id)

    def write_chunk(self, step_id: str, index: int, schema: pa.Schema, rows: list[dict[str, Any]]) -> None:
        table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
        final = step_chunk_parquet(self.job_id, step_id, index)
        tmp = final.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, final)

    def compact(self, step_id: str) -> None:
        files = step_chunk_files(self.job_id, step_id)
        final = step_final_parquet(self.job_id, step_id)
        if final.exists():
            return
        tmp = final.with_suffix(".tmp")
        con = duckdb.connect()
        try:
            if files:
                con.execute(f"COPY (SELECT * FROM read_parquet({sql_str(step_chunk_glob(self.job_id, step_id))}, union_by_name=true) "
                            f"ORDER BY {q(ROW_ID)}) TO {sql_str(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            else:
                return
        finally:
            con.close()
        os.replace(tmp, final)
        for f in files:
            f.unlink(missing_ok=True)

    def recount_from_files(self, prog: StepProgress, step: Any) -> None:
        files = step_chunk_files(self.job_id, step.id)
        if not files:
            return
        con = duckdb.connect()
        try:
            status_cols = [f"{qn.name}.status" for qn in step.questions] if isinstance(step, SemanticAnnotate) else ["match.status"]
            first = q(status_cols[0])
            fail_expr = " OR ".join(f"{q(c)} = 'failed'" for c in status_cols)
            unc_expr = " OR ".join(f"{q(c)} = 'uncertain'" for c in status_cols)
            r = con.execute(
                f"SELECT count(DISTINCT {q(ROW_ID)}), "
                f"count(DISTINCT CASE WHEN {first} IN ('missing_input','skipped') THEN {q(ROW_ID)} END), "
                f"count(DISTINCT CASE WHEN {fail_expr} THEN {q(ROW_ID)} END), "
                f"count(DISTINCT CASE WHEN {unc_expr} THEN {q(ROW_ID)} END) "
                f"FROM read_parquet({sql_str(step_chunk_glob(self.job_id, step.id))}, union_by_name=true)").fetchone()
        finally:
            con.close()
        n, skipped, failed, unc = (int(x or 0) for x in r)
        prog.skipped = skipped
        prog.failed = failed
        prog.uncertain = unc
        prog.succeeded = n - skipped - failed
        prog.chunks_done = len(files)

    def lookup_cache(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        if not keys:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with session() as s:
            for i in range(0, len(keys), 900):
                batch = keys[i:i + 900]
                for row in s.execute(select(Prediction.cache_key, Prediction.raw_json).where(Prediction.cache_key.in_(batch))).all():
                    out[row[0]] = json.loads(row[1])
        return out

    def store_cache(self, workspace_id: str, preds: list[tuple[str, dict[str, Any], int]]) -> None:
        if not preds:
            return
        seen: set[str] = set()
        rows = []
        for k, raw, tokens in preds:
            if k in seen:
                continue
            seen.add(k)
            rows.append({"cache_key": k, "workspace_id": workspace_id, "model": self.model,
                         "raw_json": json.dumps(raw, ensure_ascii=False), "input_tokens": int(tokens), "created_at": utcnow()})
        with session() as s:
            dialect = s.bind.dialect.name
            for i in range(0, len(rows), 500):
                batch = rows[i:i + 500]
                if dialect == "postgresql":
                    stmt = postgresql.insert(Prediction).values(batch).on_conflict_do_nothing(index_elements=["cache_key"])
                else:
                    stmt = sqlite.insert(Prediction).values(batch).prefix_with("OR IGNORE")
                s.execute(stmt)
            s.commit()


ARROW_TYPES = {"text": pa.string(), "integer": pa.int64(), "number": pa.float64(), "boolean": pa.bool_(),
               "date": pa.string(), "timestamp": pa.string(), "json": pa.string()}


def semantic_results_sql(job_id: str, step_id: str) -> str | None:
    final = step_final_parquet(job_id, step_id)
    if final.exists():
        return f"SELECT * FROM read_parquet({sql_str(final)})"
    if step_chunk_files(job_id, step_id):
        return f"SELECT * FROM read_parquet({sql_str(step_chunk_glob(job_id, step_id))}, union_by_name=true)"
    return None


def generate_candidates(input_path: Path, right_sql: str, step: SemanticMatch, left_cols: list[str], right_cols: list[str],
                        out: Path) -> None:
    """Exact blocking plus lexical retrieval (idf-weighted token overlap), top-k per left row."""
    con = duckdb.connect()
    try:
        con.execute("SET threads=4")
        lsel = " || ' ' || ".join(f"coalesce(CAST({q(c)} AS VARCHAR), '')" for c in step.left_columns)
        rsel = " || ' ' || ".join(f"coalesce(CAST({q(c)} AS VARCHAR), '')" for c in step.right_columns)
        lblock = ", ".join(f"{q(b.left)} AS blk_{i}" for i, b in enumerate(step.blocking))
        rblock = ", ".join(f"{q(b.right)} AS blk_{i}" for i, b in enumerate(step.blocking))
        con.execute(f"CREATE TABLE lefts AS SELECT {q(ROW_ID)} AS left_row_id, lower({lsel}) AS txt{', ' + lblock if lblock else ''} "
                    f"FROM read_parquet({sql_str(input_path)})")
        rcols_sel = ", ".join(f"{q(c)} AS {q('r_' + c)}" for c in right_cols)
        con.execute(f"CREATE TABLE rights AS SELECT {q(ROW_ID)} AS right_row_id, lower({rsel}) AS txt, {rcols_sel}"
                    f"{', ' + rblock if rblock else ''} FROM ({right_sql}) r LIMIT {int(step.max_right_rows)}")
        n_right = con.execute("SELECT count(*) FROM rights").fetchone()[0]
        con.execute("CREATE TABLE rtok AS SELECT DISTINCT right_row_id, tok FROM (SELECT right_row_id, unnest(regexp_split_to_array(txt, '[^a-z0-9]+')) AS tok FROM rights) WHERE length(tok) >= 2")
        con.execute(f"CREATE TABLE idf AS SELECT tok, ln(1.0 + {max(1, n_right)}::DOUBLE / count(*)) AS w FROM rtok GROUP BY tok "
                    f"HAVING count(*) <= GREATEST(5, {max(1, n_right)} * 0.25)")
        con.execute("CREATE TABLE ltok AS SELECT DISTINCT left_row_id, tok FROM (SELECT left_row_id, unnest(regexp_split_to_array(txt, '[^a-z0-9]+')) AS tok FROM lefts) WHERE length(tok) >= 2")
        block_join = " AND ".join(f"l.blk_{i} = r.blk_{i}" for i in range(len(step.blocking)))
        block_clause = f" AND {block_join}" if block_join else ""
        con.execute(
            f"COPY (SELECT * FROM (SELECT s.left_row_id, s.right_row_id, s.score AS retrieval_score, {', '.join('rr.' + q('r_' + c) for c in right_cols)} "
            f"FROM (SELECT lt.left_row_id, rt.right_row_id, sum(i.w) AS score FROM ltok lt JOIN idf i ON i.tok = lt.tok "
            f"JOIN rtok rt ON rt.tok = lt.tok JOIN lefts l ON l.left_row_id = lt.left_row_id JOIN rights r ON r.right_row_id = rt.right_row_id"
            f"{block_clause} GROUP BY lt.left_row_id, rt.right_row_id) s JOIN rights rr ON rr.right_row_id = s.right_row_id "
            f"QUALIFY row_number() OVER (PARTITION BY s.left_row_id ORDER BY s.score DESC, s.right_row_id) <= {int(step.candidates_per_row)}) "
            f"ORDER BY left_row_id, retrieval_score DESC) TO {sql_str(out.with_suffix('.tmp'))} (FORMAT PARQUET)")
    finally:
        con.close()
    os.replace(out.with_suffix(".tmp"), out)
