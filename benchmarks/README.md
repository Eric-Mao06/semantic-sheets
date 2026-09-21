# Benchmark: Jev spreadsheet operators vs. one-shot gpt-6-astra

For each of the six operations exercised in the MVP walkthrough, this benchmark gives the **same prompt** and the
**same CSV** to two approaches and compares their final outputs and cost:

| Arm | What happens |
|---|---|
| **Operators** (this repo) | `gpt-6-astra` (reasoning `high`) sees only the schema and ≤ 20 sample rows and writes a typed plan. `jev-1.13.0` answers every per-row semantic question; DuckDB performs the filtering, sorting, joins and arithmetic. Uncertain judgements go to a review view instead of the output. |
| **One-shot** | `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and must return the final answer as JSON that satisfies a strict schema. No tools, no code execution. |

Full numbers, every plan the planner wrote, and examples of disagreements: **[RESULTS.md](RESULTS.md)** (generated) and
`results/*.json` (raw). Prices are OpenAI and TypeSafe list prices as of 2026-09-21 and costs are computed from
the token usage each API reported.

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

## Results (single run per scenario, 2026-09-21)

| Scenario | Operators (Jev) | One-shot (Astra) | Operators cost | One-shot cost |
|---|---|---|---|---|
| Support: cancel because unaffordable | 7 rows, P 100% / R 78% | 9 rows, P 100% / R 100% | $0.086 | $0.51 (6×) |
| Banking: transfer problems (lenient gold) | pending F1 69%, failed F1 58% | pending F1 80%, failed F1 75% | $0.126 | $0.96 (8×) |
| Complaints: filter + rank | 0 rows accepted, **10 in review view** (incl. all 8 the one-shot picked) | 8 rows ranked | $0.120 | $2.13 (18×) |
| Airbnb: unreliable Wi-Fi by property | 16 reviews / 16 properties, aggregate exact | 17 reviews / 17 properties, 16 of 17 prices right | $0.127 | $1.35 (11×) |
| Retail: categories + revenue by country | 11 categories, **376 / 376 revenue cells exact** | 3 categories, 53 / 83 cells exact, worst cell off by $46,503 | $0.147 | $5.07 (34×), 26 min |
| Product matching | 171 pairs, P 97% / R 42% (+75 gold pairs in review) | 357 pairs, P 97% / R 87% | $0.115 | $1.53 (13×) |
| **Total** | | | **$0.72** | **$11.55 (16×)** |

### What the numbers say

- **Cost.** The operators cost 6–34× less per scenario (16× overall). Jev itself is $0.008–$0.05 per scenario ($0.15 for all six); 79% of the
  operators' cost is the single planner call. Jev cost grows linearly with rows (the 5,000-row CFPB job used 204K Jev
  tokens for $0.009), so the 100K-row walkthrough would be about $0.17 of Jev, while the one-shot cannot run at that
  size at any price.
- **Latency.** Operators finish in 20–50 s, almost all of it the planner; the Jev stage takes 1–14 s for 300–5,000 rows.
  The one-shot took 27 s to 26 min (the retail aggregation).
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
- **Planner variance.** The plan is a fresh generation each run. Two CFPB runs produced different boolean questions
  (`recurring_charge_match` on 2,000 rows accepted 3 rows; `ongoing_charges` on 5,000 rows accepted 0 and reviewed 10).
  Question wording and thresholds are the biggest quality lever in the operator arm, and they are editable before anything runs.
- **Ambiguity is shared.** Most banking "errors" on both sides are queries about pending top-ups, card payments or
  withdrawals that the prompt's "pending transfers" may or may not cover; both arms made the same call on 94.5% of rows (κ 0.68).

### Two defects found and fixed while building the benchmark

1. The planner prompt never described the `semantic_match` step, so the planner could not express matching and produced
   invalid join + annotate plans for the WDC scenario (`server/semsheet/planner.py`).
2. `scripts/prepare_samples.py` built the WDC catalog from the shared WDC id space, so 396 of 400 offers had their own
   identical record in the catalog; matching degenerated into finding the identical row. Those records are now excluded.

### Caveats

- One run per scenario; no repeats, so small differences (16 vs 17 reviews) are within noise.
- Ground truth is a proxy on four of six scenarios (see the notes in each result), and the CFPB export has no
  narratives, which makes that scenario thin for both arms.
- The one-shot arm is a capability probe with a strict JSON schema and an instruction to read every row; it is not how
  anyone would ship the task, and the same model plays the planner in the other arm.

## Running it

```bash
python scripts/prepare_samples.py                  # needs the raw downloads in data/raw (see README)
export OPENAI_API_KEY=... TYPESAFE_API_KEY=...
.venv/bin/python benchmarks/run.py                 # both arms, all scenarios (~$12 of gpt-6-astra, ~$0.15 of Jev)
.venv/bin/python benchmarks/run.py -s wdc_product_matching --arms pipeline
.venv/bin/python benchmarks/run.py --reuse oneshot # rerun operators + comparison, reuse stored one-shot answers
.venv/bin/python benchmarks/report.py              # regenerate RESULTS.md from results/*.json
```

`run.py` keeps its own Semantic Sheet store in `data/benchmark_store/` and stores raw arm outputs under
`data/benchmark/raw_outputs/` (both ignored by git); `results/*.json` and `RESULTS.md` are committed.

Files: `scenarios.py` (data prep, gold, one-shot schemas, comparisons), `pipeline.py` (drives the planner, Jev job and
exports in-process), `oneshot.py` (Responses API call in background mode), `common.py` (prices, metrics), `report.py`.
