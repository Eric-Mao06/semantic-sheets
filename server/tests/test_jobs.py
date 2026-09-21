import json
import time

import pytest

from semantic_sheets import worker
from semantic_sheets.db import Job, Workspace, session
from semantic_sheets.errors import BudgetExceeded, Conflict, ValidationFailed
from semantic_sheets.jev.client import FakeJevClient
from semantic_sheets.services import datasets, jobs, results
from semantic_sheets.storage import step_chunk_files, step_final_parquet
from tests.conftest import base_plan


def run(job_id, client=None):
    return worker.run_job(job_id, "test-worker", client=client)


def test_job_lifecycle_and_results(workspace, dataset):
    plan = base_plan(dataset.id)
    plan["steps"][0]["questions"][2]["instruction"] = "Lifecycle test: how urgent is this?"  # fresh cache keys
    job, created = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0}, idempotency_key="life-1")
    assert created and job.state == "queued" and job.reserved_usd > 0
    with session() as s:
        ws = s.get(Workspace, workspace.id)
        assert ws.reserved_usd >= job.reserved_usd
    assert worker.run_once() == "succeeded"
    j = jobs.describe(jobs.get(workspace.id, job.id))
    p = j["progress"]
    assert p["source_rows"] == 120 and p["succeeded"] + p["skipped"] == 120 and p["failed"] == 0 and p["pending"] == 0
    assert p["skipped"] == 15, "empty text rows are missing_input, not false"
    assert j["usage"]["requests"] > 0 and j["usage"]["cost_usd"] > 0 and j["reserved_usd"] == 0
    rv = j["result_version_id"]
    r = results.query(workspace.id, rv, {"limit": 5, "columns": ["text", "cancel_afford.p", "urgency.score"]}, max_rows=50, max_bytes=16384)
    assert r["count_status"] == "complete" and not r["provisional"] and r["total_count"] == 30
    assert all(row[2] is not None for row in r["rows"])
    scores = [row[2] for row in r["rows"]]
    assert scores == sorted(scores, reverse=True), "sorted by score desc"
    # review view of the filter: unknown rows (uncertain or missing input)
    review = results.query(workspace.id, rv, {"scope": {"review_of": "selected"}}, max_rows=50, max_bytes=16384)
    assert review["total_count"] == 15
    agg = results.query(workspace.id, rv, {"scope": {"step": "labels"}, "aggregate": {"group_by": ["topic.value"], "metrics": [{"fn": "count", "name": "n"}]}}, max_rows=50, max_bytes=16384)
    assert sum(row[1] for row in agg["rows"]) == 120
    assert step_final_parquet(job.id, "labels").exists() and not step_chunk_files(job.id, "labels")


def test_idempotency(workspace, dataset):
    a, c1 = jobs.submit(workspace.id, plan=base_plan(dataset.id), limits={"spend_target_usd": 1.0}, idempotency_key="idem-1")
    b, c2 = jobs.submit(workspace.id, plan=base_plan(dataset.id), limits={"spend_target_usd": 1.0}, idempotency_key="idem-1")
    assert a.id == b.id and c1 and not c2
    with pytest.raises(Conflict):
        jobs.submit(workspace.id, plan=base_plan(dataset.id), limits={"spend_target_usd": 2.0}, idempotency_key="idem-1")
    jobs.cancel(workspace.id, a.id)


def test_rerun_uses_cache_with_zero_provider_calls(workspace, dataset):
    plan = base_plan(dataset.id)
    job, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})
    worker.run_once()
    plan["steps"][0]["questions"][0]["thresholds"] = {"true_min": 0.6, "false_max": 0.3}
    plan["steps"][2]["by"][0]["direction"] = "asc"
    job2, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})
    client = FakeJevClient()
    assert run(job2.id, client) == "succeeded"
    assert client.calls == 0, "threshold and sort edits must not call the provider"
    j = jobs.describe(jobs.get(workspace.id, job2.id))
    assert j["usage"]["requests"] == 0 and j["progress"]["cache_hits"] > 0


def test_budget_and_row_limits_are_enforced(workspace, dataset):
    with pytest.raises(BudgetExceeded):
        jobs.submit(workspace.id, plan=base_plan(dataset.id), limits={"spend_target_usd": 0.0000001})
    with pytest.raises(ValidationFailed) as e:
        jobs.submit(workspace.id, plan=base_plan(dataset.id), limits={"max_source_rows": 10})
    assert e.value.code == "row_limit"
    with pytest.raises(ValidationFailed) as e:
        jobs.submit(workspace.id, plan=base_plan(dataset.id), limits={"max_provider_requests": 1})
    assert e.value.code == "request_limit"


def test_budget_stop_yields_partial_and_exportable(workspace, dataset, monkeypatch):
    plan = base_plan(dataset.id)
    plan["steps"][0]["questions"][0]["instruction"] = "Budget test: is the customer unhappy?"  # new cache keys
    job, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})
    with session() as s:  # shrink the target after admission to force a mid-run stop
        j = s.get(Job, job.id)
        j.limits_json = {**j.limits_json, "spend_target_usd": 0.00001}
        s.commit()
    assert run(job.id) == "partial"
    j = jobs.describe(jobs.get(workspace.id, job.id))
    assert j["terminal_reason"] == "budget" and j["progress"]["pending"] > 0
    r = results.query(workspace.id, j["result_version_id"], {"limit": 5}, max_rows=50, max_bytes=16384)
    assert r["provisional"] and r["count_status"] == "partial"
    e = results.export(workspace.id, j["result_version_id"], "csv")
    assert e.manifest_json["complete"] is False and e.manifest_json["exclusions"]["pending_rows"] > 0


def test_worker_restart_resumes_from_committed_chunks(workspace, dataset):
    plan = base_plan(dataset.id)
    plan["steps"][0]["questions"][0]["instruction"] = "Restart test: does the text mention money?"
    job, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})
    # First worker dies after the provider fails hard on the 3rd call.
    client = FakeJevClient(fail_every=3)
    client_calls_before = client.calls

    class Dying(FakeJevClient):
        async def system_one(self, *a, **k):
            if self.calls >= 10:  # the first window (2 chunks x 5 packets) commits before the 11th call crashes
                raise RuntimeError("worker crashed")
            return await super().system_one(*a, **k)

    dying = Dying()
    state = run(job.id, dying)
    assert state == "failed"
    committed = len(step_chunk_files(job.id, "labels"))
    assert committed >= 1, "chunks committed before the crash survive"
    with session() as s:  # simulate a lease expiry: worker claims and resumes
        j = s.get(Job, job.id)
        j.state = "running"
        j.lease_until = None
        s.commit()
    fresh = FakeJevClient()
    assert run(job.id, fresh) == "succeeded"
    j = jobs.describe(jobs.get(workspace.id, job.id))
    assert j["progress"]["succeeded"] + j["progress"]["skipped"] == 120
    events = jobs.events(job.id)
    resumed = [e for e in events if e.kind == "step_started" and e.payload_json.get("resumed_chunks")]
    assert resumed and resumed[-1].payload_json["resumed_chunks"] == committed
    del client_calls_before


def test_transient_provider_errors_are_retried_and_failed_rows_recorded(workspace, dataset):
    plan = base_plan(dataset.id)
    plan["steps"][0]["questions"][0]["instruction"] = "Retry test: is there a question mark?"
    job, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})
    client = FakeJevClient(fail_every=4)
    state = run(job.id, client)
    j = jobs.describe(jobs.get(workspace.id, job.id))
    assert state == "partial" and j["terminal_reason"] == "row_failures"
    assert j["progress"]["failed"] > 0 and j["usage"]["provider_errors"] > 0 and j["errors"]
    r = results.query(workspace.id, j["result_version_id"], {"scope": {"step": "labels"}, "where": {"column": "cancel_afford.status", "operator": "eq", "value": "failed"}}, max_rows=50, max_bytes=16384)
    assert r["total_count"] == j["progress"]["failed"]


def test_cancellation(workspace, dataset):
    plan = base_plan(dataset.id)
    plan["steps"][0]["questions"][0]["instruction"] = "Cancel test: is this about a package?"
    job, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})
    jobs.cancel(workspace.id, job.id)
    assert jobs.get(workspace.id, job.id).state == "cancelled"
    with session() as s:
        assert s.get(Workspace, workspace.id).reserved_usd >= 0
    # cancel mid-run
    job2, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})

    class Cancelling(FakeJevClient):
        async def system_one(self, *a, **k):
            if self.calls == 1:
                jobs.cancel(workspace.id, job2.id)
            return await super().system_one(*a, **k)

    assert run(job2.id, Cancelling()) == "cancelled"
    j = jobs.describe(jobs.get(workspace.id, job2.id))
    assert j["state"] == "cancelled" and j["progress"]["pending"] > 0
    results.query(workspace.id, j["result_version_id"], {"limit": 1}, max_rows=50, max_bytes=16384)  # still inspectable


def test_corrections_create_versions_and_keep_raw(workspace, dataset):
    job, _ = jobs.submit(workspace.id, plan=base_plan(dataset.id, output="labels"), limits={"spend_target_usd": 1.0})
    worker.run_once()
    rv = jobs.get(workspace.id, job.id).result_version_id
    before = results.row_detail(workspace.id, rv, 0)
    nv = results.patch(workspace.id, rv, [{"row_id": 0, "column": "topic.value", "value": "bug"}], {"user": "t"})
    after = results.row_detail(workspace.id, nv.id, 0)
    assert after["values"]["topic.value"] == "bug" and after["values"]["topic.raw"] == before["values"]["topic.raw"]
    assert before["values"]["topic.value"] != "bug"
    with pytest.raises(ValidationFailed):
        results.patch(workspace.id, rv, [{"row_id": 0, "column": "topic.raw", "value": "x"}], None)
    versions = results.list_versions(workspace.id, job.id)
    assert [v["number"] for v in versions] == [1, 2] and versions[1]["override_count"] == 1
    # dataset corrections produce a new dataset version and invalidate predictions for that row on the next run
    dv = datasets.patch(workspace.id, dataset.id, None, [{"row_id": 0, "column": "text", "value": "totally new text"}], {})
    page = datasets.rows_page(workspace.id, dataset.id, dv.id, 0, 1, ["text"], 10000)
    assert page["rows"][0][1] == "totally new text"
    job2, _ = jobs.submit(workspace.id, plan=base_plan(dataset.id, output="labels"), limits={"spend_target_usd": 1.0})
    client = FakeJevClient()
    run(job2.id, client)
    assert client.calls == 1, "only the corrected row misses the cache"
    datasets.set_current_version(workspace.id, dataset.id, dataset.current_version_id)


def test_export_escapes_formulas_and_writes_manifest(workspace, dataset):
    job, _ = jobs.submit(workspace.id, plan=base_plan(dataset.id, output="labels"), limits={"spend_target_usd": 1.0})
    worker.run_once()
    rv = jobs.get(workspace.id, job.id).result_version_id
    e = results.export(workspace.id, rv, "csv", columns=["text", "cancel_afford.value"])
    text = open(e.path, encoding="utf-8").read()
    assert "'=HYPERLINK" in text and e.row_count == 120 and e.manifest_json["complete"] is True
    raw = results.export(workspace.id, rv, "csv", raw=True, columns=["text"])
    assert "'=HYPERLINK" not in open(raw.path, encoding="utf-8").read()
    pq = results.export(workspace.id, rv, "parquet")
    import duckdb
    assert duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{pq.path}')").fetchone()[0] == 120
    assert e.manifest_json["plan_hash"] and e.manifest_json["dataset_version_id"]


def test_exact_ops(workspace, dataset):
    plan = {"plan_version": "1", "source": {"dataset_id": dataset.id}, "steps": [
        {"id": "d", "op": "derive", "input": "source", "columns": [{"name": "double_amount", "expr": {"fn": "mul", "args": [{"column": "amount"}, {"value": 2}]}}]},
        {"id": "f", "op": "filter", "input": "d", "where": {"column": "double_amount", "operator": "gt", "value": 3}},
        {"id": "dd", "op": "dedupe", "input": "f", "keys": ["text"]},
        {"id": "a", "op": "aggregate", "input": "f", "group_by": ["text"], "metrics": [{"name": "n", "fn": "count"}, {"name": "total", "fn": "sum", "column": "double_amount"}, {"name": "accounts", "fn": "count_distinct", "column": "account_code"}]},
        {"id": "top", "op": "limit", "input": "a", "n": 3},
    ], "output": "top"}
    job, _ = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 0.1})
    assert worker.run_once() == "succeeded"
    rv = jobs.get(workspace.id, job.id).result_version_id
    r = results.query(workspace.id, rv, {"limit": 10}, max_rows=50, max_bytes=16384)
    assert r["total_count"] == 3 and [c["name"] for c in r["columns"]][:2] == ["_row_id", "text"]
    j = results.query(workspace.id, rv, {"scope": {"step": "dd"}, "limit": 50}, max_rows=50, max_bytes=16384)
    distinct = results.query(workspace.id, rv, {"scope": {"step": "f"}, "aggregate": {"metrics": [{"fn": "count_distinct", "column": "text", "name": "d"}]}}, max_rows=50, max_bytes=16384)
    nulls = results.query(workspace.id, rv, {"scope": {"step": "f"}, "where": {"column": "text", "operator": "is_null"}, "aggregate": {"metrics": [{"fn": "count", "name": "n"}]}}, max_rows=50, max_bytes=16384)
    assert j["total_count"] == distinct["rows"][0][0] + (1 if nulls["rows"][0][0] else 0), "one row per key, NULL keys form one group"


def test_join_and_match(workspace, dataset, tmp_path):
    p = tmp_path / "catalog.csv"
    p.write_text("sku,name\nA1,Cancel order helper\nB2,Shipping address tool\nC3,Invoice page fix\n")
    right = datasets.import_local_file(workspace.id, p, "catalog")
    plan = {"plan_version": "1", "source": {"dataset_id": dataset.id}, "steps": [
        {"id": "m", "op": "semantic_match", "input": "source", "right": {"dataset_id": right.id}, "left_columns": ["text"], "right_columns": ["name"],
         "instruction": "Does the catalog item address the customer's message?", "candidates_per_row": 2, "show_right_columns": ["sku", "name"]},
    ], "output": "m"}
    c = jobs.submit(workspace.id, plan=plan, limits={"spend_target_usd": 1.0})[0]
    assert worker.run_once() == "succeeded"
    rv = jobs.get(workspace.id, c.id).result_version_id
    agg = results.query(workspace.id, rv, {"aggregate": {"group_by": ["match.status"], "metrics": [{"fn": "count", "name": "n"}]}}, max_rows=50, max_bytes=16384)
    statuses = dict((row[0], row[1]) for row in agg["rows"])
    assert sum(statuses.values()) == 120 and statuses.get("missing_input") == 15
    r = results.query(workspace.id, rv, {"limit": 3, "columns": ["text", "match.sku", "match.p", "match.status", "match.candidate_count"]}, max_rows=50, max_bytes=16384)
    assert r["columns"][-1]["name"] == "match.candidate_count"
    plan2 = {"plan_version": "1", "source": {"dataset_id": dataset.id}, "steps": [
        {"id": "d", "op": "derive", "input": "source", "columns": [{"name": "sku", "expr": {"fn": "if", "args": [{"column": "text", "operator": "contains", "value": "cancel"}, {"value": "A1"}, {"value": "B2"}]}}]},
        {"id": "j", "op": "join", "input": "d", "right": {"dataset_id": right.id}, "on": [{"left": "sku", "right": "sku"}], "right_columns": ["name"]},
    ], "output": "j"}
    c2 = jobs.submit(workspace.id, plan=plan2, limits={"spend_target_usd": 0.1})[0]
    assert worker.run_once() == "succeeded"
    r = results.query(workspace.id, jobs.get(workspace.id, c2.id).result_version_id, {"limit": 2, "columns": ["text", "right_name"]}, max_rows=50, max_bytes=16384)
    assert r["total_count"] == 120 and r["rows"][0][2] == "Cancel order helper"


def test_query_bounds_and_truncation(workspace, dataset):
    job, _ = jobs.submit(workspace.id, plan=base_plan(dataset.id, output="labels"), limits={"spend_target_usd": 1.0})
    worker.run_once()
    rv = jobs.get(workspace.id, job.id).result_version_id
    r = results.query(workspace.id, rv, {"limit": 500}, max_rows=50, max_bytes=16384)
    assert r["returned"] <= 50 and r["bytes"] <= 16384 and r["has_more"]
    small = results.query(workspace.id, rv, {"limit": 50, "max_bytes": 2000}, max_rows=50, max_bytes=16384)
    assert small["bytes"] <= 2000 and small["returned"] < 50
    with pytest.raises(ValidationFailed):
        results.query(workspace.id, rv, {"order_by": [{"column": "nope", "direction": "asc"}]}, max_rows=50, max_bytes=16384)
    ids = results.query(workspace.id, rv, {"row_ids": [3, 1], "columns": ["text"]}, max_rows=50, max_bytes=16384)
    assert [row[0] for row in ids["rows"]] == [1, 3]
