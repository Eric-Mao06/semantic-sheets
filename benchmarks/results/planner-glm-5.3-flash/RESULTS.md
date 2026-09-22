# Benchmark run `planner-glm-5.3-flash`: operators with `z-ai/glm-5.3-flash` planner vs. one-shot gpt-6-astra

Both arms receive the **same prompt** and the **same CSV** (label and leak columns removed, explicit `row_id`).

- **Operators (this repo):** `z-ai/glm-5.3-flash` (reasoning `high`, via openrouter) sees only the schema and ≤ 20 sample rows and writes a typed plan; `jev-1.13.0` answers the per-row semantic questions; DuckDB does the filtering, sorting, joins and arithmetic.
- **One-shot:** `gpt-6-astra` (reasoning `high`) receives the whole CSV plus the prompt in a single Responses API request and returns the final answer as JSON (no tools, no code).

Prices used (list, 2026-09-21): gpt-6-astra $10.00 / M input, $1.00 / M cached input, $50.00 / M output (reasoning tokens bill as output; requests over 272,000 input tokens reprice to $20.00 / $75.00); z-ai/glm-5.3-flash via OpenRouter $0.15 / M input, $0.50 / M output; jev-1.13.0 $0.042 / M input, output free. Costs are computed from the token usage each API reported.

## Summary

| Scenario | Rows | Operators (Jev) | One-shot (Astra) | Agreement |
|---|---|---|---|---|
| **Customer support: cancel because unaffordable** | 3,000 | 3 rows; P 100.0% / R 33.3% / F1 50.0%; 4 more gold rows in review view | 9 rows; P 100.0% / R 100.0% / F1 100.0% | Jaccard 0.33 (3 shared) |
| **Banking queries: separate transfer problems** | 3,080 | pending F1 58.0%, failed F1 39.2% (lenient gold); macro-F1 33.3% strict / 48.6% lenient | pending F1 79.7%, failed F1 75.0% (lenient gold); macro-F1 61.5% strict / 77.3% lenient | κ 0.51, agreement 89.7%; wrong_account 5 shared |
| **Consumer complaints: filter then rank by urgency** | 5,000 | 170 rows ranked; 78.2% carry a topical keyword; 106 rows in review view (0 of the one-shot's picks among them) | 8 rows ranked; 100.0% carry a topical keyword | Jaccard 0.05; Spearman ρ 0.29 on 8 shared; top-20 overlap 8 |
| **Airbnb reviews: unreliable Wi-Fi, grouped by property** | 1,200 | 16 reviews, 16 properties; aggregate exact | 17 reviews, 17 properties; counts consistent 17/17, prices right 16/17 | reviews Jaccard 0.94; properties Jaccard 0.94 |
| **Retail: gift categories, revenue by category and country** | 300 + 4,758 | 7 categories; revenue cells exact 267/267 | 3 categories; revenue cells exact 53/83 (within 0.5%: 55), max error $46,502.80, 0 cells missing | ARI 0.07, NMI 0.21 over 300 products |
| **Product matching: offers to catalog** | 400 + 598 | 151 pairs; P 98.0% / R 37.0% / F1 53.7% | 357 pairs; P 97.5% / R 87.0% / F1 91.9% | Jaccard 0.41 (148 shared pairs) |

| Scenario | Operators cost | One-shot cost | One-shot ÷ operators | Operators latency | One-shot latency |
|---|---|---|---|---|---|
| Customer support: cancel because unaffordable | $0.0007 planner + $0.034 Jev = **$0.035** | **$0.510** | 14.7× | 7s plan + 8s Jev | 27s |
| Banking queries: separate transfer problems | $0.0007 planner + $0.055 Jev = **$0.056** | **$0.957** | 17.1× | 14s plan + 9s Jev | 221s |
| Consumer complaints: filter then rank by urgency | $0.0010 planner + $0.010 Jev = **$0.011** | **$2.13** | 193.0× | 27s plan + 7s Jev | 43s |
| Airbnb reviews: unreliable Wi-Fi, grouped by property | $0.0010 planner + $0.015 Jev = **$0.016** | **$1.35** | 83.6× | 15s plan + 4s Jev | 102s |
| Retail: gift categories, revenue by category and country | $0.0009 planner + $0.0049 Jev = **$0.0058** | **$5.07** | 869.8× | 28s plan + 1s Jev | 1538s |
| Product matching: offers to catalog | $0.0009 planner + $0.043 Jev = **$0.044** | **$1.53** | 34.8× | 10s plan + 5s Jev | 347s |
| **Total** | **$0.168** | **$11.55** | 68.9× | | |

## Customer support: cancel because unaffordable

**Prompt:** “Find customers trying to cancel an order because they cannot afford it.”

**Inputs:** `support_requests` 3,000 rows × 3 columns (~42,683 tokens)

> 9 rows satisfy the proxy gold definition among 3000 messages.

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 2,459 in / 565 out; Jev 809,594 in over 300 requests (170 direct, 130 openrouter) | 47,046 in / 792 out (reasoning 752) |
| Cost | $0.0007 planner + $0.034 Jev = **$0.035** | **$0.510** |
| Latency | 7s plan + 8s Jev | 27s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Customers cancelling an order because they cannot afford it* — Semantically flags instructions where the customer wants to cancel an order (or an item in it) and the stated or implied reason is that they cannot afford it, including indirect phrasings like lack of money, financial trouble, or asking to cancel because of the price. Uncertain rows are routed to a review view; adjust thresholds in the filter if needed.

- `annotate_cancel_unaffordable` semantic_annotate · **unaffordable_cancel** (boolean): The text may be in any language; judge its meaning. Does this customer message express a desire to cancel an order (or remove items from an order) AND indicate that the reason is that they cannot afford it (no money, fin
- `filter_unaffordable_cancel` filter `{"column": "unaffordable_cancel.value"}`
- `project_result` project

**Comparison**

```json
{
  "gold_definition": "intent == cancel_order AND text mentions affordability (regex); the Bitext intent labels were removed from both inputs",
  "gold_size": 9,
  "pipeline": {
    "selected": 3,
    "from_step": "project_result",
    "tp": 3,
    "fp": 0,
    "fn": 6,
    "precision": 1.0,
    "recall": 0.3333,
    "f1": 0.5,
    "review_view": {
      "unaffordable_cancel": {
        "uncertain": 4
      }
    },
    "gold_rows_in_review_view": 4,
    "missed_gold_in_review_view": 4
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
    "both": 3,
    "only_a": 0,
    "only_b": 6,
    "jaccard": 0.3333
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

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 2,398 in / 689 out; Jev 1,312,996 in over 308 requests (166 direct, 142 openrouter) | 46,419 in / 9,849 out (reasoning 8,994) |
| Cost | $0.0007 planner + $0.055 Jev = **$0.056** | **$0.957** |
| Latency | 14s plan + 9s Jev | 221s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Classify transfer messages into pending, failed, or wrong-account* — Classifies each support message into one of four categories: transfer still pending, transfer failed, transfer sent to the wrong account, or other/not transfer-related. Rows can then be separated by transfer_status.value. Edit the option set or min_confidence (0.5) if you want different groupings.

- `classify_transfers` semantic_annotate · **transfer_status** (category) options=['pending_transfer', 'failed_transfer', 'wrong_account', 'other']: The text may be in any language; judge its meaning. Classify this customer support message about a money transfer into exactly one category: (1) pending_transfer if the transfer has been sent or is in progress and has no

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
    "from_step": "classify_transfers",
    "question": "transfer_status",
    "raw_labels": {
      "other": 2447,
      "pending_transfer": 230,
      "failed_transfer": 257,
      "wrong_account": 6
    },
    "label_mapping": {
      "other": "other",
      "pending_transfer": "pending",
      "failed_transfer": "failed",
      "wrong_account": "wrong_account"
    },
    "rows_labeled": 2940,
    "strict": {
      "macro_f1": 0.3329,
      "accuracy_on_rows_in_scope": 0.2044,
      "per_class": {
        "pending": {
          "predicted": 230,
          "gold": 40,
          "tp": 37,
          "fp": 193,
          "fn": 3,
          "precision": 0.1609,
          "recall": 0.925,
          "f1": 0.2741
        },
        "failed": {
          "predicted": 257,
          "gold": 80,
          "tp": 66,
          "fp": 191,
          "fn": 14,
          "precision": 0.2568,
          "recall": 0.825,
          "f1": 0.3917
        }
      }
    },
    "lenient": {
      "macro_f1": 0.4856,
      "accuracy_on_rows_in_scope": 0.3272,
      "per_class": {
        "pending": {
          "predicted": 230,
          "gold": 160,
          "tp": 113,
          "fp": 117,
          "fn": 47,
          "precision": 0.4913,
          "recall": 0.7063,
          "f1": 0.5795
        },
        "failed": {
          "predicted": 257,
          "gold": 80,
          "tp": 66,
          "fp": 191,
          "fn": 14,
          "precision": 0.2568,
          "recall": 0.825,
          "f1": 0.3917
        }
      }
    },
    "false_positive_intents": {
      "pending": {
        "pending_top_up": 33,
        "pending_card_payment": 24,
        "pending_cash_withdrawal": 23,
        "balance_not_updated_after_cheque_or_cash_deposit": 20,
        "Refund_not_showing_up": 12,
        "topping_up_by_card": 5
      },
      "failed": {
        "top_up_reverted": 38,
        "reverted_card_payment?": 36,
        "top_up_failed": 33,
        "declined_card_payment": 28,
        "beneficiary_not_allowed": 26,
        "pending_cash_withdrawal": 6
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
    "agreement": 0.8974,
    "kappa": 0.5145,
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
      "row_id": 201,
      "text": "My withdrawl is still pending.  Why?",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 202,
      "text": "My card was denied at an ATM earlier today but the transaction is pending. Please cancel it as I did not receive my money.",
      "intent": "pending_cash_withdrawal",
      "pipeline": "failed",
      "oneshot": "other"
    },
    {
      "row_id": 203,
      "text": "My transaction is still showing that is pending from my cash withdrawal.",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 206,
      "text": "Why is it taking so long for my cash withdrawal to no longer show as pending?",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 209,
      "text": "Why is my withdrawal still pending?",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 210,
      "text": "Are you sure when the withdrawal will show?",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 211,
      "text": "Hi! I was wondering if you can help me. I used the city centre ATM to get some cash, but the machine declined my card. My account shows that",
      "intent": "pending_cash_withdrawal",
      "pipeline": "failed",
      "oneshot": "other"
    },
    {
      "row_id": 213,
      "text": "My cash in the ATM is still pending",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 214,
      "text": "my atm cash out is still pending",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 215,
      "text": "I am still waiting for a cash withdrawal to show",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 216,
      "text": "Is my pending cash withdrawal processed?",
      "intent": "pending_cash_withdrawal",
      "pipeline": "pending",
      "oneshot": "other"
    },
    {
      "row_id": 217,
      "text": "how long til my cash out goes through?",
      "intent": "pending_cash_withdrawal",
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
      "row_id": 701,
      "text": "I transferred the wrong amount and would like to cancel the transaction.",
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
| Tokens | planner 3,672 in / 902 out; Jev 239,307 in over 79 requests (40 direct, 39 openrouter) | 206,753 in / 1,320 out (reasoning 1,276) |
| Cost | $0.0010 planner + $0.010 Jev = **$0.011** | **$2.13** |
| Latency | 27s plan + 7s Jev | 43s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Find post-cancellation / unauthorized recurring charge complaints and rank by urgency* — Filters complaints to those where charges continued after the customer cancelled or unauthorized/recurring charges occurred, then scores each matching complaint for urgency and ranks them most urgent first. Adjust the urgency rubric levels or the true_min threshold if you want a broader/narrower match set.

- `tag_recurring` semantic_annotate · **recurring_charge** (boolean): The text may be in any language; judge its meaning. Does this complaint describe charges continuing after the customer cancelled a service, subscription, or account, OR unauthorized charges and/or unwanted recurring (sub
- `recurring_only` filter `{"column": "recurring_charge.value"}`
- `urgency` semantic_annotate · **urgency_score** (score) levels=3: Rate how urgent this complaint is for prioritized handling. Complaints where money is still actively being taken after cancellation or repeatedly without consent are most urgent; historical or one-off occurrences are les
- `rank_urgent` sort by [('urgency_score.score', 'desc'), ('Date received', 'desc')]
- `final_cols` project

**Comparison**

```json
{
  "notes": [
    "The full 5,000-row demo sample (8 of the 15 columns) so the one-shot request stays under the 272K-token long-context tier; the 100,000-row file used in the walkthrough is ~6,783,860 tokens and cannot be sent to gpt-6-astra in one request at all.",
    "The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. There is no gold label; 169 rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows carrying one is reported only as a sanity check."
  ],
  "pipeline": {
    "selected": 170,
    "from_step": "final_cols",
    "keyword_rows_selected": 133,
    "keyword_share": 0.7824,
    "review_view": {
      "recurring_charge": {
        "uncertain": 106
      },
      "urgency_score": {}
    },
    "oneshot_rows_in_review_view": 0,
    "review_view_rows_with_keyword": 1,
    "top_10": [
      {
        "row_id": 4558,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 2631,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1863,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1951,
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
        "row_id": 1954,
        "text": "Payday loan, title loan, personal loan, or advance loan | Can't stop withdrawals from your bank account | "
      },
      {
        "row_id": 1396,
        "text": "Debt or credit management | Unauthorized withdrawals or charges | "
      },
      {
        "row_id": 951,
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
    "only_a": 162,
    "only_b": 0,
    "jaccard": 0.0471,
    "rank_spearman_on_common": {
      "n": 8,
      "rho": 0.2857
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
      "row_id": 4,
      "text": "Checking or savings account | Problem with a lender or other company charging your account | Transaction was not authorized"
    },
    {
      "row_id": 19,
      "text": "Credit card | Problem with a purchase shown on your statement | Card was charged for something you did not purchase with the card"
    },
    {
      "row_id": 25,
      "text": "Money transfer, virtual currency, or money service | Unauthorized transactions or other transaction problem | "
    },
    {
      "row_id": 45,
      "text": "Money transfer, virtual currency, or money service | Unauthorized transactions or other transaction problem | "
    },
    {
      "row_id": 81,
      "text": "Money transfer, virtual currency, or money service | Unauthorized transactions or other transaction problem | "
    },
    {
      "row_id": 91,
      "text": "Credit card | Problem with a purchase shown on your statement | Card was charged for something you did not purchase with the card"
    },
    {
      "row_id": 106,
      "text": "Credit card | Problem with a purchase shown on your statement | Card was charged for something you did not purchase with the card"
    },
    {
      "row_id": 122,
      "text": "Money transfer, virtual currency, or money service | Unauthorized transactions or other transaction problem | "
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
| Tokens | planner 4,429 in / 581 out; Jev 360,608 in over 120 requests (65 direct, 55 openrouter) | 108,201 in / 5,288 out (reasoning 4,358) |
| Cost | $0.0010 planner + $0.015 Jev = **$0.016** | **$1.35** |
| Latency | 15s plan + 4s Jev | 102s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Reviews reporting unreliable Wi-Fi, grouped by property* — Flags reviews whose text reports unreliable or problematic Wi-Fi, keeps the flagged rows, then groups by listing to count Wi-Fi complaints and compare nightly prices (average and range). Edit the confidence threshold or question criteria to make the flag stricter or looser. Listings without a numeric price are excluded from the price metrics but still counted.

- `wifi_flag` semantic_annotate · **wifi_issue** (boolean): The text (which may be in any language; judge its meaning) is a review of a rental listing. Does this review report unreliable Wi-Fi or internet — meaning it complains about or mentions problems such as slow, dropping, w
- `wifi_rows` filter `{"column": "wifi_issue.value"}`
- `price_num` compute
- `by_property` aggregate group_by=['listing_id', 'listing_name', 'borough', 'room_type'] metrics=[('count', None), ('avg', 'price_number'), ('min', 'price_number'), ('max', 'price_number')]
- `sorted` sort by [('wifi_complaints', 'desc'), ('avg_nightly_price', 'desc')]

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
      "wifi_issue": {
        "uncertain": 1
      }
    },
    "properties": 16,
    "aggregate_check": {
      "aggregate_step": "by_property",
      "groups": 16,
      "grouped_by": [
        "listing_id",
        "listing_name",
        "borough",
        "room_type"
      ],
      "count_metric": "wifi_complaints",
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

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,188 in / 609 out; Jev 116,593 in over 30 requests (15 direct, 15 openrouter) | 75,049 in / 86,409 out (reasoning 80,285) |
| Cost | $0.0009 planner + $0.0049 Jev = **$0.0058** | **$5.07** |
| Latency | 28s plan + 1s Jev | 1538s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (2 attempt(s)): *Gift category revenue by country* — Classifies each product's description into one gift category, joins the classification to the retail_revenue_by_product_and_country table on stock_code, then sums revenue per category and country. You can edit the category taxonomy or raise min_confidence if you want stricter classification.

- `classify` semantic_annotate · **gift_category** (category) options=['home_decor', 'kitchen_dining', 'bags_accessories', 'stationery_craft', 'toys_games', 'garden_outdoor', 'other']: Based on the product description (which may be in any language; judge its meaning), assign the product to exactly one gift category. Description text is in English and may be noisy/abbreviated.
- `join_country` join on [{'left': 'stock_code', 'right': 'stock_code'}] how=left
- `rev_by_cat_country` aggregate group_by=['gift_category.value', 'right.country'] metrics=[('sum', 'right.revenue'), ('count', None)]

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
      "home_decor": 90,
      "kitchen_dining": 74,
      "bags_accessories": 42,
      "garden_outdoor": 28,
      "stationery_craft": 26,
      "other": 23,
      "toys_games": 17
    },
    "aggregate_step": "rev_by_cat_country",
    "revenue_check": {
      "cells_claimed": 267,
      "cells_expected": 267,
      "cells_with_expected_counterpart": 267,
      "cells_missing": 0,
      "cells_extra": 0,
      "exact_to_cent": 267,
      "within_0_5_percent": 267,
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
    "adjusted_rand_index": 0.0654,
    "nmi": 0.2137
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

| | Operators (Jev) | One-shot (Astra) |
|---|---|---|
| Tokens | planner 4,318 in / 587 out; Jev 1,026,920 in over 200 requests (107 direct, 93 openrouter) | 66,650 in / 17,349 out (reasoning 12,338) |
| Cost | $0.0009 planner + $0.043 Jev = **$0.044** | **$1.53** |
| Latency | 10s plan + 5s Jev | 347s |
| Status | job `succeeded` | `completed` |

**Plan written by the planner** (1 attempt(s)): *Match offers to catalog products* — Finds, for each offer row, the catalog record in product_catalog that describes the exact same product, using a semantic match over brand, title and description so differing wordings still match. Rows with low match scores remain unmatched; you can tighten or loosen this by editing accept_min/reject_max.

- `match_offers` semantic_match on ['brand', 'title', 'description'] ↔ ['brand', 'title', 'description'], 5 candidates/row, accept ≥ 0.8: Do these two records describe the exact same physical product (same manufacturer product, same model/SKU, same variant)? Text may be in any language and may phrase the product differently; judge by me
- `sort_matches` sort by [('match.score', 'desc'), ('row_id', 'asc')]

**Comparison**

```json
{
  "notes": [
    "400 offers, each with exactly one true match among 598 catalog entries (WDC gold standard); every other catalog entry is a different product. 396 catalog entries that were the offers' own identical records were removed first. The cluster_id and true_catalog_id columns were removed from both inputs."
  ],
  "gold_pairs": 400,
  "pipeline": {
    "pairs": 151,
    "status_counts": {
      "uncertain": 109,
      "matched": 151,
      "unmatched": 140
    },
    "tp": 148,
    "fp": 3,
    "fn": 252,
    "precision": 0.9801,
    "recall": 0.37,
    "f1": 0.5372,
    "diagnostics": {
      "candidates_per_row": 5,
      "accept_min": 0.8,
      "reject_max": 0.3,
      "gold_entry_among_candidates": 266,
      "candidate_recall": 0.665,
      "gold_candidate_accepted_by_jev": 149,
      "gold_candidate_left_uncertain": 90,
      "gold_candidate_rejected_by_jev": 27,
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
    "both": 148,
    "only_a": 3,
    "only_b": 209,
    "jaccard": 0.4111
  }
}
```

<details><summary>Examples of disagreements</summary>

```json
{
  "pipeline_wrong": [
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

