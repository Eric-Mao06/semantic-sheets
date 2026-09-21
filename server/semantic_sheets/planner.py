"""Language-to-plan planner: bounded schema context + a frontier model with strict JSON output.

The planner sees the schema, at most 20 bounded sample rows, and the request. It never sees the table.
Its output is validated by the same compiler as MCP-supplied plans; one repair round feeds validation
errors back before failing.
"""

from __future__ import annotations

import json
from typing import Any

from .config import settings
from .errors import ValidationFailed
from .plan.compile import CompiledPlan
from .plan.schema import Plan

PLAN_GUIDE = """
You convert a spreadsheet user's request into a typed plan executed by a server. Output JSON only.

PLAN = {"plan_version":"1","source":{"dataset_id":<id>},"steps":[STEP...],"output":<step id>}
Each STEP has "id" (short snake_case, unique), "op", and "input" ("source" or an earlier step id). Steps run in order; a step's columns are its input's columns plus what it adds.

Ops (exact ops run in code; semantic ops run on a small decision model called Jev that answers typed questions per row):
- semantic_annotate {columns:[input column names], questions:[Q...], on_missing:"unknown"}.
  Q kinds:
   * boolean: {"name","kind":"boolean","instruction":<yes/no question about ONE row>,"thresholds":{"true_min":0.85,"false_max":0.15}}
       -> adds <name>.value (true/false/null), <name>.p (probability), <name>.status
   * category: {"name","kind":"category","instruction":<question>,"labels":[{"name":snake_case,"description":...}...]}
       -> adds <name>.value (label), <name>.confidence, <name>.status. "other" and "insufficient_evidence" are added automatically.
   * score: {"name","kind":"score","instruction":<what to rate>,"levels":[low ... high, 2-10 ordered level descriptions]}
       -> adds <name>.score (numeric position along the levels, 0 = first level), <name>.value (nearest level), <name>.confidence, <name>.status
- filter {where: EXPR, unknown_policy:"separate"}  (rows where the predicate is unknown go to a review view)
- sort {by:[{column, direction:"asc"|"desc"}]}   (rank = sort by a score/p column desc)
- project {columns:[...]}
- derive {columns:[{name, expr: EXPR}]}  (arithmetic, dates, string ops in code)
- aggregate {group_by:[cols], metrics:[{name, fn: count|count_distinct|sum|avg|min|max, column?}]}
- join {right:{dataset_id}, on:[{left,right}], how:"left"|"inner", right_columns:[...], prefix:"right_"}  (exact key join)
- dedupe {keys:[...]}
- limit {n}
- semantic_match {right:{dataset_id}, left_columns:[...], right_columns:[...], instruction:<"Do these describe the same ...?">, candidates_per_row:5, thresholds:{match_min:0.85,nonmatch_max:0.15}, mode:"best"}
   -> adds match.right_row_id, match.p, match.status (match|non_match|uncertain|no_candidates), match.<right col>

EXPR grammar (no SQL, no code):
  {"column": name} | {"value": literal}
  {"column": name, "operator": eq|ne|gt|gte|lt|lte|in|not_in|contains|not_contains|starts_with|ends_with|is_null|not_null|between, "value": literal}
  {"op":"and"|"or","args":[EXPR...]} | {"op":"not","arg":EXPR}
  {"fn": add|sub|mul|div|mod|abs|round|floor|ceil|lower|upper|trim|length|concat|coalesce|substr|year|month|day|date_diff|cast|if|to_date|to_number|greatest|least, "args":[EXPR...]}
  (date_diff args: [{"value":"day"}, a, b]; cast args: [expr, {"value":"number"|"text"|"integer"|"date"}])

Rules:
1. Use semantic questions only for meaning (intent, topic, sentiment, severity, relevance, matching). Use exact ops for arithmetic, dates, counting, key joins, sorting, aggregation. Never ask the model to compute numbers.
2. Each question asks ONE specific thing about ONE row; phrase it as a full question; mention negations and scope explicitly. Do not stuff multiple judgments into one question.
3. Prefer one semantic_annotate step with several independent questions over several steps. A question that depends on another answer must be a later step.
4. For "find X" requests: boolean question + filter on <name>.value = true. For "classify"/"categorize": category with a small fixed taxonomy (3-12 labels) with short descriptions. For "rank"/"score"/"urgency"/"severity": score question, then sort by <name>.score desc. For "group by theme": category, then aggregate with count (and count_distinct of an id column when the user asks about affected accounts/customers).
5. Put an exact filter BEFORE the semantic step when it narrows scope without changing the meaning (e.g. non-empty text, a date range, a product). Keep the semantic step's columns to the text fields needed for the judgment.
6. When the request mentions an operation you cannot express, do the closest exact/semantic equivalent and say so in assumptions.
7. Output the final step id in "output". Keep ids short and descriptive. Do not invent columns that are not in the schema or created by an earlier step.
8. Do not add a limit unless the user asks for "top N". Do not sort by a label column.
"""


def _client():
    from openai import OpenAI

    return OpenAI(api_key=settings().openai_api_key)


PLANNER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan": {"type": "object", "description": "The typed plan (see instructions)."},
        "summary": {"type": "string", "description": "One or two sentences describing what the plan does, for the user."},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "suggested_name": {"type": "string"},
    },
    "required": ["plan", "summary", "assumptions", "suggested_name"],
    "additionalProperties": False,
}


def compile_request(workspace_id: str, dataset_id: str, request: str, *, schema: list[dict[str, Any]], row_count: int,
                    sample: list[dict[str, Any]], other_datasets: list[dict[str, Any]] | None = None,
                    current_plan: dict[str, Any] | None = None, validate: Any = None,
                    max_output_tokens: int = 12000) -> tuple[CompiledPlan, dict[str, Any]]:
    """Returns (compiled plan, planner metadata incl. usage). `validate(plan_dict) -> CompiledPlan` raises on errors."""
    cfg = settings()
    if not cfg.openai_api_key:
        raise ValidationFailed("OPENAI_API_KEY is not configured; supply a typed plan instead", code="planner_unavailable")
    columns = [{k: v for k, v in c.items() if k in ("name", "type", "null_count", "distinct_estimate", "max_length", "examples")}
               for c in schema[:100]]
    context = {
        "dataset_id": dataset_id, "row_count": row_count, "columns": columns, "sample_rows": sample,
        "other_datasets": other_datasets or [],
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PLAN_GUIDE},
        {"role": "user", "content": "DATASET CONTEXT (schema and a bounded sample; the table itself stays on the server):\n"
                                    + json.dumps(context, ensure_ascii=False)
                                    + ("\n\nCURRENT PLAN (the user wants to refine it):\n" + json.dumps(current_plan) if current_plan else "")
                                    + f"\n\nREQUEST: {request.strip()}\n\nReturn {{\"plan\":..., \"summary\":..., \"assumptions\":[...], \"suggested_name\":...}}."},
    ]
    client = _client()
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "rounds": 0}
    last_error: dict[str, Any] | None = None
    meta: dict[str, Any] = {"model": cfg.planner_model, "reasoning_effort": cfg.planner_reasoning_effort}
    for attempt in range(3):
        resp = client.responses.create(
            model=cfg.planner_model,
            reasoning={"effort": cfg.planner_reasoning_effort},
            input=messages,
            max_output_tokens=max_output_tokens,
            text={"format": {"type": "json_schema", "name": "planner_output", "strict": False, "schema": PLANNER_SCHEMA}},
        )
        u = getattr(resp, "usage", None)
        if u is not None:
            usage_total["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
            usage_total["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
            det = getattr(u, "output_tokens_details", None)
            usage_total["reasoning_tokens"] += int(getattr(det, "reasoning_tokens", 0) or 0) if det else 0
        usage_total["rounds"] += 1
        text = resp.output_text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            last_error = {"code": "planner_invalid_json", "message": str(e)}
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"That was not valid JSON ({e}). Return the JSON object only."})
            continue
        plan_dict = parsed.get("plan") if isinstance(parsed, dict) else None
        if isinstance(plan_dict, dict):
            plan_dict.setdefault("plan_version", "1")
            plan_dict.setdefault("source", {"dataset_id": dataset_id})
            plan_dict["source"]["dataset_id"] = plan_dict["source"].get("dataset_id") or dataset_id
        try:
            compiled = validate(plan_dict)
        except ValidationFailed as e:
            last_error = e.to_dict()
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "The plan failed validation. Fix it and return the full JSON again.\n"
                                                        f"Error: {json.dumps(e.to_dict())}"})
            continue
        meta.update({"summary": parsed.get("summary", ""), "assumptions": parsed.get("assumptions", []),
                     "suggested_name": parsed.get("suggested_name", ""), "usage": usage_total, "repairs": attempt})
        return compiled, meta
    raise ValidationFailed("the planner could not produce a valid plan", code="planner_failed", details=last_error)
