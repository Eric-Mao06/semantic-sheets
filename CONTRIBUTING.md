# Contributing

Semantic Sheet is a reference implementation, kept small on purpose so the whole pipeline (planner → Jev → DuckDB)
can be read in an afternoon. Contributions that keep it that way are welcome: bug fixes, clearer docs, new sample
scenarios, benchmark runs on other planners, and engine improvements that come with tests.

## Set up

```bash
git clone https://github.com/Eric-Mao06/semantic-sheets && cd semantic-sheets
cp .env.example .env            # add your keys; see docs/configuration.md
set -a; source .env; set +a

cd server && uv sync --extra dev && cd ..
cd web && npm install && cd ..
```

Run the three processes in separate terminals:

```bash
cd server && uv run python -m semsheet.main      # API + MCP on :8000
cd server && uv run python -m semsheet.worker    # job worker
cd web && npm run dev                            # UI on :5173, proxies /api and /mcp to :8000
```

The importer, exact engine, calibration and the whole workflow (with a fake Jev provider) run without any API key:

```bash
cd server && uv run pytest -q
```

## Before opening a pull request

```bash
ruff check .                                         # from the repo root (ruff is in the server dev extras)
cd server && uv run pytest -q
cd web && npm run typecheck && npm run lint && npm run build
```

CI runs the same commands (`.github/workflows/ci.yml`).

## Conventions

- **Business logic lives in `server/semsheet/services.py`.** Route handlers (`api.py`) and MCP tool handlers
  (`mcp_server.py`) are thin adapters; if a behaviour needs to exist in both surfaces, put it in a service method.
- **Plans are the contract.** Anything a user or an agent can ask the engine to do is a typed step in
  `models.py`, compiled by `engine/exact.py` or executed by `engine/executor.py`. Neither surface accepts SQL.
- **Raw model output is never edited.** Corrections are override records in a new result version; calibration
  rewrites derived value/status columns from the stored scores, never the `.raw` column.
- **Calibration constants are versioned.** `engine/calibrate.py` documents why; if you change the rule, bump
  `VERSION` and validate on held-out rows first (`benchmarks/calibration_dev.py`).
- **Dense Python is fine, ambiguous Python is not.** Ruff enforces the baseline (`ruff.toml`). Long lines are
  allowed for SQL and dict literals; single-letter names, unchained re-raises and semicolon-joined statements are not.
- **Docs travel with code.** If you add a setting, document it in `docs/configuration.md`; a new step or question
  kind goes in `docs/plan-format.md` and the planner prompt; a new MCP tool goes in `docs/mcp.md`.

## Adding a sample dataset

1. Add a builder to `scripts/prepare_samples.py` that reads from `data/raw/` and writes a bounded CSV to
   `data/samples/`.
2. Register it in `server/semsheet/samples.py` with a one-line description and a suggested operation.
3. Whitelist the file in `.gitignore` and `.dockerignore` if it should ship with the repo (keep it small).

## Reporting benchmark runs

`benchmarks/README.md` describes the protocol. A new run should add a `results/<run>/` directory produced by
`benchmarks/run.py` and `benchmarks/report.py`, and a row in the tables of `benchmarks/RESULTS.md`; do not edit
numbers by hand.
