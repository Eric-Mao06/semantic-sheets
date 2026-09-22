"""Application services shared by the web API and the MCP adapter.

Business logic lives here (not in route handlers or MCP tool handlers): validation, estimates, scheduling,
querying, corrections, versioning and export."""
from __future__ import annotations

import json
import math
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from pydantic import ValidationError

from .config import settings
from .db import Database, dumps, loads, new_id, now, sha256
from .engine import jev
from .engine.exact import ROW_ID, Compiled, ExprCompiler, PlanError, Relation, compile_plan, connect, lit, q
from .importer import ImportError_, ImportOptions, checksum_file, import_file, report_to_dict, write_error_report
from .models import (
    AggregateStep,
    Expr,
    JobLimits,
    Plan,
    PlanEstimate,
    SemanticAnnotateStep,
    SemanticMatchStep,
    SortKey,
    SourceRef,
)
from .storage import build_context, export_path, resolve_source, upload_path, version_parquet


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            d["details"] = self.details
        return d


@dataclass
class Workspace:
    id: str
    name: str
    budget_usd: float


def plan_hash_of(plan: Plan) -> str:
    return sha256(json.dumps(plan.model_dump(mode="json", exclude={"title", "description"}), sort_keys=True, ensure_ascii=False))


def _truncate(value: Any, max_chars: int) -> tuple[Any, bool]:
    if isinstance(value, str) and len(value) > max_chars:
        return value[: max_chars - 1] + "…", True
    return value, False


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, (list, dict)):
        return json.loads(json.dumps(value, default=str))
    return str(value)


class ViewCache:
    """Byte-bounded LRU of ordered row-id snapshots keyed by (result version, revision, query hash)."""

    def __init__(self, max_entries: int = 64) -> None:
        self.max_entries = max_entries
        self._data: OrderedDict[str, list[int]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[int] | None:
        with self._lock:
            v = self._data.get(key)
            if v is not None:
                self._data.move_to_end(key)
            return v

    def put(self, key: str, value: list[int]) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


class Services:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.views = ViewCache()

    # ------------------------------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------------------------------
    def workspace_from_token(self, token: str | None) -> Workspace:
        row = self.db.workspace_for_token(token)
        if row is None:
            raise ServiceError("unauthorized", "Missing or invalid workspace token", 401)
        return Workspace(row["id"], row["name"], float(row["budget_usd"]))

    def workspace_info(self, ws: Workspace) -> dict[str, Any]:
        spend = self.db.workspace_spend(ws.id)
        return {"workspace_id": ws.id, "name": ws.name, **spend, "model": settings.jev_model, "planner_model": settings.planner_model, "retention_days": settings.retention_days,
                "limits": {"max_import_rows": settings.max_import_rows, "hard_max_source_rows": settings.hard_max_source_rows, "default_max_source_rows": settings.default_max_source_rows}}

    # ------------------------------------------------------------------------------------------
    # Uploads and datasets
    # ------------------------------------------------------------------------------------------
    def uploads_prepare(self, ws: Workspace, filename: str | None) -> dict[str, Any]:
        uid = new_id("up")
        p = upload_path(uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        expires = now() + 3600
        self.db.execute(
            "INSERT INTO uploads(id,workspace_id,filename,path,size,completed,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (uid, ws.id, filename, str(p), 0, 0, now(), expires),
        )
        return {"upload_id": uid, "upload_url": f"/api/uploads/{uid}/content", "method": "PUT", "expires_at": expires, "max_bytes": settings.max_file_bytes}

    def upload_write(self, ws: Workspace, upload_id: str, chunks: Iterable[bytes]) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM uploads WHERE id=? AND workspace_id=?", (upload_id, ws.id))
        if row is None:
            raise ServiceError("not_found", "Upload not found", 404)
        size = 0
        with open(row["path"], "wb") as fh:
            for chunk in chunks:
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise ServiceError("file_too_large", f"Upload exceeds {settings.max_file_bytes} bytes", 413)
                fh.write(chunk)
        self.db.execute("UPDATE uploads SET size=?, completed=1 WHERE id=?", (size, upload_id))
        return {"upload_id": upload_id, "size": size, "completed": True}

    def datasets_import(self, ws: Workspace, upload_id: str | None, name: str, options: dict[str, Any] | None = None, source_path: Path | None = None) -> dict[str, Any]:
        options = options or {}
        if source_path is None:
            row = self.db.one("SELECT * FROM uploads WHERE id=? AND workspace_id=?", (upload_id, ws.id))
            if row is None or not row["completed"]:
                raise ServiceError("upload_incomplete", "Upload not found or not completed", 404)
            source_path = Path(row["path"])
            filename = row["filename"] or name
        else:
            filename = source_path.name
        opts = ImportOptions(
            delimiter=options.get("delimiter"),
            header=options.get("header"),
            permissive=bool(options.get("permissive", False)),
            row_limit=options.get("row_limit"),
        )
        dataset_id = new_id("ds")
        version_id = new_id("v")
        dest = version_parquet(dataset_id, version_id)
        started = time.time()
        try:
            schema, report = import_file(source_path, dest, opts)
        except ImportError_ as e:
            report_path = settings.data_dir / "datasets" / dataset_id / "import_errors.csv"
            if e.report:
                write_error_report(e.report, report_path)
            raise ServiceError(e.code, str(e), 422, {**e.report, "error_report": f"/api/datasets/{dataset_id}/import-errors" if e.report else None}) from e
        # Preserve original bytes next to the snapshot.
        suffix = "".join(Path(filename).suffixes[-2:]) if filename else ".csv"
        kept = settings.data_dir / "datasets" / dataset_id / f"source{suffix or '.csv'}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, kept)
        checksum = checksum_file(kept)
        t = now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO datasets(id,workspace_id,name,source_filename,source_checksum,source_path,source_bytes,created_at,retention_expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (dataset_id, ws.id, name, filename, checksum, str(kept), kept.stat().st_size, t, t + settings.retention_days * 86400),
            )
            conn.execute(
                "INSERT INTO dataset_versions(id,dataset_id,version_no,parent_version_id,kind,schema_json,row_count,data_path,report_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (version_id, dataset_id, 1, None, "import", dumps(schema), report.row_count, str(dest), dumps(report_to_dict(report)), t),
            )
        if upload_id:
            self.db.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
            try:
                source_path.unlink()
            except OSError:
                pass
        return {
            "import_job": {"id": new_id("imp"), "state": "succeeded", "duration_ms": int((time.time() - started) * 1000)},
            "dataset": self.datasets_describe(ws, dataset_id, version_id, sample=False),
            "report": report_to_dict(report),
        }

    def datasets_list(self, ws: Workspace, cursor: str | None, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        params: list[Any] = [ws.id]
        where = "workspace_id=? AND deleted_at IS NULL"
        if cursor:
            where += " AND created_at < ?"
            params.append(float(cursor))
        rows = self.db.query(f"SELECT * FROM datasets WHERE {where} ORDER BY created_at DESC LIMIT ?", params + [limit + 1])
        items = []
        for r in rows[:limit]:
            ver = self.db.one("SELECT id, version_no, row_count, schema_json FROM dataset_versions WHERE dataset_id=? ORDER BY version_no DESC LIMIT 1", (r["id"],))
            items.append({
                "dataset_id": r["id"], "name": r["name"], "created_at": r["created_at"], "retention_expires_at": r["retention_expires_at"],
                "latest_version_id": ver["id"] if ver else None, "row_count": ver["row_count"] if ver else 0,
                "column_count": len(loads(ver["schema_json"])) - 1 if ver else 0, "source_filename": r["source_filename"],
            })
        return {"datasets": items, "next_cursor": str(rows[limit - 1]["created_at"]) if len(rows) > limit else None}

    def _dataset(self, ws: Workspace, dataset_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM datasets WHERE id=? AND workspace_id=? AND deleted_at IS NULL", (dataset_id, ws.id))
        if row is None:
            raise ServiceError("not_found", "Dataset not found in this workspace", 404)
        return dict(row)

    def datasets_describe(self, ws: Workspace, dataset_id: str, version_id: str | None = None, sample: bool = False, sample_rows: int | None = None) -> dict[str, Any]:
        ds = self._dataset(ws, dataset_id)
        _, ver = resolve_source(self.db, ws.id, SourceRef(dataset_id=dataset_id, version_id=version_id))
        versions = [dict(r) for r in self.db.query("SELECT id, version_no, kind, row_count, created_at, parent_version_id FROM dataset_versions WHERE dataset_id=? ORDER BY version_no", (dataset_id,))]
        schema = loads(ver["schema_json"])
        out: dict[str, Any] = {
            "dataset_id": ds["id"], "name": ds["name"], "source_filename": ds["source_filename"], "source_checksum": ds["source_checksum"],
            "source_bytes": ds["source_bytes"], "created_at": ds["created_at"], "retention_expires_at": ds["retention_expires_at"],
            "version_id": ver["id"], "version_no": ver["version_no"], "row_count": ver["row_count"],
            "schema": schema[:100], "schema_truncated": len(schema) > 100, "column_count": len(schema) - 1,
            "versions": versions, "import_report": loads(ver["report_json"], {}),
        }
        if sample:
            n = min(sample_rows or settings.sample_max_rows, settings.sample_max_rows)
            with connect() as con:
                rel = con.execute(f"SELECT * FROM read_parquet({lit(ver['data_path'])}) LIMIT {int(n)}")
                cols = [d[0] for d in rel.description]
                rows = rel.fetchall()
            out["sample"] = self._bound_rows(cols, rows, settings.sample_max_bytes, 200)[0]
        return out

    def dataset_rows_for_planner(self, ws: Workspace, dataset_id: str, version_id: str | None, n: int = 20) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        _, ver = resolve_source(self.db, ws.id, SourceRef(dataset_id=dataset_id, version_id=version_id))
        schema = loads(ver["schema_json"])
        with connect() as con:
            rel = con.execute(f"SELECT * FROM read_parquet({lit(ver['data_path'])}) USING SAMPLE reservoir({int(n)} ROWS) REPEATABLE (7)")
            cols = [d[0] for d in rel.description]
            rows = rel.fetchall()
        bounded, _ = self._bound_rows(cols, rows, settings.sample_max_bytes, 200)
        return schema, bounded

    def datasets_delete(self, ws: Workspace, dataset_id: str) -> dict[str, Any]:
        ds = self._dataset(ws, dataset_id)
        t = now()
        jobs = self.db.query("SELECT id FROM jobs WHERE workspace_id=? AND state IN ('queued','running') AND dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id=?)", (ws.id, dataset_id))
        with self.db.connect() as conn:
            for j in jobs:
                conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (j["id"],))
            conn.execute("UPDATE datasets SET deleted_at=? WHERE id=?", (t, dataset_id))
            conn.execute("UPDATE exports SET state='revoked' WHERE workspace_id=? AND result_version_id IN (SELECT id FROM result_versions WHERE dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id=?))", (ws.id, dataset_id))
        shutil.rmtree(settings.data_dir / "datasets" / dataset_id, ignore_errors=True)
        for rv in self.db.query("SELECT id FROM result_versions WHERE dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id=?)", (dataset_id,)):
            shutil.rmtree(settings.data_dir / "results" / rv["id"], ignore_errors=True)
        return {"dataset_id": dataset_id, "deleted": True, "cancelled_jobs": [j["id"] for j in jobs], "name": ds["name"]}

    def datasets_patch(self, ws: Workspace, dataset_id: str, version_id: str | None, patches: list[dict[str, Any]]) -> dict[str, Any]:
        """Bounded cell corrections to base data -> new immutable dataset version (source snapshot unchanged)."""
        if not patches or len(patches) > 1000:
            raise ServiceError("invalid_patch", "Provide between 1 and 1000 patches", 422)
        _, ver = resolve_source(self.db, ws.id, SourceRef(dataset_id=dataset_id, version_id=version_id))
        schema = loads(ver["schema_json"])
        types = {c["name"]: c["type"] for c in schema}
        for i, p in enumerate(patches):
            if p.get("column") not in types or p["column"] == ROW_ID:
                raise ServiceError("invalid_patch", f"patches[{i}].column is not a patchable column", 422, {"path": f"patches[{i}].column"})
        new_version_id = new_id("v")
        dest = version_parquet(dataset_id, new_version_id)
        by_col: dict[str, list[tuple[int, Any]]] = {}
        for p in patches:
            by_col.setdefault(p["column"], []).append((int(p["row_id"]), p.get("value")))
        with connect() as con:
            selects = []
            for c in schema:
                name = c["name"]
                if name in by_col:
                    tbl = f"patch_{len(selects)}"
                    con.execute(f"CREATE TEMP TABLE {tbl} (row_id BIGINT, value VARCHAR)")
                    con.executemany(f"INSERT INTO {tbl} VALUES (?,?)", [(r, None if v is None else str(v)) for r, v in by_col[name]])
                    duck_t = {"integer": "BIGINT", "double": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP"}.get(c["type"], "VARCHAR")
                    selects.append(f"CASE WHEN {tbl}.row_id IS NOT NULL THEN TRY_CAST({tbl}.value AS {duck_t}) ELSE b.{q(name)} END AS {q(name)}")
                else:
                    selects.append(f"b.{q(name)}")
            joins = " ".join(f"LEFT JOIN patch_{i} ON patch_{i}.row_id = b.{q(ROW_ID)}" for i, c in enumerate(schema) if c["name"] in by_col)
            con.execute(f"COPY (SELECT {', '.join(selects)} FROM read_parquet({lit(ver['data_path'])}) b {joins} ORDER BY b.{q(ROW_ID)}) TO {lit(str(dest))} (FORMAT PARQUET, COMPRESSION ZSTD)")
        t = now()
        self.db.execute(
            "INSERT INTO dataset_versions(id,dataset_id,version_no,parent_version_id,kind,schema_json,row_count,data_path,report_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new_version_id, dataset_id, int(ver["version_no"]) + 1, ver["id"], "patch", ver["schema_json"], ver["row_count"], str(dest), dumps({"patches": len(patches)}), t),
        )
        return {"dataset_id": dataset_id, "version_id": new_version_id, "parent_version_id": ver["id"], "patched_cells": len(patches)}

    # ------------------------------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------------------------------
    def parse_plan(self, plan_obj: dict[str, Any]) -> Plan:
        try:
            return Plan.model_validate(plan_obj)
        except ValidationError as e:
            issues = [{"path": ".".join(str(p) for p in err["loc"]), "code": err["type"], "message": err["msg"], "fix": None} for err in e.errors()]
            raise ServiceError("invalid_plan", "Plan failed schema validation", 422, {"issues": issues[:20]}) from e

    def plans_validate(self, ws: Workspace, plan_obj: dict[str, Any], limits: JobLimits | None = None, store: bool = True) -> dict[str, Any]:
        plan = self.parse_plan(plan_obj)
        try:
            _, ver = resolve_source(self.db, ws.id, plan.source)
        except LookupError as e:
            raise ServiceError("unknown_dataset", str(e), 404, {"issues": [{"path": "source", "code": "unknown_dataset", "message": str(e), "fix": "Use a dataset_id from datasets_list"}]}) from e
        plan.source = SourceRef(dataset_id=plan.source.dataset_id, version_id=ver["id"])
        try:
            ctx = build_context(self.db, ws.id, plan, ver, None, None)
            compiled = compile_plan(plan, ctx)
        except LookupError as e:
            raise ServiceError("unknown_dataset", str(e), 404, {"issues": [{"path": "steps", "code": "unknown_dataset", "message": str(e), "fix": None}]}) from e
        except PlanError as e:
            raise ServiceError("invalid_plan", e.message, 422, {"issues": [e.to_issue()]}) from e
        warnings: list[str] = []
        estimate = self._estimate(plan, compiled, ctx, ver, limits or JobLimits(), warnings, ws.id)
        # Ensure the SQL compiles for every step (types, functions).
        try:
            with connect(compiled) as con:
                for step in plan.steps:
                    con.execute(f"DESCRIBE {compiled.sql_for(step.id)}")
        except duckdb.Error as e:
            raise ServiceError("invalid_plan", f"Plan does not execute: {e}", 422, {"issues": [{"path": "steps", "code": "execution", "message": str(e)[:400], "fix": None}]}) from e
        phash = plan_hash_of(plan)
        if store:
            existing = self.db.one("SELECT id FROM plans WHERE workspace_id=? AND plan_hash=?", (ws.id, phash))
            if existing is None:
                self.db.execute(
                    "INSERT INTO plans(id,workspace_id,plan_hash,plan_json,estimate_json,prompt,created_at) VALUES(?,?,?,?,?,?,?)",
                    (new_id("plan"), ws.id, phash, dumps(plan.model_dump(mode="json")), dumps(estimate.model_dump()), None, now()),
                )
        output_rel = compiled.relations[plan.output]
        return {
            "plan_hash": phash,
            "plan": plan.model_dump(mode="json"),
            "estimate": estimate.model_dump(),
            "warnings": warnings,
            "required_scopes": ["read", "run"],
            "output_columns": [{"name": c.name, "type": c.type, "role": c.role} for c in output_rel.columns],
            "review_views": list(compiled.review_relations.values()),
        }

    def _cache_hits(self, keys: list[str]) -> set[str]:
        out: set[str] = set()
        for i in range(0, len(keys), 500):
            batch = keys[i : i + 500]
            marks = ",".join("?" * len(batch))
            out.update(r["cache_key"] for r in self.db.query(f"SELECT cache_key FROM prediction_cache WHERE cache_key IN ({marks})", batch))
        return out

    def _estimate(self, plan: Plan, compiled: Compiled, ctx: Any, ver: dict[str, Any], limits: JobLimits, warnings: list[str], ws_id: str = "") -> PlanEstimate:
        rows_per_request = limits.rows_per_request or settings.jev_rows_per_request
        source_rows = int(ver["row_count"])
        cache_hits_total = 0
        stages: list[dict[str, Any]] = []
        total_tokens = 0
        total_requests = 0
        total_attempts = 0
        semantic_rows = 0
        pairs_total = 0
        with connect(compiled) as con:
            for step in plan.steps:
                if not isinstance(step, (SemanticAnnotateStep, SemanticMatchStep)):
                    continue
                inp_rel = compiled.relations[step.input]
                bound = "exact"
                if inp_rel.pending_statuses:
                    # Input depends on a semantic stage that has not run: use the row count of the nearest
                    # non-dependent ancestor as an upper bound.
                    anc = step.input
                    while compiled.relations[anc].pending_statuses and anc != "source":
                        anc = next(s.input for s in plan.steps if s.id == anc)
                    count_sql = compiled.sql_for(anc)
                    bound = "upper_bound"
                else:
                    count_sql = compiled.sql_for(step.input)
                n_rows = int(con.execute(f"SELECT count(*) FROM ({count_sql}) t").fetchone()[0])
                n_rows_capped = min(n_rows, limits.max_source_rows)
                if n_rows > limits.max_source_rows:
                    warnings.append(f"step '{step.id}': {n_rows} input rows exceed max_source_rows={limits.max_source_rows}; the job will process the first {limits.max_source_rows} and finish as partial")
                if isinstance(step, SemanticAnnotateStep):
                    cols = ", ".join(q(c) for c in step.columns)
                    sample = con.execute(f"SELECT {cols} FROM ({count_sql}) t USING SAMPLE reservoir(120 ROWS) REPEATABLE (3)").fetchall()
                    per_item = [jev.item_tokens({c: (str(v) if v is not None else "") for c, v in zip(step.columns, r)}, step.questions) for r in sample] or [60]
                    mean_item = sum(per_item) / len(per_item)
                    # Cache hit ratio measured on the same sample, extrapolated to the capped row count.
                    keys = []
                    for r in sample:
                        payload = jev.row_payload(dict(zip(step.columns, r)), step.columns)
                        if payload is not None:
                            keys.extend(jev.cache_key(ws_id, plan.model, qn, payload) for qn in step.questions)
                    hit_ratio = (len(self._cache_hits(keys)) / len(keys)) if keys else 0.0
                    attempts = n_rows_capped * len(step.questions)
                    hits = int(round(attempts * hit_ratio))
                    rows_to_send = n_rows_capped * (1.0 - hit_ratio)
                    requests = math.ceil(rows_to_send / rows_per_request) if rows_to_send else 0
                    tokens = int(rows_to_send * mean_item + requests * 40)
                    cache_hits_total += hits
                    stages.append({"step": step.id, "kind": "annotate", "rows": n_rows_capped, "rows_bound": bound, "questions": len(step.questions),
                                   "inference_attempts": attempts - hits, "cache_hits_estimated": hits, "requests": requests, "input_tokens": tokens,
                                   "mean_tokens_per_row": round(mean_item, 1), "estimated_cost_usd": round(jev.cost_usd(tokens), 6)})
                    attempts -= hits
                else:
                    rpath, rschema = ctx.datasets[step.right.dataset_id]
                    right_rows = int(con.execute(f"SELECT count(*) FROM read_parquet({lit(str(rpath))})").fetchone()[0])
                    if right_rows > settings.match_max_right_rows:
                        raise ServiceError("invalid_plan", f"semantic_match right table has {right_rows} rows; the launch cap is {settings.match_max_right_rows}", 422,
                                           {"issues": [{"path": "steps", "code": "right_table_too_large", "message": "Right table exceeds the cap", "fix": "Filter or sample the right table first"}]})
                    pairs = n_rows_capped * step.candidates_per_row
                    if pairs > settings.match_max_pairs:
                        warnings.append(f"step '{step.id}': {pairs} candidate pairs exceed the pair budget {settings.match_max_pairs}; the job will stop early as partial")
                        pairs = settings.match_max_pairs
                    per_pair = 120 + 40 * (len(step.left_columns) + len(step.right_columns))
                    requests = math.ceil(pairs / max(1, rows_per_request))
                    tokens = int(pairs * per_pair)
                    attempts = pairs
                    pairs_total += pairs
                    stages.append({"step": step.id, "kind": "match", "rows": n_rows_capped, "rows_bound": bound, "right_rows": right_rows, "candidate_pairs": pairs,
                                   "inference_attempts": attempts, "requests": requests, "input_tokens": tokens, "estimated_cost_usd": round(jev.cost_usd(tokens), 6),
                                   "retrieval": settings.retrieval_version})
                semantic_rows = max(semantic_rows, n_rows_capped)
                total_tokens += tokens
                total_requests += requests
                total_attempts += attempts
        rpm = settings.jev_requests_per_minute / 60.0
        floor = max(total_tokens / settings.jev_tokens_per_second, total_requests / rpm) if total_requests else 0.0
        est_cost = jev.cost_usd(total_tokens)
        if est_cost > limits.spend_target_usd:
            warnings.append(f"estimated cost ${est_cost:.4f} exceeds spend_target_usd=${limits.spend_target_usd:.2f}; dispatch will stop at the target and the job will finish as partial")
        if total_requests > limits.max_provider_requests:
            warnings.append(f"estimated {total_requests} provider requests exceed max_provider_requests={limits.max_provider_requests}")
        if not stages:
            warnings.append("plan has no semantic steps; it runs entirely in the exact engine with no provider calls")
        return PlanEstimate(
            source_rows=source_rows, semantic_rows=semantic_rows, inference_attempts=total_attempts, provider_requests=total_requests,
            input_tokens=total_tokens, estimated_cost_usd=round(est_cost, 6), candidate_pairs=pairs_total, stages=stages, quota_floor_seconds=round(floor, 2),
            cache_hits_estimated=cache_hits_total,
        )

    def plans_compile(self, ws: Workspace, dataset_id: str, version_id: str | None, prompt: str, previous_plan: dict[str, Any] | None = None, max_attempts: int = 3) -> dict[str, Any]:
        """Language -> typed plan with the frontier planner, then validate; feed validation issues back once or twice."""
        from . import planner

        if not prompt or len(prompt) > 4000:
            raise ServiceError("invalid_request", "prompt must be 1..4000 characters", 422)
        _, ver = resolve_source(self.db, ws.id, SourceRef(dataset_id=dataset_id, version_id=version_id))
        schema, sample = self.dataset_rows_for_planner(ws, dataset_id, ver["id"])
        others = [{"dataset_id": d["dataset_id"], "name": d["name"], "columns": [c["name"] for c in loads(self.db.one("SELECT schema_json FROM dataset_versions WHERE id=?", (d["latest_version_id"],))["schema_json"])][:40]}
                  for d in self.datasets_list(ws, None, 20)["datasets"] if d["dataset_id"] != dataset_id and d["latest_version_id"]]
        feedback = None
        attempts: list[dict[str, Any]] = []
        last_error: ServiceError | None = None
        for attempt in range(max_attempts):
            try:
                out = planner.compile_prompt(prompt, dataset_id, ver["id"], schema, sample, int(ver["row_count"]), others, previous_plan, feedback)
            except planner.PlannerError as e:
                raise ServiceError("planner_failed", str(e), 502) from e
            plan_obj = out["plan"]
            try:
                validated = self.plans_validate(ws, plan_obj, None, store=True)
                self.db.execute("UPDATE plans SET prompt=? WHERE workspace_id=? AND plan_hash=? AND prompt IS NULL", (prompt, ws.id, validated["plan_hash"]))
                return {**validated, "planner": out["planner"], "attempts": attempt + 1, "title": plan_obj.get("title"), "description": plan_obj.get("description")}
            except ServiceError as e:
                last_error = e
                issues = (e.details or {}).get("issues") if isinstance(e.details, dict) else None
                feedback = json.dumps(issues or {"message": e.message}, ensure_ascii=False)
                attempts.append({"plan": plan_obj, "issues": issues or e.message})
                previous_plan = plan_obj
        assert last_error is not None
        raise ServiceError("planner_invalid_plan", f"Planner could not produce a valid plan after {max_attempts} attempts: {last_error.message}", 422, {"attempts": attempts[-1:], "issues": (last_error.details or {}).get("issues") if isinstance(last_error.details, dict) else None})

    def results_provenance(self, ws: Workspace, rv_id: str, step: str, row_id: int) -> dict[str, Any]:
        """Raw provider answers and override records for one row of a semantic step."""
        rv = self._result_version(ws, rv_id)
        plan, compiled, manifest = self._compiled_for(ws, rv)
        from .storage import derived_glob

        owner = manifest.get("derived_from") or rv_id
        g = derived_glob(owner, step)
        raw: dict[str, Any] = {}
        if g is not None:
            with connect() as con:
                res = con.execute(f"SELECT * FROM read_parquet({lit(str(g))}) WHERE {q(ROW_ID)} = {int(row_id)}")
                cols = [d[0] for d in res.description]
                row = res.fetchone()
            if row:
                for c, v in zip(cols, row):
                    if c.endswith(".raw") and v:
                        raw[c[:-4]] = json.loads(v)
                    elif c != ROW_ID:
                        raw.setdefault("_interpreted", {})[c] = _jsonable(v)
        step_obj = next((s for s in plan.steps if s.id == step), None)
        overrides = [{"column": r["column_name"], "value": loads(r["value_json"]), "reason": r["reason"], "created_at": r["created_at"]} for r in
                     self.db.query("SELECT * FROM overrides WHERE result_version_id=? AND row_id=?", (rv_id, int(row_id)))]
        return {"row_id": row_id, "step": step, "model": manifest.get("model"), "plan_hash": rv["plan_hash"], "raw": raw, "overrides": overrides,
                "questions": [qn.model_dump() for qn in getattr(step_obj, "questions", [])] if step_obj else [],
                "prompt_layout_version": settings.prompt_layout_version, "normalization_version": settings.normalization_version}

    def plan_by_hash(self, ws: Workspace, plan_hash: str) -> tuple[str, Plan]:
        row = self.db.one("SELECT id, plan_json FROM plans WHERE workspace_id=? AND plan_hash=?", (ws.id, plan_hash))
        if row is None:
            raise ServiceError("unknown_plan", "plan_hash not found; call plans_validate first", 404)
        return row["id"], Plan.model_validate(loads(row["plan_json"]))

    # ------------------------------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------------------------------
    def jobs_submit(self, ws: Workspace, plan_hash: str | None, plan_obj: dict[str, Any] | None, limits: dict[str, Any] | None, idempotency_key: str | None) -> dict[str, Any]:
        try:
            lim = JobLimits.model_validate(limits or {})
        except ValidationError as e:
            raise ServiceError("invalid_limits", "Invalid limits", 422, {"issues": [{"path": "limits." + ".".join(str(p) for p in err["loc"]), "code": err["type"], "message": err["msg"]} for err in e.errors()]}) from e
        if lim.max_source_rows > settings.hard_max_source_rows:
            raise ServiceError("invalid_limits", f"max_source_rows exceeds the hard cap {settings.hard_max_source_rows}", 422)
        if plan_hash is None:
            if plan_obj is None:
                raise ServiceError("invalid_request", "Provide plan_hash or plan", 422)
            validated = self.plans_validate(ws, plan_obj, lim)
            plan_hash = validated["plan_hash"]
        plan_id, plan = self.plan_by_hash(ws, plan_hash)
        _, ver = resolve_source(self.db, ws.id, plan.source)
        payload_hash = sha256(json.dumps({"plan_hash": plan_hash, "limits": lim.model_dump(), "version": ver["id"]}, sort_keys=True))
        if idempotency_key:
            existing = self.db.one("SELECT * FROM jobs WHERE workspace_id=? AND idempotency_key=?", (ws.id, idempotency_key))
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ServiceError("idempotency_conflict", "idempotency_key was already used with a different payload", 409)
                return self.jobs_get(ws, existing["id"])
        spend = self.db.workspace_spend(ws.id)
        if spend["spent_usd"] >= spend["budget_usd"]:
            raise ServiceError("budget_exhausted", "Workspace budget is exhausted", 402, spend)
        job_id = new_id("job")
        rv_id = new_id("rv")
        t = now()
        manifest = {"plan_hash": plan_hash, "model": plan.model, "derived_from": None, "steps": {}, "status": "running", "output": plan.output}
        progress = {"stages": {}, "rows_examined": 0, "rows_remaining": None, "errors": 0}
        usage = {"input_tokens": 0, "output_tokens": 0, "provider_requests": 0, "spent_usd": 0.0, "reserved_usd": 0.0, "cache_hits": 0, "ambiguous_attempts": 0}
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO result_versions(id,workspace_id,job_id,dataset_version_id,plan_id,plan_hash,parent_result_version_id,revision,status,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rv_id, ws.id, job_id, ver["id"], plan_id, plan_hash, None, 0, "running", dumps(manifest), t, t),
            )
            conn.execute(
                "INSERT INTO jobs(id,workspace_id,plan_id,plan_hash,dataset_version_id,idempotency_key,payload_hash,state,limits_json,progress_json,usage_json,model,result_version_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, ws.id, plan_id, plan_hash, ver["id"], idempotency_key, payload_hash, "queued", dumps(lim.model_dump()), dumps(progress), dumps(usage), plan.model, rv_id, t),
            )
        self.db.append_event(job_id, "queued", {"job_id": job_id, "result_version_id": rv_id})
        return self.jobs_get(ws, job_id)

    def _job(self, ws: Workspace, job_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (job_id, ws.id))
        if row is None:
            raise ServiceError("not_found", "Job not found in this workspace", 404)
        return dict(row)

    def jobs_get(self, ws: Workspace, job_id: str) -> dict[str, Any]:
        j = self._job(ws, job_id)
        rv = self.db.one("SELECT revision, status FROM result_versions WHERE id=?", (j["result_version_id"],))
        state = j["state"]
        interval = 2 if state in ("queued", "running") else 0
        if state == "running" and j["started_at"] and now() - j["started_at"] > 120:
            interval = 5
        errors = self.db.query("SELECT stage_id, chunk_index, error FROM job_chunks WHERE job_id=? AND state='failed' LIMIT 5", (job_id,))
        return {
            "job_id": j["id"], "state": state, "terminal_reason": j["terminal_reason"], "plan_hash": j["plan_hash"],
            "dataset_version_id": j["dataset_version_id"], "model": j["model"], "limits": loads(j["limits_json"]),
            "progress": loads(j["progress_json"]), "usage": loads(j["usage_json"]),
            "result_version_id": j["result_version_id"], "result_revision": rv["revision"] if rv else 0,
            "created_at": j["created_at"], "started_at": j["started_at"], "finished_at": j["finished_at"],
            "cancel_requested": bool(j["cancel_requested"]), "error_summary": j["error_summary"],
            "errors": [dict(e) for e in errors], "suggested_poll_seconds": interval,
        }

    def jobs_cancel(self, ws: Workspace, job_id: str) -> dict[str, Any]:
        j = self._job(ws, job_id)
        if j["state"] in ("queued",):
            with self.db.connect() as conn:
                conn.execute("UPDATE jobs SET state='cancelled', terminal_reason='cancelled', finished_at=?, cancel_requested=1 WHERE id=? AND state='queued'", (now(), job_id))
                conn.execute("UPDATE result_versions SET status='cancelled', updated_at=? WHERE id=?", (now(), j["result_version_id"]))
            self.db.append_event(job_id, "cancelled", {"job_id": job_id})
        elif j["state"] == "running":
            self.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
            self.db.append_event(job_id, "cancel_requested", {"job_id": job_id})
        return self.jobs_get(ws, job_id)

    def jobs_list(self, ws: Workspace, limit: int = 50, dataset_version_id: str | None = None) -> list[dict[str, Any]]:
        if dataset_version_id:
            rows = self.db.query("SELECT id FROM jobs WHERE workspace_id=? AND dataset_version_id=? ORDER BY created_at DESC LIMIT ?", (ws.id, dataset_version_id, limit))
        else:
            rows = self.db.query("SELECT id FROM jobs WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?", (ws.id, limit))
        return [self.jobs_get(ws, r["id"]) for r in rows]

    def jobs_events(self, ws: Workspace, job_id: str, after_seq: int) -> list[dict[str, Any]]:
        self._job(ws, job_id)
        return [{"seq": r["seq"], "type": r["type"], "created_at": r["created_at"], **loads(r["payload_json"])} for r in self.db.events_after(job_id, after_seq)]

    # ------------------------------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------------------------------
    def _result_version(self, ws: Workspace, rv_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM result_versions WHERE id=? AND workspace_id=?", (rv_id, ws.id))
        if row is None:
            raise ServiceError("not_found", "Result version not found in this workspace", 404)
        return dict(row)

    def _compiled_for(self, ws: Workspace, rv: dict[str, Any]) -> tuple[Plan, Compiled, dict[str, Any]]:
        plan_row = self.db.one("SELECT plan_json FROM plans WHERE id=?", (rv["plan_id"],))
        plan = Plan.model_validate(loads(plan_row["plan_json"]))
        ver = self.db.one("SELECT * FROM dataset_versions WHERE id=?", (rv["dataset_version_id"],))
        if ver is None:
            raise ServiceError("gone", "The dataset behind this result was deleted", 410)
        manifest = loads(rv["manifest_json"], {})
        ctx = build_context(self.db, ws.id, plan, dict(ver), rv["id"], manifest)
        return plan, compile_plan(plan, ctx), manifest

    def ensure_base_result(self, ws: Workspace, dataset_id: str, version_id: str | None) -> str:
        """Identity result version over a dataset version so the grid can page base data through the same view service."""
        _, ver = resolve_source(self.db, ws.id, SourceRef(dataset_id=dataset_id, version_id=version_id))
        schema = loads(ver["schema_json"])
        plan = Plan(source=SourceRef(dataset_id=dataset_id, version_id=ver["id"]), steps=[{"id": "view", "op": "project", "input": "source", "columns": [c["name"] for c in schema if c["name"] != ROW_ID]}], output="view", title="Base view")
        phash = plan_hash_of(plan)
        row = self.db.one("SELECT id FROM plans WHERE workspace_id=? AND plan_hash=?", (ws.id, phash))
        if row is None:
            plan_id = new_id("plan")
            self.db.execute("INSERT INTO plans(id,workspace_id,plan_hash,plan_json,estimate_json,prompt,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, ws.id, phash, dumps(plan.model_dump(mode="json")), None, None, now()))
        else:
            plan_id = row["id"]
        rv = self.db.one("SELECT id FROM result_versions WHERE workspace_id=? AND plan_id=? AND job_id IS NULL AND dataset_version_id=?", (ws.id, plan_id, ver["id"]))
        if rv is not None:
            return rv["id"]
        rv_id = new_id("rv")
        t = now()
        self.db.execute("INSERT INTO result_versions(id,workspace_id,job_id,dataset_version_id,plan_id,plan_hash,parent_result_version_id,revision,status,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rv_id, ws.id, None, ver["id"], plan_id, phash, None, 0, "complete", dumps({"plan_hash": phash, "model": None, "steps": {}, "status": "complete", "base": True}), t, t))
        return rv_id

    def results_describe(self, ws: Workspace, rv_id: str) -> dict[str, Any]:
        rv = self._result_version(ws, rv_id)
        plan, compiled, manifest = self._compiled_for(ws, rv)
        steps = []
        for s in plan.steps:
            rel = compiled.relations[s.id]
            steps.append({"id": s.id, "op": s.op, "input": s.input, "columns": [{"name": c.name, "type": c.type, "role": c.role} for c in rel.columns],
                          "provisional": bool(rel.pending_statuses), "review_view": compiled.review_relations.get(s.id), "row_preserving": rel.row_preserving})
        overrides = self.db.one("SELECT count(*) c FROM overrides WHERE result_version_id=?", (rv_id,))["c"]
        return {"result_version_id": rv_id, "job_id": rv["job_id"], "dataset_version_id": rv["dataset_version_id"], "plan_hash": rv["plan_hash"], "plan": plan.model_dump(mode="json"),
                "revision": rv["revision"], "status": rv["status"], "manifest": manifest, "steps": steps, "output": plan.output,
                "parent_result_version_id": rv["parent_result_version_id"], "overrides": overrides, "created_at": rv["created_at"]}

    def results_versions(self, ws: Workspace, rv_id: str) -> list[dict[str, Any]]:
        rv = self._result_version(ws, rv_id)
        root = rv
        while root["parent_result_version_id"]:
            root = self._result_version(ws, root["parent_result_version_id"])
        rows = self.db.query("SELECT id, parent_result_version_id, revision, status, created_at, manifest_json FROM result_versions WHERE workspace_id=? AND (id=? OR manifest_json LIKE ?) ORDER BY created_at", (ws.id, root["id"], f'%"derived_from":"{root["id"]}"%'))
        out = []
        for r in rows:
            m = loads(r["manifest_json"], {})
            ov = self.db.one("SELECT count(*) c FROM overrides WHERE result_version_id=?", (r["id"],))["c"]
            out.append({"result_version_id": r["id"], "parent_result_version_id": r["parent_result_version_id"], "status": r["status"], "created_at": r["created_at"], "overrides": ov, "note": m.get("note")})
        return out

    def _bound_rows(self, cols: list[str], rows: list[tuple], max_bytes: int, max_chars: int) -> tuple[list[dict[str, Any]], bool]:
        out: list[dict[str, Any]] = []
        size = 0
        truncated_any = False
        for r in rows:
            obj: dict[str, Any] = {}
            trunc_cols: list[str] = []
            for c, v in zip(cols, r):
                v2, t = _truncate(_jsonable(v), max_chars)
                if t:
                    trunc_cols.append(c)
                obj[c] = v2
            if trunc_cols:
                obj["_truncated"] = trunc_cols
                truncated_any = True
            s = len(dumps(obj))
            if out and size + s > max_bytes:
                return out, True
            out.append(obj)
            size += s
        return out, truncated_any

    def results_query(self, ws: Workspace, rv_id: str, step: str | None = None, columns: list[str] | None = None, where: dict[str, Any] | None = None,
                      sort: list[dict[str, Any]] | None = None, start: int = 0, limit: int = 50, max_bytes: int | None = None, max_cell_chars: int | None = None,
                      aggregate: dict[str, Any] | None = None, row_ids: list[int] | None = None, surface: str = "mcp") -> dict[str, Any]:
        rv = self._result_version(ws, rv_id)
        plan, compiled, manifest = self._compiled_for(ws, rv)
        step = step or plan.output
        if step not in compiled.relations:
            raise ServiceError("unknown_step", f"Step '{step}' is not part of this result", 404, {"steps": list(compiled.relations.keys())})
        rel = compiled.relations[step]
        page_max_rows = settings.web_page_max_rows if surface == "web" else settings.mcp_page_max_rows
        page_max_bytes = settings.web_page_max_bytes if surface == "web" else settings.mcp_page_max_bytes
        cell_chars = min(max_cell_chars or (settings.web_cell_max_chars if surface == "web" else settings.mcp_cell_max_chars), 4000)
        limit = max(1, min(limit, page_max_rows))
        max_bytes = min(max_bytes or page_max_bytes, page_max_bytes)
        base_sql = compiled.sql_for(step)
        warnings: list[str] = []
        if rel.pending_statuses:
            warnings.append("provisional: semantic inputs are still pending for some rows; counts and order can change")

        if aggregate:
            return self._adhoc_aggregate(rel, base_sql, compiled, aggregate, limit, max_bytes, cell_chars, rv, step, warnings)

        where_sql = None
        referenced_statuses: set[str] = set()
        if where:
            try:
                expr = _parse_expr(where)
                ec = ExprCompiler(rel, "where")
                where_sql = ec.compile(expr)
                referenced_statuses = {rel.semantic_statuses[c] for c in ec.referenced if c in rel.semantic_statuses}
            except PlanError as e:
                raise ServiceError("invalid_query", e.message, 422, {"issues": [e.to_issue()]}) from e
        order_sql = None
        if sort:
            keys = []
            for i, k in enumerate(sort):
                try:
                    sk = SortKey.model_validate(k)
                except ValidationError as e:
                    raise ServiceError("invalid_query", "Invalid sort key", 422, {"path": f"sort[{i}]", "error": str(e)[:200]}) from e
                if not rel.has(sk.column):
                    raise ServiceError("invalid_query", f"Sort column '{sk.column}' does not exist", 422, {"issues": [{"path": f"sort[{i}].column", "code": "unknown_column", "message": "unknown column", "fix": f"Use one of: {', '.join(rel.column_names()[:30])}"}]})
                keys.append(f"{q(sk.column)} {'DESC' if sk.direction == 'desc' else 'ASC'} NULLS LAST")
            keys.append(f"{q(ROW_ID)} ASC")
            order_sql = ", ".join(keys)
        proj = columns or [c.name for c in rel.columns]
        for c in proj:
            if not rel.has(c):
                raise ServiceError("invalid_query", f"Column '{c}' does not exist in step '{step}'", 422, {"issues": [{"path": "columns", "code": "unknown_column", "message": c, "fix": f"Use one of: {', '.join(rel.column_names()[:30])}"}]})
        if ROW_ID not in proj:
            proj = [ROW_ID] + proj

        query_hash = sha256(dumps({"step": step, "where": where, "sort": sort, "rv": rv_id, "rev": rv["revision"]}))
        with connect(compiled) as con:
            if row_ids is not None:
                ids = [int(r) for r in row_ids][:page_max_rows]
                total = None
                count_status = "n/a"
                denominator = None
            else:
                cached = self.views.get(query_hash)
                if cached is None:
                    sql = f"SELECT {q(ROW_ID)} FROM ({base_sql}) v"
                    if where_sql:
                        sql += f" WHERE COALESCE({where_sql}, FALSE)"
                    if order_sql:
                        sql += f" ORDER BY {order_sql}"
                    cached = [int(r[0]) for r in con.execute(sql).fetchall()]
                    self.views.put(query_hash, cached)
                total = len(cached)
                ids = cached[start : start + limit]
                count_status = "partial" if rel.pending_statuses else "complete"
                denominator = None
                if step != "source" and rel.row_preserving:
                    inp = next((s.input for s in plan.steps if s.id == step), None)
                    if inp:
                        denominator = int(con.execute(f"SELECT count(*) FROM ({compiled.sql_for(inp)}) d").fetchone()[0])
            rows_out: list[dict[str, Any]] = []
            has_more = False
            if ids:
                con.execute("CREATE TEMP TABLE want (row_id BIGINT, ord INTEGER)")
                con.executemany("INSERT INTO want VALUES (?,?)", [(r, i) for i, r in enumerate(ids)])
                cols_sql = ", ".join(f"v.{q(c)}" for c in proj)
                res = con.execute(f"SELECT {cols_sql} FROM ({base_sql}) v JOIN want w ON w.row_id = v.{q(ROW_ID)} ORDER BY w.ord")
                fetched = res.fetchall()
                rows_out, has_more_bytes = self._bound_rows(proj, fetched, max_bytes, cell_chars)
                has_more = has_more_bytes and len(rows_out) < len(fetched)
        next_start = start + len(rows_out)
        if total is not None:
            has_more = has_more or next_start < total
        review = compiled.review_relations
        return {
            "result_version_id": rv_id, "revision": rv["revision"], "step": step, "query_hash": query_hash,
            "columns": [{"name": c.name, "type": c.type, "role": c.role} for c in rel.columns if c.name in proj],
            "rows": rows_out, "start": start, "next_start": next_start if has_more else None, "has_more": has_more,
            "total_count": total, "count_status": count_status, "denominator": denominator,
            "provisional": bool(rel.pending_statuses), "unknown_referenced": sorted(referenced_statuses),
            "review_views": review, "warnings": warnings, "truncated_cells": any("_truncated" in r for r in rows_out),
        }

    def _adhoc_aggregate(self, rel: Relation, base_sql: str, compiled: Compiled, aggregate: dict[str, Any], limit: int, max_bytes: int, cell_chars: int, rv: dict[str, Any], step: str, warnings: list[str]) -> dict[str, Any]:
        try:
            agg = AggregateStep.model_validate({"id": "adhoc", "op": "aggregate", "input": step, **aggregate})
        except ValidationError as e:
            raise ServiceError("invalid_query", "Invalid aggregate", 422, {"error": str(e)[:300]}) from e
        for g in agg.group_by:
            if not rel.has(g):
                raise ServiceError("invalid_query", f"Group column '{g}' does not exist", 422)
        selects = [q(g) for g in agg.group_by]
        for m in agg.metrics:
            if m.fn == "count" and m.column is None:
                selects.append(f"count(*) AS {q(m.name)}")
            else:
                if not m.column or not rel.has(m.column):
                    raise ServiceError("invalid_query", f"Metric column for '{m.name}' does not exist", 422)
                expr = f"count(DISTINCT {q(m.column)})" if m.fn == "count_distinct" else f"{m.fn}({q(m.column)})"
                selects.append(f"{expr} AS {q(m.name)}")
        group = f" GROUP BY {', '.join(q(g) for g in agg.group_by)}" if agg.group_by else ""
        order = f" ORDER BY {q(agg.metrics[0].name)} DESC" if agg.metrics else ""
        with connect(compiled) as con:
            res = con.execute(f"SELECT {', '.join(selects)} FROM ({base_sql}) v{group}{order} LIMIT {int(limit) + 1}")
            cols = [d[0] for d in res.description]
            rows = res.fetchall()
            denominator = int(con.execute(f"SELECT count(*) FROM ({base_sql}) v").fetchone()[0])
        bounded, _ = self._bound_rows(cols, rows[:limit], max_bytes, cell_chars)
        return {"result_version_id": rv["id"], "revision": rv["revision"], "step": step, "aggregate": True, "columns": [{"name": c} for c in cols], "rows": bounded,
                "groups_truncated": len(rows) > limit, "denominator": denominator, "entity": f"rows of step '{step}'",
                "provisional": bool(rel.pending_statuses), "warnings": warnings + ["exact arithmetic over model-assigned groups; denominator is the row count of the step"]}

    def results_cell(self, ws: Workspace, rv_id: str, step: str | None, row_id: int, column: str) -> dict[str, Any]:
        rv = self._result_version(ws, rv_id)
        plan, compiled, _ = self._compiled_for(ws, rv)
        step = step or plan.output
        rel = compiled.relations.get(step)
        if rel is None or not rel.has(column):
            raise ServiceError("not_found", "Unknown step or column", 404)
        with connect(compiled) as con:
            row = con.execute(f"SELECT {q(column)} FROM ({compiled.sql_for(step)}) v WHERE {q(ROW_ID)} = {int(row_id)}").fetchone()
        return {"row_id": row_id, "column": column, "value": _jsonable(row[0]) if row else None, "found": row is not None}

    def results_vectors(self, ws: Workspace, rv_id: str, step: str | None, columns: list[str]) -> dict[str, Any]:
        """Complete numeric/label vectors for local (worker) filtering. Bounded to the result's row count."""
        rv = self._result_version(ws, rv_id)
        plan, compiled, _ = self._compiled_for(ws, rv)
        step = step or plan.output
        rel = compiled.relations[step]
        for c in columns:
            if not rel.has(c):
                raise ServiceError("invalid_query", f"Column '{c}' does not exist", 422)
        with connect(compiled) as con:
            sel = ", ".join([q(ROW_ID)] + [q(c) for c in columns])
            tbl = con.execute(f"SELECT {sel} FROM ({compiled.sql_for(step)}) v ORDER BY {q(ROW_ID)}").to_arrow_table()
        out: dict[str, Any] = {"row_ids": tbl.column(ROW_ID).to_pylist(), "columns": {}, "revision": rv["revision"], "complete": not rel.pending_statuses}
        for c in columns:
            col = tbl.column(c)
            t = next(cc.type for cc in rel.columns if cc.name == c)
            if pa.types.is_floating(col.type) or pa.types.is_integer(col.type) or pa.types.is_boolean(col.type):
                out["columns"][c] = {"kind": "number", "values": [None if v is None else float(v) for v in col.to_pylist()]}
            else:
                vals = col.to_pylist()
                labels = sorted({v for v in vals if v is not None}, key=str)
                index = {v: i for i, v in enumerate(labels)}
                out["columns"][c] = {"kind": "label", "labels": [str(label) for label in labels], "values": [None if v is None else index[v] for v in vals]}
            out["columns"][c]["type"] = t
        return out

    def results_patch(self, ws: Workspace, rv_id: str, overrides: list[dict[str, Any]], note: str | None = None, author: str | None = None) -> dict[str, Any]:
        """Manual corrections: explicit override records in a new result version; the raw model output is untouched."""
        if not overrides or len(overrides) > 1000:
            raise ServiceError("invalid_patch", "Provide between 1 and 1000 overrides", 422)
        rv = self._result_version(ws, rv_id)
        plan, compiled, manifest = self._compiled_for(ws, rv)
        patchable: set[str] = set()
        for s in plan.steps:
            if isinstance(s, SemanticAnnotateStep):
                for qn in s.questions:
                    patchable.add(f"{qn.name}.value")
        for i, o in enumerate(overrides):
            if o.get("column") not in patchable:
                raise ServiceError("invalid_patch", f"overrides[{i}].column must be a semantic value column ({', '.join(sorted(patchable))})", 422, {"path": f"overrides[{i}].column"})
        new_id_ = new_id("rv")
        t = now()
        new_manifest = dict(manifest)
        new_manifest["derived_from"] = manifest.get("derived_from") or rv_id
        new_manifest["note"] = note or f"{len(overrides)} manual correction(s)"
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO result_versions(id,workspace_id,job_id,dataset_version_id,plan_id,plan_hash,parent_result_version_id,revision,status,manifest_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id_, ws.id, rv["job_id"], rv["dataset_version_id"], rv["plan_id"], rv["plan_hash"], rv_id, int(rv["revision"]) + 1, rv["status"], dumps(new_manifest), t, t),
            )
            # inherit parent's overrides, then apply new ones (later wins per cell)
            conn.execute("INSERT INTO overrides(id,workspace_id,result_version_id,row_id,column_name,value_json,reason,author,created_at) "
                         "SELECT lower(hex(randomblob(8))), workspace_id, ?, row_id, column_name, value_json, reason, author, created_at FROM overrides WHERE result_version_id=?", (new_id_, rv_id))
            for o in overrides:
                conn.execute("DELETE FROM overrides WHERE result_version_id=? AND row_id=? AND column_name=?", (new_id_, int(o["row_id"]), o["column"]))
                conn.execute("INSERT INTO overrides(id,workspace_id,result_version_id,row_id,column_name,value_json,reason,author,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (new_id("ov"), ws.id, new_id_, int(o["row_id"]), o["column"], dumps(o.get("value")), o.get("reason"), author, t))
        return {"result_version_id": new_id_, "parent_result_version_id": rv_id, "overrides_applied": len(overrides)}

    def results_overrides(self, ws: Workspace, rv_id: str) -> list[dict[str, Any]]:
        self._result_version(ws, rv_id)
        return [{"row_id": r["row_id"], "column": r["column_name"], "value": loads(r["value_json"]), "reason": r["reason"], "created_at": r["created_at"]} for r in
                self.db.query("SELECT * FROM overrides WHERE result_version_id=? ORDER BY created_at", (rv_id,))]

    # ------------------------------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------------------------------
    def results_export(self, ws: Workspace, rv_id: str, fmt: str = "csv", step: str | None = None, raw: bool = False, columns: list[str] | None = None) -> dict[str, Any]:
        if fmt not in ("csv", "parquet"):
            raise ServiceError("invalid_format", "format must be csv or parquet", 422)
        rv = self._result_version(ws, rv_id)
        plan, compiled, manifest = self._compiled_for(ws, rv)
        step = step or plan.output
        if step not in compiled.relations:
            raise ServiceError("unknown_step", f"Step '{step}' is not part of this result", 404)
        rel = compiled.relations[step]
        cols = columns or rel.column_names()
        for c in cols:
            if not rel.has(c):
                raise ServiceError("invalid_query", f"Column '{c}' does not exist", 422)
        export_id = new_id("exp")
        path = export_path(export_id, fmt)
        manifest_path = export_path(export_id, "manifest.json")
        with connect(compiled) as con:
            if fmt == "csv" and not raw:
                selects = []
                for c in cols:
                    t = next(cc.type for cc in rel.columns if cc.name == c)
                    if t in ("text", "derived", "semantic_value") or t not in ("integer", "double", "boolean", "date", "timestamp"):
                        selects.append(f"CASE WHEN regexp_matches(CAST({q(c)} AS VARCHAR), '^[=+\\-@\\t\\r]') THEN '''' || CAST({q(c)} AS VARCHAR) ELSE CAST({q(c)} AS VARCHAR) END AS {q(c)}")
                    else:
                        selects.append(q(c))
                sql = f"SELECT {', '.join(selects)} FROM ({compiled.sql_for(step)}) v"
            else:
                sql = f"SELECT {', '.join(q(c) for c in cols)} FROM ({compiled.sql_for(step)}) v"
            fmt_sql = "(FORMAT CSV, HEADER TRUE)" if fmt == "csv" else "(FORMAT PARQUET, COMPRESSION ZSTD)"
            con.execute(f"COPY ({sql}) TO {lit(str(path))} {fmt_sql}")
            row_count = int(con.execute(f"SELECT count(*) FROM ({compiled.sql_for(step)}) v").fetchone()[0])
            pending = {}
            for st in rel.pending_statuses:
                pending[st] = int(con.execute(f"SELECT count(*) FROM ({compiled.sql_for(step)}) v WHERE {q(st)} = 'pending'").fetchone()[0])
        job = self.db.one("SELECT state, terminal_reason, usage_json, progress_json FROM jobs WHERE id=?", (rv["job_id"],)) if rv["job_id"] else None
        overrides = self.db.one("SELECT count(*) c FROM overrides WHERE result_version_id=?", (rv_id,))["c"]
        exp_manifest = {
            "export_id": export_id, "format": fmt, "formula_escaped": fmt == "csv" and not raw, "step": step, "columns": cols, "row_count": row_count,
            "result_version_id": rv_id, "revision": rv["revision"], "dataset_version_id": rv["dataset_version_id"], "plan_hash": rv["plan_hash"],
            "plan_version": plan.plan_version, "model": manifest.get("model"), "completed_scope": manifest.get("steps"), "complete": not rel.pending_statuses and manifest.get("status") in ("succeeded", "complete"),
            "job_state": job["state"] if job else None, "terminal_reason": job["terminal_reason"] if job else None, "pending_by_status_column": pending,
            "exclusions": {"review_views": compiled.review_relations, "note": "rows with uncertain/missing/failed semantic answers are excluded from filtered steps and kept in the review view"},
            "approximations": [s.model_dump() | {"note": "candidate-limited matching is approximate"} for s in plan.steps if isinstance(s, SemanticMatchStep)],
            "overrides": overrides, "usage": loads(job["usage_json"]) if job else None, "created_at": now(),
        }
        manifest_path.write_text(json.dumps(exp_manifest, indent=2, default=str), encoding="utf-8")
        size = path.stat().st_size
        t = now()
        self.db.execute("INSERT INTO exports(id,workspace_id,result_version_id,format,path,manifest_path,size,row_count,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (export_id, ws.id, rv_id, fmt, str(path), str(manifest_path), size, row_count, "ready", t, t + settings.retention_days * 86400))
        return {"export_id": export_id, "format": fmt, "size": size, "row_count": row_count, "status": "ready", "download_url": f"/api/exports/{export_id}/download",
                "manifest_url": f"/api/exports/{export_id}/manifest", "complete": exp_manifest["complete"], "formula_escaped": exp_manifest["formula_escaped"]}

    def export_file(self, ws: Workspace, export_id: str, manifest: bool = False) -> tuple[Path, str]:
        row = self.db.one("SELECT * FROM exports WHERE id=? AND workspace_id=?", (export_id, ws.id))
        if row is None or row["state"] != "ready":
            raise ServiceError("not_found", "Export not found or revoked", 404)
        if manifest:
            return Path(row["manifest_path"]), "application/json"
        return Path(row["path"]), "text/csv" if row["format"] == "csv" else "application/octet-stream"


def _parse_expr(obj: dict[str, Any]) -> Expr:
    from pydantic import TypeAdapter

    try:
        return TypeAdapter(Expr).validate_python(obj)
    except ValidationError as e:
        raise PlanError("where", "invalid_expression", str(e.errors()[0]["msg"]) if e.errors() else "invalid expression") from e
