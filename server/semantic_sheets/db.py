"""Durable objects: workspaces, datasets, versions, plans, jobs, results, predictions, overrides, exports."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reserved_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    path: Mapped[str] = mapped_column(String(1000))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|complete|imported|expired
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    source_upload_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_filename: Mapped[str] = mapped_column(String(500), default="")
    source_checksum: Mapped[str] = mapped_column(String(64), default="")
    source_bytes: Mapped[int] = mapped_column(Integer, default=0)
    schema_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parse_options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="importing")  # importing|ready|failed|deleted
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(40), ForeignKey("datasets.id"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="import")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Override(Base):
    """A manual correction. Never alters the original model output or the source snapshot."""

    __tablename__ = "overrides"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(10))  # dataset|result
    target_id: Mapped[str] = mapped_column(String(40), index=True)
    row_id: Mapped[int] = mapped_column(Integer)
    column: Mapped[str] = mapped_column(String(300))
    value_json: Mapped[str] = mapped_column(Text)  # JSON-encoded value (null allowed)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_overrides_target_row", Override.target_kind, Override.target_id, Override.row_id)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), index=True)
    dataset_id: Mapped[str] = mapped_column(String(40), index=True)
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    estimate_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    nl_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    planner_usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), index=True)
    plan_version_id: Mapped[str] = mapped_column(String(40), ForeignKey("plan_versions.id"))
    dataset_id: Mapped[str] = mapped_column(String(40), index=True)
    dataset_version_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    limits_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_summary_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reserved_usd: Mapped[float] = mapped_column(Float, default=0.0)
    result_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_revision: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_jobs_idem", Job.workspace_id, Job.idempotency_key, unique=True)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ResultVersion(Base):
    __tablename__ = "result_versions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dataset_version_id: Mapped[str] = mapped_column(String(40))
    plan_version_id: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(200), default="job")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Prediction(Base):
    """Prediction cache keyed by tenant, projected content, question, rubric, model and layout versions."""

    __tablename__ = "predictions"
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(60))
    raw_json: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(40), ForeignKey("workspaces.id"), index=True)
    result_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dataset_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    format: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(1000))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = settings().database_url
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _pragmas(dbapi_conn, _rec):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
