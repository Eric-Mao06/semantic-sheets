"""Exact engine: compiles the typed plan into DuckDB SQL over Parquet snapshots.

Generated (semantic) columns live in separate Parquet files keyed by `_row_id`; the compiler LEFT JOINs them
onto the base table so the upload is never copied. Rows without a committed prediction read as status
'pending'. Overrides are applied on top with their own status so the raw model output is never altered."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from ..models import (
    AggregateStep,
    Call,
    ColumnRef,
    ComputeStep,
    DistinctStep,
    Expr,
    FilterStep,
    JoinStep,
    LimitStep,
    Literal_,
    Plan,
    ProjectStep,
    SemanticAnnotateStep,
    SemanticMatchStep,
    SortStep,
    SourceRef,
)

ROW_ID = "_row_id"
GOOD_STATUSES = ("ok", "override")


class PlanError(Exception):
    def __init__(self, path: str, code: str, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.code = code
        self.message = message
        self.fix = fix

    def to_issue(self) -> dict[str, Any]:
        return {"path": self.path, "code": self.code, "message": self.message, "fix": self.fix}


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "(" + ", ".join(lit(v) for v in value) + ")"
    return "'" + str(value).replace("'", "''") + "'"


def path_lit(p: Path | str) -> str:
    return lit(str(p))


@dataclass
class Column:
    name: str
    type: str
    role: str = "source"  # source | row_id | semantic | derived | right | metric | group


@dataclass
class Relation:
    name: str
    columns: list[Column]
    row_preserving: bool = True
    semantic_statuses: dict[str, str] = field(default_factory=dict)  # output column -> status column
    pending_statuses: list[str] = field(default_factory=list)  # status columns that may be 'pending'

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def has(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)


@dataclass
class CompileContext:
    """Where the compiler finds data for each reference."""

    base_parquet: Path
    base_schema: list[dict[str, Any]]
    derived: dict[str, Path | None] = field(default_factory=dict)  # step id -> parquet glob (None = nothing committed yet)
    datasets: dict[str, tuple[Path, list[dict[str, Any]]]] = field(default_factory=dict)  # dataset_id -> (parquet, schema)
    overrides: dict[str, dict[str, list[tuple[int, Any]]]] = field(default_factory=dict)  # step id -> column -> [(row_id, value)]
    step_complete: dict[str, bool] = field(default_factory=dict)


@dataclass
class Compiled:
    ctes: list[tuple[str, str]]
    relations: dict[str, Relation]
    review_relations: dict[str, str]  # filter step id -> cte name of its review view
    output: str
    registrations: dict[str, list[tuple[int, Any]]] = field(default_factory=dict)  # table name -> rows to register

    def sql_for(self, step: str) -> str:
        return "WITH " + ",\n".join(f"{q(n)} AS ({s})" for n, s in self.ctes) + f"\nSELECT * FROM {q(step)}"

    def provisional(self, step: str) -> bool:
        """True while a semantic stage feeding this step has rows without a committed answer."""
        return bool(self.relations[step].pending_statuses)

# ------------------------------------------------------------------------------------------------
# Expressions
# ------------------------------------------------------------------------------------------------

_BINARY = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "add": "+", "sub": "-", "mul": "*", "div": "/"}


class ExprCompiler:
    def __init__(self, rel: Relation, path: str) -> None:
        self.rel = rel
        self.path = path
        self.referenced: set[str] = set()

    def compile(self, e: Expr, p: str | None = None) -> str:
        p = p or self.path
        if isinstance(e, ColumnRef):
            if not self.rel.has(e.column):
                raise PlanError(p, "unknown_column", f"Column '{e.column}' does not exist in '{self.rel.name}'", f"Use one of: {', '.join(self.rel.column_names()[:30])}")
            self.referenced.add(e.column)
            return q(e.column)
        if isinstance(e, Literal_):
            return lit(e.literal)
        assert isinstance(e, Call)
        args = e.args
        n = len(args)

        def need(k: int) -> None:
            if n != k:
                raise PlanError(p, "arity", f"Operator '{e.op}' expects {k} argument(s), got {n}")

        if e.op in _BINARY:
            need(2)
            a, b = (self.compile(x, f"{p}.args[{i}]") for i, x in enumerate(args))
            if e.op == "div":
                return f"({a} / NULLIF({b}, 0))"
            return f"({a} {_BINARY[e.op]} {b})"
        if e.op in ("and", "or"):
            if n < 2:
                raise PlanError(p, "arity", f"Operator '{e.op}' expects at least two arguments")
            return "(" + f" {e.op.upper()} ".join(self.compile(x, f"{p}.args[{i}]") for i, x in enumerate(args)) + ")"
        if e.op == "not":
            need(1)
            return f"(NOT {self.compile(args[0], p + '.args[0]')})"
        if e.op in ("contains", "icontains", "starts_with", "ends_with"):
            need(2)
            a, b = (self.compile(x, f"{p}.args[{i}]") for i, x in enumerate(args))
            if e.op == "contains":
                return f"contains(CAST({a} AS VARCHAR), {b})"
            if e.op == "icontains":
                return f"contains(lower(CAST({a} AS VARCHAR)), lower({b}))"
            fn = "starts_with" if e.op == "starts_with" else "ends_with"
            return f"{fn}(CAST({a} AS VARCHAR), {b})"
        if e.op == "in":
            need(2)
            a = self.compile(args[0], p + ".args[0]")
            if not isinstance(args[1], Literal_) or not isinstance(args[1].literal, list):
                raise PlanError(p, "invalid_in", "'in' needs a literal list as its second argument")
            return f"({a} IN {lit(args[1].literal)})"
        if e.op in ("is_null", "not_null"):
            need(1)
            a = self.compile(args[0], p + ".args[0]")
            return f"({a} IS NULL)" if e.op == "is_null" else f"({a} IS NOT NULL)"
        if e.op == "coalesce":
            if n < 1:
                raise PlanError(p, "arity", "coalesce needs at least one argument")
            return "COALESCE(" + ", ".join(self.compile(x, f"{p}.args[{i}]") for i, x in enumerate(args)) + ")"
        if e.op in ("lower", "upper", "trim", "length"):
            need(1)
            return f"{e.op}(CAST({self.compile(args[0], p + '.args[0]')} AS VARCHAR))"
        if e.op in ("year", "month"):
            need(1)
            return f"{e.op}(TRY_CAST({self.compile(args[0], p + '.args[0]')} AS TIMESTAMP))"
        if e.op == "date":
            need(1)
            return f"TRY_CAST({self.compile(args[0], p + '.args[0]')} AS DATE)"
        if e.op == "to_number":
            need(1)
            a = self.compile(args[0], p + ".args[0]")
            return f"TRY_CAST(regexp_replace(CAST({a} AS VARCHAR), '[^0-9.\\-]', '', 'g') AS DOUBLE)"
        if e.op == "replace":
            need(3)
            a, b, c = (self.compile(x, f"{p}.args[{i}]") for i, x in enumerate(args))
            return f"replace(CAST({a} AS VARCHAR), {b}, {c})"
        if e.op == "case":
            # case(cond1, val1, cond2, val2, ..., else)
            if n < 3 or n % 2 == 0:
                raise PlanError(p, "arity", "case expects pairs of (condition, value) followed by an else value")
            parts = ["CASE"]
            for i in range(0, n - 1, 2):
                parts.append(f"WHEN {self.compile(args[i], f'{p}.args[{i}]')} THEN {self.compile(args[i + 1], f'{p}.args[{i + 1}]')}")
            parts.append(f"ELSE {self.compile(args[-1], f'{p}.args[{n - 1}]')} END")
            return " ".join(parts)
        raise PlanError(p, "unknown_op", f"Unsupported operator '{e.op}'")


# ------------------------------------------------------------------------------------------------
# Plan compiler
# ------------------------------------------------------------------------------------------------

_SEM_TYPES = {"semantic_value": "VARCHAR", "double": "DOUBLE", "status": "VARCHAR"}


def semantic_output_columns(step: SemanticAnnotateStep) -> list[Column]:
    cols: list[Column] = []
    for qn in step.questions:
        for name, t in qn.output_columns():
            if t == "semantic_value":
                vt = "boolean" if qn.kind == "boolean" else "text"
            elif t == "double":
                vt = "double"
            elif t == "flag":
                vt = "boolean"
            else:
                vt = "text"
            cols.append(Column(name, vt, "semantic"))
    return cols


def match_output_columns(step: SemanticMatchStep) -> list[Column]:
    n = step.name
    return [
        Column(f"{n}.right_row_id", "integer", "semantic"),
        Column(f"{n}.score", "double", "semantic"),
        Column(f"{n}.near", "boolean", "semantic"),
        Column(f"{n}.status", "text", "semantic"),
        Column(f"{n}.candidates", "text", "semantic"),
    ]


def _duck_type(t: str) -> str:
    return {"integer": "BIGINT", "double": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP"}.get(t, "VARCHAR")


def schema_columns(schema: list[dict[str, Any]]) -> list[Column]:
    return [Column(c["name"], c["type"], "row_id" if c["name"] == ROW_ID else "source") for c in schema]


class PlanCompiler:
    def __init__(self, plan: Plan, ctx: CompileContext) -> None:
        self.plan = plan
        self.ctx = ctx
        self.ctes: list[tuple[str, str]] = []
        self.relations: dict[str, Relation] = {}
        self.review: dict[str, str] = {}
        self.registrations: dict[str, list[tuple[int, Any]]] = {}

    def compile(self) -> Compiled:
        source_cols = schema_columns(self.ctx.base_schema)
        self.ctes.append(("source", f"SELECT * FROM read_parquet({path_lit(self.ctx.base_parquet)})"))
        self.relations["source"] = Relation("source", source_cols)
        ids = {"source"}
        for i, step in enumerate(self.plan.steps):
            path = f"steps[{i}]"
            if step.id in ids:
                raise PlanError(path + ".id", "duplicate_step", f"Step id '{step.id}' is used twice")
            if step.input not in ids:
                raise PlanError(path + ".input", "unknown_input", f"Step '{step.id}' references unknown input '{step.input}'", "Use 'source' or the id of an earlier step")
            handler = getattr(self, f"_{step.op}")
            handler(step, self.relations[step.input], path)
            ids.add(step.id)
        if self.plan.output not in ids:
            raise PlanError("output", "unknown_output", f"Output '{self.plan.output}' is not a step id")
        return Compiled(self.ctes, self.relations, self.review, self.plan.output, self.registrations)

    # -- semantic ------------------------------------------------------------------
    def _semantic_annotate(self, step: SemanticAnnotateStep, inp: Relation, path: str) -> None:
        if not inp.row_preserving:
            raise PlanError(path + ".input", "needs_rows", "semantic_annotate needs a row-level input (not an aggregate)")
        for c in step.columns:
            if not inp.has(c):
                raise PlanError(path + ".columns", "unknown_column", f"Input column '{c}' does not exist", f"Use one of: {', '.join(inp.column_names()[:30])}")
        out_cols = semantic_output_columns(step)
        for c in out_cols:
            if inp.has(c.name):
                raise PlanError(path + ".questions", "column_conflict", f"Output column '{c.name}' already exists in the input")
        derived = self.ctx.derived.get(step.id)
        selects = [f"i.{q(c)}" for c in inp.column_names()]
        ov = self.ctx.overrides.get(step.id, {})
        joins = ""
        if derived is not None:
            joins += f" LEFT JOIN read_parquet({path_lit(derived)}) d ON d.{q(ROW_ID)} = i.{q(ROW_ID)}"
        for qn in step.questions:
            value_col = f"{qn.name}.value"
            status_col = f"{qn.name}.status"
            value_type = "BOOLEAN" if qn.kind == "boolean" else "VARCHAR"
            ov_rows = ov.get(value_col)
            ov_table = None
            if ov_rows:
                ov_table = f"ov_{step.id}_{qn.name}"
                self.registrations[ov_table] = ov_rows
                joins += f" LEFT JOIN {q(ov_table)} ov_{qn.name} ON ov_{qn.name}.row_id = i.{q(ROW_ID)}"
            for name, t in qn.output_columns():
                if derived is None:
                    base = "NULL"
                else:
                    base = f"d.{q(name)}"
                if name == value_col and ov_table:
                    expr = f"COALESCE(TRY_CAST(ov_{qn.name}.value AS {value_type}), CAST({base} AS {value_type}))"
                elif name == status_col:
                    expr = f"COALESCE({base}, 'pending')"
                    if ov_table:
                        expr = f"CASE WHEN ov_{qn.name}.row_id IS NOT NULL THEN 'override' ELSE {expr} END"
                elif t == "double":
                    expr = f"CAST({base} AS DOUBLE)"
                elif t == "flag":
                    expr = f"COALESCE(CAST({base} AS BOOLEAN), FALSE)"
                else:
                    expr = f"CAST({base} AS {value_type})"
                selects.append(f"{expr} AS {q(name)}")
        sql = f"SELECT {', '.join(selects)} FROM {q(inp.name)} i{joins}"
        self.ctes.append((step.id, sql))
        rel = Relation(step.id, inp.columns + out_cols, True, dict(inp.semantic_statuses), list(inp.pending_statuses))
        for qn in step.questions:
            st = f"{qn.name}.status"
            for name, _ in qn.output_columns():
                if name != st:
                    rel.semantic_statuses[name] = st
            if not self.ctx.step_complete.get(step.id, False):
                rel.pending_statuses.append(st)
        self.relations[step.id] = rel

    def _semantic_match(self, step: SemanticMatchStep, inp: Relation, path: str) -> None:
        if not inp.row_preserving:
            raise PlanError(path + ".input", "needs_rows", "semantic_match needs a row-level input")
        for c in step.left_columns:
            if not inp.has(c):
                raise PlanError(path + ".left_columns", "unknown_column", f"Left column '{c}' does not exist")
        if step.right.dataset_id not in self.ctx.datasets:
            raise PlanError(path + ".right", "unknown_dataset", f"Right dataset '{step.right.dataset_id}' is not available")
        rpath, rschema = self.ctx.datasets[step.right.dataset_id]
        rcols = {c["name"]: c["type"] for c in rschema}
        for c in step.right_columns:
            if c not in rcols:
                raise PlanError(path + ".right_columns", "unknown_column", f"Right column '{c}' does not exist")
        if step.blocking and (not inp.has(step.blocking.left) or step.blocking.right not in rcols):
            raise PlanError(path + ".blocking", "unknown_column", "Blocking columns must exist on both sides")
        out_cols = match_output_columns(step)
        derived = self.ctx.derived.get(step.id)
        right_out = step.right_output_columns if step.right_output_columns is not None else step.right_columns
        selects = [f"i.{q(c)}" for c in inp.column_names()]
        n = step.name
        joins = ""
        if derived is not None:
            joins += f" LEFT JOIN read_parquet({path_lit(derived)}) d ON d.{q(ROW_ID)} = i.{q(ROW_ID)}"
            joins += f" LEFT JOIN read_parquet({path_lit(rpath)}) r ON r.{q(ROW_ID)} = d.{q(n + '.right_row_id')}"
            selects += [
                f"d.{q(n + '.right_row_id')} AS {q(n + '.right_row_id')}",
                f"d.{q(n + '.score')} AS {q(n + '.score')}",
                f"COALESCE(d.{q(n + '.near')}, FALSE) AS {q(n + '.near')}",
                f"COALESCE(d.{q(n + '.status')}, 'pending') AS {q(n + '.status')}",
                f"d.{q(n + '.candidates')} AS {q(n + '.candidates')}",
            ]
            selects += [f"r.{q(c)} AS {q(step.right_prefix + c)}" for c in right_out]
        else:
            selects += [
                f"NULL::BIGINT AS {q(n + '.right_row_id')}",
                f"NULL::DOUBLE AS {q(n + '.score')}",
                f"FALSE AS {q(n + '.near')}",
                f"'pending' AS {q(n + '.status')}",
                f"NULL::VARCHAR AS {q(n + '.candidates')}",
            ]
            selects += [f"NULL::{_duck_type(rcols[c])} AS {q(step.right_prefix + c)}" for c in right_out]
        self.ctes.append((step.id, f"SELECT {', '.join(selects)} FROM {q(inp.name)} i{joins}"))
        cols = inp.columns + out_cols + [Column(step.right_prefix + c, rcols[c], "right") for c in right_out]
        rel = Relation(step.id, cols, True, dict(inp.semantic_statuses), list(inp.pending_statuses))
        st = f"{n}.status"
        for c in out_cols + [Column(step.right_prefix + c, rcols[c]) for c in right_out]:
            if c.name != st:
                rel.semantic_statuses[c.name] = st
        if not self.ctx.step_complete.get(step.id, False):
            rel.pending_statuses.append(st)
        self.relations[step.id] = rel

    # -- exact ---------------------------------------------------------------------
    def _filter(self, step: FilterStep, inp: Relation, path: str) -> None:
        ec = ExprCompiler(inp, path + ".where")
        where = ec.compile(step.where)
        statuses = sorted({inp.semantic_statuses[c] for c in ec.referenced if c in inp.semantic_statuses})
        good = " AND ".join(f"{q(s)} IN {lit(list(GOOD_STATUSES))}" for s in statuses)
        if statuses and step.unknown_policy in ("separate", "exclude"):
            sql = f"SELECT * FROM {q(inp.name)} WHERE COALESCE({where}, FALSE) AND {good}"
            if step.unknown_policy == "separate":
                review_name = f"{step.id}__review"
                # Rows without a usable answer, plus rows that got one but sit within the flag margin of a
                # calibrated cut (<q>.near): those stay in the output and are listed here for a spot check.
                bad = [f"{q(s)} NOT IN {lit(list(GOOD_STATUSES))}" for s in statuses]
                bad += [f"COALESCE({q(s[:-len('.status')] + '.near')}, FALSE)" for s in statuses if inp.has(s[:-len(".status")] + ".near")]
                self.ctes.append((review_name, f"SELECT * FROM {q(inp.name)} WHERE {' OR '.join(bad)}"))
                self.relations[review_name] = Relation(review_name, list(inp.columns), inp.row_preserving, dict(inp.semantic_statuses), list(inp.pending_statuses))
                self.review[step.id] = review_name
        else:
            sql = f"SELECT * FROM {q(inp.name)} WHERE COALESCE({where}, FALSE)"
        self.ctes.append((step.id, sql))
        self.relations[step.id] = Relation(step.id, list(inp.columns), inp.row_preserving, dict(inp.semantic_statuses), list(inp.pending_statuses))

    def _sort(self, step: SortStep, inp: Relation, path: str) -> None:
        keys = []
        for i, k in enumerate(step.by):
            if not inp.has(k.column):
                raise PlanError(f"{path}.by[{i}].column", "unknown_column", f"Column '{k.column}' does not exist", f"Use one of: {', '.join(inp.column_names()[:30])}")
            keys.append(f"{q(k.column)} {'DESC' if k.direction == 'desc' else 'ASC'} NULLS LAST")
        if inp.has(ROW_ID) and not any(k.column == ROW_ID for k in step.by):
            keys.append(f"{q(ROW_ID)} ASC")
        self.ctes.append((step.id, f"SELECT * FROM {q(inp.name)} ORDER BY {', '.join(keys)}"))
        self.relations[step.id] = Relation(step.id, list(inp.columns), inp.row_preserving, dict(inp.semantic_statuses), list(inp.pending_statuses))

    def _project(self, step: ProjectStep, inp: Relation, path: str) -> None:
        cols = list(step.columns)
        for c in cols:
            if not inp.has(c):
                raise PlanError(path + ".columns", "unknown_column", f"Column '{c}' does not exist", f"Use one of: {', '.join(inp.column_names()[:30])}")
        if inp.has(ROW_ID) and ROW_ID not in cols:
            cols = [ROW_ID] + cols
        self.ctes.append((step.id, f"SELECT {', '.join(q(c) for c in cols)} FROM {q(inp.name)}"))
        kept = [c for c in inp.columns if c.name in cols]
        rel = Relation(step.id, kept, inp.row_preserving, {k: v for k, v in inp.semantic_statuses.items() if k in cols and v in cols}, [s for s in inp.pending_statuses if s in cols])
        self.relations[step.id] = rel

    def _compute(self, step: ComputeStep, inp: Relation, path: str) -> None:
        selects = ["*"]
        new_cols: list[Column] = []
        rel = Relation(step.id, list(inp.columns), inp.row_preserving, dict(inp.semantic_statuses), list(inp.pending_statuses))
        for i, c in enumerate(step.columns):
            if inp.has(c.name):
                raise PlanError(f"{path}.columns[{i}].name", "column_conflict", f"Column '{c.name}' already exists")
            ec = ExprCompiler(inp, f"{path}.columns[{i}].expr")
            selects.append(f"{ec.compile(c.expr)} AS {q(c.name)}")
            new_cols.append(Column(c.name, "derived", "derived"))
            sts = {inp.semantic_statuses[r] for r in ec.referenced if r in inp.semantic_statuses}
            if len(sts) == 1:
                rel.semantic_statuses[c.name] = next(iter(sts))
        self.ctes.append((step.id, f"SELECT {', '.join(selects)} FROM {q(inp.name)}"))
        rel.columns = inp.columns + new_cols
        self.relations[step.id] = rel

    def _aggregate(self, step: AggregateStep, inp: Relation, path: str) -> None:
        for g in step.group_by:
            if not inp.has(g):
                raise PlanError(path + ".group_by", "unknown_column", f"Column '{g}' does not exist", f"Use one of: {', '.join(inp.column_names()[:30])}")
        selects = [q(g) for g in step.group_by]
        cols = [Column(g, next(c.type for c in inp.columns if c.name == g), "group") for g in step.group_by]
        for i, m in enumerate(step.metrics):
            if m.fn == "count" and m.column is None:
                expr = "count(*)"
            else:
                if m.column is None or not inp.has(m.column):
                    raise PlanError(f"{path}.metrics[{i}].column", "unknown_column", f"Metric '{m.name}' needs an existing column")
                fn = {"count": "count", "count_distinct": "count(DISTINCT", "sum": "sum", "avg": "avg", "min": "min", "max": "max"}[m.fn]
                expr = f"{fn} {q(m.column)})" if m.fn == "count_distinct" else f"{fn}({q(m.column)})"
            selects.append(f"{expr} AS {q(m.name)}")
            cols.append(Column(m.name, "double" if m.fn in ("avg", "sum") else "integer" if m.fn.startswith("count") else "derived", "metric"))
        order = ", ".join(q(g) for g in step.group_by) if step.group_by else "1"
        group = f" GROUP BY {', '.join(q(g) for g in step.group_by)}" if step.group_by else ""
        inner = f"SELECT {', '.join(selects)} FROM {q(inp.name)}{group}"
        sql = f"SELECT CAST(row_number() OVER (ORDER BY {order}) - 1 AS BIGINT) AS {q(ROW_ID)}, * FROM ({inner}) t"
        self.ctes.append((step.id, sql))
        rel = Relation(step.id, [Column(ROW_ID, "integer", "row_id")] + cols, False, {}, list(inp.pending_statuses))
        self.relations[step.id] = rel

    def _join(self, step: JoinStep, inp: Relation, path: str) -> None:
        if isinstance(step.right, SourceRef):
            if step.right.dataset_id not in self.ctx.datasets:
                raise PlanError(path + ".right", "unknown_dataset", f"Dataset '{step.right.dataset_id}' is not available in this workspace")
            rpath, rschema = self.ctx.datasets[step.right.dataset_id]
            right_rel = Relation("__right", schema_columns(rschema))
            right_src = f"read_parquet({path_lit(rpath)})"
        else:
            if step.right not in self.relations:
                raise PlanError(path + ".right", "unknown_input", f"Step '{step.right}' is not defined before this join")
            right_rel = self.relations[step.right]
            right_src = q(step.right)
        for i, k in enumerate(step.on):
            if not inp.has(k.left):
                raise PlanError(f"{path}.on[{i}].left", "unknown_column", f"Left column '{k.left}' does not exist")
            if not right_rel.has(k.right):
                raise PlanError(f"{path}.on[{i}].right", "unknown_column", f"Right column '{k.right}' does not exist")
        rcols = step.right_columns if step.right_columns is not None else [c.name for c in right_rel.columns if c.name != ROW_ID]
        for c in rcols:
            if not right_rel.has(c):
                raise PlanError(path + ".right_columns", "unknown_column", f"Right column '{c}' does not exist")
        # A join can multiply left rows (one-to-many), so the output gets fresh, deterministic row ids ordered by
        # (left row id, right row id); the original left id is kept as left_row_id for traceability.
        selects = [f"CAST(row_number() OVER (ORDER BY l.{q(ROW_ID)}, r.{q(ROW_ID)}) - 1 AS BIGINT) AS {q(ROW_ID)}", f"l.{q(ROW_ID)} AS left_row_id"]
        selects += [f"l.{q(c)}" for c in inp.column_names() if c != ROW_ID] + [f"r.{q(c)} AS {q(step.right_prefix + c)}" for c in rcols]
        on = " AND ".join(f"l.{q(k.left)} = r.{q(k.right)}" for k in step.on)
        how = "LEFT JOIN" if step.how == "left" else "JOIN"
        self.ctes.append((step.id, f"SELECT {', '.join(selects)} FROM {q(inp.name)} l {how} {right_src} r ON {on}"))
        rtypes = {c.name: c.type for c in right_rel.columns}
        cols = [Column(ROW_ID, "integer", "row_id"), Column("left_row_id", "integer", "derived")] + [c for c in inp.columns if c.name != ROW_ID] + [Column(step.right_prefix + c, rtypes[c], "right") for c in rcols]
        self.relations[step.id] = Relation(step.id, cols, False, dict(inp.semantic_statuses), list(inp.pending_statuses))

    def _distinct(self, step: DistinctStep, inp: Relation, path: str) -> None:
        cols = step.columns or [c.name for c in inp.columns if c.name != ROW_ID]
        for c in cols:
            if not inp.has(c):
                raise PlanError(path + ".columns", "unknown_column", f"Column '{c}' does not exist")
        inner = f"SELECT DISTINCT {', '.join(q(c) for c in cols)} FROM {q(inp.name)}"
        sql = f"SELECT CAST(row_number() OVER (ORDER BY {', '.join(q(c) for c in cols)}) - 1 AS BIGINT) AS {q(ROW_ID)}, * FROM ({inner}) t"
        self.ctes.append((step.id, sql))
        kept = [Column(ROW_ID, "integer", "row_id")] + [c for c in inp.columns if c.name in cols]
        self.relations[step.id] = Relation(step.id, kept, False, {}, list(inp.pending_statuses))

    def _limit(self, step: LimitStep, inp: Relation, path: str) -> None:
        self.ctes.append((step.id, f"SELECT * FROM {q(inp.name)} LIMIT {int(step.n)}"))
        self.relations[step.id] = Relation(step.id, list(inp.columns), inp.row_preserving, dict(inp.semantic_statuses), list(inp.pending_statuses))


def compile_plan(plan: Plan, ctx: CompileContext) -> Compiled:
    return PlanCompiler(plan, ctx).compile()


def connect(compiled: Compiled | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET preserve_insertion_order=true")
    if compiled:
        for name, rows in compiled.registrations.items():
            con.execute(f"CREATE TEMP TABLE {q(name)} (row_id BIGINT, value VARCHAR)")
            if rows:
                con.executemany(f"INSERT INTO {q(name)} VALUES (?, ?)", [(r, None if v is None else json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)) for r, v in rows])
    return con
