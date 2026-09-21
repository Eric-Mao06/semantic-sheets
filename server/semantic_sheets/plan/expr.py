"""Constrained expression tree -> DuckDB SQL. Neither interface accepts arbitrary SQL or Python.

Forms:
  {"column": "name"}
  {"value": <literal>}
  {"column": "name", "operator": "eq", "value": 3}            # comparison shorthand
  {"op": "and"|"or", "args": [expr, ...]}   {"op": "not", "arg": expr}
  {"op": <comparison>, "left": expr, "right": expr}
  {"fn": <function>, "args": [expr, ...]}
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..errors import ValidationFailed

COMPARISONS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "not_contains", "starts_with",
               "ends_with", "is_null", "not_null", "between", "like"}
FUNCTIONS = {
    "add": (2, 2), "sub": (2, 2), "mul": (2, 2), "div": (2, 2), "mod": (2, 2), "abs": (1, 1), "round": (1, 2),
    "floor": (1, 1), "ceil": (1, 1), "lower": (1, 1), "upper": (1, 1), "trim": (1, 1), "length": (1, 1),
    "concat": (1, 20), "coalesce": (1, 20), "substr": (2, 3), "year": (1, 1), "month": (1, 1), "day": (1, 1),
    "date_diff": (3, 3), "cast": (2, 2), "if": (3, 3), "to_date": (1, 1), "to_timestamp": (1, 1),
    "to_number": (1, 1), "greatest": (2, 20), "least": (2, 20), "is_null": (1, 1), "not": (1, 1),
}
CAST_TYPES = {"text": "VARCHAR", "integer": "BIGINT", "number": "DOUBLE", "boolean": "BOOLEAN", "date": "DATE",
              "timestamp": "TIMESTAMP"}
DATE_UNITS = {"day", "hour", "minute", "second", "month", "year", "week"}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            raise ValidationFailed("non-finite numeric literal", code="invalid_literal")
        return repr(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return "'" + v.isoformat() + "'"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, list):
        return "(" + ", ".join(sql_literal(x) for x in v) + ")"
    raise ValidationFailed(f"unsupported literal type {type(v).__name__}", code="invalid_literal")


class Compiler:
    def __init__(self, columns: dict[str, str], path: str = "where"):
        self.columns = columns
        self.path = path
        self.used: set[str] = set()

    def fail(self, msg: str, sub: str = "") -> ValidationFailed:
        return ValidationFailed(msg, code="invalid_expression", path=f"{self.path}{sub}")

    def column(self, name: Any, sub: str) -> str:
        if not isinstance(name, str) or name not in self.columns:
            hint = ", ".join(sorted(self.columns)[:30])
            raise self.fail(f"unknown column {name!r}; available: {hint}", sub)
        self.used.add(name)
        return quote_ident(name)

    def compile(self, e: Any, sub: str = "") -> str:
        if not isinstance(e, dict):
            raise self.fail("expression must be an object", sub)
        if "operator" in e:
            return self.comparison(e["operator"], self.operand(e, "column", "value", sub, left=True),
                                   e.get("value", None), sub, right_is_literal=True)
        if "fn" in e:
            return self.function(e, sub)
        if "op" in e:
            op = e["op"]
            if op in ("and", "or"):
                args = e.get("args")
                if not isinstance(args, list) or not args:
                    raise self.fail(f"{op} needs a non-empty args list", sub + ".args")
                parts = [self.compile(a, f"{sub}.args[{i}]") for i, a in enumerate(args)]
                return "(" + f" {op.upper()} ".join(parts) + ")"
            if op == "not":
                return "(NOT " + self.compile(e.get("arg"), sub + ".arg") + ")"
            if op in COMPARISONS:
                left = self.compile(e.get("left"), sub + ".left")
                right_raw = e.get("right")
                if op in ("is_null", "not_null"):
                    return self.comparison(op, left, None, sub, right_is_literal=True)
                if isinstance(right_raw, dict) and ("column" in right_raw or "fn" in right_raw or "value" in right_raw):
                    return self.comparison(op, left, self.compile(right_raw, sub + ".right"), sub, right_is_literal=False)
                return self.comparison(op, left, right_raw, sub, right_is_literal=True)
            raise self.fail(f"unknown op {op!r}", sub + ".op")
        if "column" in e:
            return self.column(e["column"], sub + ".column")
        if "value" in e:
            return sql_literal(e["value"])
        raise self.fail("expression needs one of column, value, op, fn, operator", sub)

    def operand(self, e: dict[str, Any], col_key: str, val_key: str, sub: str, left: bool) -> str:
        if col_key in e:
            return self.column(e[col_key], sub + "." + col_key)
        if "left" in e:
            return self.compile(e["left"], sub + ".left")
        raise self.fail("comparison needs a column", sub)

    def comparison(self, op: str, left: str, right: Any, sub: str, right_is_literal: bool) -> str:
        if op not in COMPARISONS:
            raise self.fail(f"unknown operator {op!r}", sub + ".operator")
        r = sql_literal(right) if right_is_literal else right
        if op == "eq":
            return f"({left} = {r})" if right is not None else f"({left} IS NULL)"
        if op == "ne":
            return f"({left} <> {r})" if right is not None else f"({left} IS NOT NULL)"
        if op == "gt":
            return f"({left} > {r})"
        if op == "gte":
            return f"({left} >= {r})"
        if op == "lt":
            return f"({left} < {r})"
        if op == "lte":
            return f"({left} <= {r})"
        if op in ("in", "not_in"):
            if not isinstance(right, list) or not right:
                raise self.fail(f"{op} needs a non-empty list value", sub + ".value")
            return f"({left} {'NOT ' if op == 'not_in' else ''}IN {sql_literal(right)})"
        if op in ("contains", "not_contains", "starts_with", "ends_with", "like"):
            if right_is_literal and not isinstance(right, str):
                raise self.fail(f"{op} needs a string value", sub + ".value")
            neg = "NOT " if op == "not_contains" else ""
            if op in ("contains", "not_contains"):
                return f"({neg}contains(lower(CAST({left} AS VARCHAR)), lower({r})))"
            if op == "starts_with":
                return f"(starts_with(lower(CAST({left} AS VARCHAR)), lower({r})))"
            if op == "ends_with":
                return f"(suffix(lower(CAST({left} AS VARCHAR)), lower({r})))"
            return f"(CAST({left} AS VARCHAR) LIKE {r})"
        if op == "is_null":
            return f"({left} IS NULL)"
        if op == "not_null":
            return f"({left} IS NOT NULL)"
        if op == "between":
            if not (isinstance(right, list) and len(right) == 2):
                raise self.fail("between needs a [low, high] value", sub + ".value")
            return f"({left} BETWEEN {sql_literal(right[0])} AND {sql_literal(right[1])})"
        raise self.fail(f"unsupported operator {op!r}", sub)

    def function(self, e: dict[str, Any], sub: str) -> str:
        fn = e.get("fn")
        if fn not in FUNCTIONS:
            raise self.fail(f"unknown function {fn!r}", sub + ".fn")
        args = e.get("args")
        lo, hi = FUNCTIONS[fn]
        if not isinstance(args, list) or not (lo <= len(args) <= hi):
            raise self.fail(f"{fn} takes {lo}..{hi} arguments", sub + ".args")
        if fn == "cast":
            t = args[1].get("value") if isinstance(args[1], dict) else None
            if t not in CAST_TYPES:
                raise self.fail(f"cast type must be one of {sorted(CAST_TYPES)}", sub + ".args[1]")
            return f"TRY_CAST({self.compile(args[0], sub + '.args[0]')} AS {CAST_TYPES[t]})"
        if fn == "date_diff":
            unit = args[0].get("value") if isinstance(args[0], dict) else None
            if unit not in DATE_UNITS:
                raise self.fail(f"date_diff unit must be one of {sorted(DATE_UNITS)}", sub + ".args[0]")
            a = self.compile(args[1], sub + ".args[1]")
            b = self.compile(args[2], sub + ".args[2]")
            return f"date_diff('{unit}', TRY_CAST({a} AS TIMESTAMP), TRY_CAST({b} AS TIMESTAMP))"
        c = [self.compile(a, f"{sub}.args[{i}]") for i, a in enumerate(args)]
        if fn == "add":
            return f"({c[0]} + {c[1]})"
        if fn == "sub":
            return f"({c[0]} - {c[1]})"
        if fn == "mul":
            return f"({c[0]} * {c[1]})"
        if fn == "div":
            return f"({c[0]} / NULLIF({c[1]}, 0))"
        if fn == "mod":
            return f"({c[0]} % NULLIF({c[1]}, 0))"
        if fn == "if":
            return f"(CASE WHEN {c[0]} THEN {c[1]} ELSE {c[2]} END)"
        if fn == "concat":
            return "concat(" + ", ".join(f"CAST({x} AS VARCHAR)" for x in c) + ")"
        if fn == "to_date":
            return f"TRY_CAST({c[0]} AS DATE)"
        if fn == "to_timestamp":
            return f"TRY_CAST({c[0]} AS TIMESTAMP)"
        if fn == "to_number":
            return f"TRY_CAST({c[0]} AS DOUBLE)"
        if fn == "is_null":
            return f"({c[0]} IS NULL)"
        if fn == "not":
            return f"(NOT {c[0]})"
        if fn in ("year", "month", "day"):
            return f"{fn}(TRY_CAST({c[0]} AS TIMESTAMP))"
        if fn == "length":
            return f"length(CAST({c[0]} AS VARCHAR))"
        if fn in ("lower", "upper", "trim"):
            return f"{fn}(CAST({c[0]} AS VARCHAR))"
        return f"{fn}(" + ", ".join(c) + ")"


def infer_type(e: Any, columns: dict[str, str]) -> str:
    """Best-effort logical type of an expression, used for derived column schemas."""
    if not isinstance(e, dict):
        return "text"
    if "operator" in e or e.get("op") in COMPARISONS or e.get("op") in ("and", "or", "not"):
        return "boolean"
    if "fn" in e:
        fn = e["fn"]
        if fn in ("add", "sub", "mul", "div", "mod", "abs", "round", "floor", "ceil", "length", "year", "month",
                  "day", "date_diff", "to_number"):
            return "number"
        if fn in ("is_null", "not"):
            return "boolean"
        if fn == "cast":
            return e["args"][1].get("value", "text") if len(e.get("args", [])) > 1 and isinstance(e["args"][1], dict) else "text"
        if fn == "to_date":
            return "date"
        if fn == "to_timestamp":
            return "timestamp"
        if fn in ("if", "coalesce", "greatest", "least") and e.get("args"):
            return infer_type(e["args"][1 if fn == "if" else 0], columns)
        return "text"
    if "column" in e:
        return columns.get(e["column"], "text")
    if "value" in e:
        v = e["value"]
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, int):
            return "integer"
        if isinstance(v, float):
            return "number"
        return "text"
    return "text"


def compile_expr(e: Any, columns: dict[str, str], path: str = "where") -> tuple[str, set[str]]:
    c = Compiler(columns, path)
    sql = c.compile(e)
    return sql, c.used
