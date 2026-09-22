# Configuration

Everything is read from environment variables in [`server/semsheet/config.py`](../server/semsheet/config.py) when
the API or the worker starts. Both processes must see the same values (a `.env` at the repo root loaded with
`set -a; source .env; set +a` is the simplest way; see `.env.example`). Values marked *fixed* have no environment
override and are changed in `config.py`.

## Providers

### Jev (row-level judgements)

| Variable | Default | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | — | TypeSafe API key. Enables the `direct` route. |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Direct route base URL (`/v1/systemone` is appended). |
| `OPENROUTER_API_KEY` | — | OpenRouter key. Enables the `openrouter` Jev route and the OpenRouter planner. |
| `AI_GATEWAY_API_KEY` | — | Vercel AI Gateway key. Enables the `vercel` Jev route. |
| `JEV_ROUTES` | `direct,openrouter,vercel` | Which routes to use; routes whose key is missing are skipped. Set `direct` to pin Jev to TypeSafe. |
| `JEV_MODEL` | `jev-1.13.0` | Model name written into plans and cache keys. |
| `JEV_OPENROUTER_MODEL` | `typesafe/jev-1.13` | The same build as listed by OpenRouter. |
| `JEV_OPENROUTER_URL` | `https://openrouter.ai/api/alpha/decisions` | OpenRouter decisions endpoint. |
| `JEV_VERCEL_MODEL` | `typesafe-ai/jev` | Jev's id on Vercel AI Gateway (unversioned; resolves to TypeSafe's current build). |
| `JEV_VERCEL_URL` | `https://ai-gateway.vercel.sh/v1/evaluate` | Vercel AI Gateway evaluate endpoint. |
| `JEV_PRICE_PER_MTOK_USD` | `0.042` | Price per million input tokens, used for estimates and the spend ledger. |
| `JEV_RPM` | `1200` | Requests per minute **per route**. |
| `JEV_TPS` | `250000` | Input tokens per second **per route**. |
| `JEV_CONCURRENCY` | `8` | In-flight requests per route. |
| `JEV_ROWS_PER_REQUEST` | `10` | Rows packed into one request (plans may override via `limits.rows_per_request`). |
| *fixed* `jev_max_state_plus_question_tokens` | 32,000 | Provider limit on state + one question. |
| *fixed* `jev_max_total_tokens` | 64,000 | Provider limit on a whole request. |
| *fixed* `jev_max_retries` / `jev_timeout_seconds` | 5 / 60 | Retry budget and HTTP timeout per request. |

At least one of `TYPESAFE_API_KEY` / `OPENROUTER_API_KEY` / `AI_GATEWAY_API_KEY` must be set for jobs to run. The
three routes speak nearly the same protocol; the client translates the one difference on the Vercel route (boolean
questions and answers are spelled `boolean`/`probability` there instead of `noul`) so stored raw answers look the
same whichever route served them. Tests use a fake provider and need no key.

### Planner (plain language → plan)

| Variable | Default | Meaning |
|---|---|---|
| `PLANNER_PROVIDER` | `openrouter` | `openrouter` (chat completions) or `openai` (Responses API). |
| `OPENAI_API_KEY` | — | Required when `PLANNER_PROVIDER=openai`. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `PLANNER_MODEL` | `deepseek/deepseek-v4.1-flash` (`gpt-6-astra` when `PLANNER_PROVIDER=openai`) | Any model id the provider accepts (e.g. `z-ai/glm-5.3-flash`). DeepSeek V4.1 Flash plans in 2–5 s; `gpt-6-astra` takes 15–50 s. |
| `PLANNER_REASONING` | `high` | Reasoning effort passed to the provider. |
| `PLANNER_MAX_OUTPUT_TOKENS` | `12000` | |
| `PLANNER_OPENROUTER_PROVIDERS` | `Together` for the default model, otherwise — | OpenRouter only: comma-separated upstream providers to try in order. Empty = OpenRouter's routing. Setting `PLANNER_MODEL` explicitly clears the default pin. |
| `PLANNER_OPENROUTER_ALLOW_FALLBACKS` | `0` | `1` lets OpenRouter fall back beyond the pinned providers. |

## Storage and workspace

| Variable | Default | Meaning |
|---|---|---|
| `SEMSHEET_DATA_DIR` | `<repo>/data` | SQLite metadata, Parquet snapshots and results, exports, uploads. |
| `SEMSHEET_SAMPLES_DIR` | `<data dir>/samples` | Demo CSVs for the landing page. Set separately when the data dir is a mounted volume (the Docker image uses `/app/samples`). |
| `SEMSHEET_DEMO_TOKEN` | `demo-token` | Bearer token of the demo workspace, created on first start. **Change it for any deployment that is reachable from the internet.** |
| `SEMSHEET_WORKSPACE_BUDGET_USD` | `25` | Total Jev spend the demo workspace may accumulate; jobs stop as `partial` at the ceiling. |
| `SEMSHEET_HOST` / `SEMSHEET_PORT` | `0.0.0.0` / `8000` | API bind address. `scripts/start.sh` maps `$PORT` to `SEMSHEET_PORT`. |
| `SEMSHEET_WORKERS` | `1` | Worker processes started by `scripts/start.sh` (Docker only). |
| *fixed* `retention_days` | 7 | Advertised retention for datasets and exports (recorded, not yet enforced by a sweeper). |

## Limits

| Variable | Default | Meaning |
|---|---|---|
| `SEMSHEET_MAX_IMPORT_ROWS` | `100000` | Rows an import may contain; larger files need an explicit `row_limit`. |
| `SEMSHEET_MAX_FILE_BYTES` | `104857600` (100 MiB) | Upload size cap. |
| `SEMSHEET_DEFAULT_MAX_SOURCE_ROWS` | `10000` | Default `limits.max_source_rows` for a job. |
| `SEMSHEET_HARD_MAX_SOURCE_ROWS` | `300000` | Ceiling a job may request. |
| `SEMSHEET_CHUNK_ROWS` | `200` | Rows per committed chunk (the first chunk is a quarter of this so results appear quickly). |
| *fixed* `default_max_provider_requests` / `default_spend_target_usd` / `default_deadline_seconds` | 12,000 / 0.50 / 600 | Job limit defaults. |
| *fixed* `match_max_right_rows` / `match_max_candidates` / `match_max_pairs` | 5,000 / 5 / 100,000 | Matching caps. |
| *fixed* web page bounds | 256 rows / 256 KiB / 512 chars per cell | `results_query` with `surface="web"`. |
| *fixed* MCP page bounds | 50 rows / 16 KiB / 240 chars per cell | `results_query` from MCP. |
| *fixed* planner sample | 20 rows / 8 KiB | What the planner sees. |

## Versions that participate in cache keys

`prompt_layout_version` (`pl-1`), `normalization_version` (`norm-1`) and `retrieval_version` (`lex-rapidfuzz-1`)
are fixed in `config.py`. Bumping one invalidates the answer cache for the affected path, which is the intended
way to force re-scoring after a change to how rows are serialised or candidates are retrieved.
