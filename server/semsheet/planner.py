"""Natural language -> typed plan using the frontier model (OpenAI Responses API).

The planner only sees the schema, at most 20 bounded sample rows, and the plan format. It never sees the full
table, and it never performs row-level classification itself; Jev does that inside the job system."""
from __future__ import annotations

import json
import time
from typing import Any

from openai import OpenAI

from .config import settings

SYSTEM_PROMPT = """You are the planner for a semantic spreadsheet. Convert the user's request about an uploaded table into ONE typed plan (JSON) that the execution engine runs. You never process rows yourself.

Execution model:
- Exact operations (filter, sort, project, compute, aggregate, join, distinct, limit) run in code (DuckDB) with exact arithmetic.
- Semantic judgements run on a small, fast decision model ("Jev", TypeSafe System One). Jev answers three question kinds per row:
  * boolean  -> yes/no probability. Use for semantic filters and flags. Output columns: <name>.value (true/false/null), <name>.score (p_yes), <name>.status
  * category -> pick one label from a fixed set you define (with short descriptions). Output: <name>.value (label), <name>.score (p_top), <name>.confidence, <name>.status
  * score    -> rate on an ordered rubric of 2-10 described levels. Output: <name>.value (level label), <name>.score (expected level index, 0-based float), <name>.confidence, <name>.status
- Jev is literal: write the exact condition. Put boundary cases in criteria. Keep one judgement per question.
- For a semantic filter like "find X because Y" or "X that also Y", write ONE boolean question that states the whole condition (X because Y) and spell out in criteria.true that implied statements count (e.g. "I cannot afford order 123" implies wanting to cancel it). Do NOT decompose one filter into several boolean questions joined with AND: each question misses some phrasings and the AND compounds the misses. Use several questions only when the user asks for several separate attributes (e.g. a filter plus an urgency score, or two independent flags).
- Multilingual text: say in the instruction that the text may be in any language and the judgement applies to its meaning.
- Jev cannot count, do arithmetic, compare dates, or generate text. Never ask it to. Use compute/aggregate for numbers.
- Always include an "other" (or "insufficient_evidence") option in category taxonomies when the user did not supply exhaustive labels.
- Do NOT show label/answer columns to the model when the user asks to evaluate accuracy against them (e.g. exclude columns named like intent/category/label/response when classifying the user text). Only include the input text columns needed for the judgement.
- A semantic step must be placed before the exact steps that use its outputs. A filter that references <name>.value uses unknown_policy "separate" (default) so rows without an answer, and rows near the decision cut, go to a review view.
- Boolean thresholds and match accept/reject cuts are calibrated automatically from the observed score distribution once the stage is scored (mode "auto", default); the numbers you write are only the fallback for tiny inputs. Set "mode":"fixed" only when the user gives an explicit probability cutoff.
- Ranking = score question + sort by <name>.score desc (tie-break is automatic). Rank descending for "most severe/urgent".
- Group/aggregate = category question + aggregate step (group_by the .value column, metrics count/sum/avg/...). Always keep a count metric.
- Matching rows of two tables that describe the same entity (same product, same company, same person) = ONE semantic_match step against the other dataset. Candidate retrieval (exact blocking plus lexical similarity) is automatic; Jev only verifies each candidate pair. Never emulate matching with join + semantic_annotate: join is exact-key only.
- Text columns holding money like "$148.04" must be converted with {"op":"to_number","args":[{"column":"price"}]} in a compute step before arithmetic or sorting.
- Column names containing dots must be written exactly (e.g. "severity.score").
- If a request needs data that is not in the schema (e.g. a revenue table), still produce the best plan over available columns and explain the gap in "description".

Plan JSON format (strict; no extra keys):
{
  "plan_version": "1",
  "source": {"dataset_id": "<given>", "version_id": "<given>"},
  "model": "jev-1.13.0",
  "title": "short title",
  "description": "one or two sentences: what the plan does, assumptions, and what a user may want to edit (thresholds, labels).",
  "steps": [ ...steps... ],
  "output": "<id of the final step>"
}
Step shapes:
 {"id","op":"semantic_annotate","input","columns":[input col names],"questions":[{"name","kind":"boolean","instruction","criteria":{"true":"...","false":"..."},"thresholds":{"true_min":0.7,"false_max":0.3}} | {"name","kind":"category","instruction","options":{"label":"description",...},"min_confidence":0.0} | {"name","kind":"score","instruction","levels":["lowest ...","...","highest ..."]}],"on_missing":"unknown"}
 {"id","op":"filter","input","where":<expr>,"unknown_policy":"separate"}
 {"id","op":"sort","input","by":[{"column","direction":"asc|desc"}]}
 {"id","op":"project","input","columns":[...]}
 {"id","op":"compute","input","columns":[{"name","expr":<expr>}]}
 {"id","op":"aggregate","input","group_by":[...],"metrics":[{"name","fn":"count|count_distinct|sum|avg|min|max","column":"<col or omit for count>"}]}
 {"id","op":"join","input","right":{"dataset_id":"<id of another workspace dataset>"},"on":[{"left","right"}],"how":"inner|left","right_columns":[...]}   (right columns appear in the output as "right.<name>", e.g. "right.country"; the join output has fresh row ids and a left_row_id column)
 {"id","op":"semantic_match","input","name":"match","right":{"dataset_id":"<id of another workspace dataset>"},"left_columns":[left cols to compare],"right_columns":[right cols to compare],"instruction":"yes/no relation to verify for one candidate pair, e.g. do the two records describe the same exact product?","criteria":{"true":"...","false":"..."},"candidates_per_row":5,"accept_min":0.8,"reject_max":0.3,"right_output_columns":[right cols to carry into the output]}   (outputs per left row: match.right_row_id, match.score, match.status (matched|uncertain|unmatched|no_candidates), and match.<name> for each right_output_columns entry)
 {"id","op":"distinct","input","columns":[...]}
 {"id","op":"limit","input","n":100}
Expression <expr>: {"column":"name"} | {"literal": value} | {"op":"eq|ne|gt|gte|lt|lte|and|or|not|contains|icontains|starts_with|ends_with|in|is_null|not_null|add|sub|mul|div|coalesce|lower|upper|length|trim|year|month|date|to_number|replace|case","args":[<expr>,...]}
Step ids and question names: letters, digits, underscore. The first step's input is "source". Return ONLY the JSON object."""


class PlannerError(Exception):
    pass


def _schema_summary(schema: list[dict[str, Any]]) -> str:
    lines = []
    for c in schema[:100]:
        if c.get("role") == "row_id":
            continue
        extra = []
        if c.get("null_count"):
            extra.append(f"nulls={c['null_count']}")
        if c.get("distinct_estimate") is not None:
            extra.append(f"~distinct={c['distinct_estimate']}")
        if c.get("max_length"):
            extra.append(f"max_len={c['max_length']}")
        samples = "; ".join(str(s)[:60] for s in (c.get("samples") or [])[:2])
        lines.append(f"- {c['name']} ({c['type']}{', ' + ', '.join(extra) if extra else ''}) e.g. {samples}")
    return "\n".join(lines)


def compile_prompt(prompt: str, dataset_id: str, version_id: str, schema: list[dict[str, Any]], sample: list[dict[str, Any]], row_count: int,
                   other_datasets: list[dict[str, Any]] | None = None, previous_plan: dict[str, Any] | None = None, feedback: str | None = None) -> dict[str, Any]:
    user = [
        f"Dataset id: {dataset_id}\nVersion id: {version_id}\nRows: {row_count}",
        "Columns:\n" + _schema_summary(schema),
        "Sample rows (bounded, at most 20):\n" + json.dumps(sample[:20], ensure_ascii=False)[: settings.sample_max_bytes],
    ]
    if other_datasets:
        user.append("Other datasets available in the workspace for joins: " + json.dumps(other_datasets, ensure_ascii=False)[:2000])
    if previous_plan is not None:
        user.append("Previous plan (edit it to satisfy the request; keep ids stable where possible):\n" + json.dumps(previous_plan, ensure_ascii=False)[:6000])
    if feedback:
        user.append("Validation feedback on the previous attempt, fix these issues:\n" + feedback[:2000])
    user.append("User request:\n" + prompt.strip())
    user.append("Respond with the plan as a single JSON object and nothing else.")
    started = time.time()
    if settings.planner_provider == "openrouter":
        text, usage_d = _call_openrouter("\n\n".join(user))
    else:
        text, usage_d = _call_openai("\n\n".join(user))
    text = _strip_code_fence(text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlannerError(f"planner returned invalid JSON: {e}") from e
    if isinstance(obj, dict) and "plan" in obj and "steps" not in obj:
        obj = obj["plan"]
    if not isinstance(obj, dict):
        raise PlannerError("planner returned a JSON value that is not an object")
    obj.setdefault("plan_version", "1")
    obj["source"] = {"dataset_id": dataset_id, "version_id": version_id}
    obj.setdefault("model", settings.jev_model)
    return {"plan": obj, "planner": {"provider": settings.planner_provider, "model": settings.planner_model, "reasoning_effort": settings.planner_reasoning_effort,
                                     "latency_ms": int((time.time() - started) * 1000), "usage": usage_d}}


def _call_openai(user_text: str) -> tuple[str, dict[str, Any]]:
    if not settings.openai_api_key:
        raise PlannerError("OPENAI_API_KEY is not configured")
    client = OpenAI(api_key=settings.openai_api_key)
    resp = client.responses.create(
        model=settings.planner_model,
        reasoning={"effort": settings.planner_reasoning_effort},
        instructions=SYSTEM_PROMPT,
        input=user_text,
        text={"format": {"type": "json_object"}},
        max_output_tokens=settings.planner_max_output_tokens,
    )
    text = getattr(resp, "output_text", None) or ""
    if not text:
        for item in resp.output or []:
            if getattr(item, "type", None) == "message":
                for part in item.content:
                    if getattr(part, "type", None) == "output_text":
                        text += part.text
    usage = getattr(resp, "usage", None)
    return text, {"input_tokens": getattr(usage, "input_tokens", None), "output_tokens": getattr(usage, "output_tokens", None),
                  "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None), "cost_usd": None}


def _call_openrouter(user_text: str) -> tuple[str, dict[str, Any]]:
    """OpenRouter speaks the OpenAI chat completions API; reasoning effort and usage accounting (including the
    provider-reported cost) go through extra_body."""
    if not settings.openrouter_api_key:
        raise PlannerError("OPENROUTER_API_KEY is not configured")
    client = OpenAI(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url, timeout=600)
    extra: dict[str, Any] = {"reasoning": {"effort": settings.planner_reasoning_effort}, "usage": {"include": True}}
    if settings.planner_openrouter_providers:
        extra["provider"] = {"order": list(settings.planner_openrouter_providers), "allow_fallbacks": settings.planner_openrouter_allow_fallbacks}
    resp = client.chat.completions.create(
        model=settings.planner_model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}],
        response_format={"type": "json_object"},
        max_tokens=settings.planner_max_output_tokens,
        extra_body=extra,
    )
    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content if choice and choice.message else None) or ""
    usage = getattr(resp, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return text, {"input_tokens": getattr(usage, "prompt_tokens", None), "output_tokens": getattr(usage, "completion_tokens", None),
                  "reasoning_tokens": getattr(details, "reasoning_tokens", None), "cost_usd": getattr(usage, "cost", None),
                  "served_by": getattr(resp, "provider", None), "served_model": getattr(resp, "model", None)}


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
