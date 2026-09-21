"""Remote MCP server (Streamable HTTP) exposing the same services as the web API.

Every tool operates inside the authenticated workspace (bearer token). Tool handlers contain no business logic:
they resolve the workspace, call a service, and wrap the result in a stable envelope
(request_id, data, warnings, next_cursor). Raw table data is never returned beyond the bounded query pages."""
from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .db import get_db
from .services import ServiceError, Services, Workspace

_services: Services | None = None


def services() -> Services:
    global _services
    if _services is None:
        _services = Services(get_db())
    return _services


class Envelope(BaseModel):
    request_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    next_cursor: str | None = None
    error: dict[str, Any] | None = None


def _ws(ctx: Context) -> Workspace:
    headers = ctx.headers or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-workspace-token")
    return services().workspace_from_token(token)


def _ok(data: dict[str, Any], warnings: list[str] | None = None, next_cursor: str | None = None) -> Envelope:
    return Envelope(request_id=uuid.uuid4().hex, data=data, warnings=warnings or [], next_cursor=next_cursor)


def _run(ctx: Context, fn, *args: Any, **kwargs: Any) -> Envelope:
    try:
        ws = _ws(ctx)
        result = fn(ws, *args, **kwargs)
    except ServiceError as e:
        return Envelope(request_id=uuid.uuid4().hex, data={}, warnings=[], error=e.to_dict() | {"status": e.status})
    warnings = []
    next_cursor = None
    if isinstance(result, dict):
        warnings = list(result.pop("warnings", []) or [])
        next_cursor = result.pop("next_cursor", None)
    return _ok(result if isinstance(result, dict) else {"items": result}, warnings, next_cursor)


mcp = MCPServer(
    name="semantic-spreadsheet",
    version="0.1.0",
    instructions=(
        "Semantic spreadsheet over uploaded tables. Workflow: datasets_list/datasets_describe -> (plans_compile or write a typed plan) -> "
        "plans_validate (free, returns plan_hash + estimate) -> jobs_submit (paid Jev inference within limits) -> jobs_get (poll) -> "
        "results_query (bounded pages / aggregates) -> results_export (file handle). Never loop over rows; ask for aggregates or small pages. "
        "Costs: only jobs_submit spends money (Jev at $0.042 per million input tokens; plans_compile spends frontier tokens). "
        "Partial results: jobs can end 'partial' (deadline, budget, provider failures); result pages carry count_status and provisional flags."
    ),
)


@mcp.tool(description="List accessible datasets in the workspace with bounded pagination and brief metadata. Free.")
def datasets_list(ctx: Context, cursor: str | None = None, limit: int = 50) -> Envelope:
    return _run(ctx, services().datasets_list, cursor, limit)


@mcp.tool(description="Schema (up to 100 columns), row count, versions, import quality statistics and an optional bounded sample (max 20 rows / 8 KiB). Free.")
def datasets_describe(ctx: Context, dataset_id: str, version_id: str | None = None, sample: bool = False, sample_rows: int = 10) -> Envelope:
    return _run(ctx, services().datasets_describe, dataset_id, version_id, sample, sample_rows)


@mcp.tool(description="Issue a short-lived upload target (PUT raw bytes to upload_url with the same bearer token). Do not put CSV content in tool arguments.")
def uploads_prepare(ctx: Context, filename: str | None = None) -> Envelope:
    return _run(ctx, services().uploads_prepare, filename)


@mcp.tool(description="Import a completed upload as a dataset. options: delimiter, header, permissive (accept malformed rows with an error report), row_limit (explicit prefix). Returns dataset handle, schema and validation report.")
def datasets_import(ctx: Context, upload_id: str, name: str, options: dict[str, Any] | None = None) -> Envelope:
    return _run(ctx, services().datasets_import, upload_id, name, options)


@mcp.tool(description="Apply bounded cell corrections to base data (patches: [{row_id, column, value}], max 1000). Returns a new dataset version; the source snapshot is unchanged.")
def datasets_patch(ctx: Context, dataset_id: str, patches: list[dict[str, Any]], version_id: str | None = None) -> Envelope:
    return _run(ctx, services().datasets_patch, dataset_id, version_id, patches)


@mcp.tool(description="Optional: convert a natural-language request into a typed plan using the frontier planner with bounded context (schema + <=20 sample rows). Costs planner tokens; no Jev inference. Callers that already have a typed plan should skip this and call plans_validate.")
def plans_compile(ctx: Context, dataset_id: str, prompt: str, version_id: str | None = None, previous_plan: dict[str, Any] | None = None) -> Envelope:
    return _run(ctx, services().plans_compile, dataset_id, version_id, prompt, previous_plan)


@mcp.tool(description="Validate a typed plan: returns plan_hash, work estimate (rows, requests, tokens, USD, quota floor), warnings, output columns and review views. No paid inference.")
def plans_validate(ctx: Context, plan: dict[str, Any]) -> Envelope:
    return _run(ctx, services().plans_validate, plan, None, True)


@mcp.tool(description="Run a validated plan (by plan_hash) against frozen versions. limits: max_source_rows (default 10000), max_provider_requests (12000), spend_target_usd (0.50), deadline_seconds (600), rows_per_request. Retrying with the same idempotency_key and payload returns the same job; a different payload fails. Spends money on Jev inference; stops as 'partial' at the budget/deadline.")
def jobs_submit(ctx: Context, plan_hash: str, idempotency_key: str, limits: dict[str, Any] | None = None) -> Envelope:
    return _run(ctx, services().jobs_submit, plan_hash, None, limits, idempotency_key)


@mcp.tool(description="Job state, progress counts (succeeded/uncertain/missing/failed/pending/skipped per stage; cache hits and inference attempts separately), provider usage, result_version_id and a suggested poll interval. Free.")
def jobs_get(ctx: Context, job_id: str) -> Envelope:
    return _run(ctx, services().jobs_get, job_id)


@mcp.tool(description="Stop scheduling further work and finalize committed partial results. In-flight provider requests may still incur cost. Closing the MCP connection does NOT cancel a job; this does.")
def jobs_cancel(ctx: Context, job_id: str) -> Envelope:
    return _run(ctx, services().jobs_cancel, job_id)


@mcp.tool(description="Project, filter (constrained expression tree), sort, page (max 50 rows / 16 KiB) or aggregate ({group_by, metrics}) a result step. step defaults to the plan output; '<filter_id>__review' returns the uncertain/missing rows of a filter. Long cells are truncated with _truncated markers. Free.")
def results_query(ctx: Context, result_version_id: str, step: str | None = None, columns: list[str] | None = None, where: dict[str, Any] | None = None,
                  sort: list[dict[str, Any]] | None = None, start: int = 0, limit: int = 20, max_bytes: int | None = None, aggregate: dict[str, Any] | None = None,
                  row_ids: list[int] | None = None) -> Envelope:
    return _run(ctx, services().results_query, result_version_id, step, columns, where, sort, start, limit, max_bytes, None, aggregate, row_ids, "mcp")


@mcp.tool(description="Raw provider answers, interpreted values and override records for one row of a semantic step (provenance). Free.")
def results_provenance(ctx: Context, result_version_id: str, step: str, row_id: int) -> Envelope:
    return _run(ctx, services().results_provenance, result_version_id, step, row_id)


@mcp.tool(description="Manual corrections to semantic value columns (overrides: [{row_id, column: '<question>.value', value, reason}]). Creates a new result version with provenance; raw model output is never altered.")
def results_patch(ctx: Context, result_version_id: str, overrides: list[dict[str, Any]], note: str | None = None) -> Envelope:
    return _run(ctx, services().results_patch, result_version_id, overrides, note, "mcp")


@mcp.tool(description="Create a CSV (formula-escaped by default; raw=true disables) or Parquet artifact of a result step with a completeness manifest. Returns a download handle, size, row count and status; the file itself never enters the tool result.")
def results_export(ctx: Context, result_version_id: str, format: str = "csv", step: str | None = None, raw: bool = False, columns: list[str] | None = None) -> Envelope:
    return _run(ctx, services().results_export, result_version_id, format, step, raw, columns)


@mcp.tool(description="Delete an authorized dataset and its dependent results, caches and download links under the published retention policy. Stops dependent jobs.")
def datasets_delete(ctx: Context, dataset_id: str) -> Envelope:
    return _run(ctx, services().datasets_delete, dataset_id)


def build_mcp_app():
    return mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
