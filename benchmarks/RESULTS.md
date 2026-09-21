# Benchmark: Jev spreadsheet operators vs. one-shot gpt-6-astra

Six walkthrough scenarios, each run two ways with the **same prompt and the same CSV**: the operators (planner writes a typed plan → `jev-1.13.0` answers per row → DuckDB does the exact work) and a single `gpt-6-astra` (reasoning `high`) request holding the whole CSV. The operators were run once per planner model; the one-shot answers are shared across runs.

Prices used (list, 2026-09-21): gpt-6-astra $10.00 / M input, $1.00 / M cached input, $50.00 / M output (reasoning tokens bill as output; requests over 272,000 input tokens reprice to $20.00 / $75.00); z-ai/glm-5.3-flash via OpenRouter $0.15 / M input, $0.50 / M output; jev-1.13.0 $0.042 / M input, output free. Costs are computed from the token usage each API reported.

| Run | Planner | Provider | Details |
|---|---|---|---|
| `planner-gpt-6-astra` | `gpt-6-astra` (reasoning `high`) | openai | [results/planner-gpt-6-astra/RESULTS.md](results/planner-gpt-6-astra/RESULTS.md) |
| `planner-glm-5.3-flash` | `z-ai/glm-5.3-flash` (reasoning `high`) | openrouter | [results/planner-glm-5.3-flash/RESULTS.md](results/planner-glm-5.3-flash/RESULTS.md) |

## Quality

| Scenario | One-shot (gpt-6-astra) | Operators, `gpt-6-astra` planner | Operators, `z-ai/glm-5.3-flash` planner |
|---|---|---|---|
| **Customer support: cancel because unaffordable** | 9 rows; P 100.0% / R 100.0% / F1 100.0% | 7 rows; P 100.0% / R 77.8% / F1 87.5% | 2 rows; P 100.0% / R 22.2% / F1 36.4%; 6 more gold rows in review view |
| **Banking queries: separate transfer problems** | pending F1 79.7%, failed F1 75.0% (lenient gold); macro-F1 61.5% strict / 77.3% lenient | pending F1 69.1%, failed F1 58.1% (lenient gold); macro-F1 44.1% strict / 63.6% lenient | pending F1 87.3%, failed F1 57.4% (lenient gold); macro-F1 46.4% strict / 72.3% lenient |
| **Consumer complaints: filter then rank by urgency** | 8 rows ranked; 100.0% carry a topical keyword | 0 rows ranked; 10 rows in review view (8 of the one-shot's picks among them) | 10 rows ranked; 100.0% carry a topical keyword; 74 rows in review view (0 of the one-shot's picks among them) |
| **Airbnb reviews: unreliable Wi-Fi, grouped by property** | 17 reviews, 17 properties; counts consistent 17/17, prices right 16/17 | 16 reviews, 16 properties; aggregate exact | 16 reviews, 16 properties; aggregate exact |
| **Retail: gift categories, revenue by category and country** | 3 categories; revenue cells exact 53/83 (within 0.5%: 55), max error $46,502.80, 0 cells missing | 11 categories; revenue cells exact 376/376 | 8 categories; revenue cells exact 278/278 |
| **Product matching: offers to catalog** | 357 pairs; P 97.5% / R 87.0% / F1 91.9% | 171 pairs; P 97.1% / R 41.5% / F1 58.1% | 175 pairs; P 97.1% / R 42.5% / F1 59.1% |

## Cost and latency

| Scenario | One-shot cost / latency | Operators cost / latency, `gpt-6-astra` planner | Operators cost / latency, `z-ai/glm-5.3-flash` planner |
|---|---|---|---|
| Customer support: cancel because unaffordable | **$0.510** · 27s | $0.052 planner + $0.033 Jev = **$0.086** · 15s + 13s | $0.0007 planner + $0.034 Jev = **$0.034** · 7s + 13s |
| Banking queries: separate transfer problems | **$0.957** · 221s | $0.076 planner + $0.050 Jev = **$0.126** · 27s + 14s | $0.0009 planner + $0.044 Jev = **$0.045** · 11s + 14s |
| Consumer complaints: filter then rank by urgency | **$2.13** · 43s | $0.111 planner + $0.0086 Jev = **$0.120** · 41s + 8s | $0.0012 planner + $0.014 Jev = **$0.015** · 10s + 5s |
| Airbnb reviews: unreliable Wi-Fi, grouped by property | **$1.35** · 102s | $0.111 planner + $0.016 Jev = **$0.127** · 34s + 4s | $0.0012 planner + $0.018 Jev = **$0.019** · 10s + 4s |
| Retail: gift categories, revenue by category and country | **$5.07** · 1538s | $0.140 planner + $0.0078 Jev = **$0.147** · 50s + 1s | $0.0010 planner + $0.0055 Jev = **$0.0065** · 12s + 1s |
| Product matching: offers to catalog | **$1.53** · 347s | $0.076 planner + $0.038 Jev = **$0.115** · 18s + 9s | $0.0010 planner + $0.038 Jev = **$0.039** · 5s + 9s |
| **Total** | **$11.55** | **$0.721** (16.0× cheaper than one-shot) | **$0.159** (72.6× cheaper than one-shot) |

## Agreement between the two arms

| Scenario | `gpt-6-astra` planner | `z-ai/glm-5.3-flash` planner |
|---|---|---|
| Customer support: cancel because unaffordable | Jaccard 0.78 (7 shared) | Jaccard 0.22 (2 shared) |
| Banking queries: separate transfer problems | κ 0.68, agreement 94.5%; wrong_account 5 shared | κ 0.73, agreement 95.4%; wrong_account 5 shared |
| Consumer complaints: filter then rank by urgency | Jaccard 0.00; Spearman ρ – on 0 shared; top-20 overlap 0 | Jaccard 0.80; Spearman ρ -0.05 on 8 shared; top-20 overlap 8 |
| Airbnb reviews: unreliable Wi-Fi, grouped by property | reviews Jaccard 0.94; properties Jaccard 0.94 | reviews Jaccard 0.94; properties Jaccard 0.94 |
| Retail: gift categories, revenue by category and country | ARI 0.06, NMI 0.24 over 300 products | ARI 0.11, NMI 0.25 over 267 products |
| Product matching: offers to catalog | Jaccard 0.45 (163 shared pairs) | Jaccard 0.46 (168 shared pairs) |

Per-run reports (every plan, every metric, examples of disagreements): [planner-gpt-6-astra](results/planner-gpt-6-astra/RESULTS.md), [planner-glm-5.3-flash](results/planner-glm-5.3-flash/RESULTS.md).
