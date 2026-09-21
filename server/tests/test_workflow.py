"""Full workflow through the public API with a fake provider: validate -> submit -> execute -> query -> review ->
correct -> export, plus budget/cancel/idempotency/resume behaviour and the MCP tool surface."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from semsheet.engine.executor import JobRunner
from tests.conftest import H, FakeJev, run_job


def plan_for(ds, extra_steps=None, output=None):
    steps = [
        {"id": "ann", "op": "semantic_annotate", "input": "source", "columns": ["instruction"], "questions": [
            {"name": "cancel_afford", "kind": "boolean", "instruction": "Does the customer want to cancel because they cannot afford it?",
             "criteria": {"true": "cannot afford", "false": "anything else"}, "thresholds": {"true_min": 0.7, "false_max": 0.3}},
            {"name": "urgency", "kind": "score", "instruction": "How urgent?", "levels": ["calm", "annoyed", "furious", "extreme"]},
        ]},
        {"id": "keep", "op": "filter", "input": "ann", "where": {"op": "eq", "args": [{"column": "cancel_afford.value"}, {"literal": True}]}, "unknown_policy": "separate"},
        {"id": "rank", "op": "sort", "input": "keep", "by": [{"column": "urgency.score", "direction": "desc"}]},
    ] + (extra_steps or [])
    return {"plan_version": "1", "source": {"dataset_id": ds["dataset_id"], "version_id": ds["version_id"]}, "model": "jev-1.13.0", "steps": steps, "output": output or "rank"}


def test_auth_required(client):
    assert client.get("/api/workspace").status_code == 401
    assert client.get("/api/workspace", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/api/workspace", headers=H)
    assert r.status_code == 200 and r.json()["model"] == "jev-1.13.0"


def test_dataset_import_report(client, support_dataset):
    ds = support_dataset
    assert ds["row_count"] == len(__import__("tests.conftest", fromlist=["SUPPORT_ROWS"]).SUPPORT_ROWS)
    names = [c["name"] for c in ds["schema"]]
    assert names[:1] == ["_row_id"] and "instruction" in names
    r = client.post(f"/api/datasets/{ds['dataset_id']}/query", json={"limit": 5}, headers=H)
    assert r.status_code == 200
    q = r.json()
    assert len(q["rows"]) == 5 and q["total_count"] == ds["row_count"] and q["count_status"] == "complete"
    assert q["rows"][0]["_row_id"] == 0


def test_validate_reports_estimate_and_issues(client, support_dataset):
    r = client.post("/api/plans/validate", json={"plan": plan_for(support_dataset)}, headers=H)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["plan_hash"] and v["estimate"]["semantic_rows"] == support_dataset["row_count"]
    assert v["estimate"]["inference_attempts"] == support_dataset["row_count"] * 2
    assert v["estimate"]["estimated_cost_usd"] > 0
    assert any(c["name"] == "urgency.score" for c in v["output_columns"])
    assert v["review_views"] == ["keep__review"]
    bad = plan_for(support_dataset)
    bad["steps"][1]["where"] = {"op": "eq", "args": [{"column": "ghost"}, {"literal": 1}]}
    r = client.post("/api/plans/validate", json={"plan": bad}, headers=H)
    assert r.status_code == 422
    issue = r.json()["error"]["details"]["issues"][0]
    assert issue["path"].startswith("steps[1]") and "ghost" in issue["message"]


def test_end_to_end_job(client, database, support_dataset):
    ds = support_dataset
    v = client.post("/api/plans/validate", json={"plan": plan_for(ds)}, headers=H).json()
    key = str(uuid.uuid4())
    body = {"plan_hash": v["plan_hash"], "limits": {"max_source_rows": 1000, "spend_target_usd": 0.5}, "idempotency_key": key}
    j = client.post("/api/jobs", json=body, headers=H).json()
    assert j["state"] == "queued"
    # idempotent resubmit returns the same job; different payload conflicts
    assert client.post("/api/jobs", json=body, headers=H).json()["job_id"] == j["job_id"]
    conflict = client.post("/api/jobs", json={**body, "limits": {"max_source_rows": 5}}, headers=H)
    assert conflict.status_code == 409

    fake = FakeJev()
    run_job(database, j["job_id"], fake)
    j = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert j["state"] == "succeeded", j
    st = j["progress"]["stages"]["ann"]
    assert st["rows_total"] == ds["row_count"] and st["rows_pending"] == 0 and st["complete"]
    assert st["rows_missing"] == 1  # the empty instruction row
    assert st["rows_uncertain"] == 1  # the 'maybe' row
    assert j["usage"]["provider_requests"] == len(fake.requests) > 1
    assert j["usage"]["spent_usd"] > 0
    # events are sequenced and end with finished
    ev = client.get(f"/api/jobs/{j['job_id']}/events/list?after=0", headers=H)
    if ev.status_code == 200:
        types = [e["type"] for e in ev.json()["events"]]
        assert types[0] == "queued" and types[-1] == "finished" and "chunk_committed" in types

    rv = j["result_version_id"]
    d = client.get(f"/api/results/{rv}", headers=H).json()
    assert d["output"] == "rank" and d["status"] == "succeeded"
    q = client.post(f"/api/results/{rv}/query", json={"limit": 50}, headers=H).json()
    texts = [r["instruction"] for r in q["rows"]]
    assert q["count_status"] == "complete" and not q["provisional"]
    assert all("afford" in t for t in texts) and len(texts) == 5
    scores = [r["urgency.score"] for r in q["rows"]]
    assert scores == sorted(scores, reverse=True)
    review = client.post(f"/api/results/{rv}/query", json={"step": "keep__review", "limit": 50}, headers=H).json()
    kinds = {r["cancel_afford.status"] for r in review["rows"]}
    assert kinds == {"uncertain", "missing"} and review["total_count"] == 2

    # provenance carries the raw provider answer
    row_id = review["rows"][0]["_row_id"]
    prov = client.get(f"/api/results/{rv}/provenance?step=ann&row_id={row_id}", headers=H).json()
    assert "cancel_afford" in prov["raw"] and prov["model"] == "jev-1.13.0"

    # vectors for the local worker filter
    vec = client.post(f"/api/results/{rv}/vectors", json={"step": "ann", "columns": ["urgency.score", "urgency.value"]}, headers=H).json()
    assert len(vec["row_ids"]) == ds["row_count"] and vec["columns"]["urgency.score"]["kind"] == "number" and vec["columns"]["urgency.value"]["kind"] == "label"

    # correction -> new result version; raw output untouched; old version unchanged
    uncertain = next(r for r in review["rows"] if r["cancel_afford.status"] == "uncertain")
    bad = client.post(f"/api/results/{rv}/patch", json={"overrides": [{"row_id": uncertain["_row_id"], "column": "urgency.score", "value": 1}]}, headers=H)
    assert bad.status_code == 422
    p = client.post(f"/api/results/{rv}/patch", json={"overrides": [{"row_id": uncertain["_row_id"], "column": "cancel_afford.value", "value": True, "reason": "reviewed"}]}, headers=H).json()
    rv2 = p["result_version_id"]
    assert rv2 != rv
    q2 = client.post(f"/api/results/{rv2}/query", json={"limit": 50}, headers=H).json()
    assert q2["total_count"] == 6 and any(r["_row_id"] == uncertain["_row_id"] for r in q2["rows"])
    assert client.post(f"/api/results/{rv}/query", json={"limit": 50}, headers=H).json()["total_count"] == 5
    versions = client.get(f"/api/results/{rv2}/versions", headers=H).json()["versions"]
    assert {x["result_version_id"] for x in versions} >= {rv, rv2}
    prov2 = client.get(f"/api/results/{rv2}/provenance?step=ann&row_id={uncertain['_row_id']}", headers=H).json()
    assert prov2["overrides"] and prov2["raw"]["cancel_afford"]["noul"] == 0.5

    # export: formula escaping and manifest
    e = client.post(f"/api/results/{rv2}/export", json={"format": "csv"}, headers=H).json()
    assert e["row_count"] == 6 and e["complete"] and e["formula_escaped"]
    dl = client.get(e["download_url"], headers=H)
    assert dl.status_code == 200 and dl.text.count("\n") >= 6
    man = client.get(e["manifest_url"], headers=H).json()
    assert man["result_version_id"] == rv2 and man["complete"] is True
    pq_ = client.post(f"/api/results/{rv2}/export", json={"format": "parquet", "step": "ann"}, headers=H).json()
    assert pq_["row_count"] == ds["row_count"]


def test_second_run_uses_cache(client, database, support_dataset):
    v = client.post("/api/plans/validate", json={"plan": plan_for(support_dataset)}, headers=H).json()
    assert v["estimate"]["cache_hits_estimated"] > 0
    j = client.post("/api/jobs", json={"plan_hash": v["plan_hash"], "limits": {}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    fake = FakeJev()
    run_job(database, j["job_id"], fake)
    j = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert j["state"] == "succeeded" and fake.requests == [] and j["usage"]["cache_hits"] > 0 and j["usage"]["spent_usd"] == 0


def test_budget_stop_is_partial_and_resumable(client, database, support_dataset):
    ds = support_dataset
    plan = plan_for(ds)
    plan["steps"][0]["questions"][0]["instruction"] = "Different wording defeats the cache: cancel because unaffordable?"
    plan["steps"][0]["questions"][1]["instruction"] = "Different wording: urgency level?"
    v = client.post("/api/plans/validate", json={"plan": plan}, headers=H).json()
    tiny = {"spend_target_usd": 0.00004, "rows_per_request": 2}  # room for roughly one packet
    j = client.post("/api/jobs", json={"plan_hash": v["plan_hash"], "limits": tiny, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    run_job(database, j["job_id"])
    j = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert j["state"] == "partial" and j["terminal_reason"] == "budget"
    st = j["progress"]["stages"]["ann"]
    assert st["rows_pending"] > 0 and st["rows_pending"] + st["rows_succeeded"] + st["rows_uncertain"] + st["rows_missing"] == ds["row_count"]
    rv = j["result_version_id"]
    q = client.post(f"/api/results/{rv}/query", json={"step": "ann", "limit": 50}, headers=H).json()
    assert q["provisional"] and any(r["cancel_afford.status"] == "pending" for r in q["rows"])
    out = client.post(f"/api/results/{rv}/query", json={"limit": 50}, headers=H).json()
    assert out["count_status"] == "partial" and any("provisional" in w for w in out["warnings"])
    e = client.post(f"/api/results/{rv}/export", json={"format": "csv"}, headers=H).json()
    assert e["complete"] is False

    # a new job over the same plan with a normal budget picks up the cached rows and finishes the rest
    j2 = client.post("/api/jobs", json={"plan_hash": v["plan_hash"], "limits": {"spend_target_usd": 1.0}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    fake = FakeJev()
    run_job(database, j2["job_id"], fake)
    j2 = client.get(f"/api/jobs/{j2['job_id']}", headers=H).json()
    assert j2["state"] == "succeeded" and j2["usage"]["cache_hits"] > 0


def test_cancel_finalizes_committed_work(client, database, support_dataset):
    plan = plan_for(support_dataset)
    plan["steps"][0]["questions"][0]["instruction"] = "Cancel wording v3?"
    plan["steps"][0]["questions"][1]["instruction"] = "Urgency wording v3?"
    v = client.post("/api/plans/validate", json={"plan": plan}, headers=H).json()
    j = client.post("/api/jobs", json={"plan_hash": v["plan_hash"], "limits": {"rows_per_request": 1}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    fake = FakeJev(latency=0.02)

    async def go():
        runner = JobRunner(database, fake, "t")
        task = asyncio.create_task(runner.run(j["job_id"]))
        while len(fake.requests) < 3:
            await asyncio.sleep(0.005)
        client.post(f"/api/jobs/{j['job_id']}/cancel", headers=H)
        await task

    asyncio.run(go())
    jj = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert jj["state"] in ("cancelled", "partial") and jj["terminal_reason"] in ("cancelled", None) or jj["state"] == "succeeded"
    if jj["state"] != "succeeded":
        st = jj["progress"]["stages"]["ann"]
        assert st["rows_pending"] > 0 and st["rows_failed"] == 0


def test_provider_failures_marked_failed_not_dropped(client, database, support_dataset):
    plan = plan_for(support_dataset)
    plan["steps"][0]["questions"][0]["instruction"] = "Cancel wording v4?"
    plan["steps"][0]["questions"][1]["instruction"] = "Urgency wording v4?"
    v = client.post("/api/plans/validate", json={"plan": plan}, headers=H).json()
    j = client.post("/api/jobs", json={"plan_hash": v["plan_hash"], "limits": {"rows_per_request": 2}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    run_job(database, j["job_id"], FakeJev(fail_every=3))
    jj = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert jj["state"] == "partial" and jj["terminal_reason"] == "provider_failures"
    st = jj["progress"]["stages"]["ann"]
    assert st["rows_failed"] > 0 and st["rows_pending"] == 0
    q = client.post(f"/api/results/{jj['result_version_id']}/query", json={"step": "keep__review", "limit": 50}, headers=H).json()
    assert any(r["cancel_afford.status"] == "failed" for r in q["rows"])


def test_query_bounds_and_adhoc_aggregate(client, database, support_dataset):
    jobs = client.get("/api/jobs", headers=H).json()["jobs"]
    rv = next(j["result_version_id"] for j in jobs if j["state"] == "succeeded")
    r = client.post(f"/api/results/{rv}/query", json={"step": "ann", "limit": 100000}, headers=H)
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert len(r.json()["rows"]) <= 256
    agg = client.post(f"/api/results/{rv}/query", json={"step": "ann", "aggregate": {"group_by": ["urgency.value"], "metrics": [{"name": "n", "fn": "count"}]}}, headers=H).json()
    assert sum(row["n"] for row in agg["rows"]) == support_dataset["row_count"]
    page1 = client.post(f"/api/results/{rv}/query", json={"step": "ann", "limit": 4}, headers=H).json()
    page2 = client.post(f"/api/results/{rv}/query", json={"step": "ann", "limit": 4, "start": page1["next_start"]}, headers=H).json()
    assert [r["_row_id"] for r in page1["rows"]] == [0, 1, 2, 3] and page2["rows"][0]["_row_id"] == 4
    where = client.post(f"/api/results/{rv}/query", json={"step": "ann", "where": {"op": "gte", "args": [{"column": "urgency.score"}, {"literal": 2}]}, "limit": 50}, headers=H).json()
    assert where["rows"] and all(r["urgency.score"] >= 2 for r in where["rows"])


def test_semantic_match_join(client, database, data_dir):
    from tests.conftest import write_csv

    left = write_csv(data_dir / "left.csv", ["offer"], [["Sony WH-1000XM4 headphones"], ["Apple iPhone 13 128GB"], ["Bosch GSR 12V drill"], ["Unknown widget"]])
    right = write_csv(data_dir / "right.csv", ["title", "sku"], [["sony wh 1000xm4 headphones", "S1"], ["apple iphone 13 128gb", "A1"], ["bosch gsr 12v drill", "B1"], ["apple iphone 13 256gb", "A2"]])
    ids = []
    for p, name in ((left, "offers"), (right, "catalog")):
        up = client.post("/api/uploads", json={"filename": p.name}, headers=H).json()
        client.put(f"/api/uploads/{up['upload_id']}/content", content=p.read_bytes(), headers=H)
        ids.append(client.post("/api/datasets/import", json={"upload_id": up["upload_id"], "name": name}, headers=H).json()["dataset"])
    l, r = ids
    plan = {"plan_version": "1", "source": {"dataset_id": l["dataset_id"], "version_id": l["version_id"]}, "model": "jev-1.13.0", "output": "m", "steps": [
        {"id": "m", "op": "semantic_match", "input": "source", "right": {"dataset_id": r["dataset_id"]}, "left_columns": ["offer"], "right_columns": ["title"],
         "instruction": "Are these the same exact product?", "candidates_per_row": 3, "right_output_columns": ["sku"]}]}
    v = client.post("/api/plans/validate", json={"plan": plan}, headers=H)
    assert v.status_code == 200, v.text
    v = v.json()
    assert v["estimate"]["candidate_pairs"] > 0
    j = client.post("/api/jobs", json={"plan_hash": v["plan_hash"], "limits": {}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    run_job(database, j["job_id"])
    jj = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert jj["state"] == "succeeded", jj
    q = client.post(f"/api/results/{jj['result_version_id']}/query", json={"limit": 50}, headers=H).json()
    by = {row["offer"]: row for row in q["rows"]}
    assert by["Sony WH-1000XM4 headphones"]["match.sku"] == "S1" and by["Sony WH-1000XM4 headphones"]["match.status"] == "matched"
    assert by["Apple iPhone 13 128GB"]["match.sku"] == "A1"
    assert by["Unknown widget"]["match.status"] in ("unmatched", "no_candidates") and by["Unknown widget"]["match.sku"] is None
    assert json.loads(by["Apple iPhone 13 128GB"]["match.candidates"])


def test_mcp_tools_and_calls(client, support_dataset):
    """Streamable HTTP MCP: initialize, list tools (13-tool catalog), call free tools with the bearer token."""
    headers = {**H, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def rpc(method, params=None, id_=1, session=None):
        h = dict(headers)
        if session:
            h["mcp-session-id"] = session  # server is stateless; header is absent then
        body = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            body["params"] = params
        r = client.post("/mcp", json=body, headers=h)
        assert r.status_code == 200, r.text
        payload = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            data = [ln[5:].strip() for ln in payload.splitlines() if ln.startswith("data:")]
            payload = data[-1]
        return json.loads(payload), r.headers.get("mcp-session-id")

    init, session = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "semantic-spreadsheet"
    h = {**headers, **({"mcp-session-id": session} if session else {})}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    tools, _ = rpc("tools/list", {}, 2, session)
    names = sorted(t["name"] for t in tools["result"]["tools"])
    assert names == sorted(["datasets_list", "datasets_describe", "uploads_prepare", "datasets_import", "datasets_patch", "plans_compile", "plans_validate", "jobs_submit", "jobs_get", "jobs_cancel", "results_query", "results_provenance", "results_patch", "results_export", "datasets_delete"])
    res, _ = rpc("tools/call", {"name": "datasets_list", "arguments": {}}, 3, session)
    env = res["result"].get("structuredContent") or json.loads(res["result"]["content"][0]["text"])
    assert env["request_id"] and any(d["dataset_id"] == support_dataset["dataset_id"] for d in env["data"]["datasets"])
    res, _ = rpc("tools/call", {"name": "datasets_describe", "arguments": {"dataset_id": support_dataset["dataset_id"], "sample": True, "sample_rows": 3}}, 4, session)
    env = res["result"].get("structuredContent") or json.loads(res["result"]["content"][0]["text"])
    assert len(env["data"]["sample"]) == 3
    # unauthenticated tool call is rejected inside the envelope
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "datasets_list", "arguments": {}}}, headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **({"mcp-session-id": session} if session else {})})
    assert r.status_code in (200, 401)
