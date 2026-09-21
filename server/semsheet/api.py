"""Web API used by the spreadsheet frontend. Thin adapter over `Services`."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .db import get_db
from .models import JobLimits
from .samples import SAMPLES, sample_path
from .services import ServiceError, Services, Workspace

from contextlib import asynccontextmanager  # noqa: E402

from .mcp_server import build_mcp_app, mcp  # noqa: E402

_mcp_app = build_mcp_app()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Semantic Spreadsheet API", version="0.1.0", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], expose_headers=["*"])

_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = Services(get_db())
    return _services


def _token(request: Request, authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-workspace-token") or request.query_params.get("token")


def workspace(request: Request, authorization: str | None = Header(default=None)) -> Workspace:
    try:
        return services().workspace_from_token(_token(request, authorization))
    except ServiceError as e:
        raise HTTPException(e.status, e.to_dict())


@app.exception_handler(ServiceError)
async def _service_error(_: Request, e: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"error": e.to_dict()})


# ------------------------------------------------------------------------------------------------
# Workspace / samples
# ------------------------------------------------------------------------------------------------


@app.get("/api/workspace")
def get_workspace(ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().workspace_info(ws)


@app.get("/api/samples")
def list_samples(ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    out = []
    for s in SAMPLES:
        p = sample_path(s["file"])
        out.append({**s, "available": p.exists(), "bytes": p.stat().st_size if p.exists() else 0})
    return {"samples": out}


class SampleImport(BaseModel):
    name: str | None = None


@app.post("/api/samples/{key}/import")
def import_sample(key: str, body: SampleImport | None = None, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    s = next((x for x in SAMPLES if x["key"] == key), None)
    if s is None or not sample_path(s["file"]).exists():
        raise HTTPException(404, {"code": "not_found", "message": "Sample not available"})
    return services().datasets_import(ws, None, (body.name if body else None) or s["title"], s.get("options"), source_path=sample_path(s["file"]))


# ------------------------------------------------------------------------------------------------
# Uploads and datasets
# ------------------------------------------------------------------------------------------------


class UploadPrepare(BaseModel):
    filename: str | None = None


@app.post("/api/uploads")
def uploads_prepare(body: UploadPrepare, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().uploads_prepare(ws, body.filename)


@app.put("/api/uploads/{upload_id}/content")
async def upload_content(upload_id: str, request: Request, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > settings.max_file_bytes:
            raise HTTPException(413, {"code": "file_too_large", "message": "Upload exceeds the size limit"})
        chunks.append(chunk)
    return services().upload_write(ws, upload_id, chunks)


class ImportBody(BaseModel):
    upload_id: str
    name: str
    options: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/datasets/import")
def datasets_import(body: ImportBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().datasets_import(ws, body.upload_id, body.name, body.options)


@app.get("/api/datasets")
def datasets_list(cursor: str | None = None, limit: int = 50, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().datasets_list(ws, cursor, limit)


@app.get("/api/datasets/{dataset_id}")
def datasets_describe(dataset_id: str, version_id: str | None = None, sample: bool = False, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().datasets_describe(ws, dataset_id, version_id, sample)


@app.get("/api/datasets/{dataset_id}/import-errors")
def dataset_import_errors(dataset_id: str, ws: Workspace = Depends(workspace)) -> FileResponse:
    p = settings.data_dir / "datasets" / dataset_id / "import_errors.csv"
    if not p.exists():
        raise HTTPException(404, {"code": "not_found", "message": "No error report"})
    return FileResponse(p, media_type="text/csv", filename="import_errors.csv")


@app.delete("/api/datasets/{dataset_id}")
def datasets_delete(dataset_id: str, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().datasets_delete(ws, dataset_id)


class PatchBody(BaseModel):
    version_id: str | None = None
    patches: list[dict[str, Any]]


@app.post("/api/datasets/{dataset_id}/patch")
def datasets_patch(dataset_id: str, body: PatchBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().datasets_patch(ws, dataset_id, body.version_id, body.patches)


class BaseQuery(BaseModel):
    version_id: str | None = None
    columns: list[str] | None = None
    where: dict[str, Any] | None = None
    sort: list[dict[str, Any]] | None = None
    start: int = 0
    limit: int = 256
    max_bytes: int | None = None
    row_ids: list[int] | None = None
    aggregate: dict[str, Any] | None = None


@app.post("/api/datasets/{dataset_id}/query")
def datasets_query(dataset_id: str, body: BaseQuery, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    """Paged view over a base dataset version (no plan). Implemented as an identity result over the source."""
    svc = services()
    rv_id = svc.ensure_base_result(ws, dataset_id, body.version_id)
    return svc.results_query(ws, rv_id, "source", body.columns, body.where, body.sort, body.start, body.limit, body.max_bytes, None, body.aggregate, body.row_ids, surface="web")


# ------------------------------------------------------------------------------------------------
# Plans
# ------------------------------------------------------------------------------------------------


class CompileBody(BaseModel):
    dataset_id: str
    version_id: str | None = None
    prompt: str
    previous_plan: dict[str, Any] | None = None


@app.post("/api/plans/compile")
async def plans_compile(body: CompileBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return await asyncio.to_thread(services().plans_compile, ws, body.dataset_id, body.version_id, body.prompt, body.previous_plan)


class ValidateBody(BaseModel):
    plan: dict[str, Any]
    limits: dict[str, Any] | None = None


@app.post("/api/plans/validate")
def plans_validate(body: ValidateBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    lim = JobLimits.model_validate(body.limits) if body.limits else None
    return services().plans_validate(ws, body.plan, lim)


# ------------------------------------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------------------------------------


class SubmitBody(BaseModel):
    plan_hash: str | None = None
    plan: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    idempotency_key: str | None = None


@app.post("/api/jobs")
def jobs_submit(body: SubmitBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().jobs_submit(ws, body.plan_hash, body.plan, body.limits, body.idempotency_key)


@app.get("/api/jobs")
def jobs_list(limit: int = 50, dataset_version_id: str | None = None, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return {"jobs": services().jobs_list(ws, limit, dataset_version_id)}


@app.get("/api/jobs/{job_id}")
def jobs_get(job_id: str, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().jobs_get(ws, job_id)


@app.post("/api/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().jobs_cancel(ws, job_id)


@app.get("/api/jobs/{job_id}/events")
async def jobs_events(job_id: str, request: Request, after: int = 0, ws: Workspace = Depends(workspace)) -> StreamingResponse:
    """Sequenced job events over SSE. Clients replay from `after` after a reconnect."""
    svc = services()
    svc.jobs_get(ws, job_id)

    async def gen():
        seq = after
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            events = await asyncio.to_thread(svc.jobs_events, ws, job_id, seq)
            for ev in events:
                seq = ev["seq"]
                yield f"id: {seq}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                if ev["type"] in ("finished", "cancelled"):
                    return
            if not events:
                idle += 1
                if idle % 20 == 0:
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------------------------
# Results
# ------------------------------------------------------------------------------------------------


class QueryBody(BaseModel):
    step: str | None = None
    columns: list[str] | None = None
    where: dict[str, Any] | None = None
    sort: list[dict[str, Any]] | None = None
    start: int = 0
    limit: int = 256
    max_bytes: int | None = None
    max_cell_chars: int | None = None
    aggregate: dict[str, Any] | None = None
    row_ids: list[int] | None = None


@app.get("/api/results/{rv_id}")
def results_describe(rv_id: str, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_describe(ws, rv_id)


@app.post("/api/results/{rv_id}/query")
def results_query(rv_id: str, body: QueryBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_query(ws, rv_id, body.step, body.columns, body.where, body.sort, body.start, body.limit, body.max_bytes, body.max_cell_chars, body.aggregate, body.row_ids, surface="web")


@app.get("/api/results/{rv_id}/cell")
def results_cell(rv_id: str, row_id: int, column: str, step: str | None = None, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_cell(ws, rv_id, step, row_id, column)


@app.get("/api/results/{rv_id}/provenance")
def results_provenance(rv_id: str, step: str, row_id: int, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_provenance(ws, rv_id, step, row_id)


class VectorsBody(BaseModel):
    step: str | None = None
    columns: list[str]


@app.post("/api/results/{rv_id}/vectors")
def results_vectors(rv_id: str, body: VectorsBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_vectors(ws, rv_id, body.step, body.columns)


class OverridesBody(BaseModel):
    overrides: list[dict[str, Any]]
    note: str | None = None


@app.post("/api/results/{rv_id}/patch")
def results_patch(rv_id: str, body: OverridesBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_patch(ws, rv_id, body.overrides, body.note, author="web")


@app.get("/api/results/{rv_id}/versions")
def results_versions(rv_id: str, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return {"versions": services().results_versions(ws, rv_id)}


@app.get("/api/results/{rv_id}/overrides")
def results_overrides(rv_id: str, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return {"overrides": services().results_overrides(ws, rv_id)}


class ExportBody(BaseModel):
    format: str = "csv"
    step: str | None = None
    raw: bool = False
    columns: list[str] | None = None


@app.post("/api/results/{rv_id}/export")
def results_export(rv_id: str, body: ExportBody, ws: Workspace = Depends(workspace)) -> dict[str, Any]:
    return services().results_export(ws, rv_id, body.format, body.step, body.raw, body.columns)


@app.get("/api/exports/{export_id}/download")
def export_download(export_id: str, ws: Workspace = Depends(workspace)) -> FileResponse:
    path, media = services().export_file(ws, export_id)
    return FileResponse(path, media_type=media, filename=path.name)


@app.get("/api/exports/{export_id}/manifest")
def export_manifest(export_id: str, ws: Workspace = Depends(workspace)) -> FileResponse:
    path, media = services().export_file(ws, export_id, manifest=True)
    return FileResponse(path, media_type=media, filename=path.name)


@app.get("/api/health")
def health() -> dict[str, Any]:
    db = get_db()
    running = db.one("SELECT count(*) c FROM jobs WHERE state='running'")["c"]
    queued = db.one("SELECT count(*) c FROM jobs WHERE state='queued'")["c"]
    return {"ok": True, "jobs_running": running, "jobs_queued": queued, "model": settings.jev_model, "planner": settings.planner_model}


# ------------------------------------------------------------------------------------------------
# MCP mount + static frontend
# ------------------------------------------------------------------------------------------------

app.mount("/mcp", _mcp_app)

_web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if _web_dist.exists():
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")
