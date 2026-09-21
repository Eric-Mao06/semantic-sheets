"""Worker: claims queued jobs (or jobs whose lease expired), runs them, and renews leases via checkpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import socket
import threading
import time
from typing import Any

from sqlalchemy import or_, select, update

from .config import settings
from .db import Job, session, utcnow
from .engine.executor import JobRunner

log = logging.getLogger("semantic_sheets.worker")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def claim_job(wid: str) -> str | None:
    cfg = settings()
    now = utcnow()
    with session() as s:
        cand = s.scalars(select(Job).where(or_(Job.state == "queued",
                                                (Job.state == "running") & (Job.lease_until < now)))
                         .order_by(Job.created_at).limit(1)).first()
        if cand is None:
            return None
        res = s.execute(update(Job).where(Job.id == cand.id, Job.state == cand.state, Job.lease_until == cand.lease_until)
                        .values(lease_owner=wid, lease_until=now + dt.timedelta(seconds=cfg.worker_lease_seconds),
                                attempts=Job.attempts + 1))
        s.commit()
        if res.rowcount != 1:
            return None
        return cand.id


def run_job(job_id: str, wid: str, client: Any = None) -> str:
    runner = JobRunner(job_id, wid, client=client)
    return asyncio.run(runner.run())


def run_forever(poll_seconds: float = 0.5, stop: threading.Event | None = None, client_factory: Any = None) -> None:
    wid = worker_id()
    log.info("worker %s started", wid)
    while stop is None or not stop.is_set():
        try:
            job_id = claim_job(wid)
        except Exception as e:  # noqa: BLE001
            log.exception("claim failed: %s", e)
            time.sleep(poll_seconds)
            continue
        if job_id is None:
            time.sleep(poll_seconds)
            continue
        log.info("worker %s running job %s", wid, job_id)
        try:
            state = run_job(job_id, wid, client=client_factory() if client_factory else None)
            log.info("job %s finished: %s", job_id, state)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed: %s", job_id, e)


def run_once(client: Any = None) -> str | None:
    wid = worker_id()
    job_id = claim_job(wid)
    if job_id is None:
        return None
    return run_job(job_id, wid, client=client)


def start_inline_worker() -> threading.Event:
    stop = threading.Event()
    t = threading.Thread(target=run_forever, kwargs={"stop": stop}, daemon=True, name="inline-worker")
    t.start()
    return stop


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_forever()


if __name__ == "__main__":
    main()
