"""Exact engine: expression compilation, plan compilation, provisional semantics, null and tie-break rules."""
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from semsheet.engine import jev
from semsheet.engine.exact import CompileContext, PlanError, compile_plan, connect
from semsheet.models import Plan, Question


def _base(tmp_path: Path) -> CompileContext:
    tbl = pa.table({
        "_row_id": pa.array([0, 1, 2, 3, 4], pa.int64()),
        "text": ["b", "a", None, "a", "c"],
        "price": ["$10.50", "$3", None, "1,200.00", "7"],
        "qty": pa.array([2, 1, 5, None, 3], pa.int64()),
        "country": ["UK", "UK", "FR", "FR", "DE"],
    })
    p = tmp_path / "base.parquet"
    pq.write_table(tbl, p)
    schema = [{"name": "_row_id", "type": "integer"}, {"name": "text", "type": "string"}, {"name": "price", "type": "string"}, {"name": "qty", "type": "integer"}, {"name": "country", "type": "string"}]
    return CompileContext(base_parquet=p, base_schema=schema)


def _plan(steps, output):
    return Plan.model_validate({"plan_version": "1", "source": {"dataset_id": "d", "version_id": "v"}, "model": "jev-1.13.0", "steps": steps, "output": output})


def _rows(compiled, step):
    with connect(compiled) as con:
        return con.execute(compiled.sql_for(step)).fetch_arrow_table().to_pylist()


def test_compute_to_number_filter_sort_nulls_last(tmp_path):
    plan = _plan([
        {"id": "c", "op": "compute", "input": "source", "columns": [
            {"name": "unit", "expr": {"op": "to_number", "args": [{"column": "price"}]}},
            {"name": "revenue", "expr": {"op": "mul", "args": [{"op": "to_number", "args": [{"column": "price"}]}, {"column": "qty"}]}},
        ]},
        {"id": "s", "op": "sort", "input": "c", "by": [{"column": "revenue", "direction": "desc"}]},
    ], "s")
    rows = _rows(compile_plan(plan, _base(tmp_path)), "s")
    units = {r["_row_id"]: r["unit"] for r in rows}
    assert units[0] == 10.5 and units[1] == 3 and units[3] == 1200.0 and units[2] is None
    revs = [r["revenue"] for r in rows]
    assert revs[:3] == [21.0, 21.0, 3.0] or revs[0] == 21.0
    assert revs[-1] is None and revs[-2] is None  # nulls last
    # ties (row 0: 10.5*2 = 21, row 4: 7*3 = 21) break by ascending row id
    top = [r["_row_id"] for r in rows if r["revenue"] == 21.0]
    assert top == [0, 4]


def test_filter_never_matches_null_and_aggregate_exact(tmp_path):
    plan = _plan([
        {"id": "f", "op": "filter", "input": "source", "where": {"op": "eq", "args": [{"column": "text"}, {"literal": "a"}]}},
        {"id": "a", "op": "aggregate", "input": "source", "group_by": ["country"], "metrics": [{"name": "n", "fn": "count"}, {"name": "q", "fn": "sum", "column": "qty"}]},
    ], "a")
    compiled = compile_plan(plan, _base(tmp_path))
    assert sorted(r["_row_id"] for r in _rows(compiled, "f")) == [1, 3]
    agg = {r["country"]: (r["n"], r["q"]) for r in _rows(compiled, "a")}
    assert agg == {"UK": (2, 3), "FR": (2, 5), "DE": (1, 3)}
    assert compiled.relations["a"].row_preserving is False


def test_semantic_columns_are_pending_until_committed(tmp_path):
    plan = _plan([
        {"id": "ann", "op": "semantic_annotate", "input": "source", "columns": ["text"], "questions": [{"name": "isa", "kind": "boolean", "instruction": "Is it a?", "criteria": {"true": "a", "false": "not a"}}]},
        {"id": "f", "op": "filter", "input": "ann", "where": {"op": "eq", "args": [{"column": "isa.value"}, {"literal": True}]}},
    ], "f")
    compiled = compile_plan(plan, _base(tmp_path))
    assert compiled.provisional("f") and compiled.provisional("ann")
    rows = _rows(compiled, "ann")
    assert all(r["isa.status"] == "pending" and r["isa.value"] is None for r in rows)
    assert _rows(compiled, "f") == []
    review = compiled.review_relations["f"]
    assert len(_rows(compiled, review)) == 5  # everything is pending -> everything is in review


def test_plan_errors_have_paths(tmp_path):
    with pytest.raises(PlanError) as e:
        compile_plan(_plan([{"id": "f", "op": "filter", "input": "source", "where": {"op": "eq", "args": [{"column": "nope"}, {"literal": 1}]}}], "f"), _base(tmp_path))
    assert e.value.path.startswith("steps[0]") and "nope" in e.value.message
    with pytest.raises(PlanError):
        compile_plan(_plan([{"id": "f", "op": "limit", "input": "ghost", "n": 5}], "f"), _base(tmp_path))
    with pytest.raises(PlanError):
        compile_plan(_plan([{"id": "f", "op": "limit", "input": "source", "n": 5}], "missing"), _base(tmp_path))


def test_model_validation_rejects_bad_questions():
    with pytest.raises(ValidationError):
        Question.model_validate({"name": "x", "kind": "category", "instruction": "pick"})  # options required
    with pytest.raises(ValidationError):
        Question.model_validate({"name": "x", "kind": "score", "instruction": "rate", "levels": ["only one"]})
    with pytest.raises(ValidationError):
        Question.model_validate({"name": "x", "kind": "boolean", "instruction": "b", "thresholds": {"true_min": 0.2, "false_max": 0.8}})
    with pytest.raises(ValidationError):
        _plan([{"id": "bad id", "op": "limit", "input": "source", "n": 5}], "bad id")


def test_pack_respects_rows_per_request_and_interpret_thresholds():
    q = Question.model_validate({"name": "q", "kind": "boolean", "instruction": "Is it x?", "criteria": {"true": "t", "false": "f"}, "thresholds": {"true_min": 0.7, "false_max": 0.3}})
    items = [jev.PacketItem(row_id=i, payload={"text": f"row {i}"}, questions=[q]) for i in range(23)]
    packets, too_long = jev.pack(items, 10)
    assert [len(p.items) for p in packets] == [10, 10, 3] and too_long == []
    assert set(packets[0].questions) == {f"r{i:02d}__q" for i in range(10)}
    assert jev.interpret(q, {"noul": 0.9})["value"] is True
    assert jev.interpret(q, {"noul": 0.1})["value"] is False
    u = jev.interpret(q, {"noul": 0.5})
    assert u["value"] is None and u["status"] == "uncertain"
    assert jev.interpret(q, None)["status"] == "failed"
    huge = [jev.PacketItem(row_id=99, payload={"text": "x" * 200_000}, questions=[q])]
    _, too_long = jev.pack(huge, 10)
    assert len(too_long) == 1


def test_cache_key_depends_on_question_and_payload_not_row():
    q = Question.model_validate({"name": "q", "kind": "boolean", "instruction": "Is it x?", "criteria": {"true": "t", "false": "f"}})
    q2 = Question.model_validate({"name": "other", "kind": "boolean", "instruction": "Is it x?", "criteria": {"true": "t", "false": "f"}})
    k1 = jev.cache_key("ws", "m", q, {"text": "hello"})
    assert k1 == jev.cache_key("ws", "m", q2, {"text": "hello"})  # question name is not part of the key
    assert k1 != jev.cache_key("ws", "m", q, {"text": "hello!"})
    assert k1 != jev.cache_key("ws", "m2", q, {"text": "hello"})
