"""Uploads, imports, descriptions, corrections and deletion."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import threading
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import select

from ..config import settings
from ..db import Dataset, DatasetVersion, Job, Override, Upload, session, utcnow
from ..errors import Conflict, NotFound, ValidationFailed
from ..ids import new_id
from ..importer import ParseOptions, bounded_sample, import_csv, preview
from ..plan.compile import ROW_ID
from ..plan.expr import quote_ident as q
from ..storage import (
    dataset_base_parquet,
    dataset_dir,
    dataset_error_report,
    dataset_source_file,
    sql_str,
    upload_path,
)
from .catalog import WorkspaceCatalog, get_dataset, resolve_version


def prepare_upload(workspace_id: str, filename: str) -> Upload:
    cfg = settings()
    up = Upload(id=new_id("up"), workspace_id=workspace_id, filename=filename[:500], path="", status="pending",
                expires_at=utcnow() + dt.timedelta(hours=6))
    up.path = str(upload_path(up.id))
    with session() as s:
        s.add(up)
        s.commit()
    return up


def get_upload(workspace_id: str, upload_id: str) -> Upload:
    with session() as s:
        up = s.get(Upload, upload_id)
        if up is None or up.workspace_id != workspace_id:
            raise NotFound(f"upload {upload_id!r} not found", code="upload_not_found")
        return up


def write_upload_stream(workspace_id: str, upload_id: str, stream: Any) -> Upload:
    cfg = settings()
    up = get_upload(workspace_id, upload_id)
    path = Path(up.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(path, "wb") as f:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > cfg.max_file_bytes:
                f.close()
                path.unlink(missing_ok=True)
                raise ValidationFailed(f"upload exceeds {cfg.max_file_bytes} bytes", code="file_too_large", path="upload")
            f.write(chunk)
    with session() as s:
        db_up = s.get(Upload, upload_id)
        db_up.bytes = total
        db_up.status = "complete"
        s.commit()
        return db_up


def preview_upload(workspace_id: str, upload_id: str, options: dict[str, Any] | None) -> dict[str, Any]:
    up = get_upload(workspace_id, upload_id)
    if up.status != "complete":
        raise ValidationFailed("upload is not complete", code="upload_incomplete")
    opts = ParseOptions.from_dict(options)
    if up.filename.endswith(".gz") and not opts.compression:
        opts.compression = "gzip"
    return preview(Path(up.path), opts)


def start_import(workspace_id: str, upload_id: str, name: str | None, options: dict[str, Any] | None,
                 *, wait: bool = False) -> Dataset:
    cfg = settings()
    up = get_upload(workspace_id, upload_id)
    if up.status == "imported":
        raise Conflict("this upload was already imported", code="upload_consumed")
    if up.status != "complete":
        raise ValidationFailed("upload is not complete", code="upload_incomplete")
    opts = ParseOptions.from_dict(options)
    if up.filename.endswith(".gz") and not opts.compression:
        opts.compression = "gzip"
    ds = Dataset(id=new_id("ds"), workspace_id=workspace_id, name=(name or up.filename)[:500], source_upload_id=up.id,
                 source_filename=up.filename, source_bytes=up.bytes, parse_options_json=opts.to_dict(),
                 status="importing", retention_until=utcnow() + dt.timedelta(days=cfg.retention_days))
    with session() as s:
        s.add(ds)
        db_up = s.get(Upload, upload_id)
        db_up.status = "imported"
        s.commit()
    if wait:
        _run_import(ds.id, Path(up.path), opts)
    else:
        threading.Thread(target=_run_import, args=(ds.id, Path(up.path), opts), daemon=True).start()
    return get_dataset_any(ds.id)


def import_local_file(workspace_id: str, path: Path, name: str, options: dict[str, Any] | None = None) -> Dataset:
    """Import a file already on the server (sample datasets, tests)."""
    up = prepare_upload(workspace_id, path.name)
    with open(path, "rb") as f:
        write_upload_stream(workspace_id, up.id, f)
    return start_import(workspace_id, up.id, name, options, wait=True)


def get_dataset_any(dataset_id: str) -> Dataset:
    with session() as s:
        ds = s.get(Dataset, dataset_id)
        if ds is None:
            raise NotFound("dataset not found", code="dataset_not_found")
        return ds


def _run_import(dataset_id: str, upload: Path, opts: ParseOptions) -> None:
    d = dataset_dir(dataset_id)
    src = dataset_source_file(dataset_id)
    try:
        shutil.copyfile(upload, src)
        res = import_csv(src, dataset_base_parquet(dataset_id), dataset_error_report(dataset_id), opts)
        with session() as s:
            ds = s.get(Dataset, dataset_id)
            v = DatasetVersion(id=new_id("dv"), dataset_id=dataset_id, number=1, reason="import")
            s.add(v)
            ds.schema_json = res.schema
            ds.row_count = res.row_count
            ds.quality_json = {**res.quality, "rejected_rows": res.rejected_rows, "warnings": res.warnings,
                               "import_seconds": res.elapsed_seconds}
            ds.parse_options_json = res.parse_options
            ds.source_checksum = res.checksum
            ds.current_version_id = v.id
            ds.status = "ready"
            s.commit()
    except Exception as e:  # noqa: BLE001 - the failure is recorded on the dataset
        msg = getattr(e, "message", str(e))
        details = getattr(e, "details", None)
        with session() as s:
            ds = s.get(Dataset, dataset_id)
            ds.status = "failed"
            ds.error = json.dumps({"code": getattr(e, "code", "import_failed"), "message": msg, "details": details})
            s.commit()


def list_datasets(workspace_id: str, cursor: str | None, limit: int) -> tuple[list[Dataset], str | None]:
    with session() as s:
        stmt = select(Dataset).where(Dataset.workspace_id == workspace_id, Dataset.status != "deleted")
        if cursor:
            stmt = stmt.where(Dataset.created_at < dt.datetime.fromisoformat(cursor))
        rows = list(s.scalars(stmt.order_by(Dataset.created_at.desc()).limit(limit + 1)).all())
        nxt = rows[limit].created_at.isoformat() if len(rows) > limit else None
        return rows[:limit], nxt


def describe(workspace_id: str, dataset_id: str, *, sample: bool = False, sample_rows: int = 20,
             sample_bytes: int = 8192, columns: list[str] | None = None) -> dict[str, Any]:
    ds = get_dataset(workspace_id, dataset_id)
    with session() as s:
        versions = list(s.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == ds.id)
                                  .order_by(DatasetVersion.number)).all())
    out: dict[str, Any] = {
        "dataset_id": ds.id, "name": ds.name, "status": ds.status, "row_count": ds.row_count,
        "schema": ds.schema_json, "quality": ds.quality_json, "parse_options": ds.parse_options_json,
        "source": {"filename": ds.source_filename, "bytes": ds.source_bytes, "checksum": ds.source_checksum},
        "current_version_id": ds.current_version_id,
        "versions": [{"version_id": v.id, "number": v.number, "parent_id": v.parent_id, "reason": v.reason,
                      "created_at": v.created_at.isoformat()} for v in versions],
        "retention_until": ds.retention_until.isoformat() if ds.retention_until else None,
        "created_at": ds.created_at.isoformat(),
    }
    if ds.error:
        out["error"] = json.loads(ds.error)
    if sample and ds.status == "ready":
        cols = columns or [c["name"] for c in ds.schema_json][:100]
        out["sample"] = bounded_sample(dataset_base_parquet(ds.id), cols, max_rows=min(sample_rows, 20),
                                       max_bytes=min(sample_bytes, 8192))
    return out


def patch(workspace_id: str, dataset_id: str, version_id: str | None, corrections: list[dict[str, Any]],
          provenance: dict[str, Any] | None) -> DatasetVersion:
    """Apply bounded row corrections; returns a new version without changing the source snapshot."""
    ds = get_dataset(workspace_id, dataset_id)
    if ds.status != "ready":
        raise ValidationFailed("dataset is not ready", code="dataset_not_ready")
    if not corrections or len(corrections) > 1000:
        raise ValidationFailed("provide 1..1000 corrections", code="invalid_patch", path="corrections")
    base = resolve_version(ds, version_id)
    cols = {c["name"]: c["type"] for c in ds.schema_json}
    for i, c in enumerate(corrections):
        if c.get("column") not in cols:
            raise ValidationFailed(f"unknown column {c.get('column')!r}", code="unknown_column", path=f"corrections[{i}].column")
        rid = c.get("row_id")
        if not isinstance(rid, int) or rid < 0 or rid >= ds.row_count:
            raise ValidationFailed("row_id out of range", code="invalid_row", path=f"corrections[{i}].row_id")
    with session() as s:
        latest = s.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == ds.id)
                           .order_by(DatasetVersion.number.desc())).first()
        v = DatasetVersion(id=new_id("dv"), dataset_id=ds.id, number=latest.number + 1, parent_id=base.id, reason="patch")
        s.add(v)
        for o in s.scalars(select(Override).where(Override.target_kind == "dataset", Override.target_id == base.id)).all():
            s.add(Override(id=new_id("ov"), target_kind="dataset", target_id=v.id, row_id=o.row_id, column=o.column,
                           value_json=o.value_json, provenance_json=o.provenance_json, created_at=o.created_at))
        for c in corrections:
            s.add(Override(id=new_id("ov"), target_kind="dataset", target_id=v.id, row_id=int(c["row_id"]),
                           column=c["column"], value_json=json.dumps(c.get("value")),
                           provenance_json={**(provenance or {}), **(c.get("provenance") or {}), "kind": "manual_override"}))
        d = s.get(Dataset, ds.id)
        d.current_version_id = v.id
        s.commit()
        return v


def set_current_version(workspace_id: str, dataset_id: str, version_id: str) -> Dataset:
    ds = get_dataset(workspace_id, dataset_id)
    v = resolve_version(ds, version_id)
    with session() as s:
        d = s.get(Dataset, ds.id)
        d.current_version_id = v.id
        s.commit()
        return d


def delete(workspace_id: str, dataset_id: str) -> dict[str, Any]:
    ds = get_dataset(workspace_id, dataset_id)
    with session() as s:
        d = s.get(Dataset, ds.id)
        d.status = "deleted"
        d.deleted_at = utcnow()
        jobs = list(s.scalars(select(Job).where(Job.dataset_id == ds.id, Job.state.in_(["queued", "running"]))).all())
        for j in jobs:
            j.cancel_requested = True
        s.commit()
    shutil.rmtree(dataset_dir(ds.id), ignore_errors=True)
    return {"dataset_id": ds.id, "status": "deleted", "cancelled_jobs": len(jobs)}


def rows_page(workspace_id: str, dataset_id: str, version_id: str | None, start: int, limit: int,
              columns: list[str] | None, max_bytes: int) -> dict[str, Any]:
    cat = WorkspaceCatalog(workspace_id)
    info = cat.dataset(dataset_id, version_id)
    src = cat.source_sql(dataset_id, version_id)
    cols = [c for c in (columns or list(info.columns)) if c in info.columns]
    sel = ", ".join([q(ROW_ID)] + [q(c) for c in cols])
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT {sel} FROM ({src}) t ORDER BY {q(ROW_ID)} LIMIT {int(limit)} OFFSET {int(start)}").fetchall()
    finally:
        con.close()
    from .results import pack_rows  # local import to avoid a cycle

    data, truncated_cells, bytes_used = pack_rows(rows, [ROW_ID] + cols, max_bytes, settings().cell_display_chars)
    return {"columns": [ROW_ID] + cols, "rows": data, "start": start, "returned": len(data),
            "total_count": info.row_count, "count_status": "complete", "has_more": start + len(data) < info.row_count,
            "next_start": start + len(data), "truncated_cells": truncated_cells, "bytes": bytes_used}


def cell(workspace_id: str, dataset_id: str, version_id: str | None, row_id: int, column: str) -> dict[str, Any]:
    cat = WorkspaceCatalog(workspace_id)
    info = cat.dataset(dataset_id, version_id)
    if column not in info.columns:
        raise NotFound("unknown column", code="unknown_column")
    con = duckdb.connect()
    try:
        row = con.execute(f"SELECT {q(column)} FROM ({cat.source_sql(dataset_id, version_id)}) t WHERE {q(ROW_ID)} = {int(row_id)}").fetchone()
    finally:
        con.close()
    if row is None:
        raise NotFound("row not found", code="row_not_found")
    v = row[0]
    return {"row_id": row_id, "column": column, "value": v if isinstance(v, (str, int, float, bool)) or v is None else str(v)}
