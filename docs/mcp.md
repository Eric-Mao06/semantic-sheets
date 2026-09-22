# MCP server

The same services that back the web app are exposed to agents as MCP tools over **Streamable HTTP** at
`POST /mcp` (stateless, JSON responses). This is what lets an assistant run the whole loop — inspect a table,
write or compile a plan, price it, run it, page through the result, export — without ever pulling the table into
its own context.

## Connecting

- URL: `http://localhost:8000/mcp` (or your deployment's origin + `/mcp`)
- Auth: `Authorization: Bearer <workspace token>` on every request (`demo-token` by default; also accepted as
  `X-Workspace-Token`)
- Headers MCP clients send anyway: `Accept: application/json, text/event-stream`, `Content-Type: application/json`

A Claude Desktop / Cursor style configuration:

```json
{
  "mcpServers": {
    "semantic-sheet": {
      "url": "http://localhost:8000/mcp",
      "headers": {"Authorization": "Bearer demo-token"}
    }
  }
}
```

Raw JSON-RPC with curl:

```bash
curl -s http://localhost:8000/mcp \
  -H 'Authorization: Bearer demo-token' -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"datasets_list","arguments":{}}}'
```

## Envelope

Every tool returns the same structure so an agent can handle results uniformly:

```json
{"request_id": "…", "data": { … }, "warnings": ["…"], "next_cursor": null, "error": null}
```

Errors are returned *inside* the envelope (not as JSON-RPC errors) with `error: {code, message, status, details}`.
`details.issues` on validation failures is a list of `{path, code, message, fix}`.

## Tools

The server's own instructions to the model:

> Workflow: `datasets_list`/`datasets_describe` → (`plans_compile` or write a typed plan) → `plans_validate` (free,
> returns `plan_hash` + estimate) → `jobs_submit` (paid Jev inference within limits) → `jobs_get` (poll) →
> `results_query` (bounded pages / aggregates) → `results_export` (file handle). Never loop over rows; ask for
> aggregates or small pages. Only `jobs_submit` spends money on Jev; `plans_compile` spends planner tokens.

| Tool | Cost | What it does |
|---|---|---|
| `datasets_list(cursor?, limit=50)` | free | Datasets in the workspace with row/column counts. |
| `datasets_describe(dataset_id, version_id?, sample=false, sample_rows=10)` | free | Schema (≤ 100 columns with types and statistics), versions, import report, optional sample (≤ 20 rows / 8 KiB). |
| `uploads_prepare(filename?)` | free | Returns an `upload_url`; PUT the raw bytes there with the same bearer token. CSV content never goes into tool arguments. |
| `datasets_import(upload_id, name, options?)` | free | Import a completed upload. `options`: `delimiter`, `header`, `permissive`, `row_limit`. |
| `datasets_patch(dataset_id, patches, version_id?)` | free | Up to 1,000 cell corrections → new dataset version. |
| `datasets_delete(dataset_id)` | free | Delete a dataset, its results, caches and exports; cancels dependent jobs. |
| `plans_compile(dataset_id, prompt, version_id?, previous_plan?)` | planner tokens | Plain language → validated plan with estimate. Skip it if you can write the plan yourself. |
| `plans_validate(plan)` | free | Validate a typed plan; returns `plan_hash`, estimate (rows, requests, tokens, USD, cache hits, quota floor), warnings, output columns and review views. |
| `jobs_submit(plan_hash, idempotency_key, limits?)` | **Jev spend** | Run a validated plan. Same key + same payload → same job; same key + different payload → `idempotency_conflict`. |
| `jobs_get(job_id)` | free | State, per-stage progress (succeeded / uncertain / flagged / missing / failed / pending / skipped), usage, `result_version_id`, `suggested_poll_seconds`. |
| `jobs_cancel(job_id)` | free | Stop scheduling; committed partial results stay queryable. Closing the connection does **not** cancel a job. |
| `results_query(result_version_id, step?, columns?, where?, sort?, start=0, limit=20, max_bytes?, aggregate?, row_ids?)` | free | Page (≤ 50 rows / 16 KiB), filter (same expression tree as plans), sort, or aggregate (`{group_by, metrics}`) any step. `<filter>__review` returns the review rows of a filter. Long cells are truncated with `_truncated` markers. |
| `results_provenance(result_version_id, step, row_id)` | free | Raw Jev answer, interpreted values and overrides for one row. |
| `results_patch(result_version_id, overrides, note?)` | free | Manual corrections (`[{row_id, column: "<q>.value", value, reason}]`) → new result version. Raw model output is untouched. |
| `results_export(result_version_id, format="csv", step?, raw=false, columns?)` | free | CSV (formula-escaped unless `raw`) or Parquet plus a manifest. Returns a download handle, never the file. |

Files come and go through the HTTP API with the same bearer token: `PUT <upload_url>` for input,
`GET /api/exports/<id>/download` and `GET /api/exports/<id>/manifest` for output.

## Partial results

Jobs can end `partial` (deadline, budget, provider failures, row cap). Everything committed so far is queryable:
`results_query` pages carry `provisional`, `count_status` (`complete` | `partial`) and `warnings`, and the export
manifest records `complete`, `pending_by_status_column` and the job's `terminal_reason`. Rows whose semantic input
is still `pending` are excluded from filters and listed in the filter's review view.

## Implementation notes

- `server/semsheet/mcp_server.py` holds no business logic: each tool resolves the workspace from the bearer
  token, calls one `Services` method and wraps the result. Adding a tool is a service method plus a 3-line
  decorator.
- The server runs `stateless_http=True`; clients that send `mcp-session-id` work, clients that do not also work.
- The FastAPI app accepts `/mcp` without a trailing slash (`_McpPathMiddleware`), which is what most clients send.
- DNS-rebinding protection is off so the endpoint can sit behind arbitrary hostnames; put it behind TLS and a real
  token in production.
