"""SQLite metadata store. Holds workspaces, datasets, versions, plans, jobs, chunks, events,
predictions cache, overrides, views and exports. Columnar data lives in Parquet files.

SQLite in WAL mode is safe for the API process and the worker process to share."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
  budget_usd REAL NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, filename TEXT, path TEXT NOT NULL,
  size INTEGER DEFAULT 0, completed INTEGER DEFAULT 0, created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
  source_filename TEXT, source_checksum TEXT, source_path TEXT, source_bytes INTEGER,
  created_at REAL NOT NULL, deleted_at REAL, retention_expires_at REAL
);
CREATE INDEX IF NOT EXISTS ix_datasets_ws ON datasets(workspace_id, created_at);
CREATE TABLE IF NOT EXISTS dataset_versions (
  id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  parent_version_id TEXT, kind TEXT NOT NULL, schema_json TEXT NOT NULL, row_count INTEGER NOT NULL,
  data_path TEXT NOT NULL, report_json TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dsv_ds ON dataset_versions(dataset_id, version_no);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, plan_hash TEXT NOT NULL, plan_json TEXT NOT NULL,
  estimate_json TEXT, prompt TEXT, created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_hash ON plans(workspace_id, plan_hash);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_hash TEXT NOT NULL,
  dataset_version_id TEXT NOT NULL, idempotency_key TEXT, payload_hash TEXT NOT NULL,
  state TEXT NOT NULL, terminal_reason TEXT, limits_json TEXT NOT NULL, progress_json TEXT NOT NULL,
  usage_json TEXT NOT NULL, model TEXT NOT NULL, result_version_id TEXT, cancel_requested INTEGER DEFAULT 0,
  error_summary TEXT, worker_id TEXT, heartbeat_at REAL,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_idem ON jobs(workspace_id, idempotency_key);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state, created_at);
CREATE TABLE IF NOT EXISTS job_chunks (
  job_id TEXT NOT NULL, stage_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
  state TEXT NOT NULL, row_count INTEGER NOT NULL, usage_json TEXT, path TEXT, error TEXT, committed_at REAL,
  PRIMARY KEY (job_id, stage_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS job_events (
  job_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (job_id, seq)
);
CREATE TABLE IF NOT EXISTS prediction_cache (
  cache_key TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, model TEXT NOT NULL, raw_json TEXT NOT NULL,
  input_tokens INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS result_versions (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, job_id TEXT, dataset_version_id TEXT NOT NULL,
  plan_id TEXT NOT NULL, plan_hash TEXT NOT NULL, parent_result_version_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rv_ws ON result_versions(workspace_id, created_at);
CREATE TABLE IF NOT EXISTS overrides (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, result_version_id TEXT NOT NULL,
  row_id INTEGER NOT NULL, column_name TEXT NOT NULL, value_json TEXT NOT NULL, reason TEXT, author TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_overrides_rv ON overrides(result_version_id);
CREATE TABLE IF NOT EXISTS exports (
  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, result_version_id TEXT NOT NULL, format TEXT NOT NULL,
  path TEXT NOT NULL, manifest_path TEXT NOT NULL, size INTEGER NOT NULL, row_count INTEGER NOT NULL,
  state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL, job_id TEXT, kind TEXT NOT NULL,
  input_tokens INTEGER NOT NULL, usd REAL NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_ws ON spend_ledger(workspace_id);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


def sha256(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or settings.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connection().executescript(SCHEMA)
        self._ensure_demo_workspace()

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside an IMMEDIATE transaction; commit on success, roll back on error."""
        conn = self._connection()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        self._local.depth = depth + 1
        try:
            yield conn
        except Exception:
            if depth == 0:
                conn.execute("ROLLBACK")
            raise
        else:
            if depth == 0:
                conn.execute("COMMIT")
        finally:
            self._local.depth = depth

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        conn = self._connection()
        return conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        conn = self._connection()
        return conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)

    # -- workspaces -----------------------------------------------------------------
    def _ensure_demo_workspace(self) -> None:
        token_hash = sha256(settings.demo_workspace_token)
        if self.one("SELECT id FROM workspaces WHERE token_hash=?", (token_hash,)) is None:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO workspaces(id,name,token_hash,budget_usd,created_at) VALUES(?,?,?,?,?)",
                    ("ws_demo", "Demo workspace", token_hash, settings.workspace_budget_usd, now()),
                )

    def create_workspace(self, name: str, token: str, budget_usd: float | None = None) -> str:
        wid = new_id("ws")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(id,name,token_hash,budget_usd,created_at) VALUES(?,?,?,?,?)",
                (wid, name, sha256(token), budget_usd if budget_usd is not None else settings.workspace_budget_usd, now()),
            )
        return wid

    def workspace_for_token(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        return self.one("SELECT * FROM workspaces WHERE token_hash=?", (sha256(token),))

    def workspace_spend(self, workspace_id: str) -> dict[str, float]:
        row = self.one(
            "SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(input_tokens),0) tokens FROM spend_ledger WHERE workspace_id=?",
            (workspace_id,),
        )
        ws = self.one("SELECT budget_usd FROM workspaces WHERE id=?", (workspace_id,))
        return {"spent_usd": float(row["usd"]), "input_tokens": int(row["tokens"]), "budget_usd": float(ws["budget_usd"]) if ws else 0.0}

    # -- events -----------------------------------------------------------------------
    def append_event(self, job_id: str, type_: str, payload: dict[str, Any]) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq),0)+1 s FROM job_events WHERE job_id=?", (job_id,)).fetchone()
            seq = int(row["s"])
            conn.execute(
                "INSERT INTO job_events(job_id,seq,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (job_id, seq, type_, dumps(payload), now()),
            )
        return seq

    def events_after(self, job_id: str, after_seq: int, limit: int = 500) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM job_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, after_seq, limit)
        )


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database()
        return _db
