# Semantic Sheet (MVP)

Turn plain language into reusable, typed operations over uploaded tables: classify feedback, score records,
filter by meaning, rank results, group themes and match rows across files. Semantic judgements run on
TypeSafe **Jev** (`jev-1.13.0`); plain language is compiled into a typed plan by a frontier model
(OpenAI `gpt-6-astra`, reasoning effort `high`). Every result is auditable, correctable, exportable, and the
same operations are exposed to agents over **MCP** (Streamable HTTP).

```
web/       React + TypeScript + Vite, Glide Data Grid, Web Workers (CSV preview, local filter/sort)
server/    FastAPI API, planner, exact engine (DuckDB), Jev executor, MCP server, worker process
scripts/   prepare_samples.py - builds the demo datasets from the raw public downloads
data/      SQLite metadata, Parquet datasets/results, exports, Jev answer cache (created at runtime)
```

## Requirements

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) (or `pip` with `server/requirements.txt`)
- Node 20+ (frontend)
- API keys in `.env` at the repo root (loaded by the server processes):

```
TYPESAFE_API_KEY=apikey_...
OPENAI_API_KEY=sk-...
PLANNER_MODEL=gpt-6-astra
PLANNER_REASONING=high
```

## Run

```bash
# backend API (port 8000) and the job worker, from server/
cd server
uv sync --extra dev
uv run python -m semsheet.main        # API: http://localhost:8000  (MCP: POST http://localhost:8000/mcp)
uv run python -m semsheet.worker      # separate terminal; several workers may run against the same store

# frontend (port 5173, proxies /api and /mcp to :8000), from web/
cd web
npm install
npm run dev
```

Open http://localhost:5173. The demo workspace token is `demo-token` (`SEMSHEET_DEMO_TOKEN`); the web app
sends it as `Authorization: Bearer demo-token`, and MCP clients use the same header.

### Demo datasets

`scripts/prepare_samples.py` turns the raw public downloads in `data/raw/` (Bitext, CFPB, Inside Airbnb NYC,
BANKING77, WDC product matching, Online Retail II) into bounded demo CSVs in `data/samples/`. The landing page
lists them under "Sample datasets"; each card carries an example operation.

## Test

```bash
cd server && uv run pytest -q          # importer, exact engine, workflow/API with a fake provider, MCP client
cd web && npx tsc -p tsconfig.app.json --noEmit && npm run build
```

## How it works

1. **Import** (`importer.py`): CSV/TSV sniffing, strict parsing with explicit columns, typed columns, stable
   `_row_id`, Parquet storage, malformed rows reported (never silently dropped).
2. **Plan** (`planner.py`, `models.py`): the frontier model sees only the schema and a bounded sample and
   returns a typed plan (`semantic_annotate`, `filter`, `sort`, `aggregate`, `compute`, `join`,
   `semantic_match`, `project`). Plans are validated and estimated (rows, Jev requests, cost, cache hits)
   before anything runs; users edit steps and questions in the Operation panel.
3. **Execute** (`engine/executor.py`, `engine/jev.py`): rows are packed into multi-row Jev requests with
   token-bucket rate limits, retries, a content-addressed answer cache and usage accounting. Jobs run in
   restart-safe chunks with budget, request and deadline guards; partial results are queryable and resumable.
   Exact steps compile to DuckDB SQL over Parquet (`engine/exact.py`).
4. **Review**: each judgement stores the raw model answer (probabilities, confidence) and status
   (`ok`, `uncertain`, `missing`); uncertain rows land in a review view; corrections create new immutable
   result versions that keep the model output.
5. **Agents** (`mcp_server.py`): the same services as MCP tools (`datasets_*`, `plans_*`, `jobs_*`,
   `results_*`) with bounded pages and structured error envelopes.
