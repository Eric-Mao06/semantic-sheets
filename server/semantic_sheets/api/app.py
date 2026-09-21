"""REST API used by the spreadsheet. Every route delegates to the services shared with the MCP adapter."""


import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .. import __version__
from ..config import settings
from ..db import Workspace, session
from ..errors import AppError, NotFound, ValidationFailed
from ..importer import bounded_sample
from ..services import datasets as ds_service
from ..services import jobs as job_service
from ..services import plans as plan_service
from ..services import results as result_service
from ..services import samples as sample_service
from ..services import workspaces
from ..storage import dataset_base_parquet, dataset_error_report
from .deps import current_workspace, scoped

log = logging.getLogger("semantic_sheets.api")


def create_app(*, mount_mcp: bool = True, serve_web: bool = True) -> FastAPI:
    cfg = settings()
    mcp_app = None
    mcp_server = None
    if mount_mcp:
        from ..mcp_server import build_mcp_server

        mcp_server = build_mcp_server()
        mcp_app = mcp_server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                                 transport_security=None)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ws, key = workspaces.ensure_default_workspace()
        if key:
            log.info("workspace %s key: %s", ws.id, key)
        stop = None
        if cfg.inline_worker:
            from ..worker import start_inline_worker

            stop = start_inline_worker()
        if mcp_server is not None:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield
        if stop is not None:
            stop.set()

    app = FastAPI(title="Semantic Sheets", version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in cfg.cors_origins.split(",") if o.strip()],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-Id"])

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict(),
                                                                   "request_id": getattr(request.state, "request_id", None)})

    api = FastAPI(title="Semantic Sheets API")
    api.add_exception_handler(AppError, app_error)

    # ---- workspace -------------------------------------------------------------------------------------
    @api.get("/workspace")
    def get_workspace(ws: Workspace = Depends(current_workspace)):
        with session() as s:
            w = s.get(Workspace, ws.id)
            return {"workspace_id": w.id, "name": w.name, "scopes": w.scopes, "budget_usd": w.budget_usd,
                    "spent_usd": w.spent_usd, "reserved_usd": w.reserved_usd, "model": cfg.jev_model,
                    "planner_model": cfg.planner_model, "limits": {"max_rows_per_file": cfg.max_rows_per_file,
                    "max_file_bytes": cfg.max_file_bytes, "default_spend_target_usd": cfg.default_spend_target_usd,
                    "retention_days": cfg.retention_days}, "fake_jev": cfg.fake_jev}

    # ---- samples ---------------------------------------------------------------------------------------
    @api.get("/samples")
    def list_samples(ws: Workspace = Depends(current_workspace)):
        return {"samples": sample_service.list_samples()}

    @api.post("/samples/{name}/import")
    def import_sample(name: str, ws: Workspace = Depends(scoped("import"))):
        ds = sample_service.import_sample(ws.id, name)
        return ds_service.describe(ws.id, ds.id)

    # ---- uploads & datasets ----------------------------------------------------------------------------
    class PrepareUpload(BaseModel):
        filename: str = Field(min_length=1, max_length=500)

    @api.post("/uploads/prepare")
    def prepare_upload(body: PrepareUpload, ws: Workspace = Depends(scoped("import"))):
        up = ds_service.prepare_upload(ws.id, body.filename)
        return {"upload_id": up.id, "upload_path": f"/api/v1/uploads/{up.id}", "method": "PUT",
                "expires_at": up.expires_at.isoformat(), "max_bytes": cfg.max_file_bytes}

    @api.put("/uploads/{upload_id}")
    async def put_upload(upload_id: str, request: Request, ws: Workspace = Depends(scoped("import"))):
        import tempfile

        tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > cfg.max_file_bytes:
                raise ValidationFailed(f"upload exceeds {cfg.max_file_bytes} bytes", code="file_too_large", path="upload")
            tmp.write(chunk)
        tmp.seek(0)
        up = await asyncio.to_thread(ds_service.write_upload_stream, ws.id, upload_id, tmp)
        return {"upload_id": up.id, "bytes": up.bytes, "status": up.status}

    @api.post("/uploads")
    async def multipart_upload(file: UploadFile = File(...), ws: Workspace = Depends(scoped("import"))):
        up = ds_service.prepare_upload(ws.id, file.filename or "upload.csv")
        saved = await asyncio.to_thread(ds_service.write_upload_stream, ws.id, up.id, file.file)
        return {"upload_id": saved.id, "bytes": saved.bytes, "status": saved.status}

    class PreviewBody(BaseModel):
        upload_id: str
        options: dict[str, Any] | None = None

    @api.post("/uploads/preview")
    def preview_upload(body: PreviewBody, ws: Workspace = Depends(scoped("import"))):
        return ds_service.preview_upload(ws.id, body.upload_id, body.options)

    class ImportBody(BaseModel):
        upload_id: str
        name: str | None = None
        options: dict[str, Any] | None = None
        wait: bool = False

    @api.post("/datasets/import")
    async def import_dataset(body: ImportBody, ws: Workspace = Depends(scoped("import"))):
        ds = await asyncio.to_thread(ds_service.start_import, ws.id, body.upload_id, body.name, body.options, wait=body.wait)
        return ds_service.describe(ws.id, ds.id) if ds.status != "importing" else {"dataset_id": ds.id, "status": ds.status, "name": ds.name}

    @api.get("/datasets")
    def list_datasets(cursor: str | None = None, limit: int = Query(25, ge=1, le=100), ws: Workspace = Depends(current_workspace)):
        rows, nxt = ds_service.list_datasets(ws.id, cursor, limit)
        return {"datasets": [{"dataset_id": d.id, "name": d.name, "status": d.status, "row_count": d.row_count,
                              "column_count": len(d.schema_json or []), "created_at": d.created_at.isoformat(),
                              "current_version_id": d.current_version_id} for d in rows], "next_cursor": nxt}

    @api.get("/datasets/{dataset_id}")
    def describe_dataset(dataset_id: str, sample: bool = False, sample_rows: int = 20, ws: Workspace = Depends(current_workspace)):
        return ds_service.describe(ws.id, dataset_id, sample=sample, sample_rows=sample_rows)

    @api.get("/datasets/{dataset_id}/rows")
    def dataset_rows(dataset_id: str, start: int = 0, limit: int = Query(256, ge=1, le=256), columns: str | None = None,
                     version_id: str | None = None, max_bytes: int = Query(262144, ge=1024, le=262144),
                     ws: Workspace = Depends(current_workspace)):
        cols = [c for c in columns.split(",") if c] if columns else None
        return ds_service.rows_page(ws.id, dataset_id, version_id, start, limit, cols, max_bytes)

    @api.get("/datasets/{dataset_id}/cell")
    def dataset_cell(dataset_id: str, row_id: int, column: str, version_id: str | None = None, ws: Workspace = Depends(current_workspace)):
        return ds_service.cell(ws.id, dataset_id, version_id, row_id, column)

    @api.get("/datasets/{dataset_id}/import-errors")
    def import_errors(dataset_id: str, ws: Workspace = Depends(current_workspace)):
        ds_service.get_dataset_any(dataset_id)
        p = dataset_error_report(dataset_id)
        if not p.exists():
            raise NotFound("no error report for this dataset", code="no_error_report")
        return FileResponse(p, media_type="text/csv", filename=f"{dataset_id}_import_errors.csv")

    class PatchBody(BaseModel):
        version_id: str | None = None
        corrections: list[dict[str, Any]]
        provenance: dict[str, Any] | None = None

    @api.post("/datasets/{dataset_id}/patch")
    def patch_dataset(dataset_id: str, body: PatchBody, ws: Workspace = Depends(scoped("run"))):
        v = ds_service.patch(ws.id, dataset_id, body.version_id, body.corrections, body.provenance)
        return {"dataset_id": dataset_id, "version_id": v.id, "number": v.number, "parent_id": v.parent_id}

    class SetVersion(BaseModel):
        version_id: str

    @api.post("/datasets/{dataset_id}/current-version")
    def set_version(dataset_id: str, body: SetVersion, ws: Workspace = Depends(scoped("run"))):
        d = ds_service.set_current_version(ws.id, dataset_id, body.version_id)
        return {"dataset_id": d.id, "current_version_id": d.current_version_id}

    @api.delete("/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str, ws: Workspace = Depends(scoped("delete"))):
        return ds_service.delete(ws.id, dataset_id)

    @api.get("/datasets/{dataset_id}/jobs")
    def dataset_jobs(dataset_id: str, ws: Workspace = Depends(current_workspace)):
        return {"jobs": [job_service.describe(j) for j in job_service.list_jobs(ws.id, dataset_id)]}

    # ---- plans -----------------------------------------------------------------------------------------
    class CompileBody(BaseModel):
        dataset_id: str
        request: str = Field(min_length=2, max_length=4000)
        current_plan: dict[str, Any] | None = None
        planning_budget_tokens: int = Field(12000, ge=1000, le=40000)

    @api.post("/plans/compile")
    async def compile_plan(body: CompileBody, ws: Workspace = Depends(scoped("run"))):
        return await asyncio.to_thread(plan_service.compile_request, ws.id, body.dataset_id, body.request,
                                       body.current_plan, body.planning_budget_tokens)

    class ValidateBody(BaseModel):
        plan: dict[str, Any]

    @api.post("/plans/validate")
    def validate_plan(body: ValidateBody, ws: Workspace = Depends(current_workspace)):
        compiled = plan_service.validate(ws.id, body.plan)
        pv = plan_service.store(ws.id, compiled)
        return plan_service.describe(compiled, pv)

    @api.get("/plans/{plan_version_id}")
    def get_plan(plan_version_id: str, ws: Workspace = Depends(current_workspace)):
        pv = plan_service.get(ws.id, plan_version_id)
        compiled = plan_service.validate(ws.id, pv.plan_json)
        return plan_service.describe(compiled, pv)

    # ---- jobs ------------------------------------------------------------------------------------------
    class SubmitBody(BaseModel):
        plan: dict[str, Any] | None = None
        plan_hash: str | None = None
        plan_version_id: str | None = None
        limits: dict[str, Any] | None = None
        idempotency_key: str | None = None

    @api.post("/jobs")
    def submit_job(body: SubmitBody, ws: Workspace = Depends(scoped("run"))):
        job, created = job_service.submit(ws.id, plan=body.plan, plan_hash=body.plan_hash, plan_version_id=body.plan_version_id,
                                          limits=body.limits, idempotency_key=body.idempotency_key)
        d = job_service.describe(job)
        d["created"] = created
        return d

    @api.get("/jobs/{job_id}")
    def get_job(job_id: str, ws: Workspace = Depends(current_workspace)):
        return job_service.describe(job_service.get(ws.id, job_id))

    @api.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, ws: Workspace = Depends(scoped("run"))):
        return job_service.describe(job_service.cancel(ws.id, job_id))

    @api.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int = 0, ws: Workspace = Depends(current_workspace)):
        job_service.get(ws.id, job_id)

        async def gen():
            seq = after
            idle = 0
            while True:
                if await request.is_disconnected():
                    return
                evs = await asyncio.to_thread(job_service.events, job_id, seq)
                for ev in evs:
                    seq = ev.seq
                    yield {"id": str(ev.seq), "event": ev.kind, "data": json.dumps({"seq": ev.seq, "kind": ev.kind, **ev.payload_json})}
                    if ev.kind == "finished":
                        return
                idle += 1
                if idle % 40 == 0:
                    yield {"event": "ping", "data": "{}"}
                await asyncio.sleep(0.25)

        return EventSourceResponse(gen())

    # ---- results ---------------------------------------------------------------------------------------
    @api.get("/results/{rv_id}")
    def result_columns(rv_id: str, ws: Workspace = Depends(current_workspace)):
        return result_service.columns_of(ws.id, rv_id)

    @api.post("/results/{rv_id}/query")
    def result_query(rv_id: str, spec: dict[str, Any] = Body(default={}), ws: Workspace = Depends(current_workspace)):
        return result_service.query(ws.id, rv_id, spec, max_rows=cfg.web_max_rows, max_bytes=cfg.web_max_bytes)

    @api.get("/results/{rv_id}/cell")
    def result_cell(rv_id: str, row_id: int, column: str, step: str | None = None, ws: Workspace = Depends(current_workspace)):
        return result_service.cell(ws.id, rv_id, row_id, column, step)

    @api.get("/results/{rv_id}/rows/{row_id}")
    def result_row(rv_id: str, row_id: int, ws: Workspace = Depends(current_workspace)):
        return result_service.row_detail(ws.id, rv_id, row_id)

    @api.get("/results/{rv_id}/vectors")
    def result_vectors(rv_id: str, columns: str, ws: Workspace = Depends(current_workspace)):
        cols = [c for c in columns.split(",") if c]
        if not cols or len(cols) > 20:
            raise ValidationFailed("provide 1..20 columns", code="invalid_columns", path="columns")
        return result_service.vectors(ws.id, rv_id, cols)

    @api.get("/results/{rv_id}/versions")
    def result_versions(rv_id: str, ws: Workspace = Depends(current_workspace)):
        rv = result_service.get_result_version(ws.id, rv_id)
        return {"job_id": rv.job_id, "versions": result_service.list_versions(ws.id, rv.job_id)}

    class ResultPatch(BaseModel):
        corrections: list[dict[str, Any]]
        provenance: dict[str, Any] | None = None

    @api.post("/results/{rv_id}/patch")
    def result_patch(rv_id: str, body: ResultPatch, ws: Workspace = Depends(scoped("run"))):
        nv = result_service.patch(ws.id, rv_id, body.corrections, body.provenance)
        return {"result_version_id": nv.id, "number": nv.number, "parent_id": nv.parent_id}

    # ---- exports ---------------------------------------------------------------------------------------
    class ExportBody(BaseModel):
        result_version_id: str
        format: str = "csv"
        raw: bool = False
        columns: list[str] | None = None
        where: dict[str, Any] | None = None
        order_by: list[dict[str, str]] | None = None
        scope: dict[str, Any] | None = None

    @api.post("/exports")
    async def create_export(body: ExportBody, ws: Workspace = Depends(scoped("export"))):
        e = await asyncio.to_thread(result_service.export, ws.id, body.result_version_id, body.format, raw=body.raw,
                                    columns=body.columns, where=body.where, order_by=body.order_by, scope=body.scope)
        return result_service.describe_export(e)

    @api.get("/exports/{export_id}")
    def get_export(export_id: str, ws: Workspace = Depends(current_workspace)):
        return result_service.describe_export(result_service.get_export(ws.id, export_id))

    @api.get("/exports/{export_id}/download")
    def download_export(export_id: str, token: str | None = None, ws_hdr: str | None = None,
                        authorization: str | None = None):
        # Downloads may be opened by a browser tab; accept ?token= as well as the bearer header.
        from fastapi import Header  # noqa: F401 - documentation only

        return _download(export_id, token)

    def _download(export_id: str, token: str | None):
        ws = workspaces.authenticate(token) if token else None
        if ws is None:
            raise ValidationFailed("pass ?token=<workspace key>", code="unauthorized")
        e = result_service.get_export(ws.id, export_id)
        media = "text/csv" if e.format == "csv" else "application/octet-stream"
        return FileResponse(e.path, media_type=media, filename=f"{export_id}.{e.format}")

    @api.get("/exports/{export_id}/manifest")
    def export_manifest(export_id: str, ws: Workspace = Depends(current_workspace)):
        return result_service.get_export(ws.id, export_id).manifest_json

    app.mount("/api/v1", api)
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    @app.get("/healthz")
    def health():
        return {"ok": True, "version": __version__}

    if serve_web:
        dist = Path(__file__).resolve().parents[3] / "web" / "dist"
        if dist.exists():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
    return app


app = create_app()
