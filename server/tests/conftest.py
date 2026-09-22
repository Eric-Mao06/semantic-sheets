"""Test fixtures: isolated data directory, a fake Jev client with deterministic answers, and an API client."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="semsheet-test-"))
os.environ["SEMSHEET_DATA_DIR"] = str(_TMP)
os.environ["TYPESAFE_API_KEY"] = "test-key"
os.environ.setdefault("OPENAI_API_KEY", "test-openai")
os.environ["SEMSHEET_CHUNK_ROWS"] = "7"  # small chunks so multi-chunk paths are exercised on tiny tables

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from semsheet import db as dbmod  # noqa: E402
from semsheet.api import app  # noqa: E402
from semsheet.engine import jev  # noqa: E402
from semsheet.engine.executor import JobRunner  # noqa: E402
from semsheet.services import Services  # noqa: E402

TOKEN = "demo-token"
H = {"Authorization": f"Bearer {TOKEN}"}


class FakeJev:
    """Deterministic stand-in for the provider. Boolean 'noul' is high when the row text contains the word
    'afford', category picks a label by keyword, score rates by exclamation marks. Records requests for asserts."""

    def __init__(self, fail_every: int | None = None, latency: float = 0.0) -> None:
        self.requests: list[jev.Packet] = []
        self.requests_made = 0
        self.ambiguous_attempts = 0
        self.fail_every = fail_every
        self.latency = latency

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def evaluate(self, packet: jev.Packet, model: str) -> jev.JevResult:
        self.requests.append(packet)
        self.requests_made += 1
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.fail_every and self.requests_made % self.fail_every == 0:
            raise jev.JevError("synthetic provider failure", status=500, retryable=True)
        answers: dict[str, dict] = {}
        for key, qd in packet.questions.items():
            ref = key.split("__")[0]
            text = " ".join(str(v) for v in _flatten(packet.state[ref])).lower()
            if qd["type"] == "noul":
                if "pair" in text or "left_record" in str(packet.state[ref]):
                    left = packet.state[ref]["left_record"]
                    right = packet.state[ref]["right_record"]
                    same = _norm(left) == _norm(right)
                    answers[key] = {"noul": 0.95 if same else 0.05}
                elif "afford" in text:
                    answers[key] = {"noul": 0.92}
                elif "maybe" in text:
                    answers[key] = {"noul": 0.5}
                else:
                    answers[key] = {"noul": 0.04}
            elif qd["type"] == "choice":
                labels = list(qd["criteria"].keys())
                pick = next((label for label in labels if label.split("_")[0] in text), labels[-1])
                probs = {label: (0.9 if label == pick else 0.1 / max(1, len(labels) - 1)) for label in labels}
                answers[key] = {"choice": pick, "confidence": 0.9, "probabilities": probs}
            else:
                n = min(text.count("!"), len(qd["criteria"]) - 1)
                answers[key] = {"score": float(n), "confidence": 0.8, "probabilities": {str(i): (0.9 if i == n else 0.1 / max(1, len(qd["criteria"]) - 1)) for i in range(len(qd["criteria"]))}}
        est = packet.estimated_tokens or 100
        return jev.JevResult(answers=answers, model=model, input_tokens=est, output_tokens=len(answers) * 5, latency_ms=1.0, attempts=1)


def _flatten(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _flatten(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _flatten(x)
    else:
        yield v


def _norm(rec: dict) -> str:
    return " ".join(str(v).lower().replace("-", " ").strip() for v in rec.values())


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _TMP


@pytest.fixture(scope="session")
def database():
    return dbmod.get_db()


@pytest.fixture(scope="session")
def services(database) -> Services:
    return Services(database)


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_jev() -> FakeJev:
    return FakeJev()


def run_job(database, job_id: str, fake: FakeJev | None = None) -> None:
    fake = fake or FakeJev()
    runner = JobRunner(database, fake, "test-worker")
    asyncio.run(runner.run(job_id))


def write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    import csv

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


SUPPORT_ROWS = [
    ["I cannot afford order 1", "ORDER", "cancel_order"],
    ["where is my package?", "ORDER", "track_order"],
    ["i cant afford order 2, cancel it", "ORDER", "cancel_order"],
    ["maybe I want to cancel", "ORDER", "cancel_order"],
    ["how do I change my address", "ACCOUNT", "edit_account"],
    ["Refund please!!", "REFUND", "get_refund"],
    ["I can no longer afford purchase 3", "ORDER", "cancel_order"],
    ["speak to a human!!!", "CONTACT", "contact_human_agent"],
    ["password reset", "ACCOUNT", "recover_password"],
    ["what payment methods do you accept", "PAYMENT", "check_payment_methods"],
    ["cancel my subscription", "CANCEL", "cancel_subscription"],
    ["unable to afford these shoes, cancel order 4", "ORDER", "cancel_order"],
    ["invoice for last month", "INVOICE", "get_invoice"],
    ["", "ORDER", "unknown"],
    ["shipping options?", "DELIVERY", "delivery_options"],
    ["can i pay later, I cannot afford the item right now, cancel order 5", "ORDER", "cancel_order"],
    ["track order 9", "ORDER", "track_order"],
]


@pytest.fixture(scope="session")
def support_dataset(client, data_dir):
    """Import a tiny support-request table once per session via the public API."""
    p = write_csv(data_dir / "support.csv", ["instruction", "category", "intent"], SUPPORT_ROWS)
    r = client.post("/api/uploads", json={"filename": "support.csv"}, headers=H)
    assert r.status_code == 200, r.text
    up = r.json()
    r = client.put(f"/api/uploads/{up['upload_id']}/content", content=p.read_bytes(), headers=H)
    assert r.status_code == 200, r.text
    r = client.post("/api/datasets/import", json={"upload_id": up["upload_id"], "name": "support", "options": {}}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["dataset"]
