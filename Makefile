.PHONY: setup api worker web build test samples walkthrough dev

PY=server/.venv/bin/python
UVICORN=server/.venv/bin/uvicorn

setup:            ## create the Python venv, install server + web dependencies
	cd server && uv venv .venv --python 3.11 && uv pip install -e ".[dev]"
	cd web && npm install

samples:          ## download the public datasets and write bounded demo samples into $$SS_DATA_DIR/samples
	$(PY) scripts/prepare_samples.py

build:            ## build the web app (served by the API at /)
	cd web && npm run build

api:              ## API + MCP (+ inline worker when SS_INLINE_WORKER=1)
	cd server && .venv/bin/uvicorn semantic_sheets.api.app:app --host 0.0.0.0 --port 8000

worker:           ## a separate worker process (run one or more)
	cd server && .venv/bin/python -m semantic_sheets.worker

dev:              ## single-process dev server: API + MCP + inline worker + built web app
	cd server && SS_INLINE_WORKER=1 .venv/bin/uvicorn semantic_sheets.api.app:app --port 8000 --reload

web:              ## Vite dev server with proxy to :8000
	cd web && npm run dev

test:             ## backend tests with the fake Jev client (no provider calls)
	cd server && SS_FAKE_JEV=1 .venv/bin/python -m pytest -q

walkthrough:      ## record the Playwright walkthrough (needs a running API on :8000 and SS_DEV_WORKSPACE_KEY)
	cd web && node e2e/walkthrough.mjs
