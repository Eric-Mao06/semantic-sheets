# Architecture

Semantic Sheet splits every plain-language request over a table into two kinds of work and sends each to the
component that is good at it:

| Work | Who does it | Cost model |
|---|---|---|
| Understanding the request and turning it into a typed plan | A frontier model (the **planner**), once per request, seeing only the schema and ≤ 20 sample rows | One call, a few thousand tokens |
| Judging each row (is this a cancellation? which category? how severe?) | **Jev** (`jev-1.13.0`, TypeSafe's decision model), once per row and question | $0.042 per million input tokens, output free |
| Filtering, sorting, joining, arithmetic, grouping | **DuckDB** over Parquet, compiled from the plan | Free, exact |

The rest of the system exists to make that split safe to ship: bounded estimates before anything runs, restart-safe
jobs, auditable per-row provenance, calibrated thresholds, corrections that never overwrite model output, and one
service layer shared by the web UI and the MCP server.

```
                         ┌──────────────────────────────┐
  CSV upload ───────────▶│ importer.py                  │──▶ Parquet snapshot + schema + import report
                         └──────────────────────────────┘
                                                              ┌───────────────────────────┐
  "find customers who…" ─────────────────────────────────────▶│ planner.py (frontier model)│──▶ typed Plan (JSON)
                                                              └───────────────────────────┘
                         ┌──────────────────────────────┐
  Plan ─────────────────▶│ engine/exact.py              │──▶ DuckDB SQL (CTE per step) + estimate
                         │ services.plans_validate      │
                         └──────────────────────────────┘
                         ┌──────────────────────────────┐     ┌────────────────┐
  jobs_submit ──────────▶│ worker.py → engine/executor  │────▶│ engine/jev.py  │──▶ TypeSafe / OpenRouter
                         │ chunks, budget, cache, retry │◀────│ packets, limits│
                         │ engine/calibrate.py          │     └────────────────┘
                         └──────────────────────────────┘
                                   │ Parquet chunks per stage, joined onto the base table at query time
                                   ▼
                         ┌──────────────────────────────┐
  web UI / MCP ─────────▶│ services.results_*           │──▶ bounded pages, aggregates, provenance, exports
                         └──────────────────────────────┘
```

## Components

### `server/semsheet/`

| Module | Role |
|---|---|
| `api.py` | FastAPI routes for the web app. Thin adapters over `Services`. Mounts the MCP app at `/mcp` and the built frontend at `/`. |
| `mcp_server.py` | The same services as MCP tools (Streamable HTTP, stateless, JSON responses). Every tool returns a stable envelope: `request_id`, `data`, `warnings`, `next_cursor`, `error`. |
| `services.py` | All business logic: uploads, import, plan validation and estimation, job submission, paging, aggregates, provenance, overrides, versions, export. |
| `models.py` | The typed plan (`Plan`, steps, `Question`, expression tree) and job limits. Pydantic with `extra="forbid"` everywhere; see [plan-format.md](plan-format.md). |
| `planner.py` | Prompt and provider calls that turn a request into a plan. Supports the OpenAI Responses API and any OpenRouter model. |
| `importer.py` | CSV/TSV sniffing, strict parsing with an explicit column list, type detection that only promotes when 100% of values parse, stable `_row_id`, Parquet output, import report with rejects and coercions. |
| `engine/exact.py` | Compiles a plan into one DuckDB query: a CTE per step, semantic columns LEFT JOINed from derived Parquet, overrides applied on top, review views for filters. |
| `engine/jev.py` | Packs rows into multi-row Jev requests, token/request rate limits per route, retries with backoff and route failover, content-addressed cache keys, answer interpretation. |
| `engine/executor.py` | Runs the semantic stages of a job chunk by chunk with budget, deadline and cancellation guards; commits each chunk atomically; calibrates thresholds once a stage is fully scored. |
| `engine/calibrate.py` | Otsu's threshold on the observed score distribution, clamped, with a fixed-threshold fallback for tiny inputs. Versioned constants. |
| `worker.py` | Claims queued jobs (or stale running ones) and runs them. Several workers can share one store. |
| `db.py` | SQLite (WAL) schema and helpers: workspaces, datasets, versions, plans, jobs, chunks, events, prediction cache, overrides, exports, spend ledger. |
| `storage.py` | File layout under `data/` and the compile context builder (which Parquet files back which step). |
| `samples.py` | The demo datasets offered on the landing page. |
| `config.py` | Settings with environment overrides; see [configuration.md](configuration.md). |

### `web/`

React + TypeScript + Vite. `Landing` (samples, upload, recent datasets) → `Workbench` (grid, command bar,
operation panel, inspector, history). The grid pages through result versions via `data/ViewController.ts`; local
filter/sort over full column vectors runs in a Web Worker. See [`web/README.md`](../web/README.md).

### `benchmarks/`

The operators-vs-one-shot comparison and the calibration protocol. See [`benchmarks/README.md`](../benchmarks/README.md).

## Data flow in detail

### 1. Import

`importer.import_file` sniffs the delimiter (DuckDB's sniffer, cross-checked against the header line), reads the
header with the `csv` module, then parses the whole file with DuckDB using an **explicit column list** and
`strict_mode`. Malformed rows are captured in `reject_errors` and reported; the import fails unless `permissive`
is set. Types are detected on the full column (integer, double, date, timestamp, boolean, else text) and only
promoted when every non-null value parses, so identifiers with leading zeros stay text. Output: a ZSTD Parquet file
with a leading `_row_id` column in file order, a schema with per-column statistics, and the original bytes kept
next to it with a checksum.

Every import is dataset version 1. `datasets_patch` (bounded cell corrections) writes a new version; the source
snapshot is never modified.

### 2. Plan

`services.plans_compile` gives the planner the schema summary, up to 20 reservoir-sampled rows (byte-bounded), the
row count, other datasets in the workspace (for joins and matching), and optionally the previous plan and
validation feedback. The planner returns the plan as JSON. Validation runs up to three attempts, feeding the
issue list back each time.

`services.plans_validate` parses the plan, compiles it (`engine/exact.py`), runs `DESCRIBE` on every step so type
errors surface before any money is spent, and computes an estimate: rows per semantic stage, Jev requests, input
tokens (measured on a 120-row sample), cache hits (looked up on the same sample), cost, and the quota floor in
seconds. Plans are stored by content hash; `jobs_submit` refers to them by `plan_hash`.

### 3. Execute

The worker claims the job and runs each semantic step in order:

- **Chunks.** Input rows (bounded by `max_source_rows`) are split into chunks (`SEMSHEET_CHUNK_ROWS`, first chunk
  smaller so results appear quickly). Each committed chunk is a Parquet file `data/results/<rv>/<step>/chunk_NNNNN.parquet`
  recorded in `job_chunks`; a restarted worker skips committed chunks.
- **Cache.** For each (row payload, question) the executor computes a key over workspace, model, question body,
  normalised payload and prompt-layout/normalisation versions, and looks it up in `prediction_cache`. Hits never
  reach the provider and are counted separately from inference attempts.
- **Packets.** Misses are packed into requests of up to `rows_per_request` rows under the provider's token limits.
  A request carries the rows as shared `state` (`r00`, `r01`, …) and one question per (row, question) pair;
  cell contents are data inside a JSON record, never concatenated into instructions.
- **Guards between packets.** Cancellation, deadline, `max_provider_requests`, and spend: the estimated cost of
  a packet is reserved against `spend_target_usd` and the workspace budget before dispatch and settled from the
  provider's reported usage after. Hitting any guard stops scheduling, lets in-flight requests finish, commits
  what is done and ends the job as `partial` with a `terminal_reason`.
- **Routes.** With both `TYPESAFE_API_KEY` and `OPENROUTER_API_KEY` set, packets go to the least-loaded of two
  routes (TypeSafe direct, OpenRouter's decisions endpoint), each with its own request and token buckets; a
  retryable failure on one route is retried on the other.
- **Interpretation.** Each answer is stored raw (`<q>.raw`, the provider's JSON) and interpreted into
  `<q>.value`, `<q>.score`, `<q>.confidence` or `<q>.near`, and `<q>.status`.

### 4. Calibrate

Once every chunk of a stage is committed, `_calibrate_stage` reads the scores of each boolean question (and the
best-candidate probability of a match step) and places the cut with `calibrate.calibrate`:

- `mode: auto` (default): Otsu's threshold on a 100-bin histogram, clamped to [0.20, 0.80]; rows within ±0.10
  of the cut are flagged in `<q>.near` but keep their value. Fewer than 50 scored rows → the planner's thresholds.
- `mode: fixed`: classic three-way — ≥ `true_min` is true, ≤ `false_max` is false, in between is `uncertain`
  with a null value.

The rewrite is idempotent (derived from `.score`, never from the previous `.value`) and the calibration record
goes into the result manifest and a `stage_calibrated` event.

### 5. Query

`engine/exact.py` turns the plan into a single `WITH … SELECT` where each step is a CTE. A semantic step LEFT
JOINs its derived chunks onto the input by `_row_id`; rows without a committed answer read as status `pending`,
so partial results are queryable at any time and pages carry `provisional` / `count_status`. Filters that reference
semantic columns only pass rows whose status is `ok` or `override`; with `unknown_policy: separate` a
`<filter>__review` relation lists the rows without a usable answer plus the near-cut rows.

`services.results_query` caches the ordered row-id list per (result version, revision, query) and pages through
it, so scrolling is cheap and stable while a job is still running. `results_vectors` returns whole columns for the
web worker's local filter/sort. `results_provenance` returns the raw provider answer, the interpreted values and
any overrides for one row.

### 6. Correct, version, export

`results_patch` creates a new result version whose manifest points at the original (`derived_from`) and stores
explicit override records; the compiler applies them on top with status `override`. Overrides are inherited by
later versions. `results_export` writes CSV (formula-escaped by default) or Parquet plus a manifest recording the
plan hash, model, completion scope, pending counts, review views and approximations.

## Storage layout

```
data/
  semsheet.sqlite              metadata (see db.py SCHEMA)
  uploads/<upload_id>          raw upload until imported
  datasets/<dataset_id>/
    source.csv                 original bytes
    <version_id>.parquet       typed snapshot per version
    import_errors.csv|json     when an import was rejected
  results/<rv_id>/<step_id>/chunk_NNNNN.parquet   derived semantic columns
  exports/<export_id>.{csv,parquet,manifest.json}
  samples/                     demo CSVs (or SEMSHEET_SAMPLES_DIR)
```

## Statuses

| Column | Values |
|---|---|
| `<q>.status` (annotate) | `ok`, `uncertain` (fixed mode only), `missing` (no input), `skipped` (`on_missing: skip`), `failed`, `input_too_long`, `pending`, `override` |
| `<match>.status` | `matched`, `unmatched`, `uncertain`, `no_candidates`, `failed`, `pending` |
| Job `state` | `queued`, `running`, `succeeded`, `partial`, `failed`, `cancelled` |
| Job `terminal_reason` | `deadline`, `budget`, `workspace_budget`, `max_provider_requests`, `pair_budget`, `max_source_rows`, `provider_failures`, `incomplete`, `cancelled`, `internal_error`, `right_table_too_large` |

## Workspaces and auth

One bearer token per workspace; the token's SHA-256 is stored. The demo workspace (`ws_demo`) is created on
first start with `SEMSHEET_DEMO_TOKEN` and `SEMSHEET_WORKSPACE_BUDGET_USD`. `Database.create_workspace` adds more.
Every service method takes the resolved `Workspace` and every query is scoped to it. The web app sends
`Authorization: Bearer <token>` (also accepted: `X-Workspace-Token` header or `?token=` for downloads and SSE).
