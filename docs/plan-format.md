# Plan format

A plan is the only thing the engine executes. The planner writes one from a plain-language request; an agent or a
user can also write one by hand and submit it through `plans_validate` → `jobs_submit`. The schema lives in
[`server/semsheet/models.py`](../server/semsheet/models.py) (Pydantic, `extra="forbid"`: unknown keys are errors).

```json
{
  "plan_version": "1",
  "source": {"dataset_id": "ds_…", "version_id": "v_…"},
  "model": "jev-1.13.0",
  "title": "Cancellations the customer cannot afford",
  "description": "One boolean question over the message text, then a filter and a sort.",
  "steps": [
    {"id": "judge", "op": "semantic_annotate", "input": "source", "columns": ["instruction"],
     "questions": [{"name": "cancel_unaffordable", "kind": "boolean",
                    "instruction": "Is the customer asking to cancel an order because they cannot afford it? The text may be in any language.",
                    "criteria": {"true": "Wants to cancel and gives cost, money or affordability as the reason, explicitly or implicitly (e.g. 'I cannot afford order 123').",
                                 "false": "Cancels for another reason, or does not ask to cancel."}}]},
    {"id": "keep", "op": "filter", "input": "judge", "where": {"op": "eq", "args": [{"column": "cancel_unaffordable.value"}, {"literal": true}]}},
    {"id": "out", "op": "sort", "input": "keep", "by": [{"column": "cancel_unaffordable.score", "direction": "desc"}]}
  ],
  "output": "out"
}
```

Rules that apply to every plan:

- `steps` is an ordered list of 1–32 steps. Each step has a unique `id` (`[A-Za-z_][A-Za-z0-9_]{0,63}`) and an
  `input` that is `"source"` or the id of an earlier step.
- `output` names the step whose rows are the result. Every step remains queryable by id.
- `source.version_id` may be omitted; validation pins it to the latest version so the job runs on frozen data.
- `model` is the Jev build; it participates in the answer cache key.

## Semantic steps (run on Jev)

### `semantic_annotate`

Ask one or more questions about every row. Only the listed `columns` (1–12) are shown to the model.

```json
{"id": "…", "op": "semantic_annotate", "input": "…", "columns": ["text"], "questions": [ … ], "on_missing": "unknown"}
```

`on_missing`: what to do with rows whose listed columns are all empty — `unknown` (status `missing`) or `skip`.

Each question has a `name` (becomes the column prefix), a `kind`, an `instruction` (3–4000 chars) and
kind-specific fields. The output columns are:

| `kind` | Jev primitive | Fields | Output columns |
|---|---|---|---|
| `boolean` | Noul (yes/no probability) | `criteria: {"true": …, "false": …}`, `thresholds` | `<name>.value` (bool), `<name>.score` (p_yes), `<name>.near` (bool), `<name>.status` |
| `category` | Choice (one label) | `options: {"label": "description", …}` (2–255), `min_confidence` | `<name>.value` (label), `<name>.score` (p_top), `<name>.confidence`, `<name>.status` |
| `score` | Score (ordered rubric) | `levels: ["lowest …", …, "highest …"]` (2–10) | `<name>.value` (level label), `<name>.score` (expected level index, float), `<name>.confidence`, `<name>.status` |

Every question also gets a `<name>.raw` column holding the provider's JSON answer (visible through
`results_provenance`, excluded from the default projection).

`thresholds` (boolean only):

```json
{"mode": "auto", "true_min": 0.85, "false_max": 0.15}
```

- `auto` (default): the cut is calibrated on the observed score distribution once the stage is fully scored
  (see [architecture.md](architecture.md#4-calibrate)); `true_min`/`false_max` are only the fallback for stages
  with fewer than 50 scored rows. Every row gets a value; rows within ±0.10 of the cut are flagged in `.near`.
- `fixed`: `score >= true_min` → true, `score <= false_max` → false, in between → `uncertain` with a null value.

Writing questions that Jev answers well:

- Jev is literal. Put the whole condition in one question and spell out boundary cases in `criteria`
  rather than splitting a filter into several booleans joined with AND (each split loses phrasings, and the AND
  compounds the loss).
- Jev cannot count, do arithmetic, compare dates or generate text. Use `compute`/`aggregate` for numbers.
- Category taxonomies should include an `other` option unless the labels are exhaustive.
- Do not show label columns to the model when the request is to evaluate against them.

### `semantic_match`

Match each input row to at most one row of another dataset (the right table, ≤ 5,000 rows). Candidates come from
exact blocking (optional) plus lexical retrieval (`rapidfuzz` token-set ratio, `candidates_per_row` ≤ 5); Jev only
verifies each candidate pair with one boolean question.

```json
{"id": "m", "op": "semantic_match", "input": "source", "name": "match",
 "right": {"dataset_id": "ds_catalog"},
 "left_columns": ["brand", "title"], "right_columns": ["brand", "title"],
 "instruction": "Do the two records describe the same exact product?",
 "criteria": {"true": "Same brand and same specific model or article number.", "false": "Different model, or only the same category."},
 "candidates_per_row": 5, "blocking": {"left": "brand", "right": "brand"},
 "threshold_mode": "auto", "accept_min": 0.8, "reject_max": 0.3,
 "right_output_columns": ["catalog_id", "price"], "right_prefix": "match."}
```

Outputs per left row: `<name>.right_row_id`, `<name>.score` (best candidate's p_same), `<name>.near`,
`<name>.status` (`matched` | `unmatched` | `uncertain` | `no_candidates` | `failed`), `<name>.candidates` (JSON list
with retrieval and model scores), and `<right_prefix><column>` for each `right_output_columns` entry of the accepted
candidate. A row with no match among its candidates is not proof that no match exists; the manifest records this.

## Exact steps (run in DuckDB)

| Step | Shape | Notes |
|---|---|---|
| `filter` | `{"op": "filter", "where": <expr>, "unknown_policy": "separate" \| "exclude" \| "include"}` | When `where` references semantic columns, only rows with status `ok`/`override` pass. `separate` (default) also creates `<id>__review`: rows without a usable answer plus near-cut rows. |
| `sort` | `{"op": "sort", "by": [{"column": "…", "direction": "asc" \| "desc"}]}` | `NULLS LAST`; `_row_id` is appended as a tie-break. |
| `project` | `{"op": "project", "columns": ["…"]}` | `_row_id` is always kept. |
| `compute` | `{"op": "compute", "columns": [{"name": "…", "expr": <expr>}]}` | New columns only; names must not collide. |
| `aggregate` | `{"op": "aggregate", "group_by": ["…"], "metrics": [{"name": "…", "fn": "count" \| "count_distinct" \| "sum" \| "avg" \| "min" \| "max", "column": "…"}]}` | Output rows get fresh `_row_id`s. `count` without `column` is `count(*)`. |
| `join` | `{"op": "join", "right": {"dataset_id": "…"} \| "<step id>", "on": [{"left": "…", "right": "…"}], "how": "inner" \| "left", "right_columns": ["…"], "right_prefix": "right."}` | Exact keys only. Output rows get fresh `_row_id`s and a `left_row_id` column (one-to-many safe). |
| `distinct` | `{"op": "distinct", "columns": ["…"]}` | Fresh `_row_id`s. |
| `limit` | `{"op": "limit", "n": 100}` | |

### Expressions

```
<expr> := {"column": "name"}
        | {"literal": string | number | boolean | null | [literals]}
        | {"op": <operator>, "args": [<expr>, …]}
```

| Operators | Arity | Notes |
|---|---|---|
| `eq ne gt gte lt lte` | 2 | |
| `add sub mul div` | 2 | `div` by zero yields null |
| `and or` | ≥ 2 | |
| `not is_null not_null` | 1 | |
| `contains icontains starts_with ends_with` | 2 | string; `icontains` is case-insensitive |
| `in` | 2 | second argument must be a literal list |
| `coalesce` | ≥ 1 | |
| `lower upper trim length` | 1 | |
| `year month date` | 1 | on timestamps / dates (TRY_CAST) |
| `to_number` | 1 | strips everything but digits, `.` and `-`; `"$148.04"` → `148.04` |
| `replace` | 3 | `replace(text, find, with)` |
| `case` | odd ≥ 3 | `case(cond1, val1, cond2, val2, …, else)` |

Column names containing dots (`severity.score`) are written exactly as-is. Filters reference semantic values as
`<name>.value`; rankings sort by `<name>.score`.

## Job limits

Passed to `jobs_submit` (web: the "Details & edit" panel; MCP: `limits`).

| Field | Default | Meaning |
|---|---|---|
| `max_source_rows` | 10,000 (hard cap 300,000) | Rows a semantic stage will process; the rest end the job as `partial` |
| `max_provider_requests` | 12,000 | Jev requests before stopping |
| `spend_target_usd` | 0.50 | Jev spend before stopping (in addition to the workspace budget) |
| `deadline_seconds` | 600 | Wall clock from job start |
| `rows_per_request` | server default (10) | Rows packed per Jev request |

## Validation issues

`plans_validate` returns HTTP 422 with `error.details.issues`, each `{path, code, message, fix}` (e.g.
`steps[1].where.args[0]`, `unknown_column`, `"Use one of: text, intent, …"`). `plans_compile` feeds these back to
the planner automatically.
