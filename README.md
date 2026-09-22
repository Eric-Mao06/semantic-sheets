# Semantic Sheet

**A working AI spreadsheet whose per-row semantic operations run on [TypeSafe Jev](https://typesafe.ai) instead of a
frontier model.** Upload a table, ask in plain language, get a typed, auditable column back — for cents rather than
dollars, in seconds rather than minutes, with exact arithmetic.

This repository is a reference implementation for teams building AI features into spreadsheet and data products.
It exists to make one argument concrete and measurable: the unit economics of "AI over every row" change
fundamentally when a frontier model does the *planning* once and a small decision model does the *judging* per row.

## The argument in numbers

Six realistic operations (semantic filter, three-way classification, filter + rank, filter + group + compare,
categorise + revenue by country, product matching) were run two ways on the same CSVs with the same prompts
([`benchmarks/`](benchmarks/README.md), list prices as of 2026-09-21):

| | One request to `gpt-6-astra` with the whole CSV | Semantic Sheet: planner + Jev + DuckDB |
|---|---|---|
| **Total cost, six scenarios** | **$11.55** | **$0.15** with a DeepSeek V4.1 Flash planner (78× cheaper) · $0.72 with a `gpt-6-astra` planner (16×) |
| Where the money goes | 100% frontier tokens, linear in table size | Jev: $0.005–0.055 per scenario at $0.042 / M input tokens. Planner: one call, $0.001–0.002 (DeepSeek) or $0.05–0.14 (Astra) |
| **Latency** | 27 s to 26 min | 5–12 s end to end (DeepSeek planner), 20–50 s (Astra planner) |
| Exact arithmetic | 53 of 83 revenue cells right; worst cell off by $46,503 | 376 of 376 right, by construction (DuckDB) |
| 100,000-row table | ~6.8 M tokens: does not fit in one request at any price | ≈ $0.17 of Jev; runs in the background with partial results streaming in |
| Semantic recall | Higher on the hardest rows (it reads everything with a frontier model) | Lower where candidate retrieval or question wording miss; every miss is visible in a review view, and the question is editable before anything runs |

The cost gap comes from two places: Jev is ~240× cheaper per input token than the frontier model, and the frontier
model's context is no longer the bottleneck, so table size stops being the cost driver. Full method, per-run plans
and every disagreement: [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

## What the product does

1. **Upload** a CSV (or pick a sample). Strict import: typed columns, stable row ids, malformed rows reported, never
   silently dropped.
2. **Ask** in plain language: *"Find customers trying to cancel an order because they cannot afford it."*
3. **See the plan** before it runs: the exact question Jev will be asked, the columns it will see, the rows, the
   requests, the estimated cost and time. Edit any of it.
4. **Run.** Rows are judged on Jev in multi-row packets with rate limits, retries, a content-addressed answer cache
   and a spend ceiling. Results appear as they commit; you can query, cancel or resume a partial job.
5. **Review.** Every cell carries the raw model answer, a score and a status. Decision thresholds are calibrated
   on the observed score distribution once a stage is scored; near-cut rows are flagged for a spot check instead of
   being dropped. Corrections create a new immutable version and never overwrite model output.
6. **Export** CSV or Parquet with a manifest that says exactly what was and was not completed.

Everything above is also exposed to agents as **MCP tools** over Streamable HTTP, with bounded pages so an
assistant can drive the whole loop without pulling the table into its context ([`docs/mcp.md`](docs/mcp.md)).

<p align="center">
  <em>Screen recordings of the six scenarios are on the media-only branch
  <a href="https://github.com/Eric-Mao06/semantic-sheets/tree/cursor/walkthrough-media-fd37/walkthroughs">cursor/walkthrough-media-fd37</a>.</em>
</p>

## How it works

```
plain language ──▶ planner (frontier model, sees schema + 20 rows) ──▶ typed plan (JSON)
                                                                          │
                       ┌──────────────────────────────────────────────────┴───────────┐
                       ▼                                                              ▼
        semantic steps: Jev, one packet of rows at a time             exact steps: DuckDB over Parquet
        boolean / category / score questions per row                  filter · sort · join · aggregate · compute
        raw answer + score + status stored per cell                   review views for rows without an answer
                       └──────────────────────────────┬───────────────────────────────┘
                                                      ▼
                     versioned result: paged queries, provenance per cell, overrides, export
```

- **The plan is the contract.** Neither the UI nor MCP accepts SQL or free-form code; every operation is a typed
  step ([`docs/plan-format.md`](docs/plan-format.md)). That is what makes the estimate honest and the run auditable.
- **Jev is asked one literal question per row**, three kinds: yes/no probability, one label from a fixed set, or a
  level on an ordered rubric. It cannot count or do arithmetic, and is never asked to.
- **Thresholds are set after seeing the scores.** The planner cannot know where Jev's scores will separate; Otsu's
  cut on the observed histogram places it, with a fixed-threshold fallback and versioned constants
  ([`benchmarks/README.md`](benchmarks/README.md#calibrated-cuts-what-changed-in-the-engine-and-the-protocol-against-overfitting)).
- **Nothing is lost silently.** Rows without a usable answer go to a review view; partial jobs keep their committed
  chunks; exports carry a completion manifest.

The full design — import, planning, execution, calibration, querying, storage layout, statuses — is in
[`docs/architecture.md`](docs/architecture.md).

## Quickstart

Requirements: Python 3.12+ with [`uv`](https://docs.astral.sh/uv/), Node 20+, and API keys for Jev
(`TYPESAFE_API_KEY` and/or `OPENROUTER_API_KEY`) and a planner (`OPENAI_API_KEY`, or reuse `OPENROUTER_API_KEY`).

```bash
git clone https://github.com/Eric-Mao06/semantic-sheets && cd semantic-sheets
cp .env.example .env                 # fill in keys; every variable is documented in docs/configuration.md
set -a; source .env; set +a

# terminal 1: API + MCP on :8000
cd server && uv sync --extra dev && uv run python -m semsheet.main
# terminal 2: job worker
cd server && uv run python -m semsheet.worker
# terminal 3: web app on :5173 (proxies /api and /mcp to :8000)
cd web && npm install && npm run dev
```

Open http://localhost:5173, pick a sample dataset, and run the suggested operation. The demo workspace token is
`demo-token`; the web app sends it as `Authorization: Bearer demo-token`, and MCP clients use the same header.

To run the planner on a cheaper model, set `PLANNER_PROVIDER=openrouter` and
`PLANNER_MODEL=deepseek/deepseek-v4.1-flash` (2–5 s per plan, ~$0.001 per call in the benchmark).

One-container deployment (Docker, Railway): [`docs/deploy.md`](docs/deploy.md).

## Repository map

```
server/semsheet/         FastAPI API, MCP server, services, typed plan models, planner, importer
server/semsheet/engine/  exact.py (plan → DuckDB SQL), jev.py (packets, limits, cache), executor.py (jobs), calibrate.py
server/tests/            importer, exact engine, calibration, end-to-end workflow with a fake Jev, MCP client
web/                     React + TypeScript + Vite; Tailwind + shadcn-style primitives; Glide Data Grid
benchmarks/              operators-vs-one-shot harness, scenarios, results per planner, calibration protocol
scripts/                 prepare_samples.py (builds data/samples from public downloads), start.sh (container entry)
data/samples/            the nine bounded demo CSVs the landing page offers (checked in)
docs/                    architecture, plan format, configuration, MCP, deployment
```

## Sample datasets

The landing page offers nine bounded CSVs built by `scripts/prepare_samples.py` from public sources, each with a
suggested operation: Bitext customer-support messages, BANKING77 queries, CFPB consumer complaints, Inside Airbnb
NYC reviews, Online Retail II products and product × country revenue, WDC product offers and catalog (for
matching), and 100,000 UC Berkeley alumni profiles. They are checked in so a fresh clone and the Docker image work
without running the script; the raw downloads and larger scale files stay local (`data/raw/`, ignored).

## Test and lint

```bash
ruff check .                                             # from the repo root
cd server && uv run pytest -q                            # 37 tests, no API keys needed (fake Jev provider)
cd web && npm run typecheck && npm run lint && npm run build
```

## What to take from this if you build spreadsheets

- **Split planning from judging.** Let the expensive model read the schema and a sample once; let a decision
  model read the rows. The estimate panel in this repo is what makes the split legible to a user before they spend.
- **Type the plan.** A constrained step language is what lets you estimate cost, compile exact parts to SQL, cache
  judgements by content, and explain the operation in plain sentences.
- **Calibrate after scoring, flag instead of withholding.** The single largest quality lever in the benchmark was
  where the boolean cut sat, not which planner wrote the question.
- **Keep the raw answer.** Provenance per cell, overrides as versions, and a manifest per export are what make an
  AI column something a finance team will accept.

## License

[MIT](LICENSE). Contributions welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
