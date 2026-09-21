import json

import pytest
from fastapi.testclient import TestClient

from semantic_sheets.api.app import create_app
from tests.conftest import base_plan

H = {"Authorization": "Bearer test-key"}


@pytest.fixture(scope="module")
def client():
    app = create_app(mount_mcp=True, serve_web=False)
    with TestClient(app) as c:
        yield c


def test_auth_and_scopes(client):
    assert client.get("/api/v1/workspace").status_code == 401
    assert client.get("/api/v1/workspace", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/workspace", headers=H).json()["name"] == "default"
    assert client.get("/api/v1/datasets/ds_nope", headers=H).status_code == 404


def test_upload_import_query_export_via_rest(client, support_csv):
    prep = client.post("/api/v1/uploads/prepare", json={"filename": "support.csv"}, headers=H).json()
    put = client.put(prep["upload_path"], content=support_csv.read_bytes(), headers=H)
    assert put.status_code == 200 and put.json()["bytes"] == support_csv.stat().st_size
    pv = client.post("/api/v1/uploads/preview", json={"upload_id": prep["upload_id"]}, headers=H).json()
    assert pv["columns"][0]["name"] == "account_code"
    imp = client.post("/api/v1/datasets/import", json={"upload_id": prep["upload_id"], "name": "rest", "wait": True}, headers=H).json()
    assert imp["status"] == "ready" and imp["row_count"] == 120
    ds = imp["dataset_id"]
    d = client.get(f"/api/v1/datasets/{ds}?sample=true", headers=H).json()
    assert len(d["sample"]) <= 20
    v = client.post("/api/v1/plans/validate", json={"plan": base_plan(ds)}, headers=H).json()
    assert v["plan_hash"] and v["estimate"]["provider_requests"] > 0
    bad = client.post("/api/v1/plans/validate", json={"plan": {"plan_version": "1", "source": {"dataset_id": ds}, "steps": [{"id": "x", "op": "filter", "where": {"column": "zz", "operator": "eq", "value": 1}}], "output": "x"}}, headers=H)
    assert bad.status_code == 422 and bad.json()["error"]["path"].startswith("steps[0].where")
    j = client.post("/api/v1/jobs", json={"plan_hash": v["plan_hash"], "limits": {"spend_target_usd": 1}, "idempotency_key": "rest-1"}, headers=H).json()
    assert j["state"] == "queued"
    from semantic_sheets import worker
    assert worker.run_once() == "succeeded"
    jg = client.get(f"/api/v1/jobs/{j['job_id']}", headers=H).json()
    assert jg["state"] == "succeeded" and jg["suggested_poll_seconds"] == 0
    ev = client.get(f"/api/v1/jobs/{j['job_id']}/events?after=0&token=test-key")
    assert ev.status_code == 200 and "event: finished" in ev.text
    rv = jg["result_version_id"]
    q = client.post(f"/api/v1/results/{rv}/query", json={"limit": 5, "columns": ["text", "urgency.score"]}, headers=H).json()
    assert q["returned"] == 5 and q["count_status"] == "complete"
    cols = client.get(f"/api/v1/results/{rv}", headers=H).json()
    assert cols["completion"]["complete"]
    vec = client.get(f"/api/v1/results/{rv}/vectors?columns=urgency.score,cancel_afford.p", headers=H).json()
    assert len(vec["row_ids"]) == q["total_count"]
    e = client.post("/api/v1/exports", json={"result_version_id": rv, "format": "csv"}, headers=H).json()
    dl = client.get(f"/api/v1/exports/{e['export_id']}/download?token=test-key")
    assert dl.status_code == 200 and dl.text.startswith("_row_id,")
    assert client.get(f"/api/v1/exports/{e['export_id']}/download").status_code == 422
    patch = client.post(f"/api/v1/results/{rv}/patch", json={"corrections": [{"row_id": 0, "column": "topic.value", "value": "bug"}]}, headers=H).json()
    assert patch["number"] == 2
    assert client.delete(f"/api/v1/datasets/{ds}", headers=H).json()["status"] == "deleted"
    assert client.get(f"/api/v1/datasets/{ds}", headers=H).status_code == 404


def test_mcp_tools_end_to_end(client, dataset):
    """Drive the mounted MCP server over Streamable HTTP with plain JSON-RPC (stateless, json_response)."""
    def call(method, params=None, id_=1):
        r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                        headers={**H, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        return r.json()

    init = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "semantic-sheets"
    tools = call("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    for n in ("datasets_list", "datasets_describe", "uploads_prepare", "datasets_import", "datasets_patch", "plans_compile", "plans_validate",
              "jobs_submit", "jobs_get", "jobs_cancel", "results_query", "results_export", "datasets_delete"):
        assert n in names
    assert all("inputSchema" in t and "outputSchema" in t for t in tools)
    res = call("tools/call", {"name": "datasets_describe", "arguments": {"dataset_id": dataset.id, "sample": True, "sample_rows": 3}})
    data = res["result"]["structuredContent"]["data"]
    assert data["row_count"] == 120 and len(data["sample"]) == 3
    v = call("tools/call", {"name": "plans_validate", "arguments": {"plan": base_plan(dataset.id)}})["result"]["structuredContent"]["data"]
    j = call("tools/call", {"name": "jobs_submit", "arguments": {"plan_hash": v["plan_hash"], "limits": {"spend_target_usd": 1}, "idempotency_key": "mcp-t1"}})["result"]["structuredContent"]["data"]
    from semantic_sheets import worker
    worker.run_once()
    jg = call("tools/call", {"name": "jobs_get", "arguments": {"job_id": j["job_id"]}})["result"]["structuredContent"]["data"]
    assert jg["state"] == "succeeded"
    q = call("tools/call", {"name": "results_query", "arguments": {"result_version_id": jg["result_version_id"], "spec": {"limit": 500}}})["result"]["structuredContent"]["data"]
    assert q["returned"] <= 50 and q["bytes"] <= 16384
    err = call("tools/call", {"name": "jobs_get", "arguments": {"job_id": "job_missing"}})["result"]["structuredContent"]
    assert err["error"]["code"] == "job_not_found"
    noauth = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "datasets_list", "arguments": {}}},
                         headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    body = noauth.json()
    assert "unauthorized" in json.dumps(body).lower() or body.get("result", {}).get("isError")
