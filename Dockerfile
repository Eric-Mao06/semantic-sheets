# Single-image deployment: the FastAPI process serves the API, the MCP endpoint and the built
# frontend (web/dist) on $PORT; the job worker runs alongside it (see scripts/start.sh).

# --- frontend -----------------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- backend ------------------------------------------------------------------------------------
FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/server/.venv \
    PATH="/app/server/.venv/bin:$PATH" \
    SEMSHEET_DATA_DIR=/app/data \
    SEMSHEET_SAMPLES_DIR=/app/samples

WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server/ /app/server/
COPY --from=web /app/web/dist /app/web/dist
# Demo datasets live in the image (not on the data volume) so redeploys keep them current.
COPY data/samples/ /app/samples/
COPY scripts/start.sh /app/scripts/start.sh
RUN chmod +x /app/scripts/start.sh && mkdir -p /app/data

EXPOSE 8000
CMD ["/app/scripts/start.sh"]
