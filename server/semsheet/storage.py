"""File layout for columnar data and helpers to build a compile context for a result version."""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any

from .config import settings
from .db import Database, loads
from .engine.exact import CompileContext
from .models import Plan, SemanticAnnotateStep, SemanticMatchStep, SourceRef


def dataset_dir(dataset_id: str) -> Path:
    return settings.data_dir / "datasets" / dataset_id


def version_parquet(dataset_id: str, version_id: str) -> Path:
    return dataset_dir(dataset_id) / f"{version_id}.parquet"


def result_dir(result_version_id: str) -> Path:
    return settings.data_dir / "results" / result_version_id


def derived_dir(result_version_id: str, step_id: str) -> Path:
    return result_dir(result_version_id) / step_id


def derived_glob(result_version_id: str, step_id: str) -> Path | None:
    d = derived_dir(result_version_id, step_id)
    if d.exists() and glob.glob(str(d / "chunk_*.parquet")):
        return d / "chunk_*.parquet"
    return None


def export_path(export_id: str, fmt: str) -> Path:
    return settings.data_dir / "exports" / f"{export_id}.{fmt}"


def upload_path(upload_id: str) -> Path:
    return settings.data_dir / "uploads" / upload_id


def load_version(db: Database, version_id: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM dataset_versions WHERE id=?", (version_id,))
    if row is None:
        return None
    return dict(row)


def resolve_source(db: Database, workspace_id: str, ref: SourceRef) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a dataset reference to (dataset row, version row), enforcing workspace ownership."""
    ds = db.one("SELECT * FROM datasets WHERE id=? AND workspace_id=? AND deleted_at IS NULL", (ref.dataset_id, workspace_id))
    if ds is None:
        raise LookupError(f"dataset '{ref.dataset_id}' not found in this workspace")
    if ref.version_id:
        ver = db.one("SELECT * FROM dataset_versions WHERE id=? AND dataset_id=?", (ref.version_id, ref.dataset_id))
    else:
        ver = db.one("SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version_no DESC LIMIT 1", (ref.dataset_id,))
    if ver is None:
        raise LookupError(f"dataset version '{ref.version_id}' not found")
    return dict(ds), dict(ver)


def build_context(db: Database, workspace_id: str, plan: Plan, base_version: dict[str, Any], result_version_id: str | None, manifest: dict[str, Any] | None) -> CompileContext:
    ctx = CompileContext(base_parquet=Path(base_version["data_path"]), base_schema=loads(base_version["schema_json"]))
    # right-hand datasets referenced by joins / matches
    for step in plan.steps:
        ref = None
        if step.op == "join" and isinstance(step.right, SourceRef):
            ref = step.right
        elif step.op == "semantic_match":
            ref = step.right
        if ref is not None and ref.dataset_id not in ctx.datasets:
            _, ver = resolve_source(db, workspace_id, ref)
            ctx.datasets[ref.dataset_id] = (Path(ver["data_path"]), loads(ver["schema_json"]))
    if result_version_id:
        derived_owner = (manifest or {}).get("derived_from") or result_version_id
        for step in plan.steps:
            if isinstance(step, (SemanticAnnotateStep, SemanticMatchStep)):
                ctx.derived[step.id] = derived_glob(derived_owner, step.id)
                ctx.step_complete[step.id] = bool(((manifest or {}).get("steps", {}).get(step.id) or {}).get("complete"))
        rows = db.query("SELECT row_id, column_name, value_json FROM overrides WHERE result_version_id=?", (result_version_id,))
        if rows:
            col_to_step: dict[str, str] = {}
            for step in plan.steps:
                if isinstance(step, SemanticAnnotateStep):
                    for qn in step.questions:
                        col_to_step[f"{qn.name}.value"] = step.id
            for r in rows:
                sid = col_to_step.get(r["column_name"])
                if sid is None:
                    continue
                ctx.overrides.setdefault(sid, {}).setdefault(r["column_name"], []).append((int(r["row_id"]), json.loads(r["value_json"])))
    return ctx
