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
- API keys exported in the environment of the API and worker processes (a `.env` at the repo root is a
  convenient place; load it with `set -a; source .env; set +a`):

```
TYPESAFE_API_KEY=apikey_...
OPENAI_API_KEY=sk-...
PLANNER_MODEL=gpt-6-astra
PLANNER_REASONING=high
```

With `OPENROUTER_API_KEY` set, two more things become available:

```
OPENROUTER_API_KEY=sk-or-...
JEV_ROUTES=direct,openrouter         # default: Jev packets are spread over TypeSafe direct and OpenRouter's
                                     # decisions endpoint (typesafe/jev-1.13), each with its own rate limit
PLANNER_PROVIDER=openrouter          # run the planner on any OpenRouter model, e.g. DeepSeek V4.1 Flash
PLANNER_MODEL=deepseek/deepseek-v4.1-flash
PLANNER_OPENROUTER_PROVIDERS=Together   # optional: pin the upstream provider (no fallbacks); empty = OpenRouter routing
```

Set `JEV_ROUTES=direct` to pin Jev to the TypeSafe API only.

## Run

```bash
# backend API (port 8000) and the job worker, from server/
set -a; source .env; set +a
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
lists them under "Sample datasets"; each card carries an example operation. The eight bounded files the landing
page uses are checked in, so a fresh clone (and the Docker image) has them without running the script.
`SEMSHEET_SAMPLES_DIR` points the API at a different samples directory (default `data/samples`).

## Deploy (Railway)

The root `Dockerfile` builds the frontend and serves it, the API and the MCP endpoint from one FastAPI
process on `$PORT`, with the job worker running alongside it (`scripts/start.sh`, `SEMSHEET_WORKERS` sets the
number of workers). `railway.json` selects the Dockerfile builder and the `/api/health` health check.

```bash
railway up                              # from the repo root; creates the project + service on first run
railway volume add --mount-path /app/data   # SQLite metadata, Parquet datasets/results, exports, Jev cache
railway variable set OPENAI_API_KEY=sk-... OPENROUTER_API_KEY=sk-or-... TYPESAFE_API_KEY=apikey_... SEMSHEET_DEMO_TOKEN=<token>
railway domain
```

Metadata lives in SQLite on the volume, so keep the service at one replica. Without `TYPESAFE_API_KEY`, set
`OPENROUTER_API_KEY` and Jev runs over OpenRouter only (the `direct` route is skipped). The bounded demo
datasets in `data/samples/` are versioned and copied into the image (`SEMSHEET_SAMPLES_DIR=/app/samples`);
the raw downloads and the large scale files stay local.

## Test

```bash
cd server && uv run pytest -q          # importer, exact engine, workflow/API with a fake provider, MCP client
cd web && npx tsc -p tsconfig.app.json --noEmit && npm run build
```

## Benchmark

`benchmarks/` compares the operators (planner + Jev + DuckDB) with handing the same CSV and prompt to `gpt-6-astra`
(reasoning `high`) in one request, on the six walkthrough scenarios: output quality against gold or agreement
metrics, token usage, cost and latency. The operators were run with three planners (`gpt-6-astra`, `z-ai/glm-5.3-flash`
and `deepseek/deepseek-v4.1-flash` via OpenRouter), plus an engine-only A/B that re-runs the DeepSeek plans with
score-calibrated cuts (protocol pre-registered and checked on held-out rows before the run). See
[`benchmarks/README.md`](benchmarks/README.md) for the method and findings,
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md) for the cross-planner summary and `benchmarks/results/<run>/RESULTS.md`
for every plan, metric and disagreement.

## Walkthroughs

Screen recordings and screenshots of the six demo scenarios are kept on the media-only branch
[`cursor/walkthrough-media-fd37`](https://github.com/Eric-Mao06/semantic-sheets/tree/cursor/walkthrough-media-fd37/walkthroughs)
so the code history stays small; open any `.mp4` there in the GitHub file viewer to play it.

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
   (`ok`, `uncertain`, `missing`). Once a stage is fully scored, boolean and match cuts are calibrated on the
   observed score distribution (`engine/calibrate.py`: Otsu's threshold, clamped, with a fixed-threshold
   fallback for tiny inputs), so every row gets an answer; rows within ±0.10 of the cut are flagged in
   `<question>.near` and listed in the filter's review view while staying in the output. `thresholds.mode: fixed`
   keeps the classic three-way behaviour. Corrections create new immutable result versions that keep the model output.
5. **Agents** (`mcp_server.py`): the same services as MCP tools (`datasets_*`, `plans_*`, `jobs_*`,
   `results_*`) with bounded pages and structured error envelopes.
