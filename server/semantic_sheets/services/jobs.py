"""Job admission, idempotency, budget reservation, lifecycle and events."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..db import Job, JobEvent, PlanVersion, ResultVersion, Workspace, canonical_json, session, sha256_text, utcnow
from ..errors import BudgetExceeded, Conflict, NotFound, ValidationFailed
from ..ids import new_id
from ..plan.compile import CompiledPlan
from . import plans as plan_service
from .catalog import WorkspaceCatalog


def normalize_limits(limits: dict[str, Any] | None) -> dict[str, Any]:
    cfg = settings()
    limits = dict(limits or {})
    out = {
        "max_source_rows": int(limits.get("max_source_rows") or cfg.default_max_source_rows),
        "max_provider_requests": int(limits.get("max_provider_requests") or cfg.default_max_provider_requests),
        "spend_target_usd": float(limits.get("spend_target_usd") if limits.get("spend_target_usd") is not None else cfg.default_spend_target_usd),
        "deadline_seconds": int(limits.get("deadline_seconds") or cfg.default_deadline_seconds),
    }
    if out["max_source_rows"] > cfg.hard_max_rows_per_file:
        raise ValidationFailed(f"max_source_rows cannot exceed {cfg.hard_max_rows_per_file}", code="limit_too_high", path="limits.max_source_rows")
    if out["spend_target_usd"] < 0 or out["deadline_seconds"] < 1:
        raise ValidationFailed("invalid limits", code="invalid_limits", path="limits")
    return out


def submit(workspace_id: str, *, plan: dict[str, Any] | None = None, plan_hash: str | None = None,
           plan_version_id: str | None = None, limits: dict[str, Any] | None = None,
           idempotency_key: str | None = None) -> tuple[Job, bool]:
    """Returns (job, created). Re-submitting the same key and payload returns the same job."""
    if plan_version_id:
        pv = plan_service.get(workspace_id, plan_version_id)
        compiled = plan_service.validate(workspace_id, pv.plan_json)
    elif plan_hash:
        pv = plan_service.by_hash(workspace_id, plan_hash)
        compiled = plan_service.validate(workspace_id, pv.plan_json)
    elif plan:
        compiled = plan_service.validate(workspace_id, plan)
        pv = plan_service.store(workspace_id, compiled)
    else:
        raise ValidationFailed("provide plan, plan_hash or plan_version_id", code="missing_plan", path="plan")
    lim = normalize_limits(limits)
    payload_hash = sha256_text(canonical_json({"plan_hash": compiled.plan_hash, "limits": lim,
                                               "version": compiled.source.version_id}))
    with session() as s:
        if idempotency_key:
            existing = s.scalars(select(Job).where(Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key)).first()
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise Conflict("idempotency key was already used with a different payload", code="idempotency_conflict")
                return existing, False
        est = compiled.estimate
        if est["semantic_rows"] > lim["max_source_rows"]:
            raise ValidationFailed(
                f"the plan touches {est['semantic_rows']} rows semantically; max_source_rows is {lim['max_source_rows']}",
                code="row_limit", path="limits.max_source_rows", details=est)
        if est["provider_requests"] > lim["max_provider_requests"]:
            raise ValidationFailed(
                f"estimated {est['provider_requests']} provider requests exceed max_provider_requests={lim['max_provider_requests']}",
                code="request_limit", path="limits.max_provider_requests", details=est)
        if est["cost_usd"] > lim["spend_target_usd"]:
            raise BudgetExceeded(
                f"estimated cost ${est['cost_usd']:.4f} exceeds spend_target_usd=${lim['spend_target_usd']:.2f}; raise the target or narrow the plan",
                code="spend_target_exceeded", details=est)
        ws = s.get(Workspace, workspace_id)
        reservation = float(est["cost_usd"])
        if (ws.spent_usd or 0.0) + (ws.reserved_usd or 0.0) + reservation > (ws.budget_usd or 0.0):
            raise BudgetExceeded(
                f"workspace budget exhausted: spent ${ws.spent_usd:.4f}, reserved ${ws.reserved_usd:.4f}, "
                f"budget ${ws.budget_usd:.2f}, this job needs ${reservation:.4f}", code="workspace_budget_exceeded")
        ws.reserved_usd = (ws.reserved_usd or 0.0) + reservation
        job = Job(id=new_id("job"), workspace_id=workspace_id, plan_version_id=pv.id, dataset_id=compiled.source.dataset_id,
                  dataset_version_id=compiled.source.version_id, idempotency_key=idempotency_key, payload_hash=payload_hash,
                  model=est["model"], state="queued", limits_json=lim, progress_json={}, usage_json={},
                  reserved_usd=reservation, deadline_at=utcnow() + dt.timedelta(seconds=lim["deadline_seconds"]))
        rv = ResultVersion(id=new_id("rv"), workspace_id=workspace_id, job_id=job.id, number=1,
                           dataset_version_id=compiled.source.version_id, plan_version_id=pv.id, reason="job")
        job.result_version_id = rv.id
        s.add(job)
        s.add(rv)
        s.add(JobEvent(job_id=job.id, seq=1, kind="queued", payload_json={"estimate": est, "limits": lim}))
        s.commit()
        return job, True


def get(workspace_id: str, job_id: str) -> Job:
    with session() as s:
        job = s.get(Job, job_id)
        if job is None or job.workspace_id != workspace_id:
            raise NotFound("job not found", code="job_not_found")
        return job


def list_jobs(workspace_id: str, dataset_id: str | None = None, limit: int = 50) -> list[Job]:
    with session() as s:
        stmt = select(Job).where(Job.workspace_id == workspace_id)
        if dataset_id:
            stmt = stmt.where(Job.dataset_id == dataset_id)
        return list(s.scalars(stmt.order_by(Job.created_at.desc()).limit(limit)).all())


def cancel(workspace_id: str, job_id: str) -> Job:
    with session() as s:
        job = s.get(Job, job_id)
        if job is None or job.workspace_id != workspace_id:
            raise NotFound("job not found", code="job_not_found")
        if job.state in ("succeeded", "partial", "failed", "cancelled"):
            return job
        job.cancel_requested = True
        if job.state == "queued":
            job.state = "cancelled"
            job.terminal_reason = "cancelled"
            job.finished_at = utcnow()
            ws = s.get(Workspace, workspace_id)
            ws.reserved_usd = max(0.0, (ws.reserved_usd or 0.0) - (job.reserved_usd or 0.0))
            job.reserved_usd = 0.0
            seq = (s.scalar(select(JobEvent.seq).where(JobEvent.job_id == job.id).order_by(JobEvent.seq.desc()).limit(1)) or 0) + 1
            s.add(JobEvent(job_id=job.id, seq=seq, kind="finished", payload_json={"state": "cancelled", "reason": "cancelled"}))
        s.commit()
        return job


def events(job_id: str, after_seq: int = 0, limit: int = 500) -> list[JobEvent]:
    with session() as s:
        return list(s.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.seq > after_seq)
                              .order_by(JobEvent.seq).limit(limit)).all())


def describe(job: Job) -> dict[str, Any]:
    terminal = job.state in ("succeeded", "partial", "failed", "cancelled")
    prog = job.progress_json or {}
    if job.state == "running" and prog:
        interval = 2 if (prog.get("source_rows", 0) < 20000) else 5
    elif job.state == "queued":
        interval = 2
    else:
        interval = 10 if not terminal else 0
    return {
        "job_id": job.id, "state": job.state, "terminal_reason": job.terminal_reason,
        "dataset_id": job.dataset_id, "dataset_version_id": job.dataset_version_id,
        "plan_version_id": job.plan_version_id, "model": job.model, "limits": job.limits_json,
        "progress": prog, "usage": job.usage_json or {}, "reserved_usd": job.reserved_usd,
        "result_version_id": job.result_version_id, "result_revision": job.result_revision,
        "errors": (job.error_summary_json or [])[:10], "error_count": len(job.error_summary_json or []),
        "cancel_requested": job.cancel_requested, "attempts": job.attempts,
        "created_at": job.created_at.isoformat(), "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "deadline_at": job.deadline_at.isoformat() if job.deadline_at else None,
        "suggested_poll_seconds": interval,
        "complete": job.state == "succeeded",
    }
