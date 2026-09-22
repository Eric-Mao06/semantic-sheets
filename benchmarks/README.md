# Benchmark: Jev spreadsheet operators vs. one-shot gpt-6-astra

For each of the six operations exercised in the MVP walkthrough, this benchmark gives the **same prompt** and the
**same CSV** to two approaches and compares their final outputs and cost:

| Arm | What happens |
|---|---|
| **Operators** (this repo) | A planner model sees only the schema and ≤ 20 sample rows and writes a typed plan. `jev-1.13.0` answers every per-row semantic question; DuckDB performs the filtering, sorting, joins and arithmetic. Uncertain judgements go to a review view instead of the output. Four runs: planner `gpt-6-astra` (reasoning `high`, OpenAI) with Jev direct; planner `z-ai/glm-5.3-flash` (reasoning `high`, OpenRouter) with Jev direct (`run1`); the same GLM planner with Jev spread over TypeSafe direct **and** OpenRouter's decisions endpoint (`planner-glm-5.3-flash`); and planner `deepseek/deepseek-v4.1-flash` (reasoning `high`, OpenRouter pinned to Together) with dual-route Jev. |
| **One-shot** | `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and must return the final answer as JSON that satisfies a strict schema. No tools, no code execution. |

Full numbers, every plan the planner wrote, and examples of disagreements: **[RESULTS.md](RESULTS.md)** (cross-run
summary), `results/<run>/RESULTS.md` (per run) and `results/<run>/*.json` (raw). Prices are OpenAI, OpenRouter and
TypeSafe list prices as of 2026-09-21 and costs are computed from the token usage each API reported.

Jev is available two ways and the engine uses both by default (`JEV_ROUTES=direct,openrouter`): `POST
https://api.typesafe.ai/v1/systemone` with a TypeSafe key, and `POST https://openrouter.ai/api/alpha/decisions` with an
OpenRouter key (model id `typesafe/jev-1.13`; it is a "decisions" model, so it does not appear in OpenRouter's chat
model list). The request and answer shapes are identical, the token counts match to the token, and the price is the same
$0.042 / M input (OpenRouter reports the cost inline). Each route keeps its own request/token buckets and concurrency
in `server/semsheet/engine/jev.py`, packets go to the least-loaded route, and a 429/5xx on one route is retried on the
other, so two routes give roughly twice the throughput.

## Scenarios

| Key | Prompt | Input handed to both arms | Ground truth |
|---|---|---|---|
| `support_requests` | Find customers trying to cancel an order because they cannot afford it. | 3,000 Bitext messages (`intent` removed) | Proxy: `intent == cancel_order` and affordability wording (9 rows) |
| `banking_queries` | Separate pending transfers, failed transfers, and transfers sent to the wrong account. | 3,080 BANKING77 queries (`category` removed) | BANKING77 intents mapped to `pending` / `failed` (strict and lenient mapping); `wrong_account` has no BANKING77 intent and is compared between arms only |
| `cfpb_complaints` | Find complaints about charges continuing after cancellation or unauthorized recurring charges, then rank by urgency. | 5,000 CFPB complaints, 8 columns (no narratives exist in the 2026 export) | None; agreement plus a keyword sanity check |
| `airbnb_reviews` | Find reviews reporting unreliable Wi-Fi, group by property, and compare nightly prices. | 1,200 multilingual reviews (all 70 that mention Wi-Fi at all plus 1,130 random) | None; agreement, plus exact recomputation of every count and price |
| `retail_gift_categories` | Classify products into gift categories, then calculate revenue by category and country using the retail revenue by product and country table. | 300 top products + 4,758 product × country revenue rows | Clustering agreement (ARI / NMI) between the two taxonomies; every revenue cell recomputed from each arm's own labels |
| `wdc_product_matching` | Match offers to the catalog for the same exact product despite different titles. | 400 offers + 598 catalog entries (`cluster_id`, `true_catalog_id` and the offers' own records removed) | WDC gold pairs, exactly one per offer |

Tables are bounded so the one-shot request stays under gpt-6-astra's 272K-token long-context price tier. The
100,000-row CFPB file used in the walkthrough is ~6.8M tokens (all 15 columns) and cannot be sent in one request at all
(context window 1.05M).

## Results (single run per scenario and planner, 2026-09-21)

The one-shot answers are the same in every row of each scenario; only the operator side changes. `GLM run1` is the
GLM 5.3 Flash planner with Jev direct only; `GLM run2` is the same planner with Jev over both routes and a fresh plan;
`DeepSeek` is DeepSeek V4.1 Flash served by Together with dual-route Jev.

| Scenario | Operators, `gpt-6-astra` planner | Operators, GLM run1 | Operators, GLM run2 | Operators, DeepSeek V4.1 Flash | One-shot (Astra) | Cost: Astra / GLM run1 / GLM run2 / DeepSeek / one-shot |
|---|---|---|---|---|---|---|
| Support: cancel because unaffordable | 7 rows, P 100% / R 78% | 2 rows, R 22%, **+6 gold rows in review** | 3 rows, R 33%, **+4 in review** | 3 rows, R 33%, **+5 in review** | 9 rows, P 100% / R 100% | $0.086 / $0.034 / $0.035 / $0.034 / $0.51 |
| Banking: transfer problems (lenient gold) | pending F1 69%, failed F1 58% | pending F1 87%, failed F1 57% | pending F1 58%, failed F1 39% | pending F1 83%, failed F1 64% | pending F1 80%, failed F1 75% | $0.126 / $0.045 / $0.056 / $0.042 / $0.96 |
| Complaints: filter + rank | 0 rows accepted, **10 in review** (incl. all 8 the one-shot picked) | 10 ranked (incl. all 8), 74 in review | 170 ranked (incl. all 8; 78% carry a keyword), 106 in review | 1 ranked, **12 in review** (7 of the one-shot's 8 among them) | 8 rows ranked | $0.120 / $0.015 / $0.011 / $0.014 / $2.13 |
| Airbnb: unreliable Wi-Fi by property | 16 reviews / 16 properties, aggregate exact | 16 / 16, exact | 16 / 16, exact | 16 / 16, exact | 17 reviews / 17 properties, 16 of 17 prices right | $0.127 / $0.019 / $0.016 / $0.015 / $1.35 |
| Retail: categories + revenue by country | 11 categories, **376 / 376 revenue cells exact** | 8 categories (33 products in review), 278 / 278 exact | 7 categories, 267 / 267 exact | 8 categories, 289 / 289 exact | 3 categories, 53 / 83 cells exact, worst cell off by $46,503 | $0.147 / $0.0065 / $0.0058 / $0.0067 / $5.07 (26 min) |
| Product matching | 171 pairs, P 97% / R 42% (+75 gold pairs in review) | 175 pairs, R 43% (+69 in review) | 151 pairs, R 37% (+90 in review) | 139 pairs, P 98% / R 34% (+103 in review) | 357 pairs, P 97% / R 87% | $0.115 / $0.039 / $0.044 / $0.036 / $1.53 |
| **Total** | | | | | | **$0.72 (16×) / $0.16 (73×) / $0.17 (69×) / $0.15 (78×) / $11.55** |

Planner latency per scenario: gpt-6-astra 15–50 s; GLM 5.3 Flash 5–28 s; DeepSeek V4.1 Flash on Together 2.0–4.5 s.
With the DeepSeek planner and dual-route Jev the whole operator pipeline finished in 5–12 s per scenario.

### What the numbers say

- **Cost.** With the Astra planner the operators cost 6–34× less than the one-shot (16× overall) and 79% of that is the
  single planner call. Swapping the planner to GLM 5.3 Flash or DeepSeek V4.1 Flash cuts the planner to $0.001–0.002
  per scenario (4–7% of the total); the operators then cost $0.15–0.17 for all six scenarios, 70–78× less than the
  one-shot, and are Jev-dominated. Jev itself is $0.005–$0.055 per scenario ($0.15 for all six) and grows linearly with rows (the
  5,000-row CFPB job used 204K Jev tokens for $0.009), so the 100K-row walkthrough would be about $0.17 of Jev, while
  the one-shot cannot run at that size at any price. Routing half of Jev through OpenRouter changes nothing on price.
- **Latency.** Operators finish in 20–50 s with the Astra planner (15–50 s of it planning), 10–35 s with GLM 5.3
  Flash (5–28 s planning) and 5–12 s with DeepSeek V4.1 Flash on Together (2.0–4.5 s planning). The Jev stage with one route took 1–14 s for 300–5,000 rows; with two routes the three
  larger jobs went from 13.3 → 7.5 s (support, 300 requests), 13.7 → 9.2 s (banking, 308) and 8.6 → 4.7 s (matching,
  200), about 1.8× throughput, with packets split 55 / 45 between direct and OpenRouter. The small jobs were already
  bound by per-request latency (~250 ms) and did not change. The one-shot took 27 s to 26 min (the retail aggregation).
- **Planner swap.** GLM 5.3 Flash produced a valid plan on the first attempt in 11 of 12 scenario runs and, per
  scenario, sometimes beat the Astra planner (banking pending-F1 87% vs 69% in run1; CFPB 10 accepted rows vs 0) and
  sometimes lost (support 22–33% recall vs 78%). Its support loss is a review-pile case: GLM's Jev question required the
  message to *both* ask for a cancellation *and* give affordability as the reason, while the Astra plan explicitly told
  Jev that "I cannot afford order 123" counts on its own. Jev scored those messages 0.3–0.7 under GLM's wording, so 4–6
  of the 9 gold rows went to the review view instead of the output. Question wording, not model size, drove the
  difference.
- **DeepSeek V4.1 Flash as planner.** Chosen provider: Together, the fastest end-to-end of the 23 OpenRouter endpoints
  on the real planner prompt (2.3–4.9 s per plan, 234–320 tok/s, full precision, served every request). CoreWeave
  (fp8) measured 4.3–6.1 s and returned "rate-limited upstream" on first contact; Makora, Modal and Novita were 3.4–8.6 s;
  DeepSeek's own endpoint was not routable with this key. Pinned with `PLANNER_OPENROUTER_PROVIDERS=Together` and
  fallbacks off. It produced valid plans first try on all six scenarios, planned in 2.0–4.5 s (1,000× cheaper than the
  Astra planner per call at $0.001–0.002), and was the best operator run on banking (pending-F1 83%, failed-F1 64%,
  κ 0.80 with the one-shot). On CFPB it chose `true_min` 0.7 again and Jev scored the relevant complaints 0.32–0.69, so
  one row was accepted and 12 went to review (7 of the one-shot's 8 picks among them): the same review-pile pattern as
  the Astra planner, from the same threshold choice.
- **Planner variance is larger than the planner-model gap.** Two GLM runs with identical inputs differed more from each
  other than from the Astra planner: banking pending-F1 87% → 58% (run2's `failed_transfer` option read "declined,
  bounced, reversed, returned…" and swept in 257 rows against 80 gold), and CFPB 10 → 170 accepted rows (run2 added
  "(b) charges the customer did not authorize" to the true-criteria, which is any unauthorized charge, not a recurring
  one). Every one of these differences is in plain text in the plan panel before the job runs.
- **Arithmetic and structure.** Where the request needs exact numbers the operators are exact by construction
  (376 / 376 revenue cells, every Airbnb count and price). The one-shot got 30 of 83 revenue cells wrong (28 by more than 0.5%; total off by
  $79K, 0.8%) and one nightly price wrong, and collapsed the taxonomy to 3 categories.
- **Semantic recall.** With the plans the planner wrote, gpt-6-astra reading every row found more: 9 vs 7 affordability
  cancellations, 8 vs 0 accepted complaints, 87% vs 42% matching recall, and higher F1 on the banking classes.
  Precision was at parity (100% / 100% on support, 97% / 97% on matching).
- **Where the operators lost recall, and whether it was visible.**
  - *Candidate retrieval*: in product matching the true catalog entry was among the 5 lexical candidates for only 266 of
    400 offers (66.5%). This ceiling is set before Jev is asked anything; 134 gold pairs were unreachable.
  - *Thresholds*: 75 matching pairs and all 8 complaint rows the one-shot returned scored between the reject and accept
    thresholds (Jev 0.58–0.74 against `accept_min` 0.7–0.8) and landed in the review view. They are not in the output, but
    they are not silently lost either; one review pass recovers them and the corrections version the result.
  - *Literal judgement*: "I can't pay for purchase …" and "I can no longer pay for purchase …" were scored as confident
    negatives for "cancel because they cannot afford it" and did not reach review.
- **Planner variance.** The plan is a fresh generation each run. Four CFPB plans produced four different boolean
  questions (`recurring_charge_match` on 2,000 rows accepted 3 rows; `ongoing_charges` on 5,000 rows accepted 0 and
  reviewed 10; GLM's `recurring_charge` accepted 10 and reviewed 74, then 170 and 106). Question wording and thresholds
  are the biggest quality lever in the operator arm, and they are editable before anything runs.

### Why flagged rows are not in the final result (the review pile)

Every semantic question comes back from Jev as a probability, and the plan carries two thresholds per question
(`true_min` and `false_max`; the schema defaults are 0.85 / 0.15 and both planners chose 0.7 / 0.3 for these
filters). A row scoring at or above `true_min` becomes `true`, at or
below `false_max` becomes `false`, and anything in between becomes `uncertain` with no value. The `filter` step that
follows runs with `unknown_policy: separate`: rows whose predicate is `true` go to the output, rows that are
`uncertain` go to a review view attached to the step, and everything downstream (sort, aggregate, join) only sees the
output. That is why the CFPB result under the Astra planner was empty: Jev gave the eight "Can't stop withdrawals from
your account" complaints 0.58–0.61 (the one-shot picked exactly those eight), which is above the reject line but below
the 0.7 accept line the planner had chosen, so they were routed to review and the ranking step ranked nothing. The
behaviour is deliberate: the output only contains rows Jev was confident about, and the middle band is handed to a
person instead of being silently dropped or silently included. The cost is that a threshold the planner picked without
seeing the score distribution can put the entire answer in the review pile. Two remedies exist in the product: lower
`true_min` in the plan (the threshold is visible and editable before the job runs), or accept the review rows in the
review tab, which versions the result. The benchmark scores only the output, which is the strict reading of "final
result"; the review counts are reported alongside so the recall lost to thresholds is visible.
- **Ambiguity is shared.** Most banking "errors" on both sides are queries about pending top-ups, card payments or
  withdrawals that the prompt's "pending transfers" may or may not cover; both arms made the same call on 94.5% of rows (κ 0.68).

### Two defects found and fixed while building the benchmark

1. The planner prompt never described the `semantic_match` step, so the planner could not express matching and produced
   invalid join + annotate plans for the WDC scenario (`server/semsheet/planner.py`).
2. `scripts/prepare_samples.py` built the WDC catalog from the shared WDC id space, so 396 of 400 offers had their own
   identical record in the catalog; matching degenerated into finding the identical row. Those records are now excluded.

### Caveats

- One run per scenario per configuration (two for the GLM planner); small differences (16 vs 17 reviews, 151–175 pairs)
  are within noise, and the two GLM runs show how wide that noise is on the scenarios that hinge on question wording.
- GLM 5.3 Flash spent 17–334 reasoning tokens per plan at `reasoning_effort: high`; OpenRouter's inline `usage.cost`
  came back as 0 for this model, so its planner cost is computed from the list price.
- Ground truth is a proxy on four of six scenarios (see the notes in each result), and the CFPB export has no
  narratives, which makes that scenario thin for both arms.
- The one-shot arm is a capability probe with a strict JSON schema and an instruction to read every row; it is not how
  anyone would ship the task, and the same model plays the planner in the other arm.

## Running it

```bash
python scripts/prepare_samples.py                  # needs the raw downloads in data/raw (see README)
export OPENAI_API_KEY=... TYPESAFE_API_KEY=...
.venv/bin/python benchmarks/run.py                 # both arms, all scenarios, gpt-6-astra planner (~$12 of gpt-6-astra, ~$0.15 of Jev)
.venv/bin/python benchmarks/run.py -s wdc_product_matching --arms pipeline
.venv/bin/python benchmarks/run.py --reuse oneshot # rerun operators + comparison, reuse stored one-shot answers
export OPENROUTER_API_KEY=...                      # enables Jev over both routes (JEV_ROUTES=direct,openrouter) and the OpenRouter planner
.venv/bin/python benchmarks/run.py --planner-provider openrouter --planner-model z-ai/glm-5.3-flash --reuse oneshot
JEV_ROUTES=direct .venv/bin/python benchmarks/run.py ...   # pin Jev to the TypeSafe API only
.venv/bin/python benchmarks/run.py --planner-provider openrouter --planner-model deepseek/deepseek-v4.1-flash \
    --planner-openrouter-providers Together --reuse oneshot   # pin the OpenRouter upstream provider (no fallbacks)
.venv/bin/python benchmarks/report.py              # regenerate RESULTS.md and results/<run>/RESULTS.md
```

Each planner configuration is a run named `planner-<model>` (override with `--run`). `run.py` keeps its own Semantic
Sheet store in `data/benchmark_store/` and stores raw operator outputs under `data/benchmark/raw_outputs/<run>/` and
one-shot answers under `data/benchmark/raw_outputs/<scenario>/` (all ignored by git); `results/<run>/*.json` and the
`RESULTS.md` files are committed. The planner provider is a server setting (`PLANNER_PROVIDER`, `PLANNER_MODEL`,
`PLANNER_REASONING`, `PLANNER_OPENROUTER_PROVIDERS`, `OPENROUTER_API_KEY`), so the app itself can run on GLM 5.3 Flash or
DeepSeek V4.1 Flash the same way.

Files: `scenarios.py` (data prep, gold, one-shot schemas, comparisons), `pipeline.py` (drives the planner, Jev job and
exports in-process), `oneshot.py` (Responses API call in background mode), `common.py` (prices, metrics), `report.py`.
