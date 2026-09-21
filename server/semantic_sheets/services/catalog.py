"""Resolve datasets and versions to DuckDB relations, applying override overlays."""

from __future__ import annotations

import json
from typing import Any

import duckdb
from sqlalchemy import select

from ..db import Dataset, DatasetVersion, Override, session
from ..errors import NotFound, ValidationFailed
from ..plan.compile import ROW_ID, DatasetInfo
from ..plan.expr import quote_ident as q, sql_literal
from ..storage import dataset_base_parquet, sql_str

DUCK_TYPES = {"text": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE",
              "timestamp": "TIMESTAMP", "json": "VARCHAR"}


def get_dataset(workspace_id: str, dataset_id: str) -> Dataset:
    with session() as s:
        ds = s.get(Dataset, dataset_id)
        if ds is None or ds.workspace_id != workspace_id or ds.status == "deleted":
            raise NotFound(f"dataset {dataset_id!r} not found", code="dataset_not_found")
        return ds


def resolve_version(ds: Dataset, version_id: str | None) -> DatasetVersion:
    with session() as s:
        vid = version_id or ds.current_version_id
        v = s.get(DatasetVersion, vid) if vid else None
        if v is None or v.dataset_id != ds.id:
            raise NotFound(f"dataset version {version_id!r} not found", code="version_not_found")
        return v


def overrides_for(kind: str, target_id: str) -> list[Override]:
    with session() as s:
        return list(s.scalars(select(Override).where(Override.target_kind == kind, Override.target_id == target_id)
                              .order_by(Override.created_at)).all())


class WorkspaceCatalog:
    """Catalog bound to one workspace; caches dataset info for the duration of a request or job."""

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self._cache: dict[tuple[str, str | None], DatasetInfo] = {}
        self._avg_cache: dict[str, dict[str, float]] = {}

    def dataset(self, dataset_id: str, version_id: str | None) -> DatasetInfo:
        key = (dataset_id, version_id)
        if key in self._cache:
            return self._cache[key]
        ds = get_dataset(self.workspace_id, dataset_id)
        if ds.status != "ready":
            raise ValidationFailed(f"dataset {dataset_id!r} is {ds.status}", code="dataset_not_ready")
        v = resolve_version(ds, version_id)
        cols = {c["name"]: c["type"] for c in ds.schema_json}
        info = DatasetInfo(dataset_id=ds.id, version_id=v.id, columns=cols, row_count=ds.row_count,
                           avg_lengths=self.avg_lengths(ds))
        self._cache[key] = info
        self._cache[(dataset_id, v.id)] = info
        return info

    def avg_lengths(self, ds: Dataset) -> dict[str, float]:
        if ds.id in self._avg_cache:
            return self._avg_cache[ds.id]
        text_cols = [c["name"] for c in ds.schema_json if c["type"] == "text"]
        out: dict[str, float] = {}
        if text_cols:
            con = duckdb.connect()
            try:
                aggs = ", ".join(f"avg(length({q(c)}))" for c in text_cols)
                row = con.execute(f"SELECT {aggs} FROM read_parquet({sql_str(dataset_base_parquet(ds.id))}) USING SAMPLE 5000 ROWS").fetchone()
                for c, v in zip(text_cols, row):
                    out[c] = float(v or 0.0)
            finally:
                con.close()
        self._avg_cache[ds.id] = out
        return out

    def source_sql(self, dataset_id: str, version_id: str | None) -> str:
        info = self.dataset(dataset_id, version_id)
        base = f"SELECT * FROM read_parquet({sql_str(dataset_base_parquet(dataset_id))})"
        ovs = overrides_for("dataset", info.version_id)
        if not ovs:
            return base
        return apply_overrides_sql(base, info.columns, ovs)


def apply_overrides_sql(base_sql: str, columns: dict[str, str], ovs: list[Override]) -> str:
    """Overlay override values (latest per row/column wins) on top of a relation by _row_id."""
    by_col: dict[str, dict[int, Any]] = {}
    for o in ovs:
        by_col.setdefault(o.column, {})[o.row_id] = json.loads(o.value_json)
    parts = [f"b.{q(c)}" for c in columns if c not in by_col] + [f"b.{q(ROW_ID)}"]
    joins = []
    for i, (col, rows) in enumerate(by_col.items()):
        if col not in columns:
            continue
        t = DUCK_TYPES.get(columns[col], "VARCHAR")
        values = ", ".join(f"({int(rid)}, {sql_literal(None if v is None else str(v))})" for rid, v in rows.items())
        alias = f"o{i}"
        joins.append(f"LEFT JOIN (SELECT * FROM (VALUES {values}) v(rid, val)) {alias} ON {alias}.rid = b.{q(ROW_ID)}")
        parts.append(f"(CASE WHEN {alias}.rid IS NOT NULL THEN TRY_CAST({alias}.val AS {t}) ELSE b.{q(col)} END) AS {q(col)}")
    ordered = [f"b.{q(ROW_ID)}"] + [p for p in parts if not p.endswith(f"b.{q(ROW_ID)}")]
    # keep original column order
    sel = []
    for c in [ROW_ID] + list(columns):
        if c == ROW_ID:
            sel.append(f"b.{q(ROW_ID)}")
        elif c in by_col:
            sel.append(next(p for p in parts if p.endswith(f" AS {q(c)}")))
        else:
            sel.append(f"b.{q(c)}")
    return f"SELECT {', '.join(sel)} FROM ({base_sql}) b " + " ".join(joins)
