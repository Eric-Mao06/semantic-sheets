# Benchmark run `planner-deepseek-v4.1-flash-calibrated`: operators with `deepseek/deepseek-v4.1-flash` planner vs. one-shot gpt-6-astra

Both arms receive the **same prompt** and the **same CSV** (label and leak columns removed, explicit `row_id`).

- **Operators (this repo):** `deepseek/deepseek-v4.1-flash` (reasoning `high`, via openrouter pinned to Together) sees only the schema and ≤ 20 sample rows and writes a typed plan; `jev-1.13.0` answers the per-row semantic questions; DuckDB does the filtering, sorting, joins and arithmetic.
- **One-shot:** `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and returns the final answer as JSON (no tools, no code).

Prices used (list, 2026-09-21): gpt-6-astra $10.00 / M input, $1.00 / M cached input, $12.50 / M cache-write input, $50.00 / M output (reasoning tokens bill as output; requests over 272,000 input tokens reprice to $20.00 / $75.00); hosted code interpreter container $0.03 per 20-minute session (5-minute minimum); z-ai/glm-5.3-flash via OpenRouter $0.15 / M input, $0.50 / M output; deepseek/deepseek-v4.1-flash via OpenRouter (Together) $0.30 / M input, $1.20 / M output, using OpenRouter's reported cost when it returns one; jev-1.13.0 $0.042 / M input, output free. Costs are computed from the token usage each API reported.

## Summary

| Scenario | Rows | Operators (Jev) | One-shot (Astra) | Agreement |
|---|---|---|---|---|
| **Customer support: cancel because unaffordable** | 3,000 | 8 rows; P 100.0% / R 88.9% / F1 94.1% | 9 rows; P 100.0% / R 100.0% / F1 100.0% | Jaccard 0.89 (8 shared) |
| **Banking queries: separate transfer problems** | 3,080 | pending F1 76.6%, failed F1 55.3% (lenient gold); macro-F1 42.3% strict / 66.0% lenient | pending F1 79.7%, failed F1 75.0% (lenient gold); macro-F1 61.5% strict / 77.3% lenient | κ 0.66, agreement 93.8%; wrong_account 5 shared |
| **Consumer complaints: filter then rank by urgency** | 5,000 | 20 rows ranked; 90.0% carry a topical keyword; 0 rows withheld as uncertain, 148 answered rows flagged near the cut (0 of the one-shot's picks in the review view) | 8 rows ranked; 100.0% carry a topical keyword | Jaccard 0.40; Spearman ρ 0.07 on 8 shared; top-20 overlap 8 |
| **Airbnb reviews: unreliable Wi-Fi, grouped by property** | 1,200 | 17 reviews, 17 properties; aggregate exact | 17 reviews, 17 properties; counts consistent 17/17, prices right 16/17 | reviews Jaccard 1.00; properties Jaccard 1.00 |
| **Retail: gift categories, revenue by category and country** | 300 + 4,758 | 8 categories; revenue cells exact 288/288 | 3 categories; revenue cells exact 53/83 (within 0.5%: 55), max error $46,502.80, 0 cells missing | ARI 0.08, NMI 0.24 over 300 products |
| **Product matching: offers to catalog** | 400 + 598 | 235 pairs; P 94.0% / R 55.2% / F1 69.6% | 357 pairs; P 97.5% / R 87.0% / F1 91.9% | Jaccard 0.59 (219 shared pairs) |

| Scenario | Operators cost | One-shot cost | One-shot ÷ operators | Operators latency | One-shot latency |
|---|---|---|---|---|---|
| Customer support: cancel because unaffordable | $0.0011 planner + $0.033 Jev = **$0.034** | **$0.510** | 15.1× | 2s plan + 7s Jev | 27s |
| Banking queries: separate transfer problems | $0.0017 planner + $0.042 Jev = **$0.044** | **$0.957** | 21.8× | 4s plan + 9s Jev | 221s |
| Consumer complaints: filter then rank by urgency | $0.0022 planner + $0.012 Jev (scores re-served from cache, 96.8% hits; source run's Jev cost shown) = **$0.014** | **$2.13** | 149.6× | 5s plan + 5s Jev | 43s |
| Airbnb reviews: unreliable Wi-Fi, grouped by property | $0.0021 planner + $0.013 Jev = **$0.015** | **$1.35** | 87.9× | 4s plan + 3s Jev | 102s |
| Retail: gift categories, revenue by category and country | $0.0019 planner + $0.0047 Jev = **$0.0067** | **$5.07** | 761.2× | 3s plan + 1s Jev | 1538s |
| Product matching: offers to catalog | $0.0014 planner + $0.034 Jev = **$0.036** | **$1.53** | 43.0× | 2s plan + 4s Jev | 347s |
| **Total** | **$0.150** | **$11.55** | 77.1× | | |

## Customer support: cancel because unaffordable

**Prompt:** “Find customers trying to cancel an order because they cannot afford it.”

**Inputs:** `support_requests` 3,000 rows × 3 columns (~42,683 tokens)

> 9 rows satisfy the proxy gold definition among 3000 messages.

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 2,514 in / 685 out; Jev 780,104 in over 300 requests (172 direct, 128 openrouter) | 47,046 in / 792 out (reasoning 752) |
| Cost | $0.0011 planner + $0.033 Jev = **$0.034** | **$0.510** |
| Latency | 2s plan + 7s Jev | 27s |
| Status | job `succeeded` | `completed` |

**Plan reused from run `planner-deepseek-v4.1-flash`**: *Customers cancelling an order due to unaffordability* — Uses one semantic boolean judgement over the free-text instruction column to find customers who want to cancel an order because they cannot afford it (including phrasings where the stated reason implies cancellation). Rows judged uncertain are routed to a separate review set. Edit the thresholds if you want a stricter or looser match.

- `judge` semantic_annotate · **cancel_unaffordable** (boolean): Judge whether the customer's text expresses that they want or need to cancel an order because they cannot afford it (financial hardship, cost or price concerns). The text may be in any language; judge only its meaning.
- `filtered` filter `{"op": "eq", "args": [{"column": "cancel_unaffordable.value"}, {"literal": true}]}`
- `out` project

**Decision cuts** (planner's true_min / false_max vs. the cut the engine used after seeing the scores)

| Question | Planner | Used | Scored rows | Flag margin |
|---|---|---|---|---|
| `judge.cancel_unaffordable` | 0.7 / 0.3 | **0.245** (Otsu 0.245) | 3,000 | ±0.1 |

**Comparison**

```json
{
  "gold_definition": "intent == cancel_order AND text mentions affordability (regex); the Bitext intent labels were removed from both inputs",
  "gold_size": 9,
  "pipeline": {
    "selected": 8,
    "from_step": "out",
    "tp": 8,
    "fp": 0,
    "fn": 1,
    "precision": 1.0,
    "recall": 0.8889,
    "f1": 0.9412,
    "review_view": {
      "cancel_unaffordable": {}
    },
    "gold_rows_in_review_view": 0,
    "missed_gold_in_review_view": 0
  },
  "oneshot": {
    "selected": 9,
    "tp": 9,
    "fp": 0,
    "fn": 0,
    "precision": 1.0,
    "recall": 1.0,
    "f1": 1.0
  },
  "agreement": {
    "both": 8,
    "only_a": 0,
    "only_b": 1,
    "jaccard": 0.8889
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "only_pipeline": [],
  "only_oneshot": [
    {
      "row_id": 663,
      "text": "I can't pay for purchase {{Order Number}}"
    }
  ],
  "missed_by_both": []
}
```

</details>


## Banking queries: separate transfer problems

**Prompt:** “Separate pending transfers, failed transfers, and transfers sent to the wrong account.”

**Inputs:** `banking_queries` 3,080 rows × 2 columns (~46,300 tokens)

> Gold classes come from the BANKING77 intents (removed from both inputs). Strict: pending_transfer -> pending; failed_transfer and declined_transfer -> failed; everything else -> other. Lenient additionally counts transfer_timing, balance_not_updated_after_bank_transfer and transfer_not_received_by_recipient as pending (a transfer that has not arrived). BANKING77 has no intent meaning 'sent to the wrong account', so that class is compared between the two approaches only. Queries about pending top-ups, card payments or cash withdrawals are 'other' under both mappings; whether they are 'pending transfers' is a judgement call that the prompt leaves open, and it drives most of the false positives on both sides.

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 2,460 in / 1,206 out; Jev 1,005,787 in over 353 requests (196 direct, 157 openrouter) | 46,419 in / 9,849 out (reasoning 8,994) |
| Cost | $0.0017 planner + $0.042 Jev = **$0.044** | **$0.957** |
| Latency | 4s plan + 9s Jev | 221s |
| Status | job `succeeded` | `completed` |

**Plan reused from run `planner-deepseek-v4.1-flash`**: *Separate transfer issues by type* — Filters the support messages down to those about a transfer problem, then classifies each into pending transfer, failed transfer, transfer sent to the wrong account, or other, and groups the results with counts. Edit the category labels/descriptions or the boolean threshold if you want a different balance of recall vs precision.

- `annot_is_transfer` semantic_annotate · **is_transfer_issue** (boolean): Does this customer support message concern a money transfer between accounts that has gone wrong or is being questioned (e.g. a transfer that hasn't arrived, failed, was sent to the wrong account, was duplicated, or is t
- `filter_transfers` filter `{"op": "eq", "args": [{"column": "is_transfer_issue.value"}, {"literal": true}]}`
- `annot_transfer_type` semantic_annotate · **transfer_type** (category) options=['pending_transfer', 'failed_transfer', 'wrong_account_transfer', 'other']: Classify the transfer problem described in this customer support message by its main issue type. The message may be in any language; classify by meaning. Choose exactly one option.
- `group_transfer_types` aggregate group_by=['transfer_type.value'] metrics=[('count', None), ('avg', 'transfer_type.confidence')]
- `sort_transfer_types` sort by [('count', 'desc')]

**Decision cuts** (planner's true_min / false_max vs. the cut the engine used after seeing the scores)

| Question | Planner | Used | Scored rows | Flag margin |
|---|---|---|---|---|
| `annot_is_transfer.is_transfer_issue` | 0.7 / 0.3 | **0.43** (Otsu 0.43) | 3,080 | ±0.1 |

**Comparison**

```json
{
  "gold_notes": [
    "Gold classes come from the BANKING77 intents (removed from both inputs). Strict: pending_transfer -> pending; failed_transfer and declined_transfer -> failed; everything else -> other. Lenient additionally counts transfer_timing, balance_not_updated_after_bank_transfer and transfer_not_received_by_recipient as pending (a transfer that has not arrived). BANKING77 has no intent meaning 'sent to the wrong account', so that class is compared between the two approaches only. Queries about pending top-ups, card payments or cash withdrawals are 'other' under both mappings; whether they are 'pending transfers' is a judgement call that the prompt leaves open, and it drives most of the false positives on both sides."
  ],
  "gold_class_sizes": {
    "strict": {
      "other": 2960,
      "failed": 80,
      "pending": 40
    },
    "lenient": {
      "other": 2840,
      "pending": 160,
      "failed": 80
    }
  },
  "pipeline": {
    "from_step": "annot_transfer_type",
    "question": "transfer_type",
    "raw_labels": {
      "failed_transfer": 155,
      "pending_transfer": 234,
      "other": 48,
      "wrong_account_transfer": 6
    },
    "label_mapping": {
      "failed_transfer": "failed",
      "pending_transfer": "pending",
      "other": "other",
      "wrong_account_transfer": "wrong_account"
    },
    "rows_labeled": 443,
    "strict": {
      "macro_f1": 0.4226,
      "accuracy_on_rows_in_scope": 0.2605,
      "per_class": {
        "pending": {
          "predicted": 234,
          "gold": 40,
          "tp": 40,
          "fp": 194,
          "fn": 0,
          "precision": 0.1709,
          "recall": 1.0,
          "f1": 0.292
        },
        "failed": {
          "predicted": 155,
          "gold": 80,
          "tp": 65,
          "fp": 90,
          "fn": 15,
          "precision": 0.4194,
          "recall": 0.8125,
          "f1": 0.5532
        }
      }
    },
    "lenient": {
      "macro_f1": 0.6599,
      "accuracy_on_rows_in_scope": 0.5255,
      "per_class": {
        "pending": {
          "predicted": 234,
          "gold": 160,
          "tp": 151,
          "fp": 83,
          "fn": 9,
          "precision": 0.6453,
          "recall": 0.9437,
          "f1": 0.7665
        },
        "failed": {
          "predicted": 155,
          "gold": 80,
          "tp": 65,
          "fp": 90,
          "fn": 15,
          "precision": 0.4194,
          "recall": 0.8125,
          "f1": 0.5532
        }
      }
    },
    "false_positive_intents": {
      "pending": {
        "Refund_not_showing_up": 39,
        "pending_top_up": 33,
        "topping_up_by_card": 6,
        "cancel_transfer": 3,
        "pending_card_payment": 1,
        "failed_transfer": 1
      },
      "failed": {
        "beneficiary_not_allowed": 31,
        "top_up_reverted": 29,
        "top_up_failed": 9,
        "pending_top_up": 5,
        "apple_pay_or_google_pay": 5,
        "reverted_card_payment?": 4
      }
    },
    "wrong_account_rows": 6,
    "wrong_account_intents": {
      "cancel_transfer": 6
    }
  },
  "oneshot": {
    "assigned": {
      "pending": 106,
      "failed": 96,
      "wrong_account": 5
    },
    "strict": {
      "macro_f1": 0.6148,
      "accuracy_on_rows_in_scope": 0.457,
      "per_class": {
        "pending": {
          "predicted": 106,
          "gold": 40,
          "tp": 35,
          "fp": 71,
          "fn": 5,
          "precision": 0.3302,
          "recall": 0.875,
          "f1": 0.4795
        },
        "failed": {
          "predicted": 96,
          "gold": 80,
          "tp": 66,
          "fp": 30,
          "fn": 14,
          "precision": 0.6875,
          "recall": 0.825,
          "f1": 0.75
        }
      }
    },
    "lenient": {
      "macro_f1": 0.7735,
      "accuracy_on_rows_in_scope": 0.637,
      "per_class": {
        "pending": {
          "predicted": 106,
          "gold": 160,
          "tp": 106,
          "fp": 0,
          "fn": 54,
          "precision": 1.0,
          "recall": 0.6625,
          "f1": 0.797
        },
        "failed": {
          "predicted": 96,
          "gold": 80,
          "tp": 66,
          "fp": 30,
          "fn": 14,
          "precision": 0.6875,
          "recall": 0.825,
          "f1": 0.75
        }
      }
    },
    "false_positive_intents": {
      "pending": {},
      "failed": {
        "beneficiary_not_allowed": 30
      }
    },
    "wrong_account_rows": 5,
    "wrong_account_intents": {
      "cancel_transfer": 5
    }
  },
  "agreement": {
    "n": 3080,
    "agreement": 0.938,
    "kappa": 0.6605,
    "wrong_account": {
      "both": 5,
      "only_a": 1,
      "only_b": 0,
      "jaccard": 0.8333
    }
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "disagreements": [
    {
      "row_id": 247,
      "text": "There is an incoming payment into my account, but it is deactivated. Will they still be processed?",
      "intent": "fiat_currency_support",
      "pipeline": "failed",
      "oneshot": "other"
    },
    {
      "row_id": 642,
      "text": "My card was topped this morning but I can't see the funds. Why didn't it complete?",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 643,
      "text": "What would lead to my top up still pending?",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 644,
      "text": "there's a delay in my top-up",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 645,
      "text": "Is there a reason my top up hasn't gone through",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 646,
      "text": "The top-up is pending.",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 648,
      "text": "I've already topped up, but I cannot see the funds being available. What happened?",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 649,
      "text": "What is going on? My top-up is still pending. I use your system all the time but now it is just showing as pending.",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 650,
      "text": "I  do not see a top-up processed.",
      "intent": "pending_top_up",
      "pipeline": "failed",
      "oneshot": "other"
    },
    {
      "row_id": 651,
      "text": "I topped up but it didn't complete",
      "intent": "pending_top_up",
      "pipeline": "failed",
      "oneshot": "other"
    },
    {
      "row_id": 652,
      "text": "So, I am a new customer and attempted to top up for the very first time today. It's already been pending for half an hour and doesn't seem t",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 653,
      "text": "My top up is pending.",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    }
  ],
  "wrong_account_pipeline": [
    {
      "row_id": 684,
      "text": "I made a transaction but did it to the wrong account.",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 686,
      "text": "I need you to cancel a transfer that I made.  It is the wrong account number and the app won't let me stop the transaction.  Please stop it!",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 687,
      "text": "I need to cancel my recent transfer immediately, I made a mistake there, please help quickly before it goes through",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 692,
      "text": "I accidentally made a transaction to the wrong account.",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 695,
      "text": "This is URGENT, I typed the wrong payment information for a payment I needed to make and have clicked send, I need to reverse the transactio",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 706,
      "text": "I submitted a transaction to the incorrect account.",
      "intent": "cancel_transfer"
    }
  ],
  "wrong_account_oneshot": [
    {
      "row_id": 684,
      "text": "I made a transaction but did it to the wrong account.",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 686,
      "text": "I need you to cancel a transfer that I made.  It is the wrong account number and the app won't let me stop the transaction.  Please stop it!",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 692,
      "text": "I accidentally made a transaction to the wrong account.",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 695,
      "text": "This is URGENT, I typed the wrong payment information for a payment I needed to make and have clicked send, I need to reverse the transactio",
      "intent": "cancel_transfer"
    },
    {
      "row_id": 706,
      "text": "I submitted a transaction to the incorrect account.",
      "intent": "cancel_transfer"
    }
  ]
}
```

</details>


## Consumer complaints: filter then rank by urgency

**Prompt:** “Find complaints about charges continuing after cancellation or unauthorized recurring charges, then rank by urgency.”

**Inputs:** `consumer_complaints` 5,000 rows × 8 columns (~205,785 tokens)

> The full 5,000-row demo sample (8 of the 15 columns) so the one-shot request stays under the 272K-token long-context tier; the 100,000-row file used in the walkthrough is ~6,783,860 tokens and cannot be sent to gpt-6-astra in one request at all.

> The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. There is no gold label; 169 rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows carrying one is reported only as a sanity check.

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 3,768 in / 1,240 out; Jev 78,243 in over 29 requests (15 direct, 14 openrouter) | 206,753 in / 1,320 out (reasoning 1,276) |
| Cost | $0.0022 planner + $0.012 Jev (scores re-served from cache, 96.8% hits; source run's Jev cost shown) = **$0.014** | **$2.13** |
| Latency | 5s plan + 5s Jev | 43s |
| Status | job `succeeded` | `completed` |

**Plan reused from run `planner-deepseek-v4.1-flash`**: *Charges after cancellation / unauthorized recurring charges, ranked by urgency* — Semantically flags consumer complaints about charges that continued after cancellation or unauthorized recurring/subscription charges (judged on the Product, Sub-product, Issue and Sub-issue text, in any language), keeps the flagged rows and ranks them by an urgency score. Thresholds (0.7/0.3) and the urgency rubric can be tuned; uncertain rows are routed to a separate review set.

- `annotate` semantic_annotate · **charges_after_cancel** (boolean): Does this consumer complaint describe either (a) charges/fees that continued after the consumer cancelled, closed, or tried to cancel the service or account, or (b) recurring or subscription charges that were unauthorize
- `annotate` semantic_annotate · **urgency** (score) levels=5: Rate how urgent this complaint is for a reviewer, based on the ongoing financial harm and repetition described in the Product, Sub-product, Issue and Sub-issue text. Higher = more urgent.
- `filter_charges` filter `{"column": "charges_after_cancel.value"}`
- `rank` sort by [('urgency.score', 'desc')]

**Decision cuts** (planner's true_min / false_max vs. the cut the engine used after seeing the scores)

| Question | Planner | Used | Scored rows | Flag margin |
|---|---|---|---|---|
| `annotate.charges_after_cancel` | 0.7 / 0.3 | **0.2** (Otsu 0.125 → clamped) | 5,000 | ±0.1 |

**Comparison**

```json
{
  "notes": [
    "The full 5,000-row demo sample (8 of the 15 columns) so the one-shot request stays under the 272K-token long-context tier; the 100,000-row file used in the walkthrough is ~6,783,860 tokens and cannot be sent to gpt-6-astra in one request at all.",
    "The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. There is no gold label; 169 rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows carrying one is reported only as a sanity check."
  ],
  "pipeline": {
    "selected": 20,
    "from_step": "rank",
    "keyword_rows_selected": 18,
    "keyword_share": 0.9,
    "review_view": {
      "charges_after_cancel": {
        "flagged_near_cut": 148
      },
      "urgency": {}
    },
    "oneshot_rows_in_review_view": 0,
    "review_view_rows_with_keyword": 127,
    "top_10": [
      {
        "row_id": 1951,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1954,
        "text": "Payday loan, title loan, personal loan, or advance loan | Can't stop withdrawals from your bank account | "
      },
      {
        "row_id": 1863,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 2631,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 3093,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 4064,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 4254,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 4558,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 902,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Transaction was not authorized"
      },
      {
        "row_id": 2141,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Transaction was not authorized"
      }
    ]
  },
  "oneshot": {
    "selected": 8,
    "keyword_rows_selected": 8,
    "keyword_share": 1.0,
    "top_10": [
      {
        "row_id": 4558,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1951,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1954,
        "text": "Payday loan, title loan, personal loan, or advance loan | Can't stop withdrawals from your bank account | "
      },
      {
        "row_id": 2631,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 3093,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 4254,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 4064,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1863,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      }
    ]
  },
  "agreement": {
    "both": 8,
    "only_a": 12,
    "only_b": 0,
    "jaccard": 0.4,
    "rank_spearman_on_common": {
      "n": 8,
      "rho": 0.0714
    },
    "top_20_overlap": {
      "k": 20,
      "overlap": 8
    }
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "only_pipeline": [
    {
      "row_id": 175,
      "text": "Checking or savings account | Credit monitoring or identity theft protection services | Problem canceling credit monitoring or identify theft protection service"
    },
    {
      "row_id": 902,
      "text": "Checking or savings account | Problem with a lender or other company charging your account | Transaction was not authorized"
    },
    {
      "row_id": 951,
      "text": "Debt or credit management | Unauthorized withdrawals or charges | "
    },
    {
      "row_id": 985,
      "text": "Checking or savings account | Closing an account | Fees charged for closing account"
    },
    {
      "row_id": 1396,
      "text": "Debt or credit management | Unauthorized withdrawals or charges | "
    },
    {
      "row_id": 2003,
      "text": "Checking or savings account | Problem with a lender or other company charging your account | Transaction was not authorized"
    },
    {
      "row_id": 2141,
      "text": "Checking or savings account | Problem with a lender or other company charging your account | Transaction was not authorized"
    },
    {
      "row_id": 2225,
      "text": "Checking or savings account | Closing an account | Fees charged for closing account"
    }
  ],
  "only_oneshot": []
}
```

</details>


## Airbnb reviews: unreliable Wi-Fi, grouped by property

**Prompt:** “Find reviews reporting unreliable Wi-Fi, group by property, and compare nightly prices.”

**Inputs:** `airbnb_reviews` 1,200 rows × 8 columns (~111,272 tokens)

> Bounded to 1200 reviews (many languages) so the one-shot request fits comfortably in context: all 70 reviews in the 5,000-review sample that mention Wi-Fi / internet at all (positive or negative) plus 1130 random others, shuffled. No gold label; the keyword share is reported as a sanity check (a review can report bad Wi-Fi without using the word, and most Wi-Fi mentions are positive).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,468 in / 1,027 out; Jev 314,243 in over 120 requests (68 direct, 52 openrouter) | 108,201 in / 5,288 out (reasoning 4,358) |
| Cost | $0.0021 planner + $0.013 Jev = **$0.015** | **$1.35** |
| Latency | 4s plan + 3s Jev | 102s |
| Status | job `succeeded` | `completed` |

**Plan reused from run `planner-deepseek-v4.1-flash`**: *Unreliable Wi-Fi reviews by property with nightly price comparison* — Flags reviews whose comments report unreliable Wi-Fi, keeps only those reviews, converts the nightly_price text to a number, and groups by property (listing_id/listing_name) to show review counts and nightly price statistics (avg, min, max). Edit the semantic filter threshold or the reliability definition if needed.

- `price_num` compute
- `wifi_flag` semantic_annotate · **unreliable_wifi** (boolean): Does this guest review report that the Wi-Fi / internet connection at the property was unreliable, weak, slow, spotty, disconnecting, or otherwise problematic? The review text may be written in any language; judge the me
- `keep_wifi_issues` filter `{"op": "eq", "args": [{"column": "unreliable_wifi.value"}, {"literal": true}]}`
- `group_by_property` aggregate group_by=['listing_id', 'listing_name'] metrics=[('count', None), ('avg', 'price_num'), ('min', 'price_num'), ('max', 'price_num')]
- `sort_by_price` sort by [('avg_nightly_price', 'desc')]

**Decision cuts** (planner's true_min / false_max vs. the cut the engine used after seeing the scores)

| Question | Planner | Used | Scored rows | Flag margin |
|---|---|---|---|---|
| `wifi_flag.unreliable_wifi` | 0.7 / 0.3 | **0.505** (Otsu 0.505) | 1,200 | ±0.1 |

**Comparison**

```json
{
  "notes": [
    "Bounded to 1200 reviews (many languages) so the one-shot request fits comfortably in context: all 70 reviews in the 5,000-review sample that mention Wi-Fi / internet at all (positive or negative) plus 1130 random others, shuffled. No gold label; the keyword share is reported as a sanity check (a review can report bad Wi-Fi without using the word, and most Wi-Fi mentions are positive)."
  ],
  "pipeline": {
    "flagged_reviews": 17,
    "from_step": "keep_wifi_issues",
    "keyword_share": 1.0,
    "review_view": {
      "unreliable_wifi": {
        "flagged_near_cut": 2
      }
    },
    "properties": 17,
    "aggregate_check": {
      "aggregate_step": "group_by_property",
      "groups": 17,
      "grouped_by": [
        "listing_id",
        "listing_name"
      ],
      "count_metric": "review_count",
      "total_count_in_aggregate": 17,
      "flagged_reviews": 17
    },
    "top_properties": [
      {
        "listing_id": "1125394067603414786",
        "listing_name": "Lovely 2 bedroom 2 bath apartment",
        "review_count": 1,
        "nightly_price": 535.97
      },
      {
        "listing_id": "1173237288996322274",
        "listing_name": "Perfectly Situated Bay Ridge Studio",
        "review_count": 1,
        "nightly_price": 287.0
      },
      {
        "listing_id": "1568939422232770187",
        "listing_name": "Elegant Hotel Room in Historic Mansfield",
        "review_count": 1,
        "nightly_price": 332.8
      },
      {
        "listing_id": "21325259",
        "listing_name": "Private Room close to the city",
        "review_count": 1,
        "nightly_price": 88.54
      },
      {
        "listing_id": "255601",
        "listing_name": "Cozy Modern 2 Bedroom Apartment 20 min from City",
        "review_count": 1,
        "nightly_price": 318.33
      },
      {
        "listing_id": "26294354",
        "listing_name": "Organic Bohemian Pad in Brooklyn",
        "review_count": 1,
        "nightly_price": null
      },
      {
        "listing_id": "28200288",
        "listing_name": "K-Town Next to Times Sq12 /Dyson Airwrap Available",
        "review_count": 1,
        "nightly_price": 287.5
      },
      {
        "listing_id": "37122502",
        "listing_name": "Amazing Micro Unit W/ communal rooftop and kitchen",
        "review_count": 1,
        "nightly_price": 165.0
      }
    ]
  },
  "oneshot": {
    "flagged_reviews": 17,
    "keyword_share": 1.0,
    "properties_claimed": 17,
    "properties_implied_by_its_flags": 17,
    "claimed_properties_matching_its_flags": 17,
    "review_counts_consistent": 17,
    "nightly_prices_correct": 16,
    "top_properties": [
      {
        "listing_name": "Private Room close to the city",
        "nightly_price": 88.54,
        "listing_id": "21325259",
        "review_count": 1
      },
      {
        "listing_name": "Stylish Private Studio In NYC",
        "nightly_price": 134.17,
        "listing_id": "5663222",
        "review_count": 1
      },
      {
        "listing_name": "Charming Murray Hill Studio, NYC",
        "nightly_price": 135.19,
        "listing_id": "46413888",
        "review_count": 1
      },
      {
        "listing_name": "Amazing Micro Unit W/ communal rooftop and kitchen",
        "nightly_price": 165,
        "listing_id": "37122502",
        "review_count": 1
      },
      {
        "listing_name": "NYC║2 Bedroom 2 BA║ FREE Garage Parking",
        "nightly_price": 211.09,
        "listing_id": "41173094",
        "review_count": 1
      },
      {
        "listing_name": "Very Comfy, Airy and Spacious, TV In Every Room!",
        "nightly_price": 228.33,
        "listing_id": "48721300",
        "review_count": 1
      },
      {
        "listing_name": "KLO Room #6 With Private Bathroom",
        "nightly_price": 258,
        "listing_id": "7919372",
        "review_count": 1
      },
      {
        "listing_name": "Perfectly Situated Bay Ridge Studio",
        "nightly_price": 287,
        "listing_id": "1173237288996322274",
        "review_count": 1
      }
    ]
  },
  "agreement": {
    "reviews": {
      "both": 17,
      "only_a": 0,
      "only_b": 0,
      "jaccard": 1.0
    },
    "properties": {
      "both": 17,
      "only_a": 0,
      "only_b": 0,
      "jaccard": 1.0
    }
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "only_pipeline": [],
  "only_oneshot": []
}
```

</details>


## Retail: gift categories, revenue by category and country

**Prompt:** “Classify products into gift categories, then calculate revenue by category and country using the retail revenue by product and country table.”

**Inputs:** `retail_products` 300 rows × 7 columns (~4,488 tokens); `retail_revenue_by_product_and_country` 4,758 rows × 5 columns (~31,412 tokens)

> Bounded to the 300 highest-revenue products and their 4758 product x country revenue rows. Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; the revenue table is recomputed exactly from each approach's own labels to check the arithmetic.

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 3,531 in / 1,102 out; Jev 112,665 in over 30 requests (16 direct, 14 openrouter) | 75,049 in / 86,409 out (reasoning 80,285) |
| Cost | $0.0019 planner + $0.0047 Jev = **$0.0067** | **$5.07** |
| Latency | 3s plan + 1s Jev | 1538s |
| Status | job `succeeded` | `completed` |

**Plan reused from run `planner-deepseek-v4.1-flash`**: *Gift category revenue by category and country* — Classifies each product description into a gift category with a semantic category step, then joins the product rows to the retail_revenue_by_product_and_country table on stock_code and aggregates revenue by category and country. Assumptions: gift taxonomy is a fixed set of broad retail gift categories with an 'other' fallback; join is an exact stock_code match, so products missing from the revenue table drop out (inner join). A user may want to edit the category labels or switch to a left join to retain unmatched products.

- `classify` semantic_annotate · **gift_category** (category) options=['home_kitchen', 'decor_ornament', 'party_celebration', 'personal_accessory', 'stationery_craft', 'toys_games', 'seasonal_holiday', 'other']: Assign the product described by this retail product description to the single best-fitting gift category. The text may be in any language; judge its meaning. Base the choice only on what the product is (its name/type), n
- `keep_cols` project
- `join_revenue` join on [{'left': 'stock_code', 'right': 'stock_code'}] how=inner
- `rev_by_cat_country` aggregate group_by=['gift_category.value', 'right.country'] metrics=[('count', None), ('sum', 'right.revenue'), ('sum', 'right.quantity'), ('sum', 'right.transactions')]
- `sorted` sort by [('total_revenue', 'desc')]

**Comparison**

```json
{
  "notes": [
    "Bounded to the 300 highest-revenue products and their 4758 product x country revenue rows. Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; the revenue table is recomputed exactly from each approach's own labels to check the arithmetic."
  ],
  "pipeline": {
    "from_step": "classify",
    "question": "gift_category",
    "products_labeled": 300,
    "categories": {
      "home_kitchen": 98,
      "decor_ornament": 66,
      "personal_accessory": 45,
      "party_celebration": 37,
      "toys_games": 18,
      "stationery_craft": 15,
      "seasonal_holiday": 12,
      "other": 9
    },
    "aggregate_step": "rev_by_cat_country",
    "revenue_check": {
      "cells_claimed": 288,
      "cells_expected": 288,
      "cells_with_expected_counterpart": 288,
      "cells_missing": 0,
      "cells_extra": 0,
      "exact_to_cent": 288,
      "within_0_5_percent": 288,
      "max_abs_error": 0.0,
      "total_revenue_claimed": 10328259.82,
      "total_revenue_expected": 10328259.82,
      "worst_cells": []
    }
  },
  "oneshot": {
    "products_labeled": 300,
    "categories": {
      "Home & lifestyle gifts": 273,
      "Toys, crafts & stationery": 26,
      "Non-gift fees": 1
    },
    "revenue_check": {
      "cells_claimed": 83,
      "cells_expected": 83,
      "cells_with_expected_counterpart": 83,
      "cells_missing": 0,
      "cells_extra": 0,
      "exact_to_cent": 53,
      "within_0_5_percent": 55,
      "max_abs_error": 46502.8,
      "total_revenue_claimed": 10248996.53,
      "total_revenue_expected": 10328259.82,
      "worst_cells": [
        {
          "category": "Home & lifestyle gifts",
          "country": "United Kingdom",
          "claimed": 8261674.71,
          "expected": 8308177.51
        },
        {
          "category": "Home & lifestyle gifts",
          "country": "EIRE",
          "claimed": 248087.24,
          "expected": 256510.87
        },
        {
          "category": "Home & lifestyle gifts",
          "country": "France",
          "claimed": 140824.85,
          "expected": 144971.84
        },
        {
          "category": "Home & lifestyle gifts",
          "country": "Germany",
          "claimed": 157950.09,
          "expected": 161944.41
        },
        {
          "category": "Home & lifestyle gifts",
          "country": "Sweden",
          "claimed": 25534.82,
          "expected": 28515.46
        },
        {
          "category": "Home & lifestyle gifts",
          "country": "Belgium",
          "claimed": 24025.03,
          "expected": 26910.97
        }
      ]
    }
  },
  "agreement": {
    "products_labeled_by_both": 300,
    "adjusted_rand_index": 0.0778,
    "nmi": 0.2429
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "label_pairs": [
    {
      "row_id": 1,
      "description": "REGENCY CAKESTAND 3 TIER",
      "pipeline": "party_celebration",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 2,
      "description": "WHITE HANGING HEART T-LIGHT HOLDER",
      "pipeline": "decor_ornament",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 3,
      "description": "JUMBO BAG RED WHITE SPOTTY ",
      "pipeline": "party_celebration",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 4,
      "description": "PAPER CRAFT , LITTLE BIRDIE",
      "pipeline": "stationery_craft",
      "oneshot": "Toys, crafts & stationery"
    },
    {
      "row_id": 5,
      "description": "PARTY BUNTING",
      "pipeline": "party_celebration",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 6,
      "description": "ASSORTED COLOUR BIRD ORNAMENT",
      "pipeline": "decor_ornament",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 7,
      "description": "PAPER CHAIN KIT 50'S CHRISTMAS ",
      "pipeline": "seasonal_holiday",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 8,
      "description": "CHILLI LIGHTS",
      "pipeline": "seasonal_holiday",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 9,
      "description": "MEDIUM CERAMIC TOP STORAGE JAR",
      "pipeline": "home_kitchen",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 10,
      "description": "POPCORN HOLDER , SMALL ",
      "pipeline": "party_celebration",
      "oneshot": "Home & lifestyle gifts"
    }
  ]
}
```

</details>


## Product matching: offers to catalog

**Prompt:** “Match offers to the catalog for the same exact product despite different titles.”

**Inputs:** `product_offers` 400 rows × 7 columns (~21,355 tokens); `product_catalog` 598 rows × 6 columns (~31,847 tokens)

> 400 offers, each with exactly one true match among 598 catalog entries (WDC gold standard); every other catalog entry is a different product. 396 catalog entries that were the offers' own identical records were removed first. The cluster_id and true_catalog_id columns were removed from both inputs.

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,365 in / 481 out; Jev 814,920 in over 200 requests (103 direct, 97 openrouter) | 66,650 in / 17,349 out (reasoning 12,338) |
| Cost | $0.0014 planner + $0.034 Jev = **$0.036** | **$1.53** |
| Latency | 2s plan + 4s Jev | 347s |
| Status | job `succeeded` | `completed` |

**Plan reused from run `planner-deepseek-v4.1-flash`**: *Match offers to catalog products* — Semantically matches each offer row to the closest records in the product_catalog dataset to find entries describing the same exact product, even when the titles differ in wording, language or formatting. Candidate pairs are retrieved automatically and verified by Jev; a pair is accepted when the match score is at least 0.8, and pairs scoring 0.3 or below are rejected. Users may want to tune accept_min/reject_max or the number of candidates per row.

- `match` semantic_match on ['brand', 'title', 'description'] ↔ ['brand', 'title', 'description'], 5 candidates/row, accept ≥ 0.8: Do the offer record and the catalog record describe the same exact product (same brand and same specific model/article number or equivalent identifying details)? The titles may be written in different

**Decision cuts** (planner's true_min / false_max vs. the cut the engine used after seeing the scores)

| Question | Planner | Used | Scored rows | Flag margin |
|---|---|---|---|---|
| `match.match` | 0.8 / 0.3 | **0.48** (Otsu 0.48) | 400 | ±0.1 |

**Comparison**

```json
{
  "notes": [
    "400 offers, each with exactly one true match among 598 catalog entries (WDC gold standard); every other catalog entry is a different product. 396 catalog entries that were the offers' own identical records were removed first. The cluster_id and true_catalog_id columns were removed from both inputs."
  ],
  "gold_pairs": 400,
  "pipeline": {
    "pairs": 235,
    "status_counts": {
      "matched": 235,
      "unmatched": 165
    },
    "tp": 221,
    "fp": 14,
    "fn": 179,
    "precision": 0.9404,
    "recall": 0.5525,
    "f1": 0.6961,
    "diagnostics": {
      "candidates_per_row": 5,
      "accept_min": 0.8,
      "reject_max": 0.3,
      "calibration": {
        "mode": "auto",
        "cut": 0.48,
        "flag_margin": 0.1,
        "n_scored": 400,
        "method": "otsu-v1",
        "raw_cut": 0.48,
        "clamped": false,
        "planner_true_min": 0.8,
        "planner_false_max": 0.3
      },
      "accept_cut_used": 0.48,
      "flagged_near_cut_pairs": 17,
      "flagged_pairs_correct": 15,
      "flagged_near_cut_rows": 27,
      "gold_entry_among_candidates": 266,
      "candidate_recall": 0.665,
      "gold_candidate_accepted_by_jev": 225,
      "gold_candidate_left_uncertain": 0,
      "gold_candidate_rejected_by_jev": 41,
      "note": "recall lost before Jev = offers whose true entry was not among the lexical candidates; recall lost at Jev = true entry present but scored below accept_min"
    }
  },
  "oneshot": {
    "pairs": 357,
    "tp": 348,
    "fp": 9,
    "fn": 52,
    "precision": 0.9748,
    "recall": 0.87,
    "f1": 0.9194
  },
  "agreement": {
    "both": 219,
    "only_a": 16,
    "only_b": 138,
    "jaccard": 0.5871
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "pipeline_wrong": [
    {
      "offer_id": 5578260,
      "title": "Jabra Biz 2300 USB Duo Headset",
      "chosen": 76663119,
      "gold": 90972374
    },
    {
      "offer_id": 10096449,
      "title": "21 oz Standard Mouth Water Bottle",
      "chosen": 10899164,
      "gold": 28179113
    },
    {
      "offer_id": 16111692,
      "title": "APC Smart-UPS 1000VA LCD 230V SMT1000I - SMT1000I",
      "chosen": 67647713,
      "gold": 25607937
    },
    {
      "offer_id": 16801792,
      "title": "Epson S042091 A2 Photo Paper Genuine",
      "chosen": 58269311,
      "gold": 79883238
    },
    {
      "offer_id": 28769706,
      "title": "AirPods 2 with Wireless Charging Case",
      "chosen": 60359264,
      "gold": 72310020
    },
    {
      "offer_id": 32868569,
      "title": "Samsung T7 Touch 2TB V-NAND USB 3.2 Gen 2 External SSD",
      "chosen": 72004049,
      "gold": 63587638
    },
    {
      "offer_id": 41616522,
      "title": "TP-Link EAP225-Outdoor Wireless-AC1200 MU-MIMO Gigabit Indoor/Outdoor Access Poi",
      "chosen": 31785582,
      "gold": 36061219
    },
    {
      "offer_id": 43630137,
      "title": "Jabra BIZ 2400 II Mono 3-1 - Mic. 82 NC, Wideband",
      "chosen": 51760904,
      "gold": 19751845
    }
  ],
  "oneshot_wrong": [
    {
      "offer_id": 10096449,
      "title": "21 oz Standard Mouth Water Bottle",
      "chosen": 10899164,
      "gold": 28179113
    },
    {
      "offer_id": 14287942,
      "title": "Planet Ocean 600M Omega Co-Axial Chronograph 45.5mm 232.30.46.51.01.002",
      "chosen": 25758152,
      "gold": 25758185
    },
    {
      "offer_id": 29909599,
      "title": "Zebra Black Printer Ribbon for P310F, P310C, P420C, P520C & P720C ID Card Printe",
      "chosen": 90938642,
      "gold": 50368196
    },
    {
      "offer_id": 41616522,
      "title": "TP-Link EAP225-Outdoor Wireless-AC1200 MU-MIMO Gigabit Indoor/Outdoor Access Poi",
      "chosen": 31785582,
      "gold": 36061219
    },
    {
      "offer_id": 43630137,
      "title": "Jabra BIZ 2400 II Mono 3-1 - Mic. 82 NC, Wideband",
      "chosen": 51760904,
      "gold": 19751845
    },
    {
      "offer_id": 51855709,
      "title": "Western Digital Dysk MY PASSPORT 1TB 2,5 black WDBYVG0010BBK-WESN",
      "chosen": 34298415,
      "gold": 50944297
    },
    {
      "offer_id": 54954273,
      "title": "Fujifilm Instax Square Film for SQ10 Hybrid Cameras 20 Shot Pack",
      "chosen": 68374441,
      "gold": 54697856
    },
    {
      "offer_id": 74610695,
      "title": "Apple APPLE AIRPODS (2ND GEN) WIRELESS HEADPHONES WITH WIRELESS CHARGING CASE",
      "chosen": 72310020,
      "gold": 81257793
    }
  ]
}
```

</details>

