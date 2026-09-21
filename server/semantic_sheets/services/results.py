"""Serve result snapshots: bounded pages, aggregates, vectors, cell detail, corrections, exports."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from typing import Any

import duckdb
from sqlalchemy import select

from ..config import settings
from ..db import Export, Job, Override, PlanVersion, ResultVersion, canonical_json, session, sha256_text, utcnow
from ..engine.exact import build_sql, order_clause
from ..engine.executor import semantic_results_sql
from ..errors import NotFound, ValidationFailed
from ..ids import new_id
from ..plan.compile import ROW_ID, CompiledPlan, compile_plan, parse_plan
from ..plan.expr import compile_expr, quote_ident as q
from ..plan.schema import Filter
from ..storage import export_manifest_path, export_path, sql_str
from .catalog import WorkspaceCatalog, apply_overrides_sql, overrides_for


def get_result_version(workspace_id: str, rv_id: str) -> ResultVersion:
    with session() as s:
        rv = s.get(ResultVersion, rv_id)
        if rv is None or rv.workspace_id != workspace_id:
            raise NotFound("result version not found", code="result_not_found")
        return rv


def get_job(job_id: str) -> Job:
    with session() as s:
        return s.get(Job, job_id)


def compiled_for(rv: ResultVersion, catalog: WorkspaceCatalog) -> tuple[CompiledPlan, Job]:
    with session() as s:
        pv = s.get(PlanVersion, rv.plan_version_id)
        job = s.get(Job, rv.job_id)
    plan = parse_plan(pv.plan_json)
    plan.source.version_id = rv.dataset_version_id  # frozen input version
    return compile_plan(plan, catalog), job


class ResultRelation:
    """SQL for a result version's output (or any step), with result-level overrides applied."""

    def __init__(self, workspace_id: str, rv_id: str):
        self.workspace_id = workspace_id
        self.rv = get_result_version(workspace_id, rv_id)
        self.catalog = WorkspaceCatalog(workspace_id)
        self.compiled, self.job = compiled_for(self.rv, self.catalog)
        self.revision = self.job.result_revision or 0
        self.overrides = overrides_for("result", self.rv.id)

    def step_columns(self, step: str) -> dict[str, str]:
        if step == "source":
            return {ROW_ID: "integer", **self.compiled.source.columns}
        if step not in self.compiled.steps:
            raise ValidationFailed(f"unknown step {step!r}", code="unknown_step", path="scope.step")
        return self.compiled.steps[step].columns

    def sql(self, step: str | None = None, review_of: str | None = None) -> str:
        target = step or self.compiled.plan.output
        if review_of:
            st = next((s for s in self.compiled.plan.steps if s.id == review_of), None)
            if not isinstance(st, Filter):
                raise ValidationFailed("review_of must name a filter step", code="invalid_scope", path="scope.review_of")
            target = review_of
        rel = build_sql(self.compiled, target, self.catalog.source_sql, lambda sid: semantic_results_sql(self.job.id, sid),
                        review_filter=review_of)
        if self.overrides and target == self.compiled.plan.output:
            rel = apply_overrides_sql(rel, {c: t for c, t in self.step_columns(target).items() if c != ROW_ID}, self.overrides)
        return rel

    def default_order(self, step: str | None) -> list[tuple[str, str]]:
        target = step or self.compiled.plan.output
        if target == "source":
            return [(ROW_ID, "asc")]
        return self.compiled.steps[target].order_by

    def completion(self) -> dict[str, Any]:
        prog = (self.job.progress_json or {})
        steps = prog.get("steps", {})
        sem = [s.id for s in self.compiled.semantic_steps()]
        complete = self.job.state == "succeeded" or (bool(sem) and all(steps.get(s, {}).get("complete") for s in sem))
        if not sem:
            complete = True
        return {"job_state": self.job.state, "complete": complete, "semantic_steps": sem,
                "steps": {s: steps.get(s, {"complete": False, "pending": None}) for s in sem},
                "source_rows": prog.get("source_rows"), "succeeded": prog.get("succeeded"),
                "failed": prog.get("failed"), "skipped": prog.get("skipped"), "pending": prog.get("pending"),
                "uncertain": prog.get("uncertain"), "revision": self.revision}


# ---- views ------------------------------------------------------------------------------------------------

_views: dict[str, dict[str, Any]] = {}
_views_lock = threading.Lock()


def _view_key(rv_id: str, revision: int, spec: dict[str, Any]) -> str:
    return sha256_text(canonical_json({"rv": rv_id, "rev": revision, "spec": spec}))[:24]


def pack_rows(rows: list[tuple], columns: list[str], max_bytes: int, display_chars: int) -> tuple[list[list[Any]], int, int]:
    """Bound wire bytes: shorten display text with an explicit marker and stop before the byte ceiling."""
    out: list[list[Any]] = []
    used = 2
    truncated = 0
    for r in rows:
        vals: list[Any] = []
        for v in r:
            if isinstance(v, str) and len(v) > display_chars:
                vals.append(v[:display_chars] + "…")
                truncated += 1
            elif isinstance(v, (str, int, float, bool)) or v is None:
                vals.append(v)
            elif isinstance(v, (dt.date, dt.datetime)):
                vals.append(v.isoformat())
            else:
                vals.append(str(v))
        s = len(json.dumps(vals, ensure_ascii=False, default=str))
        if used + s > max_bytes and out:
            break
        used += s
        out.append(vals)
    return out, truncated, used


def query(workspace_id: str, rv_id: str, spec: dict[str, Any], *, max_rows: int, max_bytes: int) -> dict[str, Any]:
    cfg = settings()
    rel = ResultRelation(workspace_id, rv_id)
    scope = spec.get("scope") or "output"
    step = None
    review_of = None
    if isinstance(scope, dict):
        step = scope.get("step")
        review_of = scope.get("review_of")
    elif scope != "output":
        raise ValidationFailed("scope must be 'output' or an object", code="invalid_scope", path="scope")
    cols = rel.step_columns(review_of or step or rel.compiled.plan.output)
    base_sql = rel.sql(step, review_of)
    where_sql = None
    if spec.get("where") is not None:
        where_sql, _ = compile_expr(spec["where"], cols, path="where")
    if spec.get("row_ids") is not None:
        ids = spec["row_ids"]
        if not isinstance(ids, list) or len(ids) > 500 or not all(isinstance(i, int) for i in ids):
            raise ValidationFailed("row_ids must be up to 500 integers", code="invalid_row_ids", path="row_ids")
        cond = f"{q(ROW_ID)} IN ({', '.join(str(i) for i in ids)})" if ids else "FALSE"
        where_sql = f"({where_sql}) AND {cond}" if where_sql else cond
    order = [(o["column"], o.get("direction", "asc")) for o in (spec.get("order_by") or [])]
    for c, d in order:
        if c not in cols:
            raise ValidationFailed(f"unknown order column {c!r}", code="unknown_column", path="order_by")
        if d not in ("asc", "desc"):
            raise ValidationFailed("direction must be asc or desc", code="invalid_order", path="order_by")
    if not order:
        order = rel.default_order(step)
    limit = min(int(spec.get("limit") or max_rows), max_rows)
    start = max(0, int(spec.get("start") or 0))
    max_bytes = min(int(spec.get("max_bytes") or max_bytes), max_bytes)
    completion = rel.completion()
    con = duckdb.connect()
    try:
        con.execute(f"CREATE VIEW rel AS {base_sql}")
        agg = spec.get("aggregate")
        if agg:
            return _aggregate(con, cols, agg, where_sql, limit, max_bytes, completion, rel)
        projection = spec.get("columns")
        if projection:
            for c in projection:
                if c not in cols:
                    raise ValidationFailed(f"unknown column {c!r}", code="unknown_column", path="columns")
            sel_cols = [ROW_ID] + [c for c in projection if c != ROW_ID] if ROW_ID in cols else list(projection)
        else:
            sel_cols = list(cols)
        where_clause = f" WHERE {where_sql}" if where_sql else ""
        view_spec = {"scope": scope, "where": spec.get("where"), "row_ids": spec.get("row_ids"), "order_by": order}
        view_id = _view_key(rel.rv.id, rel.revision, view_spec)
        with _views_lock:
            cached = _views.get(view_id)
        if cached is None:
            total = con.execute(f"SELECT count(*) FROM rel{where_clause}").fetchone()[0]
            cached = {"total_count": int(total), "revision": rel.revision, "created_at": utcnow().isoformat()}
            with _views_lock:
                if len(_views) > 2000:
                    _views.clear()
                _views[view_id] = cached
        sel = ", ".join(q(c) for c in sel_cols)
        rows = con.execute(f"SELECT {sel} FROM rel{where_clause} ORDER BY {order_clause(order, cols)} "
                           f"LIMIT {limit} OFFSET {start}").fetchall()
    finally:
        con.close()
    data, truncated, used = pack_rows(rows, sel_cols, max_bytes, cfg.cell_display_chars)
    total = cached["total_count"]
    provisional = not completion["complete"]
    return {
        "view_id": view_id, "view_revision": rel.revision, "result_version_id": rel.rv.id,
        "columns": [{"name": c, "type": cols[c]} for c in sel_cols],
        "rows": data, "row_ordinals": list(range(start, start + len(data))),
        "start": start, "returned": len(data), "next_start": start + len(data),
        "has_more": start + len(data) < total, "total_count": total,
        "count_status": "complete" if not provisional else "partial",
        "provisional": provisional, "completion": completion,
        "truncated_cells": truncated, "bytes": used,
        "denominator": {"source_rows": rel.compiled.source.row_count, "examined": completion.get("succeeded"),
                        "skipped": completion.get("skipped"), "failed": completion.get("failed")},
    }


def _aggregate(con: duckdb.DuckDBPyConnection, cols: dict[str, str], agg: dict[str, Any], where_sql: str | None,
               limit: int, max_bytes: int, completion: dict[str, Any], rel: ResultRelation) -> dict[str, Any]:
    group_by = agg.get("group_by") or []
    metrics = agg.get("metrics") or []
    if not isinstance(group_by, list) or not isinstance(metrics, list) or not metrics:
        raise ValidationFailed("aggregate needs metrics (and optional group_by)", code="invalid_aggregate", path="aggregate")
    for g in group_by:
        if g not in cols:
            raise ValidationFailed(f"unknown group column {g!r}", code="unknown_column", path="aggregate.group_by")
    sel = [q(g) for g in group_by]
    out_cols = [(g, cols[g]) for g in group_by]
    for i, m in enumerate(metrics):
        fn, col, name = m.get("fn"), m.get("column"), m.get("name") or f"{m.get('fn')}_{m.get('column') or 'rows'}"
        if fn not in ("count", "count_distinct", "sum", "avg", "min", "max"):
            raise ValidationFailed("fn must be count|count_distinct|sum|avg|min|max", code="invalid_metric", path=f"aggregate.metrics[{i}]")
        if fn != "count" and col not in cols:
            raise ValidationFailed(f"unknown metric column {col!r}", code="unknown_column", path=f"aggregate.metrics[{i}].column")
        if fn in ("sum", "avg") and cols[col] not in ("integer", "number"):
            raise ValidationFailed(f"{fn} needs a numeric column", code="invalid_metric", path=f"aggregate.metrics[{i}]")
        if fn == "count":
            sel.append(f"count({q(col)}) AS {q(name)}" if col else f"count(*) AS {q(name)}")
        elif fn == "count_distinct":
            sel.append(f"count(DISTINCT {q(col)}) AS {q(name)}")
        else:
            sel.append(f"{fn}({q(col)}) AS {q(name)}")
        out_cols.append((name, "integer" if fn in ("count", "count_distinct") else "number"))
    gb = f" GROUP BY {', '.join(q(g) for g in group_by)}" if group_by else ""
    wc = f" WHERE {where_sql}" if where_sql else ""
    ob = ", ".join(q(g) for g in group_by) if group_by else "1"
    rows = con.execute(f"SELECT {', '.join(sel)} FROM rel{wc}{gb} ORDER BY {ob} LIMIT {limit}").fetchall()
    denom = con.execute(f"SELECT count(*) FROM rel{wc}").fetchone()[0]
    data, truncated, used = pack_rows(rows, [c for c, _ in out_cols], max_bytes, settings().cell_display_chars)
    return {
        "result_version_id": rel.rv.id, "view_revision": rel.revision,
        "columns": [{"name": c, "type": t} for c, t in out_cols], "rows": data, "returned": len(data),
        "has_more": len(rows) >= limit, "denominator": {"rows_in_scope": int(denom), "source_rows": rel.compiled.source.row_count},
        "provisional": not completion["complete"], "count_status": "complete" if completion["complete"] else "partial",
        "completion": completion, "truncated_cells": truncated, "bytes": used,
        "note": "exact arithmetic over model-assigned groups; group labels are model predictions",
    }


def cell(workspace_id: str, rv_id: str, row_id: int, column: str, step: str | None = None) -> dict[str, Any]:
    rel = ResultRelation(workspace_id, rv_id)
    cols = rel.step_columns(step or rel.compiled.plan.output)
    if column not in cols:
        raise NotFound("unknown column", code="unknown_column")
    con = duckdb.connect()
    try:
        row = con.execute(f"SELECT {q(column)} FROM ({rel.sql(step)}) t WHERE {q(ROW_ID)} = {int(row_id)} LIMIT 1").fetchone()
    finally:
        con.close()
    if row is None:
        raise NotFound("row not found", code="row_not_found")
    v = row[0]
    if not (isinstance(v, (str, int, float, bool)) or v is None):
        v = str(v)
    return {"row_id": row_id, "column": column, "value": v, "type": cols[column]}


def row_detail(workspace_id: str, rv_id: str, row_id: int) -> dict[str, Any]:
    rel = ResultRelation(workspace_id, rv_id)
    cols = rel.step_columns(rel.compiled.plan.output)
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM ({rel.sql()}) t WHERE {q(ROW_ID)} = {int(row_id)} LIMIT 1")
        names = [d[0] for d in cur.description]
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        raise NotFound("row not found", code="row_not_found")
    values = {}
    for n, v in zip(names, row):
        if isinstance(v, (dt.date, dt.datetime)):
            v = v.isoformat()
        elif not (isinstance(v, (str, int, float, bool)) or v is None):
            v = str(v)
        if n.endswith(".raw") and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        values[n] = v
    ovs = [o for o in rel.overrides if o.row_id == row_id]
    return {"row_id": row_id, "values": values, "columns": cols,
            "overrides": [{"column": o.column, "value": json.loads(o.value_json), "provenance": o.provenance_json,
                           "created_at": o.created_at.isoformat()} for o in ovs]}


def vectors(workspace_id: str, rv_id: str, columns: list[str], max_rows: int = 300_000) -> dict[str, Any]:
    """Complete authorized score/label vectors for local threshold and sort edits."""
    rel = ResultRelation(workspace_id, rv_id)
    cols = rel.step_columns(rel.compiled.plan.output)
    for c in columns:
        if c not in cols:
            raise ValidationFailed(f"unknown column {c!r}", code="unknown_column", path="columns")
        if cols[c] == "json":
            raise ValidationFailed("raw json columns are not vectorised", code="invalid_column", path="columns")
    sel = ", ".join([q(ROW_ID)] + [q(c) for c in columns])
    con = duckdb.connect()
    try:
        total = con.execute(f"SELECT count(*) FROM ({rel.sql()}) t").fetchone()[0]
        if total > max_rows:
            raise ValidationFailed(f"{total} rows exceed the vector budget of {max_rows}", code="too_large")
        cur = con.execute(f"SELECT {sel} FROM ({rel.sql()}) t ORDER BY {q(ROW_ID)}")
        rows = cur.fetchall()
    finally:
        con.close()
    out: dict[str, list[Any]] = {c: [] for c in columns}
    row_ids: list[int] = []
    for r in rows:
        row_ids.append(int(r[0]))
        for i, c in enumerate(columns):
            v = r[i + 1]
            if isinstance(v, (dt.date, dt.datetime)):
                v = v.isoformat()
            out[c].append(v)
    return {"result_version_id": rel.rv.id, "revision": rel.revision, "row_ids": row_ids,
            "columns": {c: {"type": cols[c], "values": out[c]} for c in columns}, "completion": rel.completion()}


def columns_of(workspace_id: str, rv_id: str) -> dict[str, Any]:
    rel = ResultRelation(workspace_id, rv_id)
    return {"result_version_id": rel.rv.id, "revision": rel.revision, "output": rel.compiled.plan.output,
            "columns": [{"name": c, "type": t} for c, t in rel.step_columns(rel.compiled.plan.output).items()],
            "steps": [{"id": s, "op": rel.compiled.steps[s].op, "semantic": rel.compiled.steps[s].semantic,
                       "columns": list(rel.compiled.steps[s].columns)} for s in rel.compiled.order],
            "completion": rel.completion(), "job_id": rel.job.id, "job_state": rel.job.state,
            "plan_version_id": rel.rv.plan_version_id, "dataset_version_id": rel.rv.dataset_version_id}


def list_versions(workspace_id: str, job_id: str) -> list[dict[str, Any]]:
    with session() as s:
        rows = list(s.scalars(select(ResultVersion).where(ResultVersion.job_id == job_id, ResultVersion.workspace_id == workspace_id)
                              .order_by(ResultVersion.number)).all())
        out = []
        for rv in rows:
            n = s.scalar(select(Override.id).where(Override.target_kind == "result", Override.target_id == rv.id).limit(1))
            cnt = len(list(s.scalars(select(Override.id).where(Override.target_kind == "result", Override.target_id == rv.id)).all()))
            out.append({"result_version_id": rv.id, "number": rv.number, "parent_id": rv.parent_id, "reason": rv.reason,
                        "override_count": cnt, "created_at": rv.created_at.isoformat()})
        return out


def patch(workspace_id: str, rv_id: str, corrections: list[dict[str, Any]], provenance: dict[str, Any] | None) -> ResultVersion:
    """Correct result cells. Creates a new result version; the model output stays in the .raw column."""
    rel = ResultRelation(workspace_id, rv_id)
    cols = rel.step_columns(rel.compiled.plan.output)
    if not corrections or len(corrections) > 1000:
        raise ValidationFailed("provide 1..1000 corrections", code="invalid_patch", path="corrections")
    for i, c in enumerate(corrections):
        col = c.get("column")
        if col not in cols or col == ROW_ID:
            raise ValidationFailed(f"unknown column {col!r}", code="unknown_column", path=f"corrections[{i}].column")
        if col.endswith(".raw"):
            raise ValidationFailed("raw model output cannot be overridden", code="immutable_column", path=f"corrections[{i}].column")
        if not isinstance(c.get("row_id"), int):
            raise ValidationFailed("row_id must be an integer", code="invalid_row", path=f"corrections[{i}].row_id")
    with session() as s:
        latest = s.scalars(select(ResultVersion).where(ResultVersion.job_id == rel.rv.job_id)
                           .order_by(ResultVersion.number.desc())).first()
        new = ResultVersion(id=new_id("rv"), workspace_id=workspace_id, job_id=rel.rv.job_id, number=latest.number + 1,
                            parent_id=rel.rv.id, dataset_version_id=rel.rv.dataset_version_id,
                            plan_version_id=rel.rv.plan_version_id, reason="correction")
        s.add(new)
        for o in rel.overrides:
            s.add(Override(id=new_id("ov"), target_kind="result", target_id=new.id, row_id=o.row_id, column=o.column,
                           value_json=o.value_json, provenance_json=o.provenance_json, created_at=o.created_at))
        for c in corrections:
            s.add(Override(id=new_id("ov"), target_kind="result", target_id=new.id, row_id=int(c["row_id"]), column=c["column"],
                           value_json=json.dumps(c.get("value")),
                           provenance_json={**(provenance or {}), **(c.get("provenance") or {}), "kind": "manual_override",
                                            "overrides_model_output": c["column"].split(".")[0] in {st.id for st in rel.compiled.plan.steps} or "." in c["column"]}))
        s.commit()
        return new


# ---- export -------------------------------------------------------------------------------------------------

FORMULA_PREFIX = "^[=+\\-@\\t\\r]"


def export(workspace_id: str, rv_id: str, fmt: str, *, raw: bool = False, columns: list[str] | None = None,
           where: dict[str, Any] | None = None, order_by: list[dict[str, str]] | None = None, scope: dict[str, Any] | None = None) -> Export:
    if fmt not in ("csv", "parquet"):
        raise ValidationFailed("format must be csv or parquet", code="invalid_format", path="format")
    rel = ResultRelation(workspace_id, rv_id)
    step = (scope or {}).get("step")
    review_of = (scope or {}).get("review_of")
    cols = rel.step_columns(review_of or step or rel.compiled.plan.output)
    sel_cols = list(cols) if not columns else [c for c in columns if c in cols]
    if columns and len(sel_cols) != len(columns):
        raise ValidationFailed("unknown column in columns", code="unknown_column", path="columns")
    where_sql = compile_expr(where, cols, path="where")[0] if where else None
    order = [(o["column"], o.get("direction", "asc")) for o in (order_by or [])] or rel.default_order(step)
    exp = Export(id=new_id("exp"), workspace_id=workspace_id, result_version_id=rel.rv.id,
                 dataset_version_id=rel.rv.dataset_version_id, format=fmt, path="", status="running")
    path = export_path(exp.id, fmt)
    exp.path = str(path)
    with session() as s:
        s.add(exp)
        s.commit()
    if fmt == "csv" and not raw:
        sel = ", ".join(
            f"(CASE WHEN regexp_matches({q(c)}, '{FORMULA_PREFIX}') THEN '''' || {q(c)} ELSE {q(c)} END) AS {q(c)}"
            if cols[c] in ("text", "json") else q(c) for c in sel_cols)
    else:
        sel = ", ".join(q(c) for c in sel_cols)
    wc = f" WHERE {where_sql}" if where_sql else ""
    tmp = path.with_suffix(path.suffix + ".tmp")
    con = duckdb.connect()
    try:
        con.execute(f"CREATE VIEW rel AS {rel.sql(step, review_of)}")
        n = con.execute(f"SELECT count(*) FROM rel{wc}").fetchone()[0]
        opts = "FORMAT CSV, HEADER TRUE" if fmt == "csv" else "FORMAT PARQUET, COMPRESSION ZSTD"
        con.execute(f"COPY (SELECT {sel} FROM rel{wc} ORDER BY {order_clause(order, cols)}) TO {sql_str(tmp)} ({opts})")
    finally:
        con.close()
    os.replace(tmp, path)
    completion = rel.completion()
    manifest = {
        "export_id": exp.id, "format": fmt, "raw_csv": raw if fmt == "csv" else None,
        "formula_escaped": (fmt == "csv" and not raw),
        "result_version_id": rel.rv.id, "job_id": rel.job.id, "job_state": rel.job.state,
        "dataset_id": rel.compiled.source.dataset_id, "dataset_version_id": rel.rv.dataset_version_id,
        "plan_version_id": rel.rv.plan_version_id, "plan_hash": rel.compiled.plan_hash, "model": rel.job.model,
        "scope": {"step": step or rel.compiled.plan.output, "review_of": review_of},
        "columns": sel_cols, "row_count": int(n), "filter": where, "order_by": [{"column": c, "direction": d} for c, d in order],
        "completion": completion, "complete": completion["complete"] and rel.job.state == "succeeded",
        "exclusions": {"failed_rows": completion.get("failed"), "skipped_rows": completion.get("skipped"),
                       "pending_rows": completion.get("pending")},
        "approximations": [f"{st.id}: candidate-limited semantic match (top {st.candidates_per_row} lexical candidates)"
                           for st in rel.compiled.plan.steps if st.op == "semantic_match"] +
                          [f"{st.id}: score-based ranking, not pairwise top-k" for st in rel.compiled.plan.steps if st.op == "sort"],
        "overrides": len(rel.overrides), "created_at": utcnow().isoformat(),
    }
    export_manifest_path(exp.id).write_text(json.dumps(manifest, indent=2))
    with session() as s:
        e = s.get(Export, exp.id)
        e.bytes = path.stat().st_size
        e.row_count = int(n)
        e.manifest_json = manifest
        e.status = "ready"
        s.commit()
        return e


def get_export(workspace_id: str, export_id: str) -> Export:
    with session() as s:
        e = s.get(Export, export_id)
        if e is None or e.workspace_id != workspace_id:
            raise NotFound("export not found", code="export_not_found")
        return e


def describe_export(e: Export) -> dict[str, Any]:
    return {"export_id": e.id, "status": e.status, "format": e.format, "bytes": e.bytes, "row_count": e.row_count,
            "manifest": e.manifest_json, "download_path": f"/api/v1/exports/{e.id}/download",
            "download_url": f"{settings().public_base_url}/api/v1/exports/{e.id}/download", "created_at": e.created_at.isoformat()}
