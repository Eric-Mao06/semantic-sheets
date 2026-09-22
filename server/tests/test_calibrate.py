"""Threshold calibration: the unsupervised cut rule, and the post-stage pass that applies it to a job."""
from __future__ import annotations

import json
import uuid

from semsheet.engine import calibrate, jev
from tests.conftest import H, FakeJev, run_job, write_csv


# -- the rule ------------------------------------------------------------------------------------

def test_otsu_cut_lands_in_the_gap_between_two_clusters():
    scores = [0.02] * 60 + [0.05] * 40 + [0.55, 0.61, 0.7, 0.9, 0.95]
    cut = calibrate.otsu_cut(scores)
    assert 0.06 <= cut <= 0.55
    cal = calibrate.calibrate(scores, 0.7, 0.3)
    assert cal.mode == "auto" and cal.method == calibrate.VERSION and not cal.clamped and cal.cut == cut
    assert all(calibrate.decide(s, cal)[0] for s in scores if s >= 0.55)
    assert not any(calibrate.decide(s, cal)[0] for s in scores if s <= 0.05)


def test_otsu_plateau_midpoint_when_the_gap_is_wide():
    scores = [0.10] * 100 + [0.90] * 100
    assert abs(calibrate.otsu_cut(scores) - 0.5) <= 0.02


def test_single_cluster_is_clamped_so_nothing_splits_noise():
    low = [0.01 + (i % 7) * 0.01 for i in range(200)]
    cal = calibrate.calibrate(low, 0.7, 0.3)
    assert cal.clamped and cal.cut == calibrate.CLAMP[0]
    assert not any(calibrate.decide(s, cal)[0] for s in low)
    high = [0.9 + (i % 7) * 0.01 for i in range(200)]
    cal = calibrate.calibrate(high, 0.7, 0.3)
    assert cal.clamped and cal.cut == calibrate.CLAMP[1]
    assert all(calibrate.decide(s, cal)[0] for s in high)


def test_small_inputs_fall_back_to_the_planner_thresholds():
    cal = calibrate.calibrate([0.1, 0.9] * 10, 0.7, 0.3)
    assert cal.mode == "fallback" and cal.cut == 0.7 and cal.flag_margin == 0.0
    assert calibrate.calibrate([0.1] * 100, 0.7, 0.3, mode="fixed").mode == "fixed"


def test_near_flag_is_symmetric_around_the_cut():
    cal = calibrate.Calibration("auto", 0.5, 0.1, 100)
    assert calibrate.decide(0.41, cal) == (False, True)
    assert calibrate.decide(0.59, cal) == (True, True)
    assert calibrate.decide(0.39, cal) == (False, False)
    assert calibrate.decide(0.61, cal) == (True, False)


# -- the pass ------------------------------------------------------------------------------------

class ScoredJev(FakeJev):
    """Answers a boolean with the probability written in the row text ("p=0.63")."""

    async def evaluate(self, packet: jev.Packet, model: str) -> jev.JevResult:
        answers = {}
        for key, qd in packet.questions.items():
            ref = key.split("__")[0]
            text = json.dumps(packet.state[ref])
            p = float(text.split("p=")[1].split('"')[0])
            answers[key] = {"noul": p}
        return jev.JevResult(answers=answers, model=model, input_tokens=100, output_tokens=5, latency_ms=1.0, attempts=1)


def _import(client, path):
    up = client.post("/api/uploads", json={"filename": path.name}, headers=H).json()
    client.put(f"/api/uploads/{up['upload_id']}/content", content=path.read_bytes(), headers=H)
    return client.post("/api/datasets/import", json={"upload_id": up["upload_id"], "name": path.stem}, headers=H).json()["dataset"]


def _scores():
    # 70 clear negatives, a sparse middle, 10 clear positives. Otsu cuts the widest middle gap (0.31 | 0.45) -> 0.385.
    return [0.01 + (i % 5) * 0.01 for i in range(70)] + [0.24, 0.27, 0.31, 0.45] + [0.72 + (i % 3) * 0.03 for i in range(10)]


def _plan(ds, mode):
    return {"plan_version": "1", "source": {"dataset_id": ds["dataset_id"], "version_id": ds["version_id"]}, "model": "jev-1.13.0", "output": "keep", "steps": [
        {"id": "ann", "op": "semantic_annotate", "input": "source", "columns": ["text"], "questions": [
            {"name": "hit", "kind": "boolean", "instruction": "Is it a hit?", "thresholds": {"mode": mode, "true_min": 0.7, "false_max": 0.3}}]},
        {"id": "keep", "op": "filter", "input": "ann", "where": {"op": "eq", "args": [{"column": "hit.value"}, {"literal": True}]}, "unknown_policy": "separate"},
    ]}


def _run(client, database, ds, mode):
    v = client.post("/api/plans/validate", json={"plan": _plan(ds, mode)}, headers=H)
    assert v.status_code == 200, v.text
    j = client.post("/api/jobs", json={"plan_hash": v.json()["plan_hash"], "limits": {"max_source_rows": 1000}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    run_job(database, j["job_id"], ScoredJev())
    return client.get(f"/api/jobs/{j['job_id']}", headers=H).json()


def test_auto_mode_answers_every_row_and_flags_the_ones_near_the_cut(client, database, data_dir):
    scores = _scores()
    ds = _import(client, write_csv(data_dir / "calib_auto.csv", ["text"], [[f"row {i} p={s:.2f}"] for i, s in enumerate(scores)]))
    jj = _run(client, database, ds, "auto")
    assert jj["state"] == "succeeded", jj
    st = jj["progress"]["stages"]["ann"]
    assert st["rows_uncertain"] == 0 and st["rows_succeeded"] == len(scores)
    cal = calibrate.calibrate(scores, 0.7, 0.3)
    expected_flagged = sum(1 for s in scores if calibrate.decide(s, cal)[1])
    assert st["rows_flagged"] == expected_flagged > 0

    rv = jj["result_version_id"]
    res = client.get(f"/api/results/{rv}", headers=H).json()
    assert res["manifest"]["calibration"]["ann"]["hit"]["mode"] == "auto"
    assert res["manifest"]["calibration"]["ann"]["hit"]["cut"] == cal.cut

    ann = client.post(f"/api/results/{rv}/query", json={"step": "ann", "limit": 500}, headers=H).json()["rows"]
    assert all(r["hit.status"] == "ok" and r["hit.value"] is not None for r in ann)
    by_score = {round(r["hit.score"], 2): r for r in ann}
    assert by_score[0.45]["hit.value"] is True and by_score[0.31]["hit.value"] is False and by_score[0.05]["hit.value"] is False
    assert by_score[0.31]["hit.near"] is True and by_score[0.45]["hit.near"] is True
    assert by_score[0.27]["hit.near"] is False and by_score[0.72]["hit.near"] is False

    kept = client.post(f"/api/results/{rv}/query", json={"step": "keep", "limit": 500}, headers=H).json()["rows"]
    assert {round(r["hit.score"], 2) for r in kept} == {s for s in {round(x, 2) for x in scores} if s >= cal.cut}
    review = client.post(f"/api/results/{rv}/query", json={"step": "keep__review", "limit": 500}, headers=H).json()["rows"]
    assert {round(r["hit.score"], 2) for r in review} == {0.31, 0.45}  # near rows on both sides of the cut, nothing else
    # flagged rows that pass the filter are in the output *and* the review view
    assert 0.45 in {round(r["hit.score"], 2) for r in kept}


def test_match_accept_cut_is_calibrated_on_best_candidate_scores(client, database, data_dir):
    # left text carries the pair probability the fake provider returns; the right table is tiny so every
    # left row gets the same candidates and its decision depends only on that probability
    scores = [0.02 + (i % 4) * 0.01 for i in range(45)] + [0.38, 0.42, 0.46, 0.5, 0.55] + [0.9 + (i % 3) * 0.02 for i in range(10)]
    left = write_csv(data_dir / "calib_left.csv", ["offer"], [[f"widget {i} p={s:.2f}"] for i, s in enumerate(scores)])
    right = write_csv(data_dir / "calib_right.csv", ["title", "sku"], [["widget", "W1"], ["gadget", "G1"]])
    l, r = _import(client, left), _import(client, right)
    plan = {"plan_version": "1", "source": {"dataset_id": l["dataset_id"], "version_id": l["version_id"]}, "model": "jev-1.13.0", "output": "m", "steps": [
        {"id": "m", "op": "semantic_match", "input": "source", "right": {"dataset_id": r["dataset_id"]}, "left_columns": ["offer"], "right_columns": ["title"],
         "instruction": "Same product?", "candidates_per_row": 2, "accept_min": 0.8, "reject_max": 0.3, "right_output_columns": ["sku"]}]}
    v = client.post("/api/plans/validate", json={"plan": plan}, headers=H)
    assert v.status_code == 200, v.text
    j = client.post("/api/jobs", json={"plan_hash": v.json()["plan_hash"], "limits": {}, "idempotency_key": str(uuid.uuid4())}, headers=H).json()
    run_job(database, j["job_id"], ScoredJev())
    jj = client.get(f"/api/jobs/{j['job_id']}", headers=H).json()
    assert jj["state"] == "succeeded", jj
    st = jj["progress"]["stages"]["m"]
    assert st["rows_uncertain"] == 0 and st["rows_succeeded"] == len(scores)
    res = client.get(f"/api/results/{jj['result_version_id']}", headers=H).json()
    cal = res["manifest"]["calibration"]["m"]["match"]
    assert cal["mode"] == "auto" and 0.06 <= cal["cut"] <= 0.9
    rows = client.post(f"/api/results/{jj['result_version_id']}/query", json={"limit": 500}, headers=H).json()["rows"]
    for row in rows:
        p = round(row["match.score"], 2)
        assert row["match.status"] == ("matched" if p >= cal["cut"] else "unmatched")
        assert (row["match.sku"] == "W1") == (row["match.status"] == "matched")
        assert row["match.near"] == (abs(p - cal["cut"]) <= cal["flag_margin"] + 1e-9)
    assert st["rows_flagged"] == sum(1 for r_ in rows if r_["match.near"])


def test_fixed_mode_keeps_the_three_way_behaviour(client, database, data_dir):
    scores = _scores()
    ds = _import(client, write_csv(data_dir / "calib_fixed.csv", ["text"], [[f"row {i} p={s:.2f}"] for i, s in enumerate(scores)]))
    jj = _run(client, database, ds, "fixed")
    assert jj["state"] == "succeeded", jj
    st = jj["progress"]["stages"]["ann"]
    assert st["rows_uncertain"] == 2 and st["rows_flagged"] == 0  # 0.31 and 0.45 sit between false_max and true_min
    rv = jj["result_version_id"]
    res = client.get(f"/api/results/{rv}", headers=H).json()
    assert res["manifest"]["calibration"]["ann"]["hit"]["mode"] == "fixed"
    review = client.post(f"/api/results/{rv}/query", json={"step": "keep__review", "limit": 500}, headers=H).json()["rows"]
    assert {round(r["hit.score"], 2) for r in review} == {0.31, 0.45} and all(r["hit.value"] is None for r in review)
