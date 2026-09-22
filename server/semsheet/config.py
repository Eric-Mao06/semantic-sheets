"""Runtime configuration.

Every value has a default that works for a local demo; the ones that vary between deployments can be overridden
through environment variables. See docs/configuration.md for the full list."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


_DEFAULT_PLANNER_MODELS = {"openrouter": "deepseek/deepseek-v4.1-flash", "openai": "gpt-6-astra"}


def _default_planner_model() -> str:
    return _DEFAULT_PLANNER_MODELS.get(os.environ.get("PLANNER_PROVIDER", "openrouter"), "gpt-6-astra")


def _default_planner_openrouter_providers() -> str:
    v = os.environ.get("PLANNER_OPENROUTER_PROVIDERS")
    if v is not None:
        return v
    return "Together" if not os.environ.get("PLANNER_MODEL") else ""


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SEMSHEET_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data"))))
    # Demo datasets offered on the landing page; defaults to <data_dir>/samples. Set separately when the data dir is
    # a mounted volume and the samples ship with the code (see Dockerfile).
    samples_dir: Path = field(default_factory=lambda: Path(os.environ["SEMSHEET_SAMPLES_DIR"]) if os.environ.get("SEMSHEET_SAMPLES_DIR") else Path(os.environ.get("SEMSHEET_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data"))) / "samples")

    # Provider: TypeSafe Jev
    typesafe_api_key: str = field(default_factory=lambda: os.environ.get("TYPESAFE_API_KEY", ""))
    typesafe_base_url: str = field(default_factory=lambda: os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai"))
    jev_model: str = field(default_factory=lambda: os.environ.get("JEV_MODEL", "jev-1.13.0"))
    jev_price_per_mtok_usd: float = field(default_factory=lambda: _env_float("JEV_PRICE_PER_MTOK_USD", 0.042))
    # Jev is also served by OpenRouter (POST /api/alpha/decisions, same request and answer shape) and by Vercel AI
    # Gateway (POST /v1/evaluate; same shape except boolean questions/answers are spelled "boolean"/"probability"
    # rather than "noul", and usage is camelCase). All three charge the same price. Each route has its own rate
    # limit, so JEV_ROUTES="direct,openrouter,vercel" spreads packets over all of them and multiplies throughput.
    # Routes whose key is missing are skipped.
    jev_routes: tuple[str, ...] = field(default_factory=lambda: tuple(r.strip() for r in os.environ.get("JEV_ROUTES", "direct,openrouter,vercel").split(",") if r.strip()))
    jev_openrouter_model: str = field(default_factory=lambda: os.environ.get("JEV_OPENROUTER_MODEL", "typesafe/jev-1.13"))
    jev_openrouter_url: str = field(default_factory=lambda: os.environ.get("JEV_OPENROUTER_URL", "https://openrouter.ai/api/alpha/decisions"))
    # Vercel AI Gateway. AI_GATEWAY_API_KEY is the variable name Vercel's own SDKs read.
    vercel_ai_gateway_api_key: str = field(default_factory=lambda: os.environ.get("AI_GATEWAY_API_KEY", ""))
    jev_vercel_model: str = field(default_factory=lambda: os.environ.get("JEV_VERCEL_MODEL", "typesafe-ai/jev"))
    jev_vercel_url: str = field(default_factory=lambda: os.environ.get("JEV_VERCEL_URL", "https://ai-gateway.vercel.sh/v1/evaluate"))
    # Per-route limits (TypeSafe's published limits apply per account; OpenRouter's and Vercel's apply per key).
    jev_requests_per_minute: int = field(default_factory=lambda: _env_int("JEV_RPM", 1200))
    jev_tokens_per_second: int = field(default_factory=lambda: _env_int("JEV_TPS", 250_000))
    jev_max_state_plus_question_tokens: int = 32_000
    jev_max_total_tokens: int = 64_000
    jev_concurrency: int = field(default_factory=lambda: _env_int("JEV_CONCURRENCY", 8))
    jev_rows_per_request: int = field(default_factory=lambda: _env_int("JEV_ROWS_PER_REQUEST", 10))
    jev_max_retries: int = 5
    jev_timeout_seconds: float = 60.0

    # Planner: frontier model. PLANNER_PROVIDER "openrouter" (default) calls any OpenRouter model through its
    # OpenAI-compatible chat completions endpoint; "openai" calls the OpenAI Responses API directly. The default
    # planner is DeepSeek V4.1 Flash pinned to Together (2-5 s per plan in benchmarks/); gpt-6-astra takes 15-50 s.
    planner_provider: str = field(default_factory=lambda: os.environ.get("PLANNER_PROVIDER", "openrouter"))
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    openrouter_base_url: str = field(default_factory=lambda: os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    planner_model: str = field(default_factory=lambda: os.environ.get("PLANNER_MODEL") or _default_planner_model())
    # OpenRouter only: pin the upstream provider(s), e.g. "CoreWeave" or "Together,CoreWeave". Empty = OpenRouter's default routing.
    # Defaults to "Together" for the default DeepSeek planner; an explicit PLANNER_MODEL gets OpenRouter's routing.
    planner_openrouter_providers: tuple[str, ...] = field(default_factory=lambda: tuple(p.strip() for p in _default_planner_openrouter_providers().split(",") if p.strip()))
    planner_openrouter_allow_fallbacks: bool = field(default_factory=lambda: os.environ.get("PLANNER_OPENROUTER_ALLOW_FALLBACKS", "0") == "1")
    planner_reasoning_effort: str = field(default_factory=lambda: os.environ.get("PLANNER_REASONING", "high"))
    planner_max_output_tokens: int = field(default_factory=lambda: _env_int("PLANNER_MAX_OUTPUT_TOKENS", 12_000))

    # Import limits
    max_import_rows: int = field(default_factory=lambda: _env_int("SEMSHEET_MAX_IMPORT_ROWS", 100_000))
    max_file_bytes: int = field(default_factory=lambda: _env_int("SEMSHEET_MAX_FILE_BYTES", 100 * 1024 * 1024))
    retention_days: int = 7

    # Query bounds
    web_page_max_rows: int = 256
    web_page_max_bytes: int = 256 * 1024
    web_cell_max_chars: int = 512
    mcp_page_max_rows: int = 50
    mcp_page_max_bytes: int = 16 * 1024
    mcp_cell_max_chars: int = 240
    sample_max_rows: int = 20
    sample_max_bytes: int = 8 * 1024

    # Job defaults / caps
    default_max_source_rows: int = field(default_factory=lambda: _env_int("SEMSHEET_DEFAULT_MAX_SOURCE_ROWS", 10_000))
    hard_max_source_rows: int = field(default_factory=lambda: _env_int("SEMSHEET_HARD_MAX_SOURCE_ROWS", 300_000))
    default_max_provider_requests: int = 12_000
    default_spend_target_usd: float = 0.50
    default_deadline_seconds: int = 600
    workspace_budget_usd: float = field(default_factory=lambda: _env_float("SEMSHEET_WORKSPACE_BUDGET_USD", 25.0))
    chunk_rows: int = field(default_factory=lambda: _env_int("SEMSHEET_CHUNK_ROWS", 200))

    # Matching caps
    match_max_right_rows: int = 5_000
    match_max_candidates: int = 5
    match_max_pairs: int = 100_000

    # Auth
    demo_workspace_token: str = field(default_factory=lambda: os.environ.get("SEMSHEET_DEMO_TOKEN", "demo-token"))

    # Versions participating in cache keys
    prompt_layout_version: str = "pl-1"
    normalization_version: str = "norm-1"
    retrieval_version: str = "lex-rapidfuzz-1"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "semsheet.sqlite"

    def ensure_dirs(self) -> None:
        for sub in ("uploads", "datasets", "results", "exports", "views", "cache"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
