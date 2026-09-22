# Benchmark run `planner-glm-5.3-flash-run1`: operators with `z-ai/glm-5.3-flash` planner vs. one-shot gpt-6-astra

Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

Both arms receive the **same prompt** and the **same CSV** (label and leak columns removed, explicit `row_id`).

- **Operators (this repo):** `z-ai/glm-5.3-flash` (reasoning `high`, via openrouter) sees only the schema and ≤ 20 sample rows and writes a typed plan; `jev-1.13.0` answers the per-row semantic questions; DuckDB does the filtering, sorting, joins and arithmetic.
- **One-shot:** `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and returns the final answer as JSON (no tools, no code).

Prices used (list, 2026-09-21): gpt-6-astra $10.00 / M input, $1.00 / M cached input, $50.00 / M output (reasoning tokens bill as output; requests over 272,000 input tokens reprice to $20.00 / $75.00); z-ai/glm-5.3-flash via OpenRouter $0.15 / M input, $0.50 / M output; jev-1.13.0 $0.042 / M input, output free. Costs are computed from the token usage each API reported.

## Summary

| Scenario | Rows | Operators (Jev) | One-shot (Astra) | Agreement |
|---|---|---|---|---|
| **Customer support: cancel because unaffordable** | 3,000 | 2 rows; P 100.0% / R 22.2% / F1 36.4%; 6 more gold rows in review view | 9 rows; P 100.0% / R 100.0% / F1 100.0% | Jaccard 0.22 (2 shared) |
| **Banking queries: separate transfer problems** | 3,080 | pending F1 87.3%, failed F1 57.4% (lenient gold); macro-F1 46.4% strict / 72.3% lenient | pending F1 79.7%, failed F1 75.0% (lenient gold); macro-F1 61.5% strict / 77.3% lenient | κ 0.73, agreement 95.4%; wrong_account 5 shared |
| **Consumer complaints: filter then rank by urgency** | 5,000 | 10 rows ranked; 100.0% carry a topical keyword; 74 rows in review view (0 of the one-shot's picks among them) | 8 rows ranked; 100.0% carry a topical keyword | Jaccard 0.80; Spearman ρ -0.05 on 8 shared; top-20 overlap 8 |
| **Airbnb reviews: unreliable Wi-Fi, grouped by property** | 1,200 | 16 reviews, 16 properties; aggregate exact | 17 reviews, 17 properties; counts consistent 17/17, prices right 16/17 | reviews Jaccard 0.94; properties Jaccard 0.94 |
| **Retail: gift categories, revenue by category and country** | 300 + 4,758 | 8 categories; revenue cells exact 278/278 | 3 categories; revenue cells exact 53/83 (within 0.5%: 55), max error $46,502.80, 0 cells missing | ARI 0.11, NMI 0.25 over 267 products |
| **Product matching: offers to catalog** | 400 + 598 | 175 pairs; P 97.1% / R 42.5% / F1 59.1% | 357 pairs; P 97.5% / R 87.0% / F1 91.9% | Jaccard 0.46 (168 shared pairs) |

| Scenario | Operators cost | One-shot cost | One-shot ÷ operators | Operators latency | One-shot latency |
|---|---|---|---|---|---|
| Customer support: cancel because unaffordable | $0.0007 planner + $0.034 Jev = **$0.034** | **$0.510** | 14.8× | 7s plan + 13s Jev | 27s |
| Banking queries: separate transfer problems | $0.0009 planner + $0.044 Jev = **$0.045** | **$0.957** | 21.5× | 11s plan + 14s Jev | 221s |
| Consumer complaints: filter then rank by urgency | $0.0012 planner + $0.014 Jev = **$0.015** | **$2.13** | 140.7× | 10s plan + 5s Jev | 43s |
| Airbnb reviews: unreliable Wi-Fi, grouped by property | $0.0012 planner + $0.018 Jev = **$0.019** | **$1.35** | 69.4× | 10s plan + 4s Jev | 102s |
| Retail: gift categories, revenue by category and country | $0.0010 planner + $0.0055 Jev = **$0.0065** | **$5.07** | 778.9× | 12s plan + 1s Jev | 1538s |
| Product matching: offers to catalog | $0.0010 planner + $0.038 Jev = **$0.039** | **$1.53** | 39.1× | 5s plan + 9s Jev | 347s |
| **Total** | **$0.159** | **$11.55** | 72.6× | | |

## Customer support: cancel because unaffordable

**Prompt:** “Find customers trying to cancel an order because they cannot afford it.”

**Inputs:** `support_requests` 3,000 rows × 3 columns (~42,683 tokens)

> 9 rows satisfy the proxy gold definition among 3000 messages.

> Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 3,097 in / 520 out; Jev 800,747 in over 300 requests | 47,046 in / 792 out (reasoning 752) |
| Cost | $0.0007 planner + $0.034 Jev = **$0.034** | **$0.510** |
| Latency | 7s plan + 13s Jev | 27s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Customers cancelling orders because they cannot afford them* — Finds support messages where the customer wants to cancel an order and the reason is inability to afford it (implied affordability problems count). Uncertain rows are routed to a separate review view; you can tighten the threshold if too many appear.

- `annotate` semantic_annotate · **cancel_unaffordable** (boolean): Read the customer message (it may be in any language; judge its meaning, including typos). Does the customer want to cancel an order and say the reason is that they cannot afford it / have no money for it?
- `filter_cancels` filter `{"column": "cancel_unaffordable.value"}`
- `final` project

**Comparison**

```json
{
  "gold_definition": "intent == cancel_order AND text mentions affordability (regex); the Bitext intent labels were removed from both inputs",
  "gold_size": 9,
  "pipeline": {
    "selected": 2,
    "from_step": "final",
    "tp": 2,
    "fp": 0,
    "fn": 7,
    "precision": 1.0,
    "recall": 0.2222,
    "f1": 0.3636,
    "review_view": {
      "cancel_unaffordable": {
        "uncertain": 6
      }
    },
    "gold_rows_in_review_view": 6,
    "missed_gold_in_review_view": 6
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
    "both": 2,
    "only_a": 0,
    "only_b": 7,
    "jaccard": 0.2222
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "only_pipeline": [],
  "only_oneshot": [
    {
      "row_id": 22,
      "text": "I cannot afford order {{Order Number}}"
    },
    {
      "row_id": 262,
      "text": "i cant afford order {{Order Number}}"
    },
    {
      "row_id": 663,
      "text": "I can't pay for purchase {{Order Number}}"
    },
    {
      "row_id": 1009,
      "text": "I can no longer pay for purchase {{Order Number}}"
    },
    {
      "row_id": 2937,
      "text": "I can't afford order {{Order Number}}"
    },
    {
      "row_id": 2951,
      "text": "I can no longer afford purchase {{Order Number}}"
    },
    {
      "row_id": 2973,
      "text": "I can no longer pay for order {{Order Number}}"
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

> Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 3,027 in / 920 out; Jev 1,038,965 in over 308 requests | 46,419 in / 9,849 out (reasoning 8,994) |
| Cost | $0.0009 planner + $0.044 Jev = **$0.045** | **$0.957** |
| Latency | 11s plan + 14s Jev | 221s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Classify transfer messages as pending, failed, or wrong account* — Classifies each message into one of the three requested transfer situations plus an 'other' bucket (non-transfer messages), then splits the rows into three separate filtered views and produces a count summary. You can lower min_confidence (0.5) to route fewer rows to 'other', or rename the labels.

- `classify` semantic_annotate · **transfer_situation** (category) options=['pending_transfer', 'failed_transfer', 'wrong_account_transfer', 'other']: Read the customer message (it may be in any language; judge its meaning). Does it describe a money transfer (a payment sent between accounts, e.g. 'transfer', 'sent money to my account', 'moved funds'), and if so, which 
- `pending_view` filter `{"op": "eq", "args": [{"column": "transfer_situation.value"}, {"literal": "pending_transfer"}]}`
- `failed_view` filter `{"op": "eq", "args": [{"column": "transfer_situation.value"}, {"literal": "failed_transfer"}]}`
- `wrong_account_view` filter `{"op": "eq", "args": [{"column": "transfer_situation.value"}, {"literal": "wrong_account_transfer"}]}`
- `summary` aggregate group_by=['transfer_situation.value'] metrics=[('count', None)]

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
    "from_step": "classify",
    "question": "transfer_situation",
    "raw_labels": {
      "other": 2640,
      "pending_transfer": 186,
      "failed_transfer": 150,
      "wrong_account_transfer": 5
    },
    "label_mapping": {
      "other": "other",
      "pending_transfer": "pending",
      "failed_transfer": "failed",
      "wrong_account_transfer": "wrong_account"
    },
    "rows_labeled": 2981,
    "strict": {
      "macro_f1": 0.4639,
      "accuracy_on_rows_in_scope": 0.3037,
      "per_class": {
        "pending": {
          "predicted": 186,
          "gold": 40,
          "tp": 40,
          "fp": 146,
          "fn": 0,
          "precision": 0.2151,
          "recall": 1.0,
          "f1": 0.354
        },
        "failed": {
          "predicted": 150,
          "gold": 80,
          "tp": 66,
          "fp": 84,
          "fn": 14,
          "precision": 0.44,
          "recall": 0.825,
          "f1": 0.5739
        }
      }
    },
    "lenient": {
      "macro_f1": 0.7233,
      "accuracy_on_rows_in_scope": 0.6078,
      "per_class": {
        "pending": {
          "predicted": 186,
          "gold": 160,
          "tp": 151,
          "fp": 35,
          "fn": 9,
          "precision": 0.8118,
          "recall": 0.9437,
          "f1": 0.8728
        },
        "failed": {
          "predicted": 150,
          "gold": 80,
          "tp": 66,
          "fp": 84,
          "fn": 14,
          "precision": 0.44,
          "recall": 0.825,
          "f1": 0.5739
        }
      }
    },
    "false_positive_intents": {
      "pending": {
        "pending_top_up": 25,
        "cancel_transfer": 5,
        "topping_up_by_card": 2,
        "pending_card_payment": 1,
        "Refund_not_showing_up": 1,
        "failed_transfer": 1
      },
      "failed": {
        "beneficiary_not_allowed": 30,
        "top_up_reverted": 24,
        "reverted_card_payment?": 16,
        "top_up_failed": 9,
        "pending_top_up": 2,
        "apple_pay_or_google_pay": 2
      }
    },
    "wrong_account_rows": 5,
    "wrong_account_intents": {
      "cancel_transfer": 5
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
    "agreement": 0.9542,
    "kappa": 0.7257,
    "wrong_account": {
      "both": 5,
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
  "disagreements": [
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
    },
    {
      "row_id": 654,
      "text": "Can you look into my top up please.  I made it over three hours ago and yet it's still pending.",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 655,
      "text": "Can you please make my top up go through as soon as possible. I really need the money and it has been pending for an hour already.",
      "intent": "pending_top_up",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 656,
      "text": "What's going on with the top-up? All it says is pending, pending, pending! I use your service several times a week and have never had a prob",
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

> Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,287 in / 1,037 out; Jev 333,279 in over 72 requests | 206,753 in / 1,320 out (reasoning 1,276) |
| Cost | $0.0012 planner + $0.014 Jev = **$0.015** | **$2.13** |
| Latency | 10s plan + 5s Jev | 43s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Recurring/unauthorized charge complaints ranked by urgency* — Flags complaints about charges continuing after cancellation or unauthorized recurring charges, filters to those, and ranks them by urgency (score 0-4). Edit the urgency rubric or the filter threshold if you want a broader or narrower review set.

- `annotate` semantic_annotate · **recurring_charge** (boolean): Based on the complaint details (which may be in any language; judge the meaning), is this complaint about charges that continued after the consumer cancelled a service or subscription, or about unauthorized recurring cha
- `annotate` semantic_annotate · **urgency** (score) levels=5: Rate the urgency of handling this complaint, based on the complaint details (which may be in any language; judge the meaning). Consider financial harm to the consumer, whether the harmful charging is likely still ongoing
- `filter_recurring` filter `{"column": "recurring_charge.value"}`
- `rank_by_urgency` sort by [('urgency.score', 'desc')]

**Comparison**

```json
{
  "notes": [
    "The full 5,000-row demo sample (8 of the 15 columns) so the one-shot request stays under the 272K-token long-context tier; the 100,000-row file used in the walkthrough is ~6,783,860 tokens and cannot be sent to gpt-6-astra in one request at all.",
    "The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. There is no gold label; 169 rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows carrying one is reported only as a sanity check."
  ],
  "pipeline": {
    "selected": 10,
    "from_step": "rank_by_urgency",
    "keyword_rows_selected": 10,
    "keyword_share": 1.0,
    "review_view": {
      "recurring_charge": {
        "uncertain": 74
      },
      "urgency": {}
    },
    "oneshot_rows_in_review_view": 0,
    "review_view_rows_with_keyword": 74,
    "top_10": [
      {
        "row_id": 1951,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1863,
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
        "row_id": 951,
        "text": "Debt or credit management | Unauthorized withdrawals or charges | "
      },
      {
        "row_id": 1396,
        "text": "Debt or credit management | Unauthorized withdrawals or charges | "
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
    "only_a": 2,
    "only_b": 0,
    "jaccard": 0.8,
    "rank_spearman_on_common": {
      "n": 8,
      "rho": -0.0476
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
      "row_id": 951,
      "text": "Debt or credit management | Unauthorized withdrawals or charges | "
    },
    {
      "row_id": 1396,
      "text": "Debt or credit management | Unauthorized withdrawals or charges | "
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

> Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,970 in / 912 out; Jev 433,043 in over 120 requests | 108,201 in / 5,288 out (reasoning 4,358) |
| Cost | $0.0012 planner + $0.018 Jev = **$0.019** | **$1.35** |
| Latency | 10s plan + 4s Jev | 102s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Reviews reporting unreliable Wi-Fi grouped by property with price comparison* — Flags each review for complaints about unreliable Wi-Fi, keeps only flagged rows, converts the text nightly price to a number, and aggregates by listing to show review count and average nightly price per property. Adjust the true_min threshold to be stricter or looser about what counts as a Wi-Fi reliability complaint.

- `flag_wifi` semantic_annotate · **wifi_unreliable** (boolean): Does this guest review report problems with unreliable Wi-Fi? The text may be in any language; judge the meaning, not the language. Count it as true if the guest complains about Wi-Fi being slow, dropping, not working, w
- `wifi_rows` filter `{"column": "wifi_unreliable.value"}`
- `with_price` compute
- `by_listing` aggregate group_by=['listing_id', 'listing_name', 'neighbourhood', 'borough'] metrics=[('count', None), ('avg', 'nightly_price_num'), ('min', 'nightly_price_num'), ('max', 'nightly_price_num')]
- `sorted` sort by [('wifi_complaint_reviews', 'desc'), ('avg_nightly_price', 'desc')]

**Comparison**

```json
{
  "notes": [
    "Bounded to 1200 reviews (many languages) so the one-shot request fits comfortably in context: all 70 reviews in the 5,000-review sample that mention Wi-Fi / internet at all (positive or negative) plus 1130 random others, shuffled. No gold label; the keyword share is reported as a sanity check (a review can report bad Wi-Fi without using the word, and most Wi-Fi mentions are positive)."
  ],
  "pipeline": {
    "flagged_reviews": 16,
    "from_step": "wifi_rows",
    "keyword_share": 1.0,
    "review_view": {
      "wifi_unreliable": {
        "uncertain": 1
      }
    },
    "properties": 16,
    "aggregate_check": {
      "aggregate_step": "by_listing",
      "groups": 16,
      "grouped_by": [
        "listing_id",
        "listing_name",
        "neighbourhood",
        "borough"
      ],
      "count_metric": "wifi_complaint_reviews",
      "total_count_in_aggregate": 16,
      "flagged_reviews": 16
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
      "both": 16,
      "only_a": 0,
      "only_b": 1,
      "jaccard": 0.9412
    },
    "properties": {
      "both": 16,
      "only_a": 0,
      "only_b": 1,
      "jaccard": 0.9412
    }
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "only_pipeline": [],
  "only_oneshot": [
    {
      "row_id": 664,
      "text": "Jessica,<br/>We loved your home.  It was beautiful, comfortable, and nice touches all around.  I'm glad we were able to help you and your pa"
    }
  ]
}
```

</details>


## Retail: gift categories, revenue by category and country

**Prompt:** “Classify products into gift categories, then calculate revenue by category and country using the retail revenue by product and country table.”

**Inputs:** `retail_products` 300 rows × 7 columns (~4,488 tokens); `retail_revenue_by_product_and_country` 4,758 rows × 5 columns (~31,412 tokens)

> Bounded to the 300 highest-revenue products and their 4758 product x country revenue rows. Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; the revenue table is recomputed exactly from each approach's own labels to check the arithmetic.

> Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 3,891 in / 849 out; Jev 130,993 in over 30 requests | 75,049 in / 86,409 out (reasoning 80,285) |
| Cost | $0.0010 planner + $0.0055 Jev = **$0.0065** | **$5.07** |
| Latency | 12s plan + 1s Jev | 1538s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Gift category revenue by country* — Classifies each product's description into a gift category, joins the classified products to the per-country revenue table by stock_code, then sums revenue per category and country. You can edit the category labels or thresholds; rows with uncertain categories are kept in a separate review view and excluded from the totals.

- `classify_gifts` semantic_annotate · **gift_category** (category) options=['home_decor', 'kitchen_dining', 'bags_accessories', 'stationery_craft', 'toys_games', 'garden_outdoor', 'fashion_apparel', 'other']: Based on the product description (product descriptions are typically in English and may be short or abbreviated; judge by meaning), which gift category does this product best belong to? Pick the single best-fitting categ
- `join_country_revenue` join on [{'left': 'stock_code', 'right': 'stock_code'}] how=inner
- `revenue_by_category_country` aggregate group_by=['gift_category.value', 'right.country'] metrics=[('sum', 'right.revenue'), ('count_distinct', 'stock_code'), ('count', None)]

**Comparison**

```json
{
  "notes": [
    "Bounded to the 300 highest-revenue products and their 4758 product x country revenue rows. Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; the revenue table is recomputed exactly from each approach's own labels to check the arithmetic."
  ],
  "pipeline": {
    "from_step": "classify_gifts",
    "question": "gift_category",
    "products_labeled": 267,
    "categories": {
      "home_decor": 108,
      "kitchen_dining": 65,
      "bags_accessories": 36,
      "stationery_craft": 22,
      "toys_games": 17,
      "garden_outdoor": 8,
      "other": 8,
      "fashion_apparel": 3
    },
    "aggregate_step": "revenue_by_category_country",
    "revenue_check": {
      "cells_claimed": 315,
      "cells_expected": 278,
      "cells_with_expected_counterpart": 278,
      "cells_missing": 0,
      "cells_extra": 37,
      "exact_to_cent": 278,
      "within_0_5_percent": 278,
      "max_abs_error": 0.0,
      "total_revenue_claimed": 10328259.82,
      "total_revenue_expected": 9327839.94,
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
    "products_labeled_by_both": 267,
    "adjusted_rand_index": 0.1125,
    "nmi": 0.2478
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
      "pipeline": "kitchen_dining",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 2,
      "description": "WHITE HANGING HEART T-LIGHT HOLDER",
      "pipeline": "home_decor",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 3,
      "description": "JUMBO BAG RED WHITE SPOTTY ",
      "pipeline": "bags_accessories",
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
      "pipeline": "home_decor",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 6,
      "description": "ASSORTED COLOUR BIRD ORNAMENT",
      "pipeline": "home_decor",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 7,
      "description": "PAPER CHAIN KIT 50'S CHRISTMAS ",
      "pipeline": "stationery_craft",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 8,
      "description": "CHILLI LIGHTS",
      "pipeline": "home_decor",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 9,
      "description": "MEDIUM CERAMIC TOP STORAGE JAR",
      "pipeline": "kitchen_dining",
      "oneshot": "Home & lifestyle gifts"
    },
    {
      "row_id": 10,
      "description": "POPCORN HOLDER , SMALL ",
      "pipeline": "kitchen_dining",
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

> Earlier GLM 5.3 Flash planner run; Jev called through the TypeSafe API only (JEV_ROUTES=direct).

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,629 in / 552 out; Jev 910,920 in over 200 requests | 66,650 in / 17,349 out (reasoning 12,338) |
| Cost | $0.0010 planner + $0.038 Jev = **$0.039** | **$1.53** |
| Latency | 5s plan + 9s Jev | 347s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Match offers to catalog products* — For every offer row, candidate catalog rows are retrieved automatically (exact blocking plus lexical similarity) and Jev verifies whether the pair describes the exact same physical product, tolerating different titles, translations, abbreviations and extra/missing wording. Edit candidates_per_row, accept_min or reject_max to trade recall vs precision; uncertain pairs remain in a review view.

- `match_catalog` semantic_match on ['brand', 'title', 'description'] ↔ ['brand', 'title', 'description'], 5 candidates/row, accept ≥ 0.8: Do these two records describe the exact same product (same brand and same specific model/item), even if the titles are worded differently, translated, abbreviated, or include extra or missing details 

**Comparison**

```json
{
  "notes": [
    "400 offers, each with exactly one true match among 598 catalog entries (WDC gold standard); every other catalog entry is a different product. 396 catalog entries that were the offers' own identical records were removed first. The cluster_id and true_catalog_id columns were removed from both inputs."
  ],
  "gold_pairs": 400,
  "pipeline": {
    "pairs": 175,
    "status_counts": {
      "matched": 175,
      "uncertain": 86,
      "unmatched": 139
    },
    "tp": 170,
    "fp": 5,
    "fn": 230,
    "precision": 0.9714,
    "recall": 0.425,
    "f1": 0.5913,
    "diagnostics": {
      "candidates_per_row": 5,
      "accept_min": 0.8,
      "reject_max": 0.3,
      "gold_entry_among_candidates": 266,
      "candidate_recall": 0.665,
      "gold_candidate_accepted_by_jev": 171,
      "gold_candidate_left_uncertain": 69,
      "gold_candidate_rejected_by_jev": 26,
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
    "both": 168,
    "only_a": 7,
    "only_b": 189,
    "jaccard": 0.4615
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
      "offer_id": 28769706,
      "title": "AirPods 2 with Wireless Charging Case",
      "chosen": 60359264,
      "gold": 72310020
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
      "offer_id": 73554318,
      "title": "Cartus cerneala Epson 79XL magenta, singlepack DURABrite ultra ink, capacitate (",
      "chosen": 74965852,
      "gold": 58023892
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

