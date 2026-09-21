"""Compile the exact part of a plan into DuckDB SQL over Parquet.

Semantic steps are represented by their committed result files (joined by _row_id); when nothing has
been committed yet, the step's columns are NULL with status 'pending'.
"""

from __future__ import annotations

from typing import Callable

from ..plan.compile import ROW_ID, CompiledPlan, StepInfo
from ..plan.expr import compile_expr, quote_ident as q
from ..plan.schema import (
    Aggregate,
    Dedupe,
    Derive,
    Filter,
    Join,
    Limit,
    Project,
    SemanticAnnotate,
    SemanticMatch,
    Sort,
    match_columns,
    question_columns,
)

SourceSql = Callable[[str, str | None], str]  # (dataset_id, version_id) -> relation SQL
SemanticSql = Callable[[str], str | None]  # step_id -> relation SQL of committed results, or None


def order_clause(order_by: list[tuple[str, str]], columns: dict[str, str]) -> str:
    parts = [f"{q(c)} {d.upper()} NULLS LAST" for c, d in order_by if c in columns]
    if ROW_ID in columns and ROW_ID not in [c for c, _ in order_by]:
        parts.append(f"{q(ROW_ID)} ASC")
    return ", ".join(parts) if parts else "1"


def build_sql(compiled: CompiledPlan, output: str, source_sql: SourceSql, semantic_sql: SemanticSql,
              *, review_filter: str | None = None) -> str:
    """Return a SELECT for the given step id (or 'source').

    review_filter: id of a filter step whose *unknown* rows should be returned instead of its matches.
    """
    plan = compiled.plan
    ctes: list[str] = [f"s_source AS (SELECT * FROM ({source_sql(plan.source.dataset_id, plan.source.version_id)}) src)"]
    by_id = {s.id: s for s in plan.steps}
    for sid in compiled.ancestors(output) if output != "source" else []:
        step = by_id[sid]
        info = compiled.steps[sid]
        inp = "s_source" if step.input == "source" else f"s_{step.input}"
        inp_info = compiled.steps.get(step.input) or _source_info(compiled)
        ctes.append(f"s_{sid} AS ({step_sql(step, info, inp, inp_info, compiled, source_sql, semantic_sql, review_filter == sid)})")
    final = "s_source" if output == "source" else f"s_{output}"
    return "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM {final}"


def _source_info(compiled: CompiledPlan) -> StepInfo:
    cols = {ROW_ID: "integer", **compiled.source.columns}
    return StepInfo("source", "source", cols, compiled.source.row_count, True, order_by=[(ROW_ID, "asc")])


def step_sql(step, info: StepInfo, inp: str, inp_info: StepInfo, compiled: CompiledPlan, source_sql: SourceSql,
             semantic_sql: SemanticSql, review: bool) -> str:
    if isinstance(step, SemanticAnnotate):
        res = semantic_sql(step.id)
        outs = [c for qn in step.questions for c in question_columns(qn)]
        if res is None:
            nulls = ", ".join(_null_col(name, t, "pending") for name, t in outs)
            return f"SELECT {inp}.*, {nulls} FROM {inp}"
        sel = ", ".join(
            f"COALESCE(r.{q(name)}, 'pending') AS {q(name)}" if name.endswith(".status") else f"r.{q(name)} AS {q(name)}"
            for name, _ in outs)
        return f"SELECT {inp}.*, {sel} FROM {inp} LEFT JOIN ({res}) r ON r.{q(ROW_ID)} = {inp}.{q(ROW_ID)}"

    if isinstance(step, Filter):
        sql, _ = compile_expr(step.where, inp_info.columns)
        if review:
            return f"SELECT * FROM {inp} WHERE ({sql}) IS NULL"
        if step.unknown_policy == "include":
            return f"SELECT * FROM {inp} WHERE ({sql}) IS NOT FALSE"
        return f"SELECT * FROM {inp} WHERE ({sql}) IS TRUE"

    if isinstance(step, Sort):
        return f"SELECT * FROM {inp} ORDER BY {order_clause(info.order_by, info.columns)}"

    if isinstance(step, Project):
        return f"SELECT {', '.join(q(c) for c in info.columns)} FROM {inp}"

    if isinstance(step, Derive):
        exprs = []
        cols = dict(inp_info.columns)
        for d in step.columns:
            sql, _ = compile_expr(d.expr, cols)
            exprs.append(f"{sql} AS {q(d.name)}")
            cols[d.name] = info.columns[d.name]
        return f"SELECT {inp}.*, {', '.join(exprs)} FROM {inp}"

    if isinstance(step, Aggregate):
        groups = [q(g) for g in step.group_by]
        metrics = []
        for m in step.metrics:
            col = q(m.column) if m.column else None
            if m.fn == "count":
                metrics.append(f"count({col}) AS {q(m.name)}" if col else f"count(*) AS {q(m.name)}")
            elif m.fn == "count_distinct":
                metrics.append(f"count(DISTINCT {col}) AS {q(m.name)}")
            else:
                metrics.append(f"{m.fn}({col}) AS {q(m.name)}")
        ob = ", ".join(groups) if groups else "1"
        rn = f"(row_number() OVER (ORDER BY {ob})) - 1 AS {q(ROW_ID)}"
        parts = [rn] + groups + metrics
        gb = f" GROUP BY {', '.join(groups)}" if groups else ""
        return f"SELECT {', '.join(parts)} FROM {inp}{gb}"

    if isinstance(step, Join):
        right = source_sql(step.right.dataset_id, step.right.version_id)
        rcols = step.right_columns if step.right_columns is not None else [c for c in info.right.columns]
        rsel = ", ".join(f"r.{q(c)} AS {q(step.prefix + c)}" for c in rcols)
        on = " AND ".join(f"l.{q(k.left)} = r.{q(k.right)}" for k in step.on)
        how = "INNER JOIN" if step.how == "inner" else "LEFT JOIN"
        lcols = ", ".join(f"l.{q(c)}" for c in inp_info.columns if c != ROW_ID)
        return (f"SELECT (row_number() OVER (ORDER BY l.{q(ROW_ID)}, r.{q(ROW_ID)})) - 1 AS {q(ROW_ID)}, {lcols}, "
                f"{rsel}, l.{q(ROW_ID)} AS _left_row_id FROM {inp} l {how} ({right}) r ON {on}")

    if isinstance(step, Dedupe):
        keys = ", ".join(q(k) for k in step.keys)
        return f"SELECT * FROM {inp} QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY {q(ROW_ID)}) = 1"

    if isinstance(step, Limit):
        return f"SELECT * FROM {inp} ORDER BY {order_clause(inp_info.order_by, inp_info.columns)} LIMIT {step.n}"

    if isinstance(step, SemanticMatch):
        right = source_sql(step.right.dataset_id, step.right.version_id)
        res = semantic_sql(step.id)
        show = step.show_right_columns or step.right_columns
        base_cols = [c for c, _ in match_columns(step, info.right.columns) if not c.startswith("match.") or
                     c in ("match.right_row_id", "match.p", "match.status", "match.candidate_count",
                           "match.candidates", "match.raw")]
        if res is None:
            nulls = ", ".join(_null_col(name, t, "pending") for name, t in match_columns(step, info.right.columns))
            return f"SELECT {inp}.*, {nulls}, {inp}.{q(ROW_ID)} AS _left_row_id FROM {inp}"
        msel = ", ".join(
            f"COALESCE(m.{q(c)}, 'pending') AS {q(c)}" if c == "match.status" else f"m.{q(c)} AS {q(c)}"
            for c in base_cols)
        rsel = ", ".join(f"r.{q(c)} AS {q('match.' + c)}" for c in show)
        lcols = ", ".join(f"l.{q(c)}" for c in inp_info.columns if c != ROW_ID)
        if step.mode == "all":
            rid = f"(row_number() OVER (ORDER BY l.{q(ROW_ID)}, m.{q('match.right_row_id')})) - 1 AS {q(ROW_ID)}"
        else:
            rid = f"l.{q(ROW_ID)} AS {q(ROW_ID)}"
        return (f"SELECT {rid}, {lcols}, {msel}, {rsel}, l.{q(ROW_ID)} AS _left_row_id FROM {inp} l "
                f"LEFT JOIN ({res}) m ON m.{q(ROW_ID)} = l.{q(ROW_ID)} "
                f"LEFT JOIN ({right}) r ON r.{q(ROW_ID)} = m.{q('match.right_row_id')}")

    raise ValueError(f"unsupported step {step.op}")  # pragma: no cover


DUCK_TYPES = {"text": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE",
              "timestamp": "TIMESTAMP", "json": "VARCHAR"}


def _null_col(name: str, t: str, status: str) -> str:
    if name.endswith(".status"):
        return f"'{status}' AS {q(name)}"
    return f"CAST(NULL AS {DUCK_TYPES.get(t, 'VARCHAR')}) AS {q(name)}"
