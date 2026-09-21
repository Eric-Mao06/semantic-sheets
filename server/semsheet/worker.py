"""Worker process: claims queued jobs, runs them, and reclaims jobs whose worker died.

Run with `python -m semsheet.worker`. Several workers can run against the same SQLite/Parquet store; each
claims jobs atomically. Committed chunks are reused after a restart."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys

from .config import settings
from .db import get_db, now
from .engine.executor import JobRunner
from .engine.jev import JevClient

log = logging.getLogger("semsheet.worker")
STALE_SECONDS = 90


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = get_db()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    log.info("worker %s started (model=%s, concurrency=%d, rows/request=%d)", worker_id, settings.jev_model, settings.jev_concurrency, settings.jev_rows_per_request)
    async with JevClient() as client:
        runner = JobRunner(db, client, worker_id)
        while not stop.is_set():
            job_id = claim(db, worker_id)
            if job_id is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            log.info("running job %s", job_id)
            try:
                await runner.run(job_id)
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed", job_id)
            log.info("job %s done", job_id)
    log.info("worker %s stopped", worker_id)


def claim(db, worker_id: str) -> str | None:
    """Atomically claim one queued job, or a running job whose heartbeat is stale (worker died)."""
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            row = conn.execute("SELECT id FROM jobs WHERE state='running' AND (heartbeat_at IS NULL OR heartbeat_at < ?) ORDER BY created_at LIMIT 1", (now() - STALE_SECONDS,)).fetchone()
            if row is None:
                return None
            log.warning("reclaiming stale job %s", row["id"])
        conn.execute("UPDATE jobs SET worker_id=?, heartbeat_at=? WHERE id=?", (worker_id, now(), row["id"]))
        return row["id"]


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
