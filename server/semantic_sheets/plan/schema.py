"""Typed plan format shared by the spreadsheet, the REST API and the MCP server."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PLAN_VERSION = "1"
PROMPT_LAYOUT_VERSION = "1"
DEFAULT_LABELS = [
    {"name": "other", "description": "None of the listed labels applies."},
    {"name": "insufficient_evidence", "description": "The text does not contain enough information to decide."},
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Strict):
    dataset_id: str
    version_id: str | None = None


class Thresholds(Strict):
    true_min: float = Field(0.85, ge=0.0, le=1.0)
    false_max: float = Field(0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "Thresholds":
        if self.false_max > self.true_min:
            raise ValueError("false_max must be <= true_min")
        return self


class Label(Strict):
    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("label names must match ^[A-Za-z][A-Za-z0-9_]{0,63}$")
        return v


class BooleanQuestion(Strict):
    name: str
    kind: Literal["boolean"] = "boolean"
    instruction: str = Field(min_length=3, max_length=2000)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    criteria: dict[str, str | None] | None = None  # {"true": ..., "false": ...}

    @field_validator("criteria")
    @classmethod
    def _criteria(cls, v: dict[str, str | None] | None) -> dict[str, str | None] | None:
        if v is not None and set(v) - {"true", "false"}:
            raise ValueError("criteria keys must be 'true' and/or 'false'")
        return v


class CategoryQuestion(Strict):
    name: str
    kind: Literal["category"] = "category"
    instruction: str = Field(min_length=3, max_length=2000)
    labels: list[Label] = Field(min_length=1, max_length=253)
    add_default_labels: bool = True
    min_confidence: float = Field(0.0, ge=0.0, le=1.0)

    def effective_labels(self) -> list[Label]:
        names = {l.name for l in self.labels}
        out = list(self.labels)
        if self.add_default_labels:
            for d in DEFAULT_LABELS:
                if d["name"] not in names:
                    out.append(Label(**d))
        return out


class ScoreQuestion(Strict):
    name: str
    kind: Literal["score"] = "score"
    instruction: str = Field(min_length=3, max_length=2000)
    levels: list[str] = Field(min_length=2, max_length=10)


Question = Annotated[Union[BooleanQuestion, CategoryQuestion, ScoreQuestion], Field(discriminator="kind")]


class StepBase(Strict):
    id: str
    input: str = "source"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not NAME_RE.match(v) or v == "source":
            raise ValueError("step ids must match ^[A-Za-z][A-Za-z0-9_]{0,63}$ and cannot be 'source'")
        return v


class SemanticAnnotate(StepBase):
    op: Literal["semantic_annotate"]
    columns: list[str] = Field(min_length=1, max_length=20)
    questions: list[Question] = Field(min_length=1, max_length=20)
    on_missing: Literal["unknown", "skip"] = "unknown"

    @model_validator(mode="after")
    def _unique(self) -> "SemanticAnnotate":
        names = [q.name for q in self.questions]
        if len(set(names)) != len(names):
            raise ValueError("question names must be unique within a step")
        for n in names:
            if not NAME_RE.match(n):
                raise ValueError(f"question name {n!r} must match ^[A-Za-z][A-Za-z0-9_]{{0,63}}$")
        return self


class Filter(StepBase):
    op: Literal["filter"]
    where: dict[str, Any]
    unknown_policy: Literal["separate", "exclude", "include"] = "separate"


class SortKey(Strict):
    column: str
    direction: Literal["asc", "desc"] = "asc"


class Sort(StepBase):
    op: Literal["sort"]
    by: list[SortKey] = Field(min_length=1, max_length=8)


class Project(StepBase):
    op: Literal["project"]
    columns: list[str] = Field(min_length=1)


class DerivedColumn(Strict):
    name: str
    expr: dict[str, Any]

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("derived column names must match ^[A-Za-z][A-Za-z0-9_]{0,63}$")
        return v


class Derive(StepBase):
    op: Literal["derive"]
    columns: list[DerivedColumn] = Field(min_length=1, max_length=50)


class Metric(Strict):
    name: str
    fn: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("metric names must match ^[A-Za-z][A-Za-z0-9_]{0,63}$")
        return v


class Aggregate(StepBase):
    op: Literal["aggregate"]
    group_by: list[str] = Field(default_factory=list, max_length=8)
    metrics: list[Metric] = Field(min_length=1, max_length=20)


class JoinKey(Strict):
    left: str
    right: str


class Join(StepBase):
    op: Literal["join"]
    right: Source
    on: list[JoinKey] = Field(min_length=1, max_length=4)
    how: Literal["inner", "left"] = "left"
    right_columns: list[str] | None = None
    prefix: str = "right_"


class Dedupe(StepBase):
    op: Literal["dedupe"]
    keys: list[str] = Field(min_length=1, max_length=8)


class Limit(StepBase):
    op: Literal["limit"]
    n: int = Field(ge=1, le=1_000_000)


class MatchThresholds(Strict):
    match_min: float = Field(0.85, ge=0.0, le=1.0)
    nonmatch_max: float = Field(0.15, ge=0.0, le=1.0)


class SemanticMatch(StepBase):
    op: Literal["semantic_match"]
    right: Source
    left_columns: list[str] = Field(min_length=1, max_length=10)
    right_columns: list[str] = Field(min_length=1, max_length=10)
    instruction: str = Field(min_length=3, max_length=2000)
    blocking: list[JoinKey] = Field(default_factory=list, max_length=4)
    candidates_per_row: int = Field(5, ge=1, le=20)
    max_right_rows: int = Field(5000, ge=1, le=5000)
    thresholds: MatchThresholds = Field(default_factory=MatchThresholds)
    mode: Literal["best", "all"] = "best"
    show_right_columns: list[str] | None = None


Step = Annotated[
    Union[SemanticAnnotate, Filter, Sort, Project, Derive, Aggregate, Join, Dedupe, Limit, SemanticMatch],
    Field(discriminator="op"),
]


class Plan(Strict):
    plan_version: Literal["1"] = "1"
    source: Source
    model: str | None = None
    steps: list[Step] = Field(min_length=1, max_length=40)
    output: str

    @model_validator(mode="after")
    def _ids(self) -> "Plan":
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique")
        return self


def question_columns(q: BooleanQuestion | CategoryQuestion | ScoreQuestion) -> list[tuple[str, str]]:
    """Output columns (name, logical type) produced by a semantic question."""
    n = q.name
    if q.kind == "boolean":
        return [(f"{n}.value", "boolean"), (f"{n}.p", "number"), (f"{n}.status", "text"), (f"{n}.raw", "json")]
    if q.kind == "category":
        return [(f"{n}.value", "text"), (f"{n}.confidence", "number"), (f"{n}.status", "text"), (f"{n}.raw", "json")]
    return [
        (f"{n}.score", "number"),
        (f"{n}.value", "text"),
        (f"{n}.confidence", "number"),
        (f"{n}.status", "text"),
        (f"{n}.raw", "json"),
    ]


def match_columns(step: SemanticMatch, right_schema: dict[str, str]) -> list[tuple[str, str]]:
    cols: list[tuple[str, str]] = [
        ("match.right_row_id", "integer"),
        ("match.p", "number"),
        ("match.status", "text"),
        ("match.candidate_count", "integer"),
        ("match.candidates", "json"),
        ("match.raw", "json"),
    ]
    for c in step.show_right_columns or step.right_columns:
        cols.append((f"match.{c}", right_schema.get(c, "text")))
    return cols
