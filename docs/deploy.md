# Deploying

The root `Dockerfile` builds the frontend and produces one image in which a single FastAPI process serves the API,
the MCP endpoint and the built web app on `$PORT`, with one or more job workers running alongside it
(`scripts/start.sh`). Metadata lives in SQLite, so run **one replica** and give it a persistent volume.

## Docker (anywhere)

```bash
docker build -t semantic-sheet .
docker run --rm -p 8000:8000 \
  -v semsheet-data:/app/data \
  -e TYPESAFE_API_KEY=apikey_... -e OPENROUTER_API_KEY=sk-or-... -e OPENAI_API_KEY=sk-... \
  -e SEMSHEET_DEMO_TOKEN=change-me \
  semantic-sheet
```

Open http://localhost:8000. The image copies the bounded demo datasets to `/app/samples`
(`SEMSHEET_SAMPLES_DIR`), separate from the data volume, so redeploys keep them current.

`SEMSHEET_WORKERS=2` starts two workers; both share the store and claim jobs atomically. If either the API or a
worker exits, the container exits so the platform restarts it.

## Railway

`railway.json` selects the Dockerfile builder, the `/api/health` health check and one replica.

```bash
railway up                                        # creates the project + service on first run
railway volume add --mount-path /app/data         # SQLite metadata, Parquet, exports, Jev cache
railway variable set TYPESAFE_API_KEY=apikey_... OPENROUTER_API_KEY=sk-or-... OPENAI_API_KEY=sk-... SEMSHEET_DEMO_TOKEN=<token>
railway domain
```

Without `TYPESAFE_API_KEY`, set `OPENROUTER_API_KEY` and Jev runs over OpenRouter only (the `direct` route is
skipped). To run the planner on a cheaper model, also set `PLANNER_PROVIDER=openrouter` and
`PLANNER_MODEL=deepseek/deepseek-v4.1-flash` (see [configuration.md](configuration.md)).

## Before exposing it

- Set `SEMSHEET_DEMO_TOKEN` to a real secret. The default `demo-token` is for local use.
- Set `SEMSHEET_WORKSPACE_BUDGET_USD` to the total you are willing to spend on Jev for that workspace; jobs stop as
  `partial` at the ceiling and `GET /api/workspace` shows the running total.
- Put the service behind TLS. CORS is open (`*`) because the web app and MCP clients can live anywhere; the bearer
  token is the access control.
- `retention_days` is recorded on datasets and exports but nothing deletes old data yet; size the volume
  accordingly or add a sweeper.
