import os
import shutil
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="ss_test_"))
os.environ["SS_DATA_DIR"] = str(TMP)
os.environ["SS_FAKE_JEV"] = "1"
os.environ["SS_DEV_WORKSPACE_KEY"] = "test-key"
os.environ["SS_CHUNK_ROWS"] = "50"
os.environ["OPENAI_API_KEY"] = ""

from semantic_sheets.config import settings  # noqa: E402
from semantic_sheets.services import workspaces  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _env():
    settings()
    yield
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def workspace():
    ws, key = workspaces.ensure_default_workspace()
    return ws


@pytest.fixture(scope="session")
def support_csv(tmp_path_factory):
    """A small support-ticket table with tricky cells: leading zeros, quotes, newlines, empties, formulas."""
    p = tmp_path_factory.mktemp("data") / "support.csv"
    rows = ["account_code,text,amount,created"]
    texts = [
        "I want to cancel my order because I cannot afford it right now",
        "Where is my package? It should have arrived yesterday",
        "How do I change my shipping address",
        "Please cancel the subscription, it is too expensive for me",
        "The app crashes every time I open the invoice page",
        "=HYPERLINK(\"http://x\") tricky formula text",
        "",
        "\"quoted, with comma\" and a\nnewline inside",
    ]
    for i in range(120):
        t = texts[i % len(texts)].replace('"', '""')
        if t:
            t += f" (ticket {i})"  # unique per row so the prediction cache does not collapse the fixture
        rows.append(f"{i:04d},\"{t}\",{(i % 7) * 1.5 if i % 9 else ''},2024-01-{(i % 28) + 1:02d}")
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


@pytest.fixture(scope="session")
def dataset(workspace, support_csv):
    from semantic_sheets.services import datasets

    return datasets.import_local_file(workspace.id, support_csv, "support")


def base_plan(dataset_id: str, **kw):
    plan = {
        "plan_version": "1", "source": {"dataset_id": dataset_id},
        "steps": [
            {"id": "labels", "op": "semantic_annotate", "input": "source", "columns": ["text"], "questions": [
                {"name": "cancel_afford", "kind": "boolean", "instruction": "Is the customer cancelling because they cannot afford it?",
                 "criteria": {"true": "cancel, afford, expensive"}},
                {"name": "topic", "kind": "category", "instruction": "What is the topic?", "labels": [
                    {"name": "cancel", "description": "cancel order subscription"}, {"name": "shipping", "description": "package shipping address arrive"},
                    {"name": "bug", "description": "crashes app invoice page"}]},
                {"name": "urgency", "kind": "score", "instruction": "How urgent?", "levels": ["low", "medium", "high"]},
            ]},
            {"id": "selected", "op": "filter", "input": "labels", "where": {"column": "cancel_afford.value", "operator": "eq", "value": True}},
            {"id": "ranked", "op": "sort", "input": "selected", "by": [{"column": "urgency.score", "direction": "desc"}]},
        ],
        "output": "ranked",
    }
    plan.update(kw)
    return plan
