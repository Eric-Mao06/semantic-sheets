"""Typed plan format shared by the spreadsheet and the MCP server.

Every operation declares its input, output columns, missing-data behaviour and completion scope.
The expression tree is deliberately constrained: neither interface accepts arbitrary SQL or Python."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLAN_VERSION = "1"

# ------------------------------------------------------------------------------------------------
# Expression tree
# ------------------------------------------------------------------------------------------------

ExprOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte",
    "and", "or", "not",
    "contains", "icontains", "starts_with", "ends_with", "in",
    "is_null", "not_null",
    "add", "sub", "mul", "div",
    "coalesce", "lower", "upper", "length", "trim", "year", "month", "date",
    "to_number", "replace", "case",
]


class ColumnRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str


class Literal_(BaseModel):
    model_config = ConfigDict(extra="forbid")
    literal: str | int | float | bool | None | list[str | int | float | bool | None]


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: ExprOp
    args: list[Expr] = Field(default_factory=list)


Expr = ColumnRef | Literal_ | Call
Call.model_rebuild()


# ------------------------------------------------------------------------------------------------
# Semantic questions (Jev primitives)
# ------------------------------------------------------------------------------------------------


class BooleanThresholds(BaseModel):
    """How a boolean question's probability becomes a value.

    mode=auto  -> the cut is calibrated from the score distribution once the whole stage has been scored
                  (see engine/calibrate.py); every row gets a value, rows near the cut are flagged in <name>.near.
                  true_min/false_max are only used as the fallback when there are too few scored rows.
    mode=fixed -> classic three-way: score >= true_min is true, <= false_max is false, in between is 'uncertain'
                  with a null value."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "fixed"] = "auto"
    true_min: float = Field(0.85, ge=0.0, le=1.0)
    false_max: float = Field(0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> BooleanThresholds:
        if self.false_max > self.true_min:
            raise ValueError("thresholds.false_max must be <= thresholds.true_min")
        return self


class Question(BaseModel):
    """One semantic judgement applied to every row of the input.

    kind=boolean  -> Jev Noul.   Outputs <name>.value (bool|null), <name>.score (p_yes), <name>.near (bool: within the flag margin of the cut), <name>.status
    kind=category -> Jev Choice. Outputs <name>.value (label|null), <name>.score (p_top), <name>.confidence, <name>.status
    kind=score    -> Jev Score.  Outputs <name>.value (level label), <name>.score (expected level index), <name>.confidence, <name>.status
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    kind: Literal["boolean", "category", "score"]
    instruction: str = Field(min_length=3, max_length=4000)
    # boolean
    criteria: dict[Literal["true", "false"], str] | None = None
    thresholds: BooleanThresholds = Field(default_factory=BooleanThresholds)
    # category
    options: dict[str, str | None] | None = None
    min_confidence: float = Field(0.0, ge=0.0, le=1.0)
    # score
    levels: list[str] | None = None

    @model_validator(mode="after")
    def _check_kind(self) -> Question:
        if self.kind == "category":
            if not self.options or len(self.options) < 2:
                raise ValueError("category question needs at least two options")
            if len(self.options) > 255:
                raise ValueError("category question supports at most 255 options")
        if self.kind == "score":
            if not self.levels or len(self.levels) < 2:
                raise ValueError("score question needs at least two ordered levels")
            if len(self.levels) > 10:
                raise ValueError("score question supports at most 10 levels")
        return self

    def output_columns(self) -> list[tuple[str, str]]:
        cols = [(f"{self.name}.value", "semantic_value"), (f"{self.name}.score", "double"), (f"{self.name}.status", "status")]
        if self.kind in ("category", "score"):
            cols.insert(2, (f"{self.name}.confidence", "double"))
        else:
            cols.insert(2, (f"{self.name}.near", "flag"))
        return cols


# ------------------------------------------------------------------------------------------------
# Steps
# ------------------------------------------------------------------------------------------------


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    version_id: str | None = None


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    input: str = Field(description="'source' or the id of an earlier step")


class SemanticAnnotateStep(StepBase):
    op: Literal["semantic_annotate"]
    columns: list[str] = Field(min_length=1, max_length=12, description="Input columns shown to the model")
    questions: list[Question] = Field(min_length=1, max_length=16)
    on_missing: Literal["unknown", "skip"] = "unknown"


class FilterStep(StepBase):
    op: Literal["filter"]
    where: Expr
    unknown_policy: Literal["separate", "exclude", "include"] = "separate"


class SortKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    direction: Literal["asc", "desc"] = "asc"


class SortStep(StepBase):
    op: Literal["sort"]
    by: list[SortKey] = Field(min_length=1)


class ProjectStep(StepBase):
    op: Literal["project"]
    columns: list[str] = Field(min_length=1)


class ComputedColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    expr: Expr


class ComputeStep(StepBase):
    op: Literal["compute"]
    columns: list[ComputedColumn] = Field(min_length=1)


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    fn: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: str | None = None


class AggregateStep(StepBase):
    op: Literal["aggregate"]
    group_by: list[str] = Field(default_factory=list)
    metrics: list[Metric] = Field(min_length=1)


class JoinKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str
    right: str


class JoinStep(StepBase):
    op: Literal["join"]
    right: SourceRef | str = Field(description="Dataset reference or the id of an earlier step")
    on: list[JoinKey] = Field(min_length=1)
    how: Literal["inner", "left"] = "inner"
    right_columns: list[str] | None = None
    right_prefix: str = "right."


class DistinctStep(StepBase):
    op: Literal["distinct"]
    columns: list[str] | None = None


class LimitStep(StepBase):
    op: Literal["limit"]
    n: int = Field(ge=1, le=1_000_000)


class SemanticMatchStep(StepBase):
    """Candidate-limited semantic matching against a bounded right table.

    Outputs: <name>.right_row_id, <name>.score, <name>.near (bool), <name>.status (matched|uncertain|unmatched|no_candidates|failed),
    <name>.candidates (JSON), plus right_prefix + right column for the accepted candidate.

    threshold_mode=auto calibrates the accept cut from the distribution of best-candidate probabilities once the
    stage is scored (accept_min/reject_max are the fallback for small inputs); fixed uses them as given."""

    op: Literal["semantic_match"]
    name: str = Field(default="match", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    right: SourceRef
    left_columns: list[str] = Field(min_length=1, max_length=8)
    right_columns: list[str] = Field(min_length=1, max_length=8)
    instruction: str = Field(min_length=3, max_length=4000, description="Yes/no relation to verify for each candidate pair")
    criteria: dict[Literal["true", "false"], str] | None = None
    candidates_per_row: int = Field(5, ge=1, le=5)
    blocking: JoinKey | None = Field(None, description="Optional exact blocking key (left column = right column)")
    threshold_mode: Literal["auto", "fixed"] = "auto"
    accept_min: float = Field(0.8, ge=0.0, le=1.0)
    reject_max: float = Field(0.3, ge=0.0, le=1.0)
    right_output_columns: list[str] | None = None
    right_prefix: str = "match."


Step = Annotated[
    SemanticAnnotateStep | FilterStep | SortStep | ProjectStep | ComputeStep | AggregateStep | JoinStep | DistinctStep | LimitStep | SemanticMatchStep,
    Field(discriminator="op"),
]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_version: Literal["1"] = PLAN_VERSION
    source: SourceRef
    model: str = "jev-1.13.0"
    steps: list[Step] = Field(min_length=1, max_length=32)
    output: str
    title: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)


# ------------------------------------------------------------------------------------------------
# Job limits & envelopes
# ------------------------------------------------------------------------------------------------


class JobLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_source_rows: int = Field(10_000, ge=1)
    max_provider_requests: int = Field(12_000, ge=1)
    spend_target_usd: float = Field(0.50, ge=0.0)
    deadline_seconds: int = Field(600, ge=5, le=6 * 3600)
    rows_per_request: int | None = Field(None, ge=1, le=50)


class PlanEstimate(BaseModel):
    source_rows: int
    semantic_rows: int
    inference_attempts: int
    provider_requests: int
    input_tokens: int
    estimated_cost_usd: float
    candidate_pairs: int = 0
    cache_hits_estimated: int = 0
    stages: list[dict[str, Any]] = Field(default_factory=list)
    # Lower bound from the per-route rate limits, spread over every Jev route that has a key (`jev_routes`).
    quota_floor_seconds: float = 0.0
    jev_routes: list[str] = Field(default_factory=list)
