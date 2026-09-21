# Semantic Sheets

A spreadsheet that turns plain language into reusable, typed operations over uploaded tables. The
dataset stays on the server. A frontier model (gpt-6-astra) sees only the schema, at most 20 sample rows
and the plan; Jev (TypeSafe's System One model, `jev-1.13.0`, pinned per job) answers the per-row
semantic questions; DuckDB does arithmetic, joins, sorting and aggregation. The same engine is exposed
as a remote MCP server so agents can delegate bulk table work without reading rows.

This is the MVP build described in `docs/DESIGN.md`.

## What works

| Area | Status |
| --- | --- |
| Import | CSV/TSV (optionally gzip), server-side sniff + full-file validation, stable `_row_id`, leading-zero identifiers kept as text, malformed rows rejected with a downloadable error report or skipped under `permissive`, explicit `max_rows` only (never silent truncation), 100k-row cap (configurable to 300k). |
| Plans | Typed, versioned plan graph: `semantic_annotate` (boolean / category / score questions), `filter`, `sort`, `project`, `derive`, `aggregate`, `join`, `dedupe`, `limit`, `semantic_match`. Constrained expression tree instead of SQL. Validation returns field paths, output columns, work estimate and warnings. |
| Planner | Language -> plan with gpt-6-astra (`reasoning.effort=high`, strict JSON), bounded context, one repair round through the validator. MCP callers can skip it and submit typed plans. |
| Execution | Separate API and worker processes (or an inline worker for dev). Chunked Jev requests with token-aware packing (10 rows/request by default, benchmarked below), per-row question isolation, retries with backoff and `retry-after`, shared request/token limiter, in-flight bounds, spend reservation before dispatch and reconciliation from provider usage, hard caps on rows/requests/spend/deadline, transactional chunk checkpoints, resume after a worker restart, cancellation with inspectable partial results. |
| Cache | Predictions keyed by tenant, model, prompt-layout version, question spec (thresholds excluded) and normalised input. Threshold, sort and weight edits make zero provider calls (tested). |
| Results | Result versions with completion metadata; bounded pages (256 rows / 256 KiB for the sheet, 50 rows / 16 KiB for MCP), review view for unknown/uncertain rows of a filter, exact aggregates with denominators, per-cell detail, full score vectors for local filtering, corrections as override records with provenance (new version; raw output immutable), CSV (formula-escaped by default) and Parquet export with a completeness manifest. |
| Matching | Exact blocking + idf-weighted lexical candidates (top-k per row, right table <= 5,000 rows), Jev verifies every pair, match / non_match / uncertain / no_candidates with candidate provenance. |
| Sheet | Glide Data Grid over a byte-bounded LRU block cache with request de-duplication and generation-based cancellation; Papa Parse preview in a worker while the file uploads; command bar; editable operation panel (instructions, labels, levels, thresholds, sort order, spend target); status strip (examined / remaining / errors / uncertain / spend / cache hits); inspector with raw probabilities; corrections and version history (undo = select an earlier version); review view; export. |
| MCP | Streamable HTTP at `/mcp` (official Python SDK 2.2): `datasets_list`, `datasets_describe`, `uploads_prepare`, `datasets_import`, `datasets_patch`, `plans_compile`, `plans_validate`, `jobs_submit`, `jobs_get`, `jobs_cancel`, `results_query`, `results_cell`, `results_patch`, `results_export`, `datasets_delete`. Structured envelopes (`request_id`, `data`, `warnings`, `next_cursor`), input and output schemas, idempotency keys, bounded responses, error envelopes with field paths. |
| Auth | Workspace bearer keys with read / run / import / export / delete scopes; object IDs grant no access. |

Not in this build (as scoped by the design): XLSX/Parquet import, embedding-based candidate retrieval,
pairwise top-k ranking, prose aggregation, web enrichment, user code.

## Quick start

```bash
cp .env.example .env            # fill in TYPESAFE_API_KEY, OPENAI_API_KEY, SS_DEV_WORKSPACE_KEY
make setup                      # python venv (uv) + npm install
make samples                    # downloads the public datasets, writes demo samples (~1 GB of downloads)
make build                      # builds the web app into web/dist (served by the API)
set -a; source .env; set +a
make dev                        # http://127.0.0.1:8000  (API + MCP at /mcp + inline worker + sheet)
```

Production layout: `make api` and one or more `make worker` processes against the same
`SS_DATABASE_URL` (SQLite by default; Postgres via `postgresql+psycopg://...`) and `SS_DATA_DIR`.

Offline / CI: `SS_FAKE_JEV=1` swaps in a deterministic stand-in for Jev (`make test`).

## Using the MCP server

```python
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

http = httpx2.AsyncClient(headers={"Authorization": "Bearer <workspace key>"})
async with streamable_http_client("http://127.0.0.1:8000/mcp/", http_client=http) as (r, w):
    async with ClientSession(r, w) as s:
        await s.initialize()
        ds = (await s.call_tool("datasets_list", {})).structured_content["data"][0]["dataset_id"]
        plan = {"plan_version": "1", "source": {"dataset_id": ds}, "steps": [
            {"id": "labels", "op": "semantic_annotate", "input": "source", "columns": ["text"], "questions": [
                {"name": "onboarding", "kind": "boolean", "instruction": "Does the text describe an onboarding problem?"},
                {"name": "severity", "kind": "score", "instruction": "Rate the impact described in the text.",
                 "levels": ["No stated disruption", "Work slowed", "Core workflow blocked", "Service unusable"]}]},
            {"id": "selected", "op": "filter", "input": "labels", "where": {"column": "onboarding.value", "operator": "eq", "value": True}},
            {"id": "ranked", "op": "sort", "input": "selected", "by": [{"column": "severity.score", "direction": "desc"}]}],
            "output": "ranked"}
        v = (await s.call_tool("plans_validate", {"plan": plan})).structured_content["data"]      # free: estimate + hash
        j = (await s.call_tool("jobs_submit", {"plan_hash": v["plan_hash"], "idempotency_key": "demo-1",
                                                "limits": {"spend_target_usd": 0.5, "max_source_rows": 10000}})).structured_content["data"]
        # poll jobs_get (suggested_poll_seconds), then:
        top = await s.call_tool("results_query", {"result_version_id": j["result_version_id"], "spec": {"limit": 20}})
        out = await s.call_tool("results_export", {"result_version_id": j["result_version_id"], "format": "csv"})
```

Files are uploaded by the host: `uploads_prepare` returns a `PUT` target; raw CSV never travels through
tool arguments, and exports come back as download handles with a manifest.

## Measurements (21 September 2026, shared demo quota, `jev-1.13.0`)

These are measurements of this build, not guarantees. Scripts: `scripts/benchmark_packing.py`,
`scripts/eval_bitext.py`.

Packing gate (300 Bitext rows, boolean + 12-label category, 8 in flight):

| rows / request | requests | rows/s | tokens/row | boolean decision agreement vs isolated | category agreement vs isolated |
| --- | --- | --- | --- | --- | --- |
| 1 | 300 | 33 | 481 | 100% | 100% |
| 5 | 60 | 109 | 288 | 100% | 91.7% |
| 10 | 30 | 214 | 262 | 100% | 90.7% |
| 20 | 15 | 286 | 250 | 100% | 91.3% |

Against ground truth (Bitext held-out `intent`, full 27-label taxonomy, 2,000 rows) packing did not
hurt: isolated rows 85.5% accuracy in 40 s ($0.060); 10 rows/request 89.3% in 6.4 s ($0.041). Restricting
to predictions with confidence >= 0.9 gives 97.9% precision at 72% coverage; >= 0.7 gives 94.2% at 88%.
Category answers are less stable than boolean ones under packing, so the default stays 10 rows/request
with `SS_JEV_PACK_ROWS=1` available per deployment.

Semantic match, WDC 80pair test split (4,500 labelled pairs, hard negatives): pair judgement precision /
recall 87.6% / 29.6% at p >= 0.85, 75.8% / 72.0% at p >= 0.5; 12.8% of pairs land in the uncertain
band. Lexical top-5 candidate recall on the offer tables is 66.6%, which is why the design keeps an
embedding index as the next retrieval step.

Throughput: 5,000 Bitext rows with two questions in 13.3 s (376 rows/s, $0.053); 3,080 BANKING77 rows in
~8 s. The 100,000-row MCP acceptance scenario (`scripts/mcp_acceptance.py`, Online Retail II, boolean +
category) succeeded in 82 s with 570 provider requests and $0.038 because repeated descriptions hit the
cache; the calling model saw 25 tool responses totalling 53 KB (about 13k tokens) and no raw rows. A
100k scan of unique long text is a 4-5 minute background job at this packing, quota permitting.

A recorded walkthrough with screenshots is in `docs/walkthrough/README.md`; `make walkthrough` re-records it.

## Layout

```
server/semantic_sheets/
  importer.py         CSV/TSV import and profiling (DuckDB)
  plan/               typed plan schema, expression compiler, validation + estimates
  engine/exact.py     plan graph -> DuckDB SQL over Parquet
  engine/executor.py  job runner: chunks, packing, cache, budgets, checkpoints, matching
  jev/                Jev client (retries, limiter), fake client, packing + interpretation
  services/           datasets, plans, jobs, results, workspaces, samples (shared by REST + MCP)
  api/app.py          REST API + SSE job events + static web app
  mcp_server.py       MCP tool catalog (Streamable HTTP)
  worker.py           claim/lease loop
web/src/              React + Glide Data Grid sheet, block cache, filter + preview workers
scripts/              sample preparation, packing benchmark, accuracy eval
docs/DESIGN.md        the design document this build follows
```
