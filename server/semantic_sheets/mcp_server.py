"""Remote MCP server (Streamable HTTP). Thin adapters over the same services the REST API uses.

Every tool operates inside the authenticated workspace (Authorization: Bearer <workspace key>). Results are
bounded: at most 50 rows / 16 KiB per query, schema summaries up to 100 columns, samples off by default.
Raw CSV never travels through tool arguments; exports return handles, never file contents.
"""


import uuid
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import settings
from .errors import AppError, Unauthorized
from .services import datasets as ds_service
from .services import jobs as job_service
from .services import plans as plan_service
from .services import results as result_service
from .services.workspaces import authenticate, require_scope


def _workspace(ctx: Context, scope: str):
    headers = ctx.headers or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-api-key")
    if not token:
        raise Unauthorized("Authorization: Bearer <workspace key> is required")
    ws = authenticate(token)
    require_scope(ws, scope)
    return ws


def _envelope(data: Any, warnings: list[str] | None = None, next_cursor: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"request_id": uuid.uuid4().hex[:16], "data": data, "warnings": warnings or []}
    if next_cursor is not None:
        out["next_cursor"] = next_cursor
    return out


def _error(e: AppError) -> dict[str, Any]:
    return {"request_id": uuid.uuid4().hex[:16], "error": e.to_dict(), "data": None, "warnings": []}


def build_mcp_server() -> MCPServer:
    cfg = settings()
    server = MCPServer(
        "semantic-sheets",
        title="Semantic Sheets",
        version="0.1.0",
        instructions=(
            "Bulk semantic operations over server-side tables. Describe a dataset, validate a typed plan "
            "(plans_validate is free), submit one job (jobs_submit reserves budget; jobs_get suggests a polling "
            "interval), then query a small aggregate or selected rows (results_query, max 50 rows / 16 KiB) or export "
            "the full result (results_export returns a download handle). Never loop over rows; the server does not "
            "ask the client model to classify rows. Files are uploaded by the host through uploads_prepare, not "
            "serialized into tool arguments."),
    )

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    mutating = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read_only, description="List accessible datasets with bounded pagination and brief metadata.")
    def datasets_list(ctx: Context, cursor: str | None = None,
                      limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "read")
            rows, nxt = ds_service.list_datasets(ws.id, cursor, limit)
            return _envelope([{"dataset_id": d.id, "name": d.name, "status": d.status, "row_count": d.row_count,
                               "column_count": len(d.schema_json or []), "current_version_id": d.current_version_id,
                               "created_at": d.created_at.isoformat()} for d in rows], next_cursor=nxt)
        except AppError as e:
            return _error(e)

    @server.tool(annotations=read_only, description=(
        "Return schema (up to 100 columns per page), row count, versions, quality statistics, and an optional bounded "
        "sample (at most 20 rows and 8 KiB). Long cells are shortened with an explicit marker."))
    def datasets_describe(ctx: Context, dataset_id: str, sample: bool = False,
                          sample_rows: Annotated[int, Field(ge=1, le=20)] = 10,
                          schema_page: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "read")
            d = ds_service.describe(ws.id, dataset_id, sample=sample, sample_rows=sample_rows)
            schema = d["schema"]
            d["schema"] = schema[schema_page * 100:(schema_page + 1) * 100]
            d["schema_pages"] = (len(schema) + 99) // 100
            for c in d["schema"]:
                c.pop("examples", None) if not sample else None
            return _envelope(d, warnings=(d.get("quality") or {}).get("warnings", []))
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Issue a short-lived upload target. The host PUTs the raw CSV/TSV bytes (optionally gzip) to upload_url with the "
        "same Authorization header, then calls datasets_import with the upload_id. Never pass file contents as arguments."))
    def uploads_prepare(ctx: Context, filename: str) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "import")
            up = ds_service.prepare_upload(ws.id, filename)
            return _envelope({"upload_id": up.id, "upload_url": f"{cfg.public_base_url}/api/v1/uploads/{up.id}", "method": "PUT",
                              "expires_at": up.expires_at.isoformat(), "max_bytes": cfg.max_file_bytes,
                              "retention_days": cfg.retention_days})
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Import a completed upload with explicit parse options (delimiter, header, all_text, permissive, max_rows). "
        "Returns the dataset handle and import status; poll datasets_describe until status is 'ready' or 'failed'. "
        "Malformed rows fail the import unless permissive=true; the error report is downloadable."))
    def datasets_import(ctx: Context, upload_id: str, name: str | None = None, options: dict[str, Any] | None = None,
                        wait: bool = True) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "import")
            ds = ds_service.start_import(ws.id, upload_id, name, options, wait=wait)
            d = ds_service.describe(ws.id, ds.id) if ds.status == "ready" else {"dataset_id": ds.id, "status": ds.status, "error": ds.error}
            return _envelope(d)
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Apply bounded row corrections (max 1000) to source cells and return a new dataset version. The source snapshot "
        "is unchanged; corrections are override records with provenance."))
    def datasets_patch(ctx: Context, dataset_id: str, corrections: list[dict[str, Any]], version_id: str | None = None,
                       provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "run")
            v = ds_service.patch(ws.id, dataset_id, version_id, corrections, provenance)
            return _envelope({"dataset_id": dataset_id, "version_id": v.id, "number": v.number, "parent_id": v.parent_id})
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Optional language-to-plan conversion with bounded context (schema + <=20 sample rows) and an explicit planning "
        "budget in output tokens. Uses a frontier planner model and costs planner tokens; callers that already have a "
        "typed plan should call plans_validate instead. Returns the validated plan, its hash, estimate and warnings."))
    def plans_compile(ctx: Context, dataset_id: str, request: str, current_plan: dict[str, Any] | None = None,
                      planning_budget_tokens: Annotated[int, Field(ge=1000, le=40000)] = 12000) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "run")
            return _envelope(plan_service.compile_request(ws.id, dataset_id, request, current_plan, planning_budget_tokens))
        except AppError as e:
            return _error(e)

    @server.tool(annotations=read_only, description=(
        "Validate a typed plan and return its plan_hash, per-step output columns, work estimate (rows, provider "
        "requests, input tokens, cost), warnings, and required scopes. Makes no paid inference."))
    def plans_validate(ctx: Context, plan: dict[str, Any]) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "read")
            compiled = plan_service.validate(ws.id, plan)
            pv = plan_service.store(ws.id, compiled)
            return _envelope(plan_service.describe(compiled, pv), warnings=compiled.warnings)
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Run a validated plan (by plan_hash or inline plan) against frozen input versions with limits "
        "{max_source_rows, max_provider_requests, spend_target_usd, deadline_seconds} and an idempotency_key. "
        "Reserves spend before dispatch; the same key + payload returns the same job; a different payload conflicts. "
        "Semantic inference is billed by the provider; partial results remain queryable after cancellation, budget or deadline stops."))
    def jobs_submit(ctx: Context, plan_hash: str | None = None, plan: dict[str, Any] | None = None,
                    limits: dict[str, Any] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "run")
            job, created = job_service.submit(ws.id, plan=plan, plan_hash=plan_hash, limits=limits, idempotency_key=idempotency_key)
            d = job_service.describe(job)
            d["created"] = created
            return _envelope(d)
        except AppError as e:
            return _error(e)

    @server.tool(annotations=read_only, description=(
        "Return job state (queued|running|succeeded|partial|failed|cancelled), disjoint source-row counts "
        "(succeeded/failed/pending/skipped; uncertain is a subset of succeeded), provider usage, result_version_id, "
        "a bounded error summary and suggested_poll_seconds."))
    def jobs_get(ctx: Context, job_id: str) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "read")
            return _envelope(job_service.describe(job_service.get(ws.id, job_id)))
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Stop scheduling further work and finalize committed partial results. In-flight provider requests may still incur cost."))
    def jobs_cancel(ctx: Context, job_id: str) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "run")
            return _envelope(job_service.describe(job_service.cancel(ws.id, job_id)))
        except AppError as e:
            return _error(e)

    @server.tool(annotations=read_only, description=(
        "Project, filter, sort, inspect selected rows (row_ids), or aggregate a result through a constrained expression "
        "tree. spec = {scope?: 'output'|{step}|{review_of: <filter step>}, columns?, where?: EXPR, order_by?: [{column,direction}], "
        "start?, limit? (<=50), max_bytes? (<=16384), row_ids?: [...], aggregate?: {group_by, metrics:[{name, fn, column}]}}. "
        "Responses mark truncation, partial completion and the scan denominator. Use results_export for full data."))
    def results_query(ctx: Context, result_version_id: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "read")
            spec = dict(spec or {})
            return _envelope(result_service.query(ws.id, result_version_id, spec, max_rows=cfg.mcp_max_rows, max_bytes=cfg.mcp_max_bytes))
        except AppError as e:
            return _error(e)

    @server.tool(annotations=read_only, description=(
        "Fetch one cell's full value (for text that results_query truncated) from a result version."))
    def results_cell(ctx: Context, result_version_id: str, row_id: int, column: str) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "read")
            return _envelope(result_service.cell(ws.id, result_version_id, row_id, column))
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Correct result cells (max 1000). Creates a new result version; the original model output stays in the .raw columns."))
    def results_patch(ctx: Context, result_version_id: str, corrections: list[dict[str, Any]],
                      provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "run")
            nv = result_service.patch(ws.id, result_version_id, corrections, provenance)
            return _envelope({"result_version_id": nv.id, "number": nv.number, "parent_id": nv.parent_id})
        except AppError as e:
            return _error(e)

    @server.tool(annotations=mutating, description=(
        "Create a CSV or Parquet artifact of a result (optionally filtered/projected) and return a download handle, "
        "size, format, row count and a completeness manifest. CSV escapes formula-leading text unless raw=true. "
        "The file is never returned inline."))
    def results_export(ctx: Context, result_version_id: str, format: str = "csv", raw: bool = False,
                       columns: list[str] | None = None, where: dict[str, Any] | None = None,
                       scope: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "export")
            e = result_service.export(ws.id, result_version_id, format, raw=raw, columns=columns, where=where, scope=scope)
            d = result_service.describe_export(e)
            d["download_url"] = d["download_url"] + "?token=<workspace key>"
            return _envelope(d)
        except AppError as e:
            return _error(e)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
                 description=(
        f"Delete an authorized dataset and its dependent retained objects (jobs are cancelled, caches removed, download "
        f"links invalidated). Demo uploads are retained for {cfg.retention_days} days otherwise."))
    def datasets_delete(ctx: Context, dataset_id: str) -> dict[str, Any]:
        try:
            ws = _workspace(ctx, "delete")
            return _envelope(ds_service.delete(ws.id, dataset_id))
        except AppError as e:
            return _error(e)

    return server


def main() -> None:  # standalone streamable HTTP server (also mounted at /mcp by the API app)
    import uvicorn

    server = build_mcp_server()
    from mcp.server.transport_security import TransportSecuritySettings

    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    uvicorn.run(app, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
