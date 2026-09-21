"""Runtime configuration. Every limit here is a proposed default from the design document."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("SS_DATA_DIR", "./data")).resolve())
    database_url: str = _env("SS_DATABASE_URL", "")
    # Providers
    typesafe_api_key: str = _env("TYPESAFE_API_KEY", "")
    typesafe_base_url: str = _env("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
    jev_model: str = _env("SS_JEV_MODEL", "jev-1.13.0")
    jev_price_per_million_input: float = _env_float("SS_JEV_PRICE_PER_M", 0.042)
    jev_requests_per_minute: int = _env_int("SS_JEV_RPM", 1200)
    jev_tokens_per_second: int = _env_int("SS_JEV_TPS", 250_000)
    jev_max_in_flight: int = _env_int("SS_JEV_MAX_IN_FLIGHT", 12)
    jev_pack_rows: int = _env_int("SS_JEV_PACK_ROWS", 10)
    jev_state_token_budget: int = _env_int("SS_JEV_STATE_TOKEN_BUDGET", 24_000)
    jev_total_token_budget: int = _env_int("SS_JEV_TOTAL_TOKEN_BUDGET", 56_000)
    jev_timeout_seconds: float = _env_float("SS_JEV_TIMEOUT", 30.0)
    jev_max_retries: int = _env_int("SS_JEV_MAX_RETRIES", 4)
    fake_jev: bool = _env_bool("SS_FAKE_JEV", False)
    openai_api_key: str = _env("OPENAI_API_KEY", "")
    planner_model: str = _env("SS_PLANNER_MODEL", "gpt-6-astra")
    planner_reasoning_effort: str = _env("SS_PLANNER_REASONING", "high")
    planner_sample_rows: int = _env_int("SS_PLANNER_SAMPLE_ROWS", 20)
    # Import limits
    max_file_bytes: int = _env_int("SS_MAX_FILE_BYTES", 100 * 1024 * 1024)
    max_rows_per_file: int = _env_int("SS_MAX_ROWS_PER_FILE", 100_000)
    hard_max_rows_per_file: int = _env_int("SS_HARD_MAX_ROWS", 300_000)
    max_columns: int = _env_int("SS_MAX_COLUMNS", 500)
    # Job limits
    default_max_source_rows: int = _env_int("SS_DEFAULT_MAX_SOURCE_ROWS", 100_000)
    default_max_provider_requests: int = _env_int("SS_DEFAULT_MAX_REQUESTS", 200_000)
    default_spend_target_usd: float = _env_float("SS_DEFAULT_SPEND_TARGET", 5.0)
    default_deadline_seconds: int = _env_int("SS_DEFAULT_DEADLINE", 3600)
    chunk_rows: int = _env_int("SS_CHUNK_ROWS", 200)
    match_max_right_rows: int = _env_int("SS_MATCH_MAX_RIGHT_ROWS", 5000)
    match_max_candidates: int = _env_int("SS_MATCH_MAX_CANDIDATES", 20)
    # Query limits
    mcp_max_rows: int = _env_int("SS_MCP_MAX_ROWS", 50)
    mcp_max_bytes: int = _env_int("SS_MCP_MAX_BYTES", 16 * 1024)
    web_max_rows: int = _env_int("SS_WEB_MAX_ROWS", 256)
    web_max_bytes: int = _env_int("SS_WEB_MAX_BYTES", 256 * 1024)
    cell_display_chars: int = _env_int("SS_CELL_DISPLAY_CHARS", 300)
    # Workspace / auth
    dev_workspace_key: str = _env("SS_DEV_WORKSPACE_KEY", "")
    workspace_budget_usd: float = _env_float("SS_WORKSPACE_BUDGET_USD", 25.0)
    retention_days: int = _env_int("SS_RETENTION_DAYS", 7)
    inline_worker: bool = _env_bool("SS_INLINE_WORKER", False)
    worker_lease_seconds: int = _env_int("SS_WORKER_LEASE_SECONDS", 60)
    cors_origins: str = _env("SS_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    public_base_url: str = _env("SS_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

    def __post_init__(self) -> None:
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'meta.db'}"

    def ensure_dirs(self) -> None:
        for sub in ("uploads", "datasets", "jobs", "views", "exports", "samples"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings() -> None:
    """Re-read the environment (tests)."""
    global _settings
    _settings = None
