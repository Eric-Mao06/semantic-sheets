"""Plan validation, storage and the language-to-plan planner entry point."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db import PlanVersion, session
from ..errors import NotFound, ValidationFailed
from ..ids import new_id
from ..plan.compile import CompiledPlan, compile_plan, parse_plan
from .catalog import WorkspaceCatalog


def validate(workspace_id: str, plan_dict: dict[str, Any]) -> CompiledPlan:
    plan = parse_plan(plan_dict)
    return compile_plan(plan, WorkspaceCatalog(workspace_id))


def store(workspace_id: str, compiled: CompiledPlan, *, nl_request: str | None = None, parent_id: str | None = None,
          planner_usage: dict[str, Any] | None = None) -> PlanVersion:
    with session() as s:
        existing = s.scalars(select(PlanVersion).where(PlanVersion.workspace_id == workspace_id,
                                                        PlanVersion.plan_hash == compiled.plan_hash)).first()
        if existing is not None and not nl_request:
            return existing
        pv = PlanVersion(id=new_id("pl"), workspace_id=workspace_id, dataset_id=compiled.plan.source.dataset_id,
                         plan_hash=compiled.plan_hash, plan_json=compiled.plan.model_dump(mode="json", exclude_none=True),
                         estimate_json=compiled.estimate, warnings_json=compiled.warnings, nl_request=nl_request,
                         parent_id=parent_id, planner_usage_json=planner_usage or {})
        s.add(pv)
        s.commit()
        return pv


def get(workspace_id: str, plan_version_id: str) -> PlanVersion:
    with session() as s:
        pv = s.get(PlanVersion, plan_version_id)
        if pv is None or pv.workspace_id != workspace_id:
            raise NotFound("plan not found", code="plan_not_found")
        return pv


def by_hash(workspace_id: str, plan_hash: str) -> PlanVersion:
    with session() as s:
        pv = s.scalars(select(PlanVersion).where(PlanVersion.workspace_id == workspace_id,
                                                  PlanVersion.plan_hash == plan_hash).order_by(PlanVersion.created_at.desc())).first()
        if pv is None:
            raise NotFound("no validated plan with that hash", code="plan_not_found")
        return pv


def describe(compiled: CompiledPlan, pv: PlanVersion | None = None) -> dict[str, Any]:
    out = {
        "plan_hash": compiled.plan_hash,
        "plan": compiled.plan.model_dump(mode="json", exclude_none=True),
        "estimate": compiled.estimate,
        "warnings": compiled.warnings,
        "required_scopes": compiled.required_scopes,
        "steps": [{"id": s, "op": compiled.steps[s].op, "semantic": compiled.steps[s].semantic,
                   "columns": compiled.steps[s].columns, "estimate": compiled.steps[s].estimate}
                  for s in compiled.order],
        "output_columns": compiled.output.columns,
    }
    if pv is not None:
        out["plan_version_id"] = pv.id
        out["nl_request"] = pv.nl_request
        out["parent_id"] = pv.parent_id
        out["created_at"] = pv.created_at.isoformat()
    return out


def compile_request(workspace_id: str, dataset_id: str, request: str, current_plan: dict[str, Any] | None,
                    planning_budget_tokens: int) -> dict[str, Any]:
    """Language -> plan with bounded context: schema, <=20 sample rows, other dataset names."""
    from ..importer import bounded_sample
    from ..planner import compile_request as plan_it
    from ..storage import dataset_base_parquet
    from . import datasets as ds_service
    from .catalog import get_dataset

    ds = get_dataset(workspace_id, dataset_id)
    if ds.status != "ready":
        raise ValidationFailed("dataset is not ready", code="dataset_not_ready")
    cols = [c["name"] for c in ds.schema_json][:100]
    sample = bounded_sample(dataset_base_parquet(ds.id), cols, max_rows=20, max_bytes=8192)
    others, _ = ds_service.list_datasets(workspace_id, None, 25)
    other_info = [{"dataset_id": d.id, "name": d.name, "row_count": d.row_count,
                   "columns": [c["name"] for c in (d.schema_json or [])][:40]} for d in others if d.id != ds.id and d.status == "ready"]
    compiled, meta = plan_it(workspace_id, ds.id, request, schema=ds.schema_json, row_count=ds.row_count, sample=sample,
                             other_datasets=other_info, current_plan=current_plan,
                             validate=lambda pd: validate(workspace_id, pd), max_output_tokens=planning_budget_tokens)
    pv = store(workspace_id, compiled, nl_request=request, planner_usage=meta.get("usage"))
    out = describe(compiled, pv)
    out["planner"] = meta
    return out
