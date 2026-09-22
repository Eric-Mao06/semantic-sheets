"""Render benchmarks/results/*.json into benchmarks/RESULTS.md."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import ASTRA_PRICES, JEV_PRICE_PER_MTOK_INPUT, LONG_CONTEXT_INPUT_TOKENS, PLANNER_PRICES, RESULTS_DIR  # noqa: E402

ORDER = ["support_requests", "banking_queries", "cfpb_complaints", "airbnb_reviews", "retail_gift_categories", "wdc_product_matching"]
RUN_ORDER = ["planner-gpt-6-astra", "planner-glm-5.3-flash-run1", "planner-glm-5.3-flash", "planner-deepseek-v4.1-flash", "planner-deepseek-v4.1-flash-calibrated"]


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
        rv = p.get("missed_gold_in_review_view") or 0
        fl = _flagged(p)
        return (f"{p.get('selected')} rows; P {_f(p.get('precision'), pct=True)} / R {_f(p.get('recall'), pct=True)} / F1 {_f(p.get('f1'), pct=True)}"
                + (f"; {rv} missed gold rows sit in the review view" if rv else "") + (f"; {fl} answered rows flagged near the cut" if fl else ""),
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
        fl = _flagged(p)
        return (f"{p.get('selected')} rows ranked" + (f"; {_f(p.get('keyword_share'), pct=True)} carry a topical keyword" if p.get("selected") else "")
                + f"; {rv} rows withheld as uncertain" + (f", {fl} answered rows flagged near the cut" if fl else "") + f" ({p.get('oneshot_rows_in_review_view')} of the one-shot's picks in the review view)",
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
    routes = pc.get("jev_route_requests") or {}
    route_s = (" (" + ", ".join(f"{n} {k}" for k, n in sorted(routes.items())) + ")") if routes else ""
    p = f"planner {pl.get('input_tokens') or 0:,} in / {pl.get('output_tokens') or 0:,} out; Jev {pc.get('jev_input_tokens') or 0:,} in over {pc.get('jev_requests') or 0:,} requests{route_s}"
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


def _flagged(p: dict[str, Any]) -> int:
    return sum((p.get("review_view") or {}).get(q, {}).get("flagged_near_cut", 0) for q in (p.get("review_view") or {}))


def _calibration_rows(r: dict[str, Any]) -> list[str]:
    """One line per calibrated cut: what the planner wrote, what the engine used, and how many rows sit near it."""
    cal = r["pipeline"].get("calibration") or {}
    lines = []
    for step_id, qs in cal.items():
        for qname, c in qs.items():
            if c.get("mode") == "auto":
                how = f"Otsu {c.get('raw_cut')}" + (" → clamped" if c.get("clamped") else "")
                lines.append(f"| `{step_id}.{qname}` | {c.get('planner_true_min')} / {c.get('planner_false_max')} | **{c.get('cut')}** ({how}) | {c.get('n_scored'):,} | ±{c.get('flag_margin')} |")
            else:
                lines.append(f"| `{step_id}.{qname}` | {c.get('planner_true_min')} / {c.get('planner_false_max')} | {c.get('cut')} ({c.get('mode')}: planner thresholds kept) | {c.get('n_scored'):,} | – |")
    return lines


def _planner_desc(results: list[dict[str, Any]]) -> tuple[str, str, str]:
    cfg = (results[0].get("planner_config") or {}) if results else {}
    model = cfg.get("model") or ((results[0]["pipeline"].get("planner") or {}).get("model") if results else None) or "gpt-6-astra"
    provider = cfg.get("provider") or "openai"
    effort = cfg.get("reasoning_effort") or "high"
    served = sorted({str(((r["pipeline"].get("planner") or {}).get("usage") or {}).get("served_by")) for r in results} - {"None"})
    pinned = cfg.get("openrouter_providers") or []
    if pinned:
        provider += f" pinned to {', '.join(pinned)}" + (f" (served by {', '.join(served)})" if served and served != pinned else "")
    elif served:
        provider += f" (served by {', '.join(served)})"
    return model, provider, effort


def _price_line() -> str:
    p = ASTRA_PRICES["short"]
    pl = ASTRA_PRICES["long"]
    glm = PLANNER_PRICES["z-ai/glm-5.3-flash"]
    ds = PLANNER_PRICES["deepseek/deepseek-v4.1-flash"]
    return (f"Prices used (list, 2026-09-21): gpt-6-astra ${p['input']:.2f} / M input, ${p['cached_input']:.2f} / M cached input, ${p['output']:.2f} / M output "
            f"(reasoning tokens bill as output; requests over {LONG_CONTEXT_INPUT_TOKENS:,} input tokens reprice to ${pl['input']:.2f} / ${pl['output']:.2f}); "
            f"z-ai/glm-5.3-flash via OpenRouter ${glm['input']:.2f} / M input, ${glm['output']:.2f} / M output; "
            f"deepseek/deepseek-v4.1-flash via OpenRouter (Together) ${ds['input']:.2f} / M input, ${ds['output']:.2f} / M output, using OpenRouter's reported cost when it returns one; "
            f"jev-1.13.0 ${JEV_PRICE_PER_MTOK_INPUT} / M input, output free. Costs are computed from the token usage each API reported.\n")


def render(results: list[dict[str, Any]]) -> str:
    model, provider, effort = _planner_desc(results)
    out: list[str] = []
    run = results[0].get("run") or ""
    out.append(f"# Benchmark run `{run}`: operators with `{model}` planner vs. one-shot gpt-6-astra\n")
    for n in results[0].get("notes") or []:
        if "Earlier" in n or "JEV_ROUTES" in n:
            out.append(n + "\n")
    out.append("Both arms receive the **same prompt** and the **same CSV** (label and leak columns removed, explicit `row_id`).\n")
    out.append(f"- **Operators (this repo):** `{model}` (reasoning `{effort}`, via {provider}) sees only the schema and ≤ 20 sample rows and writes a typed plan; "
               "`jev-1.13.0` answers the per-row semantic questions; DuckDB does the filtering, sorting, joins and arithmetic.\n"
               "- **One-shot:** `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and returns the final answer as JSON (no tools, no code).\n")
    out.append(_price_line())

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
            reused = (r["pipeline"].get("planner") or {}).get("reused_from_run")
            who = f"**Plan reused from run `{reused}`**" if reused else f"**Plan written by the planner** ({(r['pipeline'].get('planner') or {}).get('attempts')} attempt(s))"
            out.append(f"{who}: *{plan.get('title', '')}* — {plan.get('description', '')}\n")
            out.extend(_plan_summary(plan))
            out.append("")
        cal_rows = _calibration_rows(r)
        if cal_rows:
            out.append("**Decision cuts** (planner's true_min / false_max vs. the cut the engine used after seeing the scores)\n")
            out.append("| Question | Planner | Used | Scored rows | Flag margin |")
            out.append("|---|---|---|---|---|")
            out.extend(cal_rows)
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


def _load_run(run_dir: Path) -> list[dict[str, Any]]:
    results = []
    for k in ORDER:
        p = run_dir / f"{k}.json"
        if p.exists():
            results.append(json.loads(p.read_text(encoding="utf-8")))
    return results


def render_index(runs: dict[str, list[dict[str, Any]]]) -> str:
    """Top-level RESULTS.md: the one-shot baseline against the operators under each planner."""
    out: list[str] = []
    out.append("# Benchmark: Jev spreadsheet operators vs. one-shot gpt-6-astra\n")
    out.append("Six walkthrough scenarios, each run two ways with the **same prompt and the same CSV**: the operators (planner writes a typed plan → `jev-1.13.0` "
               "answers per row → DuckDB does the exact work) and a single `gpt-6-astra` (reasoning `high`) request holding the whole CSV. "
               "The operators were run once per planner model; the one-shot answers are shared across runs.\n")
    out.append(_price_line())
    names = [r for r in RUN_ORDER if r in runs] + [r for r in runs if r not in RUN_ORDER]
    descs = {r: _planner_desc(runs[r]) for r in names}
    out.append("| Run | Planner | Provider | Jev routes | Decision cuts | Details |")
    out.append("|---|---|---|---|---|---|")
    for r in names:
        m, prov, eff = descs[r]
        routes: dict[str, int] = {}
        for x in runs[r]:
            for k, n in ((x["pipeline"].get("cost") or {}).get("jev_route_requests") or {}).items():
                routes[k] = routes.get(k, 0) + int(n)
        route_s = ", ".join(f"{k} ({n} requests)" for k, n in sorted(routes.items())) if routes else "direct"
        auto = any(c.get("mode") == "auto" for x in runs[r] for qs in (x["pipeline"].get("calibration") or {}).values() for c in qs.values())
        reused = (runs[r][0].get("planner_config") or {}).get("plans_reused_from")
        cuts = ("calibrated on the scores (Otsu)" if auto else "planner's fixed thresholds") + (f"; plans reused from `{reused}`" if reused else "")
        out.append(f"| `{r}` | `{m}` (reasoning `{eff}`) | {prov} | {route_s} | {cuts} | [results/{r}/RESULTS.md](results/{r}/RESULTS.md) |")
    out.append("")
    by_run = {r: {x["scenario"]: x for x in runs[r]} for r in names}
    base = runs[names[0]]
    out.append("## Quality\n")
    out.append("| Scenario | One-shot (gpt-6-astra) | " + " | ".join(f"Operators, `{descs[r][0]}` planner" for r in names) + " |")
    out.append("|---|---|" + "---|" * len(names))
    for r0 in base:
        k = r0["scenario"]
        _, ho, _ = _headline(r0)
        cells = []
        for r in names:
            x = by_run[r].get(k)
            cells.append(_headline(x)[0] if x else "–")
        out.append(f"| **{r0['title']}** | {ho} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("## Cost and latency\n")
    out.append("| Scenario | One-shot cost / latency | " + " | ".join(f"Operators cost / latency, `{descs[r][0]}` planner" for r in names) + " |")
    out.append("|---|---|" + "---|" * len(names))
    totals = {r: 0.0 for r in names}
    tot_o = 0.0
    for r0 in base:
        k = r0["scenario"]
        oc = (r0["oneshot"].get("cost") or {}).get("usd")
        tot_o += oc or 0.0
        cells = []
        for r in names:
            x = by_run[r].get(k)
            if not x:
                cells.append("–")
                continue
            c = x["pipeline"].get("cost") or {}
            t = x["pipeline"].get("timings_seconds") or {}
            totals[r] += c.get("total_usd") or 0.0
            cells.append(f"{_usd(c.get('planner_usd'))} planner + {_usd(c.get('jev_usd_from_measured_tokens'))} Jev = **{_usd(c.get('total_usd'))}** · {t.get('planner', 0):.0f}s + {t.get('job', 0):.0f}s")
        out.append(f"| {r0['title']} | **{_usd(oc)}** · {_latency(r0)[1]} | " + " | ".join(cells) + " |")
    out.append("| **Total** | **" + _usd(tot_o) + "** | " + " | ".join(f"**{_usd(totals[r])}** ({round(tot_o / totals[r], 1) if totals[r] else '–'}× cheaper than one-shot)" for r in names) + " |")
    out.append("")
    out.append("## Agreement between the two arms\n")
    out.append("| Scenario | " + " | ".join(f"`{descs[r][0]}` planner" for r in names) + " |")
    out.append("|---|" + "---|" * len(names))
    for r0 in base:
        k = r0["scenario"]
        out.append(f"| {r0['title']} | " + " | ".join((_headline(by_run[r][k])[2] if k in by_run[r] else "–") for r in names) + " |")
    out.append("")
    out.append("Per-run reports (every plan, every metric, examples of disagreements): " + ", ".join(f"[{r}](results/{r}/RESULTS.md)" for r in names) + ".\n")
    return "\n".join(out)


def main() -> int:
    runs: dict[str, list[dict[str, Any]]] = {}
    for d in sorted(p for p in RESULTS_DIR.iterdir() if p.is_dir()):
        results = _load_run(d)
        if results:
            runs[d.name] = results
            (d / "RESULTS.md").write_text(render(results), encoding="utf-8")
            print(f"wrote {d / 'RESULTS.md'} ({len(results)} scenarios)")
    if not runs:
        print("no results found", file=sys.stderr)
        return 1
    (HERE / "RESULTS.md").write_text(render_index(runs), encoding="utf-8")
    print(f"wrote {HERE / 'RESULTS.md'} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
