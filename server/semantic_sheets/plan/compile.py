"""Validate a plan graph, resolve columns, estimate work, and describe every step's output schema."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from ..config import settings
from ..db import canonical_json, sha256_text
from ..errors import ValidationFailed
from .expr import compile_expr, infer_type
from .schema import (
    Aggregate,
    Dedupe,
    Derive,
    Filter,
    Join,
    Limit,
    Plan,
    Project,
    SemanticAnnotate,
    SemanticMatch,
    Sort,
    match_columns,
    question_columns,
)

ROW_ID = "_row_id"
SEMANTIC_OPS = {"semantic_annotate", "semantic_match"}
TOKENS_PER_CHAR = 0.28  # conservative planning constant (~3.6 chars/token for English prose)
ROW_OVERHEAD_TOKENS = 24
REQUEST_OVERHEAD_TOKENS = 60


@dataclass
class DatasetInfo:
    dataset_id: str
    version_id: str
    columns: dict[str, str]  # ordered name -> logical type
    row_count: int
    avg_lengths: dict[str, float] = field(default_factory=dict)


class Catalog(Protocol):
    def dataset(self, dataset_id: str, version_id: str | None) -> DatasetInfo: ...


@dataclass
class StepInfo:
    id: str
    op: str
    columns: dict[str, str]
    est_rows: int
    row_count_known: bool
    semantic: bool = False
    input_columns: list[str] = field(default_factory=list)
    estimate: dict[str, Any] = field(default_factory=dict)
    order_by: list[tuple[str, str]] = field(default_factory=list)
    right: DatasetInfo | None = None


@dataclass
class CompiledPlan:
    plan: Plan
    plan_hash: str
    source: DatasetInfo
    steps: dict[str, StepInfo]
    order: list[str]
    warnings: list[str]
    estimate: dict[str, Any]
    required_scopes: list[str]

    @property
    def output(self) -> StepInfo:
        return self.steps[self.plan.output]

    def semantic_steps(self) -> list[StepInfo]:
        return [self.steps[s] for s in self.order if self.steps[s].semantic]

    def ancestors(self, step_id: str) -> list[str]:
        """Step ids upstream of `step_id` (including itself), in plan order."""
        wanted: set[str] = set()
        by_id = {s.id: s for s in self.plan.steps}
        stack = [step_id]
        while stack:
            cur = stack.pop()
            if cur == "source" or cur in wanted:
                continue
            wanted.add(cur)
            stack.append(by_id[cur].input)
        return [s for s in self.order if s in wanted]


def parse_plan(plan_dict: dict[str, Any]) -> Plan:
    try:
        return Plan.model_validate(plan_dict)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise ValidationFailed(first["msg"], code="invalid_plan", path=loc, details=[
            {"path": ".".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()[:20]
        ]) from e


def plan_hash(plan: Plan) -> str:
    return sha256_text(canonical_json(plan.model_dump(mode="json", exclude_none=True)))


def compile_plan(plan: Plan, catalog: Catalog) -> CompiledPlan:
    cfg = settings()
    warnings: list[str] = []
    src = catalog.dataset(plan.source.dataset_id, plan.source.version_id)
    model = plan.model or cfg.jev_model
    steps: dict[str, StepInfo] = {}
    order: list[str] = []
    scopes = {"read", "run"}
    total_requests = 0
    total_tokens = 0
    total_pairs = 0
    total_semantic_rows = 0
    source_info = StepInfo("source", "source", dict(src.columns), src.row_count, True, order_by=[(ROW_ID, "asc")])
    source_info.columns = {ROW_ID: "integer", **src.columns}

    def resolve_input(step_id: str, ref: str) -> StepInfo:
        if ref == "source":
            return source_info
        if ref not in steps:
            raise ValidationFailed(
                f"step {step_id!r} refers to input {ref!r}, which must be an earlier step or 'source'",
                code="unknown_input", path=f"steps[{step_id}].input")
        return steps[ref]

    for idx, step in enumerate(plan.steps):
        path = f"steps[{idx}]"
        inp = resolve_input(step.id, step.input)
        cols = dict(inp.columns)
        info = StepInfo(step.id, step.op, cols, inp.est_rows, inp.row_count_known, order_by=list(inp.order_by))

        if isinstance(step, SemanticAnnotate):
            for c in step.columns:
                if c not in cols:
                    raise ValidationFailed(f"unknown column {c!r}", code="unknown_column", path=f"{path}.columns")
            for q in step.questions:
                for name, t in question_columns(q):
                    if name in cols:
                        raise ValidationFailed(f"output column {name!r} already exists", code="duplicate_column",
                                               path=f"{path}.questions")
                    cols[name] = t
                if q.kind == "category":
                    names = [l.name for l in q.effective_labels()]
                    if len(names) != len(set(names)):
                        raise ValidationFailed("duplicate label names", code="duplicate_label", path=f"{path}.questions")
                    if len(names) > 255:
                        raise ValidationFailed("at most 255 labels", code="too_many_labels", path=f"{path}.questions")
            avg_chars = sum(_avg_len(src, inp, c) for c in step.columns)
            row_tokens = int(avg_chars * TOKENS_PER_CHAR) + ROW_OVERHEAD_TOKENS
            q_tokens = sum(_question_tokens(q) for q in step.questions)
            pack = max(1, cfg.jev_pack_rows)
            per_packet_tokens = pack * (row_tokens + q_tokens) + REQUEST_OVERHEAD_TOKENS
            if per_packet_tokens > cfg.jev_total_token_budget or pack * row_tokens > cfg.jev_state_token_budget:
                pack = max(1, min(pack, cfg.jev_state_token_budget // max(1, row_tokens)))
            rows = inp.est_rows
            requests = math.ceil(rows / pack) if rows else 0
            tokens = rows * (row_tokens + q_tokens) + requests * REQUEST_OVERHEAD_TOKENS
            info.semantic = True
            info.input_columns = list(step.columns)
            info.estimate = {
                "rows": rows, "row_count_known": inp.row_count_known, "questions": len(step.questions),
                "pack_rows": pack, "requests": requests, "input_tokens": tokens,
                "cost_usd": round(tokens / 1e6 * cfg.jev_price_per_million_input, 4),
                "model": model,
            }
            total_requests += requests
            total_tokens += tokens
            total_semantic_rows += rows
            if not inp.row_count_known:
                warnings.append(f"{step.id}: the input row count depends on an earlier filter; the estimate uses the upper bound.")
            if any(q.kind == "boolean" and q.thresholds.true_min < 0.6 for q in step.questions):
                warnings.append(f"{step.id}: a true_min below 0.6 accepts weak evidence; thresholds are not accuracy guarantees.")

        elif isinstance(step, Filter):
            sql, used = compile_expr(step.where, cols, path=f"{path}.where")
            info.estimate = {"filter_sql": sql}
            info.row_count_known = False
            for c in used:
                if c.endswith(".value") or c.endswith(".p") or c.endswith(".score"):
                    break
            else:
                # An exact filter can run before inference if the caller placed it before the semantic step.
                pass

        elif isinstance(step, Sort):
            for k in step.by:
                if k.column not in cols:
                    raise ValidationFailed(f"unknown sort column {k.column!r}", code="unknown_column", path=f"{path}.by")
            ob = [(k.column, k.direction) for k in step.by]
            if ROW_ID in cols and ROW_ID not in [c for c, _ in ob]:
                ob.append((ROW_ID, "asc"))
            info.order_by = ob
            if any(cols[k.column] in ("text", "json") and k.column.endswith(".value") for k in step.by):
                warnings.append(f"{step.id}: sorting by a label column orders alphabetically, not by strength; sort by a score or p column for ranking.")

        elif isinstance(step, Project):
            for c in step.columns:
                if c not in cols:
                    raise ValidationFailed(f"unknown column {c!r}", code="unknown_column", path=f"{path}.columns")
            keep = [ROW_ID] if ROW_ID in cols else []
            keep += [c for c in step.columns if c != ROW_ID]
            info.columns = {c: cols[c] for c in keep}

        elif isinstance(step, Derive):
            for i, d in enumerate(step.columns):
                if d.name in cols:
                    raise ValidationFailed(f"column {d.name!r} already exists", code="duplicate_column",
                                           path=f"{path}.columns[{i}].name")
                compile_expr(d.expr, cols, path=f"{path}.columns[{i}].expr")
                cols[d.name] = infer_type(d.expr, cols)

        elif isinstance(step, Aggregate):
            out: dict[str, str] = {ROW_ID: "integer"}
            for g in step.group_by:
                if g not in cols:
                    raise ValidationFailed(f"unknown group column {g!r}", code="unknown_column", path=f"{path}.group_by")
                out[g] = cols[g]
            for i, m in enumerate(step.metrics):
                if m.fn != "count" and not m.column:
                    raise ValidationFailed(f"metric {m.name!r} needs a column", code="invalid_metric",
                                           path=f"{path}.metrics[{i}]")
                if m.column and m.column not in cols:
                    raise ValidationFailed(f"unknown metric column {m.column!r}", code="unknown_column",
                                           path=f"{path}.metrics[{i}].column")
                if m.fn in ("sum", "avg") and cols[m.column] not in ("integer", "number"):
                    raise ValidationFailed(f"{m.fn} needs a numeric column; {m.column!r} is {cols[m.column]}",
                                           code="invalid_metric", path=f"{path}.metrics[{i}]")
                if m.name in out:
                    raise ValidationFailed(f"duplicate output column {m.name!r}", code="duplicate_column",
                                           path=f"{path}.metrics[{i}].name")
                out[m.name] = "integer" if m.fn in ("count", "count_distinct") else (
                    cols[m.column] if m.fn in ("min", "max") else "number")
            info.columns = out
            info.row_count_known = False
            info.est_rows = min(inp.est_rows, 100_000)
            info.order_by = [(g, "asc") for g in step.group_by] or [(ROW_ID, "asc")]
            if any(m.fn == "sum" for m in step.metrics) and not step.group_by:
                warnings.append(f"{step.id}: a sum over feedback-style rows can overcount repeated entities; derive distinct ids and join to an authoritative table first if needed.")

        elif isinstance(step, Join):
            right = catalog.dataset(step.right.dataset_id, step.right.version_id)
            for i, k in enumerate(step.on):
                if k.left not in cols:
                    raise ValidationFailed(f"unknown left key {k.left!r}", code="unknown_column", path=f"{path}.on[{i}].left")
                if k.right not in right.columns:
                    raise ValidationFailed(f"unknown right key {k.right!r}", code="unknown_column", path=f"{path}.on[{i}].right")
            rcols = step.right_columns if step.right_columns is not None else list(right.columns)
            for c in rcols:
                if c not in right.columns:
                    raise ValidationFailed(f"unknown right column {c!r}", code="unknown_column", path=f"{path}.right_columns")
                name = step.prefix + c
                if name in cols:
                    raise ValidationFailed(f"column {name!r} already exists; change prefix", code="duplicate_column", path=f"{path}.prefix")
                cols[name] = right.columns[c]
            cols["_left_row_id"] = "integer"
            info.right = right
            info.row_count_known = False
            info.order_by = [(ROW_ID, "asc")]

        elif isinstance(step, Dedupe):
            for k in step.keys:
                if k not in cols:
                    raise ValidationFailed(f"unknown key {k!r}", code="unknown_column", path=f"{path}.keys")
            info.row_count_known = False

        elif isinstance(step, Limit):
            info.est_rows = min(inp.est_rows, step.n)
            info.row_count_known = inp.row_count_known

        elif isinstance(step, SemanticMatch):
            right = catalog.dataset(step.right.dataset_id, step.right.version_id)
            for c in step.left_columns:
                if c not in cols:
                    raise ValidationFailed(f"unknown left column {c!r}", code="unknown_column", path=f"{path}.left_columns")
            for c in step.right_columns + (step.show_right_columns or []):
                if c not in right.columns:
                    raise ValidationFailed(f"unknown right column {c!r}", code="unknown_column", path=f"{path}.right_columns")
            for i, b in enumerate(step.blocking):
                if b.left not in cols or b.right not in right.columns:
                    raise ValidationFailed("unknown blocking column", code="unknown_column", path=f"{path}.blocking[{i}]")
            if right.row_count > step.max_right_rows:
                raise ValidationFailed(
                    f"the right table has {right.row_count} rows; matching is limited to {step.max_right_rows} "
                    "(filter or sample it first)", code="right_table_too_large", path=f"{path}.right")
            for name, t in match_columns(step, right.columns):
                if name in cols:
                    raise ValidationFailed(f"column {name!r} already exists", code="duplicate_column", path=path)
                cols[name] = t
            cols["_left_row_id"] = "integer"
            pairs = inp.est_rows * step.candidates_per_row
            pair_chars = sum(_avg_len(src, inp, c) for c in step.left_columns) + sum(
                right.avg_lengths.get(c, 40.0) for c in step.right_columns)
            pair_tokens = int(pair_chars * TOKENS_PER_CHAR) + ROW_OVERHEAD_TOKENS + _tokens(step.instruction)
            pack = max(1, min(cfg.jev_pack_rows, cfg.jev_state_token_budget // max(1, pair_tokens)))
            requests = math.ceil(pairs / pack) if pairs else 0
            tokens = pairs * pair_tokens + requests * REQUEST_OVERHEAD_TOKENS
            info.semantic = True
            info.input_columns = list(step.left_columns) + [b.left for b in step.blocking]
            info.right = right
            info.estimate = {
                "left_rows": inp.est_rows, "right_rows": right.row_count, "candidates_per_row": step.candidates_per_row,
                "pairs": pairs, "pack_pairs": pack, "requests": requests, "input_tokens": tokens,
                "cost_usd": round(tokens / 1e6 * cfg.jev_price_per_million_input, 4), "model": model,
                "retrieval": "exact_blocking+lexical_v1",
            }
            total_requests += requests
            total_tokens += tokens
            total_pairs += pairs
            total_semantic_rows += inp.est_rows
            warnings.append(f"{step.id}: candidate-limited matching is approximate; a miss means no match among "
                            f"{step.candidates_per_row} candidates, not proof that none exists.")
            info.row_count_known = False
            if step.mode == "all":
                info.order_by = [(ROW_ID, "asc")]
        else:  # pragma: no cover
            raise ValidationFailed(f"unsupported op {step.op}", code="unsupported_op", path=path)

        steps[step.id] = info
        order.append(step.id)

    if plan.output not in steps:
        raise ValidationFailed(f"output {plan.output!r} is not a step id", code="unknown_output", path="output")

    # Filters placed after a global sort/aggregate are allowed; the compiler keeps step order as written.
    est = {
        "source_rows": src.row_count,
        "semantic_rows": total_semantic_rows,
        "provider_requests": total_requests,
        "input_tokens": total_tokens,
        "candidate_pairs": total_pairs,
        "cost_usd": round(total_tokens / 1e6 * cfg.jev_price_per_million_input, 4),
        "model": model,
        "semantic_steps": [s for s in order if steps[s].semantic],
    }
    if total_semantic_rows > cfg.default_max_source_rows:
        warnings.append(f"the plan touches {total_semantic_rows} rows semantically; jobs default to a cap of {cfg.default_max_source_rows}.")
    return CompiledPlan(plan, plan_hash(plan), src, steps, order, warnings, est, sorted(scopes))


def _avg_len(src: DatasetInfo, inp: StepInfo, col: str) -> float:
    if col in src.avg_lengths:
        return src.avg_lengths[col]
    t = inp.columns.get(col, "text")
    return 40.0 if t == "text" else 8.0


def _tokens(text: str) -> int:
    return int(len(text) * TOKENS_PER_CHAR) + 2


def _question_tokens(q: Any) -> int:
    n = _tokens(q.instruction) + 12
    if q.kind == "category":
        n += sum(_tokens(l.name) + _tokens(l.description or "") for l in q.effective_labels())
    elif q.kind == "score":
        n += sum(_tokens(l) for l in q.levels)
    elif q.criteria:
        n += sum(_tokens(v or "") for v in q.criteria.values())
    return n
