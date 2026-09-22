"""Benchmark scenarios: the six operations exercised in the MVP walkthrough.

Each scenario prepares bounded CSVs from data/samples (label and leak columns removed, an explicit `row_id`
added so both approaches refer to the same rows), defines the one-shot output schema, and compares the final
outputs of the two approaches (Jev operators behind the planner vs. gpt-6-astra reading the whole CSV)."""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from common import (
    BENCH_DATA_DIR,
    SAMPLES_DIR,
    adjusted_rand_index,
    cohens_kappa,
    estimate_tokens,
    head,
    normalized_mutual_information,
    prf,
    set_agreement,
    spearman,
    top_k_overlap,
)

SEED = 7


@dataclass
class Table:
    name: str
    path: Path
    row_count: int
    columns: list[str]
    est_tokens: int


@dataclass
class Prepared:
    tables: list[Table]  # tables[0] is the planner's primary dataset
    gold: Any = None
    notes: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Table:
        return self.tables[0]


@dataclass
class Scenario:
    key: str
    title: str
    prompt: str
    prepare: Callable[[], Prepared]
    oneshot_schema: dict[str, Any]
    oneshot_hint: str
    compare: Callable[[Prepared, Any, Any], dict[str, Any]]
    max_source_rows: int = 10_000


def _write(df: pd.DataFrame, name: str) -> Table:
    out = BENCH_DATA_DIR / f"{name}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return Table(name=name, path=out, row_count=len(df), columns=list(df.columns), est_tokens=estimate_tokens(out.read_text(encoding="utf-8")))


def _csv(name: str, **kw: Any) -> pd.DataFrame:
    return pd.read_csv(SAMPLES_DIR / name, dtype=str, keep_default_na=False, **kw)


def _int_list(xs: Any) -> list[int]:
    out: list[int] = []
    for x in xs or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------------------------------------
# Pipeline result helpers (duck-typed: see pipeline.PipelineResult)
# ------------------------------------------------------------------------------------------------


def _steps(pr: Any, op: str) -> list[dict[str, Any]]:
    return [s for s in pr.plan.get("steps", []) if s.get("op") == op]


def _frame(pr: Any, step_id: str) -> pd.DataFrame | None:
    return pr.steps.get(step_id)


def _row_ids(pr: Any, df: pd.DataFrame, table: str) -> list[int]:
    """Map a step frame back to the CSV `row_id` values, in the frame's row order."""
    if df is None or df.empty:
        return []
    if "row_id" in df.columns:
        return [int(x) for x in pd.to_numeric(df["row_id"], errors="coerce").dropna()]
    key = "left_row_id" if "left_row_id" in df.columns else "_row_id"
    m = pr.datasets[table]["row_id_map"]
    return [m[int(x)] for x in df[key] if int(x) in m]


def _keeps_source_rows(pr: Any, step_id: str) -> bool:
    """True when every step between `step_id` and the source preserves source row identity (aggregate, join and
    distinct renumber rows, so their `_row_id` no longer refers to the imported table)."""
    by_id = {s["id"]: s for s in pr.plan.get("steps", [])}
    cur = step_id
    while cur in by_id:
        if by_id[cur]["op"] in ("aggregate", "join", "distinct"):
            return False
        cur = by_id[cur].get("input")
    return True


def _selected_rows(pr: Any, table: str) -> tuple[list[int], str]:
    """Rows kept by the plan: the output step when it still carries source row identity, else the last filter
    step that does."""
    out_id = pr.plan["output"]
    out = _frame(pr, out_id)
    if out is not None and _keeps_source_rows(pr, out_id):
        return _row_ids(pr, out, table), out_id
    for s in reversed(_steps(pr, "filter")):
        if _keeps_source_rows(pr, s["id"]):
            return _row_ids(pr, _frame(pr, s["id"]), table), s["id"]
    return [], "(none)"


def _review_rows(pr: Any, table: str) -> dict[str, Any]:
    """Rows the pipeline lists in the review view, per boolean question: unanswered ones (uncertain / missing /
    failed, which never reach the output) and, with calibrated cuts, answered rows flagged near the cut
    (`flagged_near_cut`; these *are* in the output)."""
    out: dict[str, Any] = {}
    for s in _steps(pr, "semantic_annotate"):
        df = _frame(pr, s["id"])
        if df is None:
            continue
        for q in s.get("questions", []):
            col = f"{q['name']}.status"
            if col in df.columns:
                ids = _row_ids(pr, df, table)
                by_status: dict[str, list[int]] = {}
                for i, st in zip(ids, df[col]):
                    if st not in (None, "ok") and st == st:
                        by_status.setdefault(str(st), []).append(i)
                near_col = f"{q['name']}.near"
                if near_col in df.columns:
                    flagged = [i for i, nr in zip(ids, df[near_col]) if nr is True or nr == 1]
                    if flagged:
                        by_status["flagged_near_cut"] = flagged
                out[q["name"]] = {k: len(v) for k, v in by_status.items()}
                out[f"{q['name']}__rows"] = sorted(set(sum(by_status.values(), [])))
    return out


def _labels(pr: Any, table: str, kind: str) -> tuple[dict[int, Any], str, str]:
    """row_id -> value for the first question of the given kind in the first semantic_annotate step that has one."""
    for s in _steps(pr, "semantic_annotate"):
        for q in s.get("questions", []):
            if q.get("kind") == kind:
                df = _frame(pr, s["id"])
                if df is None:
                    continue
                col = f"{q['name']}.value"
                ids = _row_ids(pr, df, table)
                vals = list(df[col]) if col in df.columns else []
                return {i: v for i, v in zip(ids, vals) if v is not None and v == v and v != ""}, s["id"], q["name"]
    return {}, "(none)", "(none)"


def _score_ranking(pr: Any, table: str) -> tuple[list[int], str]:
    """Rows of the output step ordered by the first score question, highest first (or output order)."""
    out_id = pr.plan["output"]
    df = _frame(pr, out_id)
    if df is None:
        return [], out_id
    score_cols = [c for c in df.columns if c.endswith(".score") and pd.api.types.is_numeric_dtype(pd.to_numeric(df[c], errors="coerce"))]
    for s in _steps(pr, "semantic_annotate"):
        for q in s.get("questions", []):
            if q.get("kind") == "score" and f"{q['name']}.score" in score_cols:
                df = df.assign(_k=pd.to_numeric(df[f"{q['name']}.score"], errors="coerce")).sort_values("_k", ascending=False, kind="stable")
                return _row_ids(pr, df, table), out_id
    return _row_ids(pr, df, table), out_id


# ------------------------------------------------------------------------------------------------
# 1. Customer support: semantic filter with a causal condition
# ------------------------------------------------------------------------------------------------

_AFFORD = re.compile(r"afford|can.?t pay|cannot pay|no longer pay|unable to pay|too expensive|no money|out of money|financial|budget|too much money", re.I)


def prep_support() -> Prepared:
    df = _csv("bitext_support_3000.csv")
    df.insert(0, "row_id", range(1, len(df) + 1))
    gold = {int(r.row_id) for r in df.itertuples() if r.intent == "cancel_order" and _AFFORD.search(r.instruction)}
    table = _write(df[["row_id", "flags", "instruction"]], "support_requests")
    return Prepared([table], gold={"rows": gold, "definition": "intent == cancel_order AND text mentions affordability (regex); the Bitext intent labels were removed from both inputs"},
                    notes=[f"{len(gold)} rows satisfy the proxy gold definition among {len(df)} messages."])


def _column_by_row_id(path: Path, column: str) -> dict[int, str]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {int(i): str(v) for i, v in zip(df["row_id"], df[column])}


def compare_support(prep: Prepared, pr: Any, os_: Any) -> dict[str, Any]:
    table = prep.primary.name
    text = _column_by_row_id(prep.primary.path, "instruction")
    gold = prep.gold["rows"]
    pipe_ids, step = _selected_rows(pr, table)
    pipe, one = set(pipe_ids), set(_int_list((os_.output or {}).get("matching_row_ids")))
    ex = lambda ids: [{"row_id": i, "text": text.get(i, "")[:140]} for i in head(sorted(ids))]  # noqa: E731
    review = _review_rows(pr, table)
    review_ids = set().union(*[set(v) for k, v in review.items() if k.endswith("__rows")]) if review else set()
    return {
        "gold_definition": prep.gold["definition"], "gold_size": len(gold),
        "pipeline": {"selected": len(pipe), "from_step": step, **prf(pipe, gold),
                     "review_view": {k: v for k, v in review.items() if not k.endswith("__rows")}, "gold_rows_in_review_view": len(gold & review_ids), "missed_gold_in_review_view": len((gold - pipe) & review_ids)},
        "oneshot": {"selected": len(one), **prf(one, gold)},
        "agreement": set_agreement(pipe, one),
        "examples": {"only_pipeline": ex(pipe - one), "only_oneshot": ex(one - pipe), "missed_by_both": ex(gold - pipe - one)},
    }


SUPPORT = Scenario(
    key="support_requests", title="Customer support: cancel because unaffordable",
    prompt="Find customers trying to cancel an order because they cannot afford it.",
    prepare=prep_support,
    oneshot_schema={"type": "object", "properties": {"matching_row_ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["matching_row_ids"], "additionalProperties": False},
    oneshot_hint="Return the row_id of every message that matches the request, and nothing else.",
    compare=compare_support,
)

# ------------------------------------------------------------------------------------------------
# 2. Banking queries: fixed-taxonomy classification with gold labels
# ------------------------------------------------------------------------------------------------

_BANK_GOLD_STRICT = {"pending_transfer": "pending", "failed_transfer": "failed", "declined_transfer": "failed"}
# Lenient: intents that describe a transfer that has not arrived yet also count as "pending".
_BANK_GOLD_LENIENT = {**_BANK_GOLD_STRICT, "transfer_timing": "pending", "balance_not_updated_after_bank_transfer": "pending", "transfer_not_received_by_recipient": "pending"}
_BANK_CLASSES = ["pending", "failed", "wrong_account", "other"]


def prep_banking() -> Prepared:
    df = _csv("banking77_test.csv")
    df.insert(0, "row_id", range(1, len(df) + 1))
    strict = {int(r.row_id): _BANK_GOLD_STRICT.get(r.category, "other") for r in df.itertuples()}
    lenient = {int(r.row_id): _BANK_GOLD_LENIENT.get(r.category, "other") for r in df.itertuples()}
    intents = {int(r.row_id): r.category for r in df.itertuples()}
    table = _write(df[["row_id", "text"]], "banking_queries")
    return Prepared([table], gold={"strict": strict, "lenient": lenient, "intents": intents}, notes=[
        "Gold classes come from the BANKING77 intents (removed from both inputs). Strict: pending_transfer -> pending; failed_transfer and declined_transfer -> failed; "
        "everything else -> other. Lenient additionally counts transfer_timing, balance_not_updated_after_bank_transfer and transfer_not_received_by_recipient as pending "
        "(a transfer that has not arrived). BANKING77 has no intent meaning 'sent to the wrong account', so that class is compared between the two approaches only. "
        "Queries about pending top-ups, card payments or cash withdrawals are 'other' under both mappings; whether they are 'pending transfers' is a judgement call that "
        "the prompt leaves open, and it drives most of the false positives on both sides."])


def normalize_bank_label(v: Any) -> str:
    s = str(v).lower()
    if "pending" in s or "process" in s or "not yet" in s:
        return "pending"
    if "fail" in s or "declin" in s or "reject" in s or "unsuccess" in s:
        return "failed"
    if "wrong" in s or "incorrect" in s or "recipient" in s or "not received" in s or "not_received" in s or "unintended" in s:
        return "wrong_account"
    return "other"


def _class_report(pred: dict[int, str], gold: dict[int, str], classes: tuple[str, ...] = ("pending", "failed")) -> dict[str, Any]:
    """Precision / recall / F1 per gold class; rows the approach put in a class without gold (wrong_account) count as
    false positives for the class they were taken from and are listed separately."""
    keys = sorted(gold)
    per: dict[str, Any] = {}
    f1s = []
    for c in classes:
        p = {k for k in keys if pred.get(k, "other") == c}
        g = {k for k in keys if gold[k] == c}
        per[c] = {"predicted": len(p), "gold": len(g), **prf(p, g)}
        if per[c]["f1"] is not None:
            f1s.append(per[c]["f1"])
    scored = [k for k in keys if pred.get(k, "other") in classes or gold[k] in classes]
    acc = sum(1 for k in scored if pred.get(k, "other") == gold[k]) / len(scored) if scored else None
    return {"macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else None, "accuracy_on_rows_in_scope": round(acc, 4) if acc is not None else None, "per_class": per}


def _fp_intents(pred: dict[int, str], gold_cls: str, intents: dict[int, str], gold: dict[int, str]) -> dict[str, int]:
    return dict(Counter(intents[k] for k, v in pred.items() if v == gold_cls and gold.get(k) != gold_cls).most_common(6))


def compare_banking(prep: Prepared, pr: Any, os_: Any) -> dict[str, Any]:
    table = prep.primary.name
    strict: dict[int, str] = prep.gold["strict"]
    lenient: dict[int, str] = prep.gold["lenient"]
    intents: dict[int, str] = prep.gold["intents"]
    raw, step, qname = _labels(pr, table, "category")
    raw_counts = Counter(str(v) for v in raw.values())
    pipe = {k: normalize_bank_label(v) for k, v in raw.items()}
    pipe_labels_mapping = {lab: normalize_bank_label(lab) for lab in raw_counts}
    out = os_.output or {}
    one: dict[int, str] = {}
    for c in ("pending", "failed", "wrong_account"):
        for i in _int_list(out.get(c)):
            one[i] = c
    all_pred_pipe = {k: pipe.get(k, "other") for k in strict}
    all_pred_one = {k: one.get(k, "other") for k in strict}
    text = _column_by_row_id(prep.primary.path, "text")
    disagreements = [k for k in sorted(strict) if all_pred_pipe[k] != all_pred_one[k]]
    wa_pipe = {k for k, v in all_pred_pipe.items() if v == "wrong_account"}
    wa_one = {k for k, v in all_pred_one.items() if v == "wrong_account"}
    return {
        "gold_notes": prep.notes, "gold_class_sizes": {"strict": dict(Counter(strict.values())), "lenient": dict(Counter(lenient.values()))},
        "pipeline": {"from_step": step, "question": qname, "raw_labels": dict(raw_counts), "label_mapping": pipe_labels_mapping, "rows_labeled": len(raw),
                     "strict": _class_report(all_pred_pipe, strict), "lenient": _class_report(all_pred_pipe, lenient),
                     "false_positive_intents": {"pending": _fp_intents(all_pred_pipe, "pending", intents, lenient), "failed": _fp_intents(all_pred_pipe, "failed", intents, lenient)},
                     "wrong_account_rows": len(wa_pipe), "wrong_account_intents": dict(Counter(intents[k] for k in wa_pipe))},
        "oneshot": {"assigned": {c: len([k for k, v in one.items() if v == c]) for c in _BANK_CLASSES[:-1]},
                    "strict": _class_report(all_pred_one, strict), "lenient": _class_report(all_pred_one, lenient),
                    "false_positive_intents": {"pending": _fp_intents(all_pred_one, "pending", intents, lenient), "failed": _fp_intents(all_pred_one, "failed", intents, lenient)},
                    "wrong_account_rows": len(wa_one), "wrong_account_intents": dict(Counter(intents[k] for k in wa_one))},
        "agreement": {**cohens_kappa(all_pred_pipe, all_pred_one), "wrong_account": set_agreement(wa_pipe, wa_one)},
        "examples": {"disagreements": [{"row_id": k, "text": text.get(k, "")[:140], "intent": intents[k], "pipeline": all_pred_pipe[k], "oneshot": all_pred_one[k]} for k in head(disagreements, 12)],
                     "wrong_account_pipeline": [{"row_id": k, "text": text.get(k, "")[:140], "intent": intents[k]} for k in head(sorted(wa_pipe))],
                     "wrong_account_oneshot": [{"row_id": k, "text": text.get(k, "")[:140], "intent": intents[k]} for k in head(sorted(wa_one))]},
    }


BANKING = Scenario(
    key="banking_queries", title="Banking queries: separate transfer problems",
    prompt="Separate pending transfers, failed transfers, and transfers sent to the wrong account.",
    prepare=prep_banking,
    oneshot_schema={"type": "object", "properties": {
        "pending": {"type": "array", "items": {"type": "integer"}},
        "failed": {"type": "array", "items": {"type": "integer"}},
        "wrong_account": {"type": "array", "items": {"type": "integer"}}},
        "required": ["pending", "failed", "wrong_account"], "additionalProperties": False},
    oneshot_hint="Return three lists of row_id values: queries about a pending transfer, about a failed transfer, and about a transfer sent to the wrong account. Leave every other query out of all three lists.",
    compare=compare_banking,
)

# ------------------------------------------------------------------------------------------------
# 3. Consumer complaints: semantic filter then ranking by a rubric
# ------------------------------------------------------------------------------------------------

_CFPB_KEYS = re.compile(r"unauthori|cancel|recurring|continu|subscription|charged after|stop (?:withdrawals|payments|charges)|charging your account", re.I)


def prep_cfpb() -> Prepared:
    df = _csv("cfpb_complaints_5000.csv").sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    df.insert(0, "row_id", range(1, len(df) + 1))
    cols = ["row_id", "Date received", "Product", "Sub-product", "Issue", "Sub-issue", "Company", "State"]
    table = _write(df[cols], "consumer_complaints")
    kw = {int(r["row_id"]) for r in df.to_dict("records") if _CFPB_KEYS.search(f"{r['Issue']} {r['Sub-issue']}")}
    scale = SAMPLES_DIR / "cfpb_complaints_100k.csv"
    scale_tokens = estimate_tokens(scale.read_text(encoding="utf-8")) if scale.exists() else None
    return Prepared([table], gold={"keyword_rows": kw}, notes=[
        f"The full 5,000-row demo sample (8 of the 15 columns) so the one-shot request stays under the 272K-token long-context tier; the 100,000-row file used in the "
        f"walkthrough is ~{scale_tokens:,} tokens and cannot be sent to gpt-6-astra in one request at all." if scale_tokens else "The full 5,000-row demo sample (8 of the 15 columns).",
        "The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. "
        f"There is no gold label; {len(kw)} rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows "
        "carrying one is reported only as a sanity check."])


def compare_cfpb(prep: Prepared, pr: Any, os_: Any) -> dict[str, Any]:
    table = prep.primary.name
    src = pd.read_csv(prep.primary.path, dtype=str, keep_default_na=False)
    txt = {int(r["row_id"]): f"{r['Product']} | {r['Issue']} | {r['Sub-issue']}"[:160] for r in src.to_dict("records")}
    kw: set[int] = prep.gold["keyword_rows"]
    pipe_rank, step = _score_ranking(pr, table)
    one_rank = _int_list((os_.output or {}).get("ranked_row_ids"))
    pipe, one = set(pipe_rank), set(one_rank)
    review = _review_rows(pr, table)
    review_ids = set().union(*[set(v) for k, v in review.items() if k.endswith("__rows")]) if review else set()
    return {
        "notes": prep.notes,
        "pipeline": {"selected": len(pipe), "from_step": step, "keyword_rows_selected": len(pipe & kw), "keyword_share": round(len(pipe & kw) / len(pipe), 4) if pipe else None,
                     "review_view": {k: v for k, v in review.items() if not k.endswith("__rows")},
                     "oneshot_rows_in_review_view": len(one & review_ids), "review_view_rows_with_keyword": len(review_ids & kw),
                     "top_10": [{"row_id": i, "text": txt.get(i, "")} for i in pipe_rank[:10]]},
        "oneshot": {"selected": len(one), "keyword_rows_selected": len(one & kw), "keyword_share": round(len(one & kw) / len(one), 4) if one else None,
                    "top_10": [{"row_id": i, "text": txt.get(i, "")} for i in one_rank[:10]]},
        "agreement": {**set_agreement(pipe, one), "rank_spearman_on_common": spearman(pipe_rank, one_rank), "top_20_overlap": top_k_overlap(pipe_rank, one_rank, 20)},
        "examples": {"only_pipeline": [{"row_id": i, "text": txt.get(i, "")} for i in head(sorted(pipe - one))], "only_oneshot": [{"row_id": i, "text": txt.get(i, "")} for i in head(sorted(one - pipe))]},
    }


CFPB = Scenario(
    key="cfpb_complaints", title="Consumer complaints: filter then rank by urgency",
    prompt="Find complaints about charges continuing after cancellation or unauthorized recurring charges, then rank by urgency.",
    prepare=prep_cfpb,
    oneshot_schema={"type": "object", "properties": {"ranked_row_ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ranked_row_ids"], "additionalProperties": False},
    oneshot_hint="Return the row_id of every matching complaint, ordered from most urgent to least urgent.",
    compare=compare_cfpb,
)

# ------------------------------------------------------------------------------------------------
# 4. Airbnb reviews: multilingual semantic filter, group by property, compare prices
# ------------------------------------------------------------------------------------------------

_WIFI = re.compile(r"wi-?fi|internet|wireless|wlan", re.I)
_AIRBNB_ROWS = 1200


def _price(v: Any) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def prep_airbnb() -> Prepared:
    full = _csv("airbnb_reviews_5000.csv")
    mentions = full[full["comments"].str.contains(_WIFI)]
    rest = full.drop(mentions.index).sample(n=_AIRBNB_ROWS - len(mentions), random_state=SEED)
    df = pd.concat([mentions, rest]).sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    df.insert(0, "row_id", range(1, len(df) + 1))
    cols = ["row_id", "listing_id", "listing_name", "neighbourhood", "borough", "room_type", "nightly_price", "comments"]
    table = _write(df[cols], "airbnb_reviews")
    kw = {int(r.row_id) for r in df.itertuples() if _WIFI.search(r.comments)}
    return Prepared([table], gold={"keyword_rows": kw}, notes=[
        f"Bounded to {len(df)} reviews (many languages) so the one-shot request fits comfortably in context: all {len(mentions)} reviews in the 5,000-review "
        f"sample that mention Wi-Fi / internet at all (positive or negative) plus {len(rest)} random others, shuffled. "
        "No gold label; the keyword share is reported as a sanity check (a review can report bad Wi-Fi without using the word, and most Wi-Fi mentions are positive)."])


def _properties_from_flags(src: pd.DataFrame, ids: set[int]) -> dict[str, dict[str, Any]]:
    sub = src[src["row_id"].astype(int).isin(ids)]
    out: dict[str, dict[str, Any]] = {}
    for lid, g in sub.groupby("listing_id"):
        out[str(lid)] = {"listing_id": str(lid), "listing_name": g["listing_name"].iloc[0], "review_count": int(len(g)), "nightly_price": _price(g["nightly_price"].iloc[0])}
    return out


def compare_airbnb(prep: Prepared, pr: Any, os_: Any) -> dict[str, Any]:
    table = prep.primary.name
    src = pd.read_csv(prep.primary.path, dtype=str, keep_default_na=False)
    txt = {int(r.row_id): r.comments[:140] for r in src.itertuples()}
    kw: set[int] = prep.gold["keyword_rows"]
    pipe_ids, step = _selected_rows(pr, table)
    out = os_.output or {}
    pipe, one = set(pipe_ids), set(_int_list(out.get("wifi_review_row_ids")))
    pipe_props = _properties_from_flags(src, pipe)
    one_props_truth = _properties_from_flags(src, one)
    # One-shot arithmetic check: does its property table agree with its own flagged reviews and the data?
    claimed = out.get("properties") or []
    count_ok = price_ok = 0
    for p in claimed:
        lid = str(p.get("listing_id"))
        t = one_props_truth.get(lid)
        if t and int(p.get("review_count") or -1) == t["review_count"]:
            count_ok += 1
        if t and p.get("nightly_price") is not None and t["nightly_price"] is not None and abs(float(p["nightly_price"]) - t["nightly_price"]) < 0.01:
            price_ok += 1
    claimed_ids = {str(p.get("listing_id")) for p in claimed}
    # Pipeline aggregate step (if the plan produced one) checked against the exact recomputation.
    agg_check: dict[str, Any] = {"aggregate_step": None}
    for s in _steps(pr, "aggregate"):
        df = _frame(pr, s["id"])
        if df is None:
            continue
        gb = [c for c in s.get("group_by", []) if c in df.columns]
        cnt = [m["name"] for m in s.get("metrics", []) if m.get("fn") == "count" and m["name"] in df.columns]
        if gb and cnt:
            agg_check = {"aggregate_step": s["id"], "groups": int(len(df)), "grouped_by": gb, "count_metric": cnt[0],
                         "total_count_in_aggregate": int(pd.to_numeric(df[cnt[0]], errors="coerce").sum()), "flagged_reviews": len(pipe)}
        break
    review = _review_rows(pr, table)
    return {
        "notes": prep.notes,
        "pipeline": {"flagged_reviews": len(pipe), "from_step": step, "keyword_share": round(len(pipe & kw) / len(pipe), 4) if pipe else None,
                     "review_view": {k: v for k, v in review.items() if not k.endswith("__rows")},
                     "properties": len(pipe_props), "aggregate_check": agg_check,
                     "top_properties": sorted(pipe_props.values(), key=lambda p: -p["review_count"])[:8]},
        "oneshot": {"flagged_reviews": len(one), "keyword_share": round(len(one & kw) / len(one), 4) if one else None,
                    "properties_claimed": len(claimed), "properties_implied_by_its_flags": len(one_props_truth),
                    "claimed_properties_matching_its_flags": len(claimed_ids & set(one_props_truth)),
                    "review_counts_consistent": count_ok, "nightly_prices_correct": price_ok,
                    "top_properties": sorted(claimed, key=lambda p: -(p.get("review_count") or 0))[:8]},
        "agreement": {"reviews": set_agreement(pipe, one), "properties": set_agreement(set(pipe_props), set(one_props_truth))},
        "examples": {"only_pipeline": [{"row_id": i, "text": txt.get(i, "")} for i in head(sorted(pipe - one))], "only_oneshot": [{"row_id": i, "text": txt.get(i, "")} for i in head(sorted(one - pipe))]},
    }


AIRBNB = Scenario(
    key="airbnb_reviews", title="Airbnb reviews: unreliable Wi-Fi, grouped by property",
    prompt="Find reviews reporting unreliable Wi-Fi, group by property, and compare nightly prices.",
    prepare=prep_airbnb,
    oneshot_schema={"type": "object", "properties": {
        "wifi_review_row_ids": {"type": "array", "items": {"type": "integer"}},
        "properties": {"type": "array", "items": {"type": "object", "properties": {
            "listing_id": {"type": "string"}, "listing_name": {"type": "string"}, "review_count": {"type": "integer"}, "nightly_price": {"type": ["number", "null"]}},
            "required": ["listing_id", "listing_name", "review_count", "nightly_price"], "additionalProperties": False}}},
        "required": ["wifi_review_row_ids", "properties"], "additionalProperties": False},
    oneshot_hint="Return the row_id of every review that reports unreliable Wi-Fi (in any language), then one entry per property (listing_id) among those reviews with the number of such reviews and the nightly price as a number.",
    compare=compare_airbnb,
)

# ------------------------------------------------------------------------------------------------
# 5. Retail: open taxonomy classification, then join + aggregate on a second table
# ------------------------------------------------------------------------------------------------

_RETAIL_PRODUCTS = 300


def prep_retail() -> Prepared:
    prod = _csv("online_retail_products.csv")
    prod["revenue_f"] = pd.to_numeric(prod["revenue"], errors="coerce")
    prod = prod.sort_values("revenue_f", ascending=False).head(_RETAIL_PRODUCTS).drop(columns=["revenue_f"]).reset_index(drop=True)
    prod.insert(0, "row_id", range(1, len(prod) + 1))
    codes = set(prod["stock_code"])
    pc = _csv("online_retail_product_country.csv")
    pc = pc[pc["stock_code"].isin(codes)].reset_index(drop=True)
    t1 = _write(prod[["row_id", "stock_code", "description", "revenue", "quantity", "transactions", "countries"]], "retail_products")
    t2 = _write(pc[["stock_code", "country", "revenue", "quantity", "transactions"]], "retail_revenue_by_product_and_country")
    return Prepared([t1, t2], notes=[
        f"Bounded to the {len(prod)} highest-revenue products and their {len(pc)} product x country revenue rows. "
        "Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; "
        "the revenue table is recomputed exactly from each approach's own labels to check the arithmetic."])


def _expected_revenue(labels: dict[int, str], prod: pd.DataFrame, pc: pd.DataFrame) -> dict[tuple[str, str], float]:
    code_to_cat = {r.stock_code: labels[int(r.row_id)] for r in prod.itertuples() if int(r.row_id) in labels}
    pc2 = pc.assign(category=pc["stock_code"].map(code_to_cat), rev=pd.to_numeric(pc["revenue"], errors="coerce")).dropna(subset=["category"])
    g = pc2.groupby(["category", "country"])["rev"].sum()
    return {(str(c), str(k)): round(float(v), 2) for (c, k), v in g.items()}


def _revenue_check(claimed: list[dict[str, Any]], expected: dict[tuple[str, str], float]) -> dict[str, Any]:
    exact = close = 0
    max_abs = 0.0
    seen: set[tuple[str, str]] = set()
    worst: list[dict[str, Any]] = []
    for c in claimed:
        key = (str(c.get("category")), str(c.get("country")))
        seen.add(key)
        exp = expected.get(key)
        if exp is None:
            continue
        v = float(c.get("revenue") or 0.0)
        err = abs(v - exp)
        if err <= 0.011:
            exact += 1
        if err <= max(0.005 * abs(exp), 0.011):
            close += 1
        if err > max_abs:
            max_abs = err
        if err > max(0.005 * abs(exp), 0.011):
            worst.append({"category": key[0], "country": key[1], "claimed": v, "expected": exp})
    worst.sort(key=lambda w: -abs(w["claimed"] - w["expected"]))
    total_claimed = round(sum(float(c.get("revenue") or 0.0) for c in claimed), 2)
    total_expected = round(sum(expected.values()), 2)
    return {"cells_claimed": len(claimed), "cells_expected": len(expected), "cells_with_expected_counterpart": len(seen & set(expected)),
            "cells_missing": len(set(expected) - seen), "cells_extra": len(seen - set(expected)), "exact_to_cent": exact, "within_0_5_percent": close,
            "max_abs_error": round(max_abs, 2), "total_revenue_claimed": total_claimed, "total_revenue_expected": total_expected, "worst_cells": worst[:6]}


def compare_retail(prep: Prepared, pr: Any, os_: Any) -> dict[str, Any]:
    prod = pd.read_csv(prep.tables[0].path, dtype=str, keep_default_na=False)
    pc = pd.read_csv(prep.tables[1].path, dtype=str, keep_default_na=False)
    raw, step, qname = _labels(pr, prep.tables[0].name, "category")
    pipe = {k: str(v) for k, v in raw.items()}
    out = os_.output or {}
    one = {int(p["row_id"]): str(p["category"]) for p in out.get("product_categories") or [] if str(p.get("row_id", "")).lstrip("-").isdigit()}
    # Pipeline aggregate: find the aggregate step frame with category + country + a revenue sum
    pipe_claimed: list[dict[str, Any]] = []
    agg_step = None
    for s in _steps(pr, "aggregate"):
        df = _frame(pr, s["id"])
        if df is None:
            continue
        gb = s.get("group_by", [])
        sums = [m["name"] for m in s.get("metrics", []) if m.get("fn") == "sum" and m["name"] in df.columns]
        cat_col = next((c for c in gb if c.endswith(".value")), None)
        country_col = next((c for c in gb if "country" in c.lower()), None)
        if cat_col in df.columns and country_col in df.columns and sums:
            agg_step = s["id"]
            pipe_claimed = [{"category": str(r[cat_col]), "country": str(r[country_col]), "revenue": float(r[sums[0]]) if pd.notna(r[sums[0]]) else 0.0} for _, r in df.iterrows()]
            break
    desc = {int(r.row_id): r.description for r in prod.itertuples()}
    both = sorted(set(pipe) & set(one))
    pairs = [{"row_id": k, "description": desc[k], "pipeline": pipe[k], "oneshot": one[k]} for k in head(both, 10)]
    return {
        "notes": prep.notes,
        "pipeline": {"from_step": step, "question": qname, "products_labeled": len(pipe), "categories": dict(Counter(pipe.values()).most_common()),
                     "aggregate_step": agg_step, "revenue_check": _revenue_check(pipe_claimed, _expected_revenue(pipe, prod, pc)) if pipe_claimed else None},
        "oneshot": {"products_labeled": len(one), "categories": dict(Counter(one.values()).most_common()),
                    "revenue_check": _revenue_check(out.get("revenue_by_category_and_country") or [], _expected_revenue(one, prod, pc))},
        "agreement": {"products_labeled_by_both": len(both), "adjusted_rand_index": adjusted_rand_index(pipe, one), "nmi": normalized_mutual_information(pipe, one)},
        "examples": {"label_pairs": pairs},
    }


RETAIL = Scenario(
    key="retail_gift_categories", title="Retail: gift categories, revenue by category and country",
    prompt="Classify products into gift categories, then calculate revenue by category and country using the retail revenue by product and country table.",
    prepare=prep_retail,
    oneshot_schema={"type": "object", "properties": {
        "product_categories": {"type": "array", "items": {"type": "object", "properties": {"row_id": {"type": "integer"}, "category": {"type": "string"}}, "required": ["row_id", "category"], "additionalProperties": False}},
        "revenue_by_category_and_country": {"type": "array", "items": {"type": "object", "properties": {"category": {"type": "string"}, "country": {"type": "string"}, "revenue": {"type": "number"}}, "required": ["category", "country", "revenue"], "additionalProperties": False}}},
        "required": ["product_categories", "revenue_by_category_and_country"], "additionalProperties": False},
    oneshot_hint="Assign exactly one gift category to every product row (use a small, consistent set of category names), then return the total revenue for every (category, country) pair computed from the second table by summing the revenue column of the products in that category.",
    compare=compare_retail,
)

# ------------------------------------------------------------------------------------------------
# 6. Product matching across two tables (WDC gold standard)
# ------------------------------------------------------------------------------------------------


def prep_wdc() -> Prepared:
    left = _csv("wdc_offers_left.csv")
    right = _csv("wdc_catalog_right.csv")
    # WDC uses one id space for both sides, so the catalog can contain an offer's own identical record; drop those
    # (none of them is a gold match) so matching cannot degenerate into finding the identical row.
    self_records = right["catalog_id"].isin(set(left["offer_id"]))
    right = right[~self_records].reset_index(drop=True)
    left.insert(0, "row_id", range(1, len(left) + 1))
    gold = {int(r.offer_id): int(r.true_catalog_id) for r in left.itertuples()}
    assert set(gold.values()) <= set(right["catalog_id"].astype(int)), "gold catalog entries must remain in the catalog"
    t1 = _write(left[["row_id", "offer_id", "brand", "title", "description", "price", "currency"]], "product_offers")
    t2 = _write(right[["catalog_id", "brand", "title", "description", "price", "currency"]], "product_catalog")
    return Prepared([t1, t2], gold={"pairs": gold}, notes=[
        f"{len(left)} offers, each with exactly one true match among {len(right)} catalog entries (WDC gold standard); every other catalog entry is a "
        f"different product. {int(self_records.sum())} catalog entries that were the offers' own identical records were removed first. "
        "The cluster_id and true_catalog_id columns were removed from both inputs."])


def compare_wdc(prep: Prepared, pr: Any, os_: Any) -> dict[str, Any]:
    gold: dict[int, int] = prep.gold["pairs"]
    gold_pairs = set(gold.items())
    offers = pd.read_csv(prep.tables[0].path, dtype=str, keep_default_na=False)
    rowid_to_offer = {int(r.row_id): int(r.offer_id) for r in offers.itertuples()}
    # Pipeline: semantic_match step -> (left offer, right catalog id)
    pipe_pairs: set[tuple[int, int]] = set()
    status_counts: dict[str, int] = {}
    diagnostics: dict[str, Any] = {}
    ms = _steps(pr, "semantic_match")
    if ms:
        name = ms[0].get("name", "match")
        df = _frame(pr, ms[0]["id"])
        if df is not None:
            right = pr.datasets[prep.tables[1].name]["frame"]
            right_map = {int(a): int(b) for a, b in zip(right["_row_id"], right["catalog_id"])}
            status_counts = dict(Counter(str(s) for s in df.get(f"{name}.status", pd.Series(dtype=str))))
            left_ids = _row_ids(pr, df, prep.tables[0].name)
            gold_in_cands = gold_accepted = gold_uncertain = gold_rejected = 0
            cal = ((getattr(pr, "calibration", None) or {}).get(ms[0]["id"]) or {}).get(name) or {}
            accept_cut = float(cal["cut"]) if cal.get("mode") == "auto" else float(ms[0].get("accept_min", 0.8))
            reject_cut = accept_cut if cal.get("mode") == "auto" else float(ms[0].get("reject_max", 0.3))
            near = list(df[f"{name}.near"]) if f"{name}.near" in df.columns else [False] * len(df)
            flagged_pairs = set()
            for rid, status, rr, cands, nr in zip(left_ids, df.get(f"{name}.status", []), df.get(f"{name}.right_row_id", []), df.get(f"{name}.candidates", []), near):
                if str(status) == "matched" and pd.notna(rr) and int(rr) in right_map:
                    pipe_pairs.add((rowid_to_offer[rid], right_map[int(rr)]))
                    if nr is True or nr == 1:
                        flagged_pairs.add((rowid_to_offer[rid], right_map[int(rr)]))
                try:
                    clist = json.loads(cands) if isinstance(cands, str) else []
                except ValueError:
                    clist = []
                gold_cat = gold.get(rowid_to_offer.get(rid, -1))
                hit = next((c for c in clist if right_map.get(int(c.get("right_row_id", -1))) == gold_cat), None)
                if hit is not None:
                    gold_in_cands += 1
                    p = float(hit.get("p_same") or 0.0)
                    if p >= accept_cut:
                        gold_accepted += 1
                    elif p > reject_cut:
                        gold_uncertain += 1
                    else:
                        gold_rejected += 1
            diagnostics = {"candidates_per_row": ms[0].get("candidates_per_row"), "accept_min": ms[0].get("accept_min"), "reject_max": ms[0].get("reject_max"),
                           "calibration": cal or None, "accept_cut_used": accept_cut,
                           "flagged_near_cut_pairs": len(flagged_pairs), "flagged_pairs_correct": len(flagged_pairs & gold_pairs),
                           "flagged_near_cut_rows": int(sum(1 for nr in near if nr is True or nr == 1)),
                           "gold_entry_among_candidates": gold_in_cands, "candidate_recall": round(gold_in_cands / len(gold), 4) if gold else None,
                           "gold_candidate_accepted_by_jev": gold_accepted, "gold_candidate_left_uncertain": gold_uncertain, "gold_candidate_rejected_by_jev": gold_rejected,
                           "note": "recall lost before Jev = offers whose true entry was not among the lexical candidates; recall lost at Jev = true entry present but scored below accept_min"}
    one_pairs = {(int(m["offer_id"]), int(m["catalog_id"])) for m in (os_.output or {}).get("matches") or [] if str(m.get("offer_id", "")).isdigit() and str(m.get("catalog_id", "")).isdigit()}
    title = {int(r.offer_id): r.title[:80] for r in offers.itertuples()}
    return {
        "notes": prep.notes, "gold_pairs": len(gold_pairs),
        "pipeline": {"pairs": len(pipe_pairs), "status_counts": status_counts, **prf(pipe_pairs, gold_pairs), "diagnostics": diagnostics},
        "oneshot": {"pairs": len(one_pairs), **prf(one_pairs, gold_pairs)},
        "agreement": set_agreement(pipe_pairs, one_pairs),
        "examples": {"pipeline_wrong": [{"offer_id": o, "title": title.get(o), "chosen": c, "gold": gold.get(o)} for o, c in head(sorted(pipe_pairs - gold_pairs))],
                     "oneshot_wrong": [{"offer_id": o, "title": title.get(o), "chosen": c, "gold": gold.get(o)} for o, c in head(sorted(one_pairs - gold_pairs))]},
    }


WDC = Scenario(
    key="wdc_product_matching", title="Product matching: offers to catalog",
    prompt="Match offers to the catalog for the same exact product despite different titles.",
    prepare=prep_wdc,
    oneshot_schema={"type": "object", "properties": {"matches": {"type": "array", "items": {"type": "object", "properties": {"offer_id": {"type": "integer"}, "catalog_id": {"type": "integer"}}, "required": ["offer_id", "catalog_id"], "additionalProperties": False}}}, "required": ["matches"], "additionalProperties": False},
    oneshot_hint="For every offer that has the same exact product in the catalog, return its offer_id with the catalog_id of that product. Omit offers with no match.",
    compare=compare_wdc,
)

SCENARIOS: dict[str, Scenario] = {s.key: s for s in (SUPPORT, BANKING, CFPB, AIRBNB, RETAIL, WDC)}
