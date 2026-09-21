import pytest

from semantic_sheets.errors import ValidationFailed
from semantic_sheets.plan.expr import compile_expr
from semantic_sheets.services import plans
from tests.conftest import base_plan

COLS = {"text": "text", "amount": "number", "flag.value": "boolean", "score": "number", "when": "date"}


def test_expression_compiler_forms():
    sql, used = compile_expr({"column": "flag.value", "operator": "eq", "value": True}, COLS)
    assert sql == '("flag.value" = TRUE)' and used == {"flag.value"}
    sql, _ = compile_expr({"op": "and", "args": [{"column": "amount", "operator": "gte", "value": 2}, {"op": "not", "arg": {"column": "text", "operator": "contains", "value": "x'y"}}]}, COLS)
    assert "lower('x''y')" in sql and " AND " in sql
    sql, _ = compile_expr({"fn": "div", "args": [{"column": "amount"}, {"value": 0}]}, COLS)
    assert "NULLIF" in sql
    sql, _ = compile_expr({"fn": "date_diff", "args": [{"value": "day"}, {"column": "when"}, {"value": "2024-02-01"}]}, COLS)
    assert "date_diff('day'" in sql


@pytest.mark.parametrize("expr,path", [
    ({"column": "nope", "operator": "eq", "value": 1}, "where.column"),
    ({"column": "amount", "operator": "matches", "value": ".*"}, "where.operator"),
    ({"fn": "cast", "args": [{"column": "amount"}, {"value": "blob"}]}, "where.args[1]"),
    ({"fn": "date_diff", "args": [{"value": "fortnight"}, {"column": "when"}, {"column": "when"}]}, "where.args[0]"),
    ({"op": "in", "left": {"column": "amount"}, "right": []}, "where.value"),
])
def test_expression_errors_have_paths(expr, path):
    with pytest.raises(ValidationFailed) as e:
        compile_expr(expr, COLS)
    assert e.value.path == path


def test_validate_resolves_columns_and_estimates(workspace, dataset):
    c = plans.validate(workspace.id, base_plan(dataset.id))
    out = c.output.columns
    for name in ("cancel_afford.value", "cancel_afford.p", "topic.value", "topic.confidence", "urgency.score", "urgency.value", "urgency.status"):
        assert name in out
    assert c.estimate["semantic_rows"] == 120 and c.estimate["provider_requests"] >= 12 and c.estimate["cost_usd"] > 0
    assert c.steps["ranked"].order_by[0] == ("urgency.score", "desc") and c.steps["ranked"].order_by[-1] == ("_row_id", "asc")


def test_validate_errors(workspace, dataset):
    bad = base_plan(dataset.id)
    bad["steps"][1]["where"] = {"column": "does_not_exist", "operator": "eq", "value": 1}
    with pytest.raises(ValidationFailed) as e:
        plans.validate(workspace.id, bad)
    assert e.value.path.startswith("steps[1].where")
    bad = base_plan(dataset.id)
    bad["steps"][1]["input"] = "ranked"  # forward reference
    with pytest.raises(ValidationFailed) as e:
        plans.validate(workspace.id, bad)
    assert e.value.code == "unknown_input"
    bad = base_plan(dataset.id)
    bad["steps"].append({"id": "agg", "op": "aggregate", "input": "labels", "group_by": ["topic.value"], "metrics": [{"name": "s", "fn": "sum", "column": "text"}]})
    with pytest.raises(ValidationFailed) as e:
        plans.validate(workspace.id, bad)
    assert e.value.code == "invalid_metric"
    with pytest.raises(ValidationFailed) as e:
        plans.validate(workspace.id, {"plan_version": "1", "source": {"dataset_id": dataset.id}, "steps": [], "output": "x"})
    assert e.value.code == "invalid_plan"


def test_plan_hash_is_stable_and_threshold_independent_cache_spec(workspace, dataset):
    from semantic_sheets.jev.packing import cache_key, question_spec
    from semantic_sheets.plan.schema import BooleanQuestion, Thresholds

    a = BooleanQuestion(name="q", instruction="Is it?", thresholds=Thresholds(true_min=0.9, false_max=0.1))
    b = BooleanQuestion(name="q", instruction="Is it?", thresholds=Thresholds(true_min=0.6, false_max=0.2))
    assert cache_key("ws", "m", question_spec(a), {"text": "x"}) == cache_key("ws", "m", question_spec(b), {"text": "x"})
    c1 = plans.validate(workspace.id, base_plan(dataset.id))
    c2 = plans.validate(workspace.id, base_plan(dataset.id))
    assert c1.plan_hash == c2.plan_hash
