"""Render benchmarks/results/*.json into benchmarks/RESULTS.md."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import ASTRA_PRICES, JEV_PRICE_PER_MTOK_INPUT, LONG_CONTEXT_INPUT_TOKENS, RESULTS_DIR  # noqa: E402

ORDER = ["support_requests", "banking_queries", "cfpb_complaints", "airbnb_reviews", "retail_gift_categories", "wdc_product_matching"]


def _f(x: Any, nd: int = 2, pct: bool = False) -> str:
    if x is None:
        return "–"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int,)) and not pct:
        return f"{x:,}"
    if isinstance(x, float):
        return f"{x * 100:.1f}%" if pct else f"{x:,.{nd}f}"
    return str(x)


def _usd(x: Any) -> str:
    return "–" if x is None else f"${x:,.4f}" if x < 0.01 else f"${x:,.3f}" if x < 1 else f"${x:,.2f}"


def _headline(r: dict[str, Any]) -> tuple[str, str, str]:
    c = r.get("comparison", {})
    p, o, a = c.get("pipeline", {}), c.get("oneshot", {}), c.get("agreement", {})
    k = r["scenario"]
    if "error" in c:
        return c["error"], "", ""
    if k == "support_requests":
        return (f"{p.get('selected')} rows; P {_f(p.get('precision'), pct=True)} / R {_f(p.get('recall'), pct=True)} / F1 {_f(p.get('f1'), pct=True)}",
                f"{o.get('selected')} rows; P {_f(o.get('precision'), pct=True)} / R {_f(o.get('recall'), pct=True)} / F1 {_f(o.get('f1'), pct=True)}",
                f"Jaccard {_f(a.get('jaccard'))} ({a.get('both')} shared)")
    if k == "banking_queries":
        def cls(side: dict[str, Any]) -> str:
            s, l = side.get("strict") or {}, side.get("lenient") or {}
            pc = (l.get("per_class") or {})
            return (f"pending F1 {_f((pc.get('pending') or {}).get('f1'), pct=True)}, failed F1 {_f((pc.get('failed') or {}).get('f1'), pct=True)} (lenient gold); "
                    f"macro-F1 {_f(s.get('macro_f1'), pct=True)} strict / {_f(l.get('macro_f1'), pct=True)} lenient")
        return (cls(p), cls(o), f"κ {_f(a.get('kappa'))}, agreement {_f(a.get('agreement'), pct=True)}; wrong_account {(a.get('wrong_account') or {}).get('both')} shared")
    if k == "cfpb_complaints":
        rv = sum((p.get("review_view") or {}).get(q, {}).get("uncertain", 0) for q in (p.get("review_view") or {}))
        return (f"{p.get('selected')} rows ranked" + (f"; {_f(p.get('keyword_share'), pct=True)} carry a topical keyword" if p.get("selected") else "") + f"; {rv} rows in review view ({p.get('oneshot_rows_in_review_view')} of the one-shot's picks among them)",
                f"{o.get('selected')} rows ranked; {_f(o.get('keyword_share'), pct=True)} carry a topical keyword",
                f"Jaccard {_f(a.get('jaccard'))}; Spearman ρ {_f((a.get('rank_spearman_on_common') or {}).get('rho'))} on {(a.get('rank_spearman_on_common') or {}).get('n')} shared; top-20 overlap {(a.get('top_20_overlap') or {}).get('overlap')}")
    if k == "airbnb_reviews":
        return (f"{p.get('flagged_reviews')} reviews, {p.get('properties')} properties; aggregate exact",
                f"{o.get('flagged_reviews')} reviews, {o.get('properties_claimed')} properties; counts consistent {o.get('review_counts_consistent')}/{o.get('properties_claimed')}, prices right {o.get('nightly_prices_correct')}/{o.get('properties_claimed')}",
                f"reviews Jaccard {_f((a.get('reviews') or {}).get('jaccard'))}; properties Jaccard {_f((a.get('properties') or {}).get('jaccard'))}")
    if k == "retail_gift_categories":
        rc_p, rc_o = p.get("revenue_check") or {}, o.get("revenue_check") or {}
        return (f"{len(p.get('categories') or {})} categories; revenue cells exact {rc_p.get('exact_to_cent')}/{rc_p.get('cells_expected')}" if rc_p else f"{len(p.get('categories') or {})} categories; no aggregate step",
                f"{len(o.get('categories') or {})} categories; revenue cells exact {rc_o.get('exact_to_cent')}/{rc_o.get('cells_expected')} (within 0.5%: {rc_o.get('within_0_5_percent')}), max error {_usd(rc_o.get('max_abs_error'))}, {rc_o.get('cells_missing')} cells missing",
                f"ARI {_f(a.get('adjusted_rand_index'))}, NMI {_f(a.get('nmi'))} over {a.get('products_labeled_by_both')} products")
    if k == "wdc_product_matching":
        return (f"{p.get('pairs')} pairs; P {_f(p.get('precision'), pct=True)} / R {_f(p.get('recall'), pct=True)} / F1 {_f(p.get('f1'), pct=True)}",
                f"{o.get('pairs')} pairs; P {_f(o.get('precision'), pct=True)} / R {_f(o.get('recall'), pct=True)} / F1 {_f(o.get('f1'), pct=True)}",
                f"Jaccard {_f(a.get('jaccard'))} ({a.get('both')} shared pairs)")
    return "", "", ""


def _cost_row(r: dict[str, Any]) -> tuple[str, str, str, str]:
    pc = r["pipeline"].get("cost") or {}
    oc = r["oneshot"].get("cost") or {}
    planner = pc.get("planner_usd")
    jev = pc.get("jev_usd_from_measured_tokens")
    total = pc.get("total_usd")
    return (f"{_usd(planner)} planner + {_usd(jev)} Jev = **{_usd(total)}**" if total is not None else "–",
            f"**{_usd(oc.get('usd'))}**" if oc else "–",
            f"{(total or 0) and oc.get('usd') and round(oc['usd'] / total, 1) or '–'}×" if total and oc.get("usd") else "–",
            "")


def _latency(r: dict[str, Any]) -> tuple[str, str]:
    t = r["pipeline"].get("timings_seconds") or {}
    p = f"{t.get('planner', 0):.0f}s plan + {t.get('job', 0):.0f}s Jev" if t else "–"
    o = f"{r['oneshot'].get('latency_seconds'):.0f}s" if r["oneshot"].get("latency_seconds") is not None else "–"
    return p, o


def _tokens(r: dict[str, Any]) -> tuple[str, str]:
    pc = r["pipeline"].get("cost") or {}
    pl = (r["pipeline"].get("planner") or {}).get("usage") or {}
    ou = r["oneshot"].get("usage") or {}
    p = f"planner {pl.get('input_tokens') or 0:,} in / {pl.get('output_tokens') or 0:,} out; Jev {pc.get('jev_input_tokens') or 0:,} in over {pc.get('jev_requests') or 0:,} requests"
    o = f"{ou.get('input_tokens') or 0:,} in / {ou.get('output_tokens') or 0:,} out (reasoning {ou.get('reasoning_tokens') or 0:,})"
    return p, o


def _plan_summary(plan: dict[str, Any]) -> list[str]:
    lines = []
    for s in plan.get("steps", []):
        op = s.get("op")
        if op == "semantic_annotate":
            for q in s.get("questions", []):
                extra = f" options={list((q.get('options') or {}).keys())}" if q.get("kind") == "category" else f" levels={len(q.get('levels') or [])}" if q.get("kind") == "score" else ""
                lines.append(f"- `{s['id']}` semantic_annotate · **{q['name']}** ({q['kind']}){extra}: {q.get('instruction', '')[:220]}")
        elif op == "semantic_match":
            lines.append(f"- `{s['id']}` semantic_match on {s.get('left_columns')} ↔ {s.get('right_columns')}, {s.get('candidates_per_row')} candidates/row, accept ≥ {s.get('accept_min')}: {s.get('instruction', '')[:200]}")
        elif op == "aggregate":
            lines.append(f"- `{s['id']}` aggregate group_by={s.get('group_by')} metrics={[(m.get('fn'), m.get('column')) for m in s.get('metrics', [])]}")
        elif op == "filter":
            lines.append(f"- `{s['id']}` filter `{json.dumps(s.get('where'))[:160]}`")
        elif op == "sort":
            lines.append(f"- `{s['id']}` sort by {[(b.get('column'), b.get('direction')) for b in s.get('by', [])]}")
        elif op == "join":
            lines.append(f"- `{s['id']}` join on {s.get('on')} how={s.get('how')}")
        else:
            lines.append(f"- `{s['id']}` {op}")
    return lines


def _json_block(obj: Any, limit: int = 6000) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    if len(s) > limit:
        s = s[:limit] + "\n… (truncated; full data in results/*.json)"
    return f"```json\n{s}\n```"


def render(results: list[dict[str, Any]]) -> str:
    out: list[str] = []
    out.append("# Benchmark: Jev spreadsheet operators vs. one-shot gpt-6-astra\n")
    out.append("Both arms receive the **same prompt** and the **same CSV** (label and leak columns removed, explicit `row_id`).\n")
    out.append("- **Operators (this repo):** `gpt-6-astra` (reasoning `high`) sees only the schema and ≤ 20 sample rows and writes a typed plan; "
               "`jev-1.13.0` answers the per-row semantic questions; DuckDB does the filtering, sorting, joins and arithmetic.\n"
               "- **One-shot:** `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and returns the final answer as JSON (no tools, no code).\n")
    p = ASTRA_PRICES["short"]
    pl = ASTRA_PRICES["long"]
    out.append(f"Prices used (list, 2026-09-21): gpt-6-astra ${p['input']:.2f} / M input, ${p['cached_input']:.2f} / M cached input, ${p['output']:.2f} / M output "
               f"(reasoning tokens bill as output; requests over {LONG_CONTEXT_INPUT_TOKENS:,} input tokens reprice to ${pl['input']:.2f} / ${pl['output']:.2f}); "
               f"jev-1.13.0 ${JEV_PRICE_PER_MTOK_INPUT} / M input, output free. Costs below are computed from the token usage each API reported.\n")

    out.append("## Summary\n")
    out.append("| Scenario | Rows | Operators (Jev) | One-shot (Astra) | Agreement |")
    out.append("|---|---|---|---|---|")
    for r in results:
        hp, ho, ha = _headline(r)
        rows = " + ".join(f"{i['rows']:,}" for i in r["inputs"])
        out.append(f"| **{r['title']}** | {rows} | {hp} | {ho} | {ha} |")
    out.append("")
    out.append("| Scenario | Operators cost | One-shot cost | One-shot ÷ operators | Operators latency | One-shot latency |")
    out.append("|---|---|---|---|---|---|")
    tot_p = tot_o = 0.0
    for r in results:
        cp, co, ratio, _ = _cost_row(r)
        lp, lo = _latency(r)
        out.append(f"| {r['title']} | {cp} | {co} | {ratio} | {lp} | {lo} |")
        tot_p += (r["pipeline"].get("cost") or {}).get("total_usd") or 0.0
        tot_o += (r["oneshot"].get("cost") or {}).get("usd") or 0.0
    out.append(f"| **Total** | **{_usd(tot_p)}** | **{_usd(tot_o)}** | {round(tot_o / tot_p, 1) if tot_p else '–'}× | | |")
    out.append("")

    for r in results:
        out.append(f"## {r['title']}\n")
        out.append(f"**Prompt:** “{r['prompt']}”\n")
        out.append("**Inputs:** " + "; ".join(f"`{i['name']}` {i['rows']:,} rows × {len(i['columns'])} columns (~{i['approx_tokens']:,} tokens)" for i in r["inputs"]) + "\n")
        for n in r.get("notes", []):
            out.append(f"> {n}\n")
        tp, to = _tokens(r)
        cp, co, ratio, _ = _cost_row(r)
        lp, lo = _latency(r)
        out.append("| | Operators (Jev) | One-shot (Astra) |")
        out.append("|---|---|---|")
        out.append(f"| Tokens | {tp} | {to} |")
        out.append(f"| Cost | {cp} | {co} |")
        out.append(f"| Latency | {lp} | {lo} |")
        job = r["pipeline"].get("job") or {}
        out.append(f"| Status | job `{job.get('state')}`{(' (' + str(job.get('terminal_reason')) + ')') if job.get('terminal_reason') else ''}{'; ' + r['pipeline']['error'] if r['pipeline'].get('error') else ''} | `{r['oneshot'].get('status')}`{'; ' + r['oneshot']['error'] if r['oneshot'].get('error') else ''} |")
        out.append("")
        plan = r["pipeline"].get("plan") or {}
        if plan:
            out.append(f"**Plan written by the planner** ({(r['pipeline'].get('planner') or {}).get('attempts')} attempt(s)): *{plan.get('title', '')}* — {plan.get('description', '')}\n")
            out.extend(_plan_summary(plan))
            out.append("")
        out.append("**Comparison**\n")
        comp = dict(r.get("comparison") or {})
        examples = comp.pop("examples", None)
        out.append(_json_block(comp))
        if examples:
            out.append("\n<details><summary>Examples of disagreements</summary>\n")
            out.append(_json_block(examples, 5000))
            out.append("\n</details>\n")
        out.append("")
    return "\n".join(out)


def main() -> int:
    results = []
    for k in ORDER:
        p = RESULTS_DIR / f"{k}.json"
        if p.exists():
            results.append(json.loads(p.read_text(encoding="utf-8")))
    if not results:
        print("no results found", file=sys.stderr)
        return 1
    (HERE / "RESULTS.md").write_text(render(results), encoding="utf-8")
    print(f"wrote {HERE / 'RESULTS.md'} ({len(results)} scenarios)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
