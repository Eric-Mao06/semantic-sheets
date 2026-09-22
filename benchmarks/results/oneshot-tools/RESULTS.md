# Benchmark run `oneshot-tools`: one-shot gpt-6-astra with the code interpreter

The same prompt and the same inline CSV as the plain one-shot arm, plus the hosted **code interpreter** with the CSV files mounted in the container, so the model can run Python for the exact work (counts, sums, joins, sorting, assembling the JSON) while still reading every row for the semantic judgements. Still one Responses API request (reasoning `high`, strict JSON schema); the tool loop runs server-side and the reported usage covers every model turn. Cost adds the container session ($0.03 per 20 minutes, 5-minute minimum) and prices cache-write tokens at the cache-write rate.

Prices used (list, 2026-09-21): gpt-6-astra $10.00 / M input, $1.00 / M cached input, $12.50 / M cache-write input, $50.00 / M output (reasoning tokens bill as output; requests over 272,000 input tokens reprice to $20.00 / $75.00); hosted code interpreter container $0.03 per 20-minute session (5-minute minimum); z-ai/glm-5.3-flash via OpenRouter $0.15 / M input, $0.50 / M output; deepseek/deepseek-v4.1-flash via OpenRouter (Together) $0.30 / M input, $1.20 / M output, using OpenRouter's reported cost when it returns one; jev-1.13.0 $0.042 / M input, output free. Costs are computed from the token usage each API reported.

## Summary

| Scenario | Rows | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|---|
| **Customer support: cancel because unaffordable** | 3,000 | 9 rows; P 100.0% / R 100.0% / F1 100.0% | 9 rows; P 100.0% / R 100.0% / F1 100.0% |
| **Banking queries: separate transfer problems** | 3,080 | pending F1 79.7%, failed F1 75.0% (lenient gold); macro-F1 61.5% strict / 77.3% lenient | pending F1 79.1%, failed F1 75.9% (lenient gold); macro-F1 61.6% strict / 77.5% lenient |
| **Consumer complaints: filter then rank by urgency** | 5,000 | 8 rows ranked; 100.0% carry a topical keyword | 8 rows ranked; 100.0% carry a topical keyword |
| **Airbnb reviews: unreliable Wi-Fi, grouped by property** | 1,200 | 17 reviews, 17 properties; counts consistent 17/17, prices right 16/17 | 17 reviews, 17 properties; counts consistent 17/17, prices right 16/17 |
| **Retail: gift categories, revenue by category and country** | 300 + 4,758 | 3 categories; revenue cells exact 53/83 (within 0.5%: 55), max error $46,502.80, 0 cells missing | 8 categories; revenue cells exact 264/264 (within 0.5%: 264), max error $0.0000, 0 cells missing |
| **Product matching: offers to catalog** | 400 + 598 | 357 pairs; P 97.5% / R 87.0% / F1 91.9% | 361 pairs; P 96.7% / R 87.2% / F1 91.7% |

| Scenario | No tools: cost · latency | One-shot + code interpreter (gpt-6-astra): cost · latency · tool calls | Tokens, no tools | Tokens with tools |
|---|---|---|---|---|
| Customer support: cancel because unaffordable | **$0.510** · 27s | **$0.644** ($0.614 model + $0.030 container) · 18s · 1 tool calls | 47,046 in / 792 out (reasoning 752) | 48,952 in / 406 out (reasoning 368) |
| Banking queries: separate transfer problems | **$0.957** · 221s | **$1.15** ($1.12 model + $0.030 container) · 224s · 5 tool calls | 46,419 in / 9,849 out (reasoning 8,994) | 62,076 in / 7,659 out (reasoning 7,050) |
| Consumer complaints: filter then rank by urgency | **$2.13** · 43s | **$2.75** ($2.72 model + $0.030 container) · 55s · 3 tool calls | 206,753 in / 1,320 out (reasoning 1,276) | 213,351 in / 1,236 out (reasoning 1,194) |
| Airbnb reviews: unreliable Wi-Fi, grouped by property | **$1.35** · 102s | **$1.61** ($1.58 model + $0.030 container) · 80s · 4 tool calls | 108,201 in / 5,288 out (reasoning 4,358) | 116,794 in / 2,831 out (reasoning 1,911) |
| Retail: gift categories, revenue by category and country | **$5.07** · 1538s | **$1.86** ($1.83 model + $0.030 container) · 326s · 7 tool calls | 75,049 in / 86,409 out (reasoning 80,285) | 96,077 in / 13,471 out (reasoning 3,736) |
| Product matching: offers to catalog | **$1.53** · 347s | **$2.36** ($2.33 model + $0.030 container) · 436s · 15 tool calls | 66,650 in / 17,349 out (reasoning 12,338) | 128,684 in / 17,411 out (reasoning 12,344) |
| **Total** | **$11.55** | **$10.37** (0.9× the no-tools cost) | | |

## Customer support: cancel because unaffordable

**Prompt:** “Find customers trying to cancel an order because they cannot afford it.”

**Inputs:** `support_requests` 3,000 rows × 3 columns (~42,683 tokens)

> 9 rows satisfy the proxy gold definition among 3000 messages.

| | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|
| Tokens | 47,046 in / 792 out (reasoning 752) | 48,952 in / 406 out (reasoning 368) |
| Cached / cache-write input | 0 / 0 | 1,453 / 46,928 |
| Cost | **$0.510** · 27s | **$0.644** ($0.614 model + $0.030 container) · 18s · 1 tool calls |
| Status | `completed` | `completed` |

<details><summary>Code the model ran (1 call)</summary>

**Call 1** (`completed`)

```python
import csv, json
path="/mnt/data/file-Sns5G8CHfXtyg5AWWMfWq7-support_requests.csv"
with open(path, newline="", encoding="utf-8") as f:
    rows=list(csv.DictReader(f))
selected_ids={22,262,663,1009,2479,2887,2937,2951,2973}
matches=[]
for row in rows:
    if int(row["row_id"]) in selected_ids:
        matches.append(int(row["row_id"]))
        print(row["row_id"],row["instruction"])
print(len(rows))
print(json.dumps({"matching_row_ids":matches}))
```

</details>

**Comparison** (one-shot side; the operators arm was not run in this run)

```json
{
  "gold_definition": "intent == cancel_order AND text mentions affordability (regex); the Bitext intent labels were removed from both inputs",
  "gold_size": 9,
  "oneshot": {
    "selected": 9,
    "tp": 9,
    "fp": 0,
    "fn": 0,
    "precision": 1.0,
    "recall": 1.0,
    "f1": 1.0
  }
}
```

<details><summary>Examples</summary>

```json
{
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
      "row_id": 2479,
      "text": "I can't afford order {{Order Number}}, could you cancel it?"
    },
    {
      "row_id": 2887,
      "text": "I can no longer afford purchase {{Order Number}}"
    },
    {
      "row_id": 2937,
      "text": "I can't afford order {{Order Number}}"
    },
    {
      "row_id": 2951,
      "text": "I can no longer afford purchase {{Order Number}}"
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

| | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|
| Tokens | 46,419 in / 9,849 out (reasoning 8,994) | 62,076 in / 7,659 out (reasoning 7,050) |
| Cached / cache-write input | 0 / 0 | 0 / 47,753 |
| Cost | **$0.957** · 221s | **$1.15** ($1.12 model + $0.030 container) · 224s · 5 tool calls |
| Status | `completed` | `completed` |

<details><summary>Code the model ran (5 calls)</summary>

**Call 1** (`completed`)

```python
import pandas as pd, json
df = pd.read_csv('/mnt/data/file-4JVFkrVLZvJhHzuYsAKG4g-banking_queries.csv')
assert df.shape == (3080, 2)
assert df.row_id.is_unique
rows = dict(zip(df.row_id, df.text))
def show(a, b):
    for rid in df.loc[df.row_id.between(a,b),'row_id']:
        print(f'{rid}: {rows[rid]}')
show(681,720)
show(841,880)
```

**Call 2** (`completed`)

```python
show(1721,1760)
show(1841,1880)
show(2041,2080)
show(2161,2200)
show(2281,2320)
show(2681,2720)
```

**Call 3** (`completed`)

```python
selected = {'failed': set(), 'wrong_account': set(), 'pending': set()}

selected['wrong_account'].update([684, 686, 692, 695, 706])

selected['failed'].update([
    1721, 1723, 1724, 1725, 1728, 1729, 1730, 1732, 1733, 1734,
    1737, 1738, 1739, 1741, 1742, 1743, 1744, 1745, 1746, 1747,
    1748, 1749, 1750, 1751, 1753, 1754, 1758, 1760,
    2161, 2162, 2163, 2164, 2165, 2168, 2170, 2171, 2172, 2174,
    2177, 2179, 2180, 2181, 2182, 2183, 2184, 2185, 2186, 2187,
    2188, 2191, 2193, 2194, 2195, 2197, 2199, 2200,
    2281, 2282, 2283, 2284, 2285, 2286, 2287, 2288, 2289, 2290,
    2291, 2292, 2293, 2294, 2295, 2296, 2297, 2298, 2299, 2300,
    2302, 2303, 2304, 2305, 2306, 2307, 2308, 2309, 2310, 2311,
    2312, 2313, 2315, 2316, 2317, 2318, 2319, 2320
])

selected['pending'].update([
    841, 842, 844, 845, 848, 849, 851, 852, 854, 855, 856, 857,
    858, 859, 860, 861, 862, 863, 865, 866, 872, 873, 874, 875,
    876, 877, 878,
    1331, 1334,
    1841, 1843, 1844, 1845, 1846, 1847, 1848, 1849, 1850, 1851,
    1853, 1854, 1855, 1856, 1857, 1858, 1859, 1860, 1861, 1862,
    1863, 1864, 1865, 1866, 1868, 1870, 1871, 1872, 1873, 1874,
    1875, 1876, 1877, 1878, 1879,
    2041, 2044
# … (1,511 characters in total)
```

**Call 4** (`completed`)

```python
show(1321,1360)
show(241,280)
```

**Call 5** (`completed`)

```python
result = {k: sorted(int(x) for x in selected[k]) for k in ('failed', 'wrong_account', 'pending')}
all_ids = [rid for ids in result.values() for rid in ids]
assert len(all_ids) == len(set(all_ids))
assert set(all_ids).issubset(set(df.row_id))
print(json.dumps(result))
```

</details>

**Comparison** (one-shot side; the operators arm was not run in this run)

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
  "oneshot": {
    "assigned": {
      "pending": 108,
      "failed": 94,
      "wrong_account": 5
    },
    "strict": {
      "macro_f1": 0.6158,
      "accuracy_on_rows_in_scope": 0.457,
      "per_class": {
        "pending": {
          "predicted": 108,
          "gold": 40,
          "tp": 35,
          "fp": 73,
          "fn": 5,
          "precision": 0.3241,
          "recall": 0.875,
          "f1": 0.473
        },
        "failed": {
          "predicted": 94,
          "gold": 80,
          "tp": 66,
          "fp": 28,
          "fn": 14,
          "precision": 0.7021,
          "recall": 0.825,
          "f1": 0.7586
        }
      }
    },
    "lenient": {
      "macro_f1": 0.7748,
      "accuracy_on_rows_in_scope": 0.637,
      "per_class": {
        "pending": {
          "predicted": 108,
          "gold": 160,
          "tp": 106,
          "fp": 2,
          "fn": 54,
          "precision": 0.9815,
          "recall": 0.6625,
          "f1": 0.791
        },
        "failed": {
          "predicted": 94,
          "gold": 80,
          "tp": 66,
          "fp": 28,
          "fn": 14,
          "precision": 0.7021,
          "recall": 0.825,
          "f1": 0.7586
        }
      }
    },
    "false_positive_intents": {
      "pending": {
        "topping_up_by_card": 2
      },
      "failed": {
        "beneficiary_not_allowed": 28
      }
    },
    "wrong_account_rows": 5,
    "wrong_account_intents": {
      "cancel_transfer": 5
    }
  }
}
```

<details><summary>Examples</summary>

```json
{
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

> The full 5,000-row demo sample (8 of the 15 columns).

> The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. There is no gold label; 169 rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows carrying one is reported only as a sanity check.

| | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|
| Tokens | 206,753 in / 1,320 out (reasoning 1,276) | 213,351 in / 1,236 out (reasoning 1,194) |
| Cached / cache-write input | 0 / 0 | 0 / 208,088 |
| Cost | **$2.13** · 43s | **$2.75** ($2.72 model + $0.030 container) · 55s · 3 tool calls |
| Status | `completed` | `completed` |

<details><summary>Code the model ran (3 calls)</summary>

**Call 1** (`completed`)

```python
import pandas as pd, json
p = '/mnt/data/file-QYsnfgBhi3ZRPF5YRiEuja-consumer_complaints.csv'
df = pd.read_csv(p, keep_default_na=False)
print(df.shape)
pairs = df.groupby(['Issue','Sub-issue'], sort=True, dropna=False).size()
for (issue, sub), n in pairs.items():
    print(f'{n} | {issue} | {sub}')
```

**Call 2** (`completed`)

```python
review_pairs = {
("Can't stop withdrawals from your bank account", ""),
("Problem with a lender or other company charging your account", "Can't stop withdrawals from your account"),
("Credit monitoring or identity theft protection services", "Problem canceling credit monitoring or identify theft protection service"),
("Credit monitoring or identity theft protection services", "Billing dispute for services"),
("Unauthorized withdrawals or charges", ""),
}
review = df[[ (i,s) in review_pairs for i,s in zip(df['Issue'],df['Sub-issue']) ]]
print(review.to_csv(index=False))
```

**Call 3** (`completed`)

```python
matching_pairs = {
    ("Can't stop withdrawals from your bank account", ""),
    ("Problem with a lender or other company charging your account", "Can't stop withdrawals from your account"),
}
selected_ids = []
for row in df.to_dict('records'):
    if (row['Issue'], row['Sub-issue']) in matching_pairs:
        selected_ids.append(row['row_id'])
matches = df[df['row_id'].isin(selected_ids)].copy()
matches['urgency'] = [2 if p == 'Payday loan' else 1 for p in matches['Sub-product']]
ranked = matches.sort_values(['urgency','Date received','row_id'], ascending=[False,False,True])
answer = {"ranked_row_ids": ranked['row_id'].tolist()}
print(json.dumps(answer))
```

</details>

**Comparison** (one-shot side; the operators arm was not run in this run)

```json
{
  "notes": [
    "The full 5,000-row demo sample (8 of the 15 columns).",
    "The 2026 CFPB export has no free-text narratives; both approaches judge the Product / Issue / Sub-issue text, so matches are rare. There is no gold label; 169 rows carry a topical keyword (unauthorized / cancel / recurring / continuing / can't stop withdrawals) and the share of selected rows carrying one is reported only as a sanity check."
  ],
  "oneshot": {
    "selected": 8,
    "keyword_rows_selected": 8,
    "keyword_share": 1.0,
    "top_10": [
      {
        "row_id": 1954,
        "text": "Payday loan, title loan, personal loan, or advance loan | Can't stop withdrawals from your bank account | "
      },
      {
        "row_id": 4558,
        "text": "Checking or savings account | Problem with a lender or other company charging your account | Can't stop withdrawals from your account"
      },
      {
        "row_id": 1951,
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
  }
}
```

<details><summary>Examples</summary>

```json
{
  "only_oneshot": [
    {
      "row_id": 1863,
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
    }
  ]
}
```

</details>


## Airbnb reviews: unreliable Wi-Fi, grouped by property

**Prompt:** “Find reviews reporting unreliable Wi-Fi, group by property, and compare nightly prices.”

**Inputs:** `airbnb_reviews` 1,200 rows × 8 columns (~111,272 tokens)

> Bounded to 1200 reviews (many languages) so the one-shot request fits comfortably in context: all 70 reviews in the 5,000-review sample that mention Wi-Fi / internet at all (positive or negative) plus 1130 random others, shuffled. No gold label; the keyword share is reported as a sanity check (a review can report bad Wi-Fi without using the word, and most Wi-Fi mentions are positive).

| | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|
| Tokens | 108,201 in / 5,288 out (reasoning 4,358) | 116,794 in / 2,831 out (reasoning 1,911) |
| Cached / cache-write input | 0 / 0 | 0 / 109,534 |
| Cost | **$1.35** · 102s | **$1.61** ($1.58 model + $0.030 container) · 80s · 4 tool calls |
| Status | `completed` | `completed` |

<details><summary>Code the model ran (4 calls)</summary>

**Call 1** (`completed`)

```python
import csv, json
from decimal import Decimal
path = '/mnt/data/file-UQVHEGocrtVvZSx5XNykxy-airbnb_reviews.csv'
with open(path, newline='', encoding='utf-8') as f:
    reviews = list(csv.DictReader(f))
len(reviews), list(reviews[0])
```

**Call 2** (`completed`)

```python
candidate_ids = [13,85,132,166,253,296,317,475,519,537,543,621,664,744,832,995,1107]
by_id = {int(r["row_id"]):r for r in reviews}
for row_id in candidate_ids + [953,1054]:
    r = by_id[row_id]
    print(json.dumps({k:r[k] for k in ['row_id','listing_id','listing_name','nightly_price','comments']},ensure_ascii=False))
```

**Call 3** (`completed`)

```python
from collections import defaultdict
all_by_listing = defaultdict(list)
for r in reviews:
    all_by_listing[r["listing_id"]].append(r)

for row_id in candidate_ids:
    r = by_id[row_id]
    group = all_by_listing[r["listing_id"]]
    print(r["listing_id"], [(a["row_id"], a["nightly_price"]) for a in group])
```

**Call 4** (`completed`)

```python
selected_ids = set(candidate_ids)
selected = [r for r in reviews if int(r['row_id']) in selected_ids]
groups = defaultdict(list)
for r in selected:
    groups[r['listing_id']].append(r)

properties = []
for listing_id, rows in groups.items():
    raw_prices = {r["nightly_price"] for r in all_by_listing[listing_id] if r["nightly_price"]}
    assert len(raw_prices) <= 1
    price = float(Decimal(next(iter(raw_prices)).replace("$","").replace(",",""))) if raw_prices else None
    properties.append({
        "listing_name": rows[0]["listing_name"],
        "nightly_price": price,
        "listing_id": listing_id,
        "review_count": len(rows)
    })
properties.sort(key=lambda p: (p['nightly_price'] is None, p['nightly_price'] if p['nightly_price'] is not None else 0, p['listing_id']))
answer = {
    "wifi_review_row_ids": sorted(int(r["row_id"]) for r in selected),
    "properties": properties
}
assert sum(p["review_count"] for p in properties) == len(selected)
print(json.dumps(answer, ensure_ascii=False, indent=2))
```

</details>

**Comparison** (one-shot side; the operators arm was not run in this run)

```json
{
  "notes": [
    "Bounded to 1200 reviews (many languages) so the one-shot request fits comfortably in context: all 70 reviews in the 5,000-review sample that mention Wi-Fi / internet at all (positive or negative) plus 1130 random others, shuffled. No gold label; the keyword share is reported as a sanity check (a review can report bad Wi-Fi without using the word, and most Wi-Fi mentions are positive)."
  ],
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
        "nightly_price": 165.0,
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
        "nightly_price": 258.0,
        "listing_id": "7919372",
        "review_count": 1
      },
      {
        "listing_name": "Perfectly Situated Bay Ridge Studio",
        "nightly_price": 287.0,
        "listing_id": "1173237288996322274",
        "review_count": 1
      }
    ]
  }
}
```

<details><summary>Examples</summary>

```json
{
  "only_oneshot": [
    {
      "row_id": 13,
      "text": "Great location, roof is wonderful, easy check in and check out. Rooms are definitely small, but good for one person. Bathroom was clean, bed"
    },
    {
      "row_id": 85,
      "text": "Mooi, ruim appartement, van alle gemakken voorzien. Dichtbij het metrostation.<br/>Angelo reageerde snel op mijn vragen. <br/>Zou hier zeker"
    },
    {
      "row_id": 132,
      "text": "O espaço é bom, é possível preparar suas refeições, é muito perto do metrô, por outro lado o wi-fi não pega muito bem e o banheiro entope."
    },
    {
      "row_id": 166,
      "text": "Great location super close to subway stations, and the host was always quick to respond to my messages. The apartment is small but still con"
    },
    {
      "row_id": 253,
      "text": "The place is wonderfully kept and has great ambience. Necessities like towels, kitchen utensils and such are easily accessible. The girlfrie"
    },
    {
      "row_id": 296,
      "text": "We took 2 teenagers, 2 early 20’s and us big kids to the Big Apple for a few days after Christmas. The City didn’t disappoint. <br/>I would "
    },
    {
      "row_id": 317,
      "text": "Fatou’s house is in a strategic spot, near MTA stations and with lots of restaurants/shops around. <br/>The room was smaller and older than "
    },
    {
      "row_id": 475,
      "text": "Nice large apartment with underground parking for one car (must be small-not sure if a large SUV would fit). The internet was spotty on some"
    }
  ]
}
```

</details>


## Retail: gift categories, revenue by category and country

**Prompt:** “Classify products into gift categories, then calculate revenue by category and country using the retail revenue by product and country table.”

**Inputs:** `retail_products` 300 rows × 7 columns (~4,488 tokens); `retail_revenue_by_product_and_country` 4,758 rows × 5 columns (~31,412 tokens)

> Bounded to the 300 highest-revenue products and their 4758 product x country revenue rows. Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; the revenue table is recomputed exactly from each approach's own labels to check the arithmetic.

| | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|
| Tokens | 75,049 in / 86,409 out (reasoning 80,285) | 96,077 in / 13,471 out (reasoning 3,736) |
| Cached / cache-write input | 0 / 0 | 0 / 76,427 |
| Cost | **$5.07** · 1538s | **$1.86** ($1.83 model + $0.030 container) · 326s · 7 tool calls |
| Status | `completed` | `completed` |

<details><summary>Code the model ran (7 calls)</summary>

**Call 1** (`completed`)

```python
import csv, json
from decimal import Decimal
from collections import defaultdict, Counter
p_path="/mnt/data/file-8RNup583N5MqW3JWMZW4r4-retail_products.csv"
r_path="/mnt/data/file-1LENa5NKBzyyH4Q2eKEdBf-retail_revenue_by_product_and_country.csv"
with open(p_path,newline='') as f:
    products=list(csv.DictReader(f))
with open(r_path,newline='') as f:
    revenues=list(csv.DictReader(f))
len(products),len(revenues),len({r['country'] for r in revenues})
```

**Call 2** (`completed`)

```python
for p in products:
    print(p['row_id'],p['stock_code'],p['description'])
```

**Call 3** (`completed`)

```python
category_names = {
    "H": "Home & Garden",
    "K": "Kitchen & Dining",
    "B": "Bags & Accessories",
    "T": "Toys & Games",
    "A": "Arts, Crafts & Stationery",
    "P": "Seasonal & Party",
    "W": "Wellness & Personal Care",
    "N": "Non-gift Fees",
}
assignments = {
1:"K",2:"H",3:"B",4:"A",5:"P",6:"H",7:"P",8:"H",9:"K",10:"K",
11:"B",12:"H",13:"B",14:"H",15:"B",16:"H",17:"H",18:"H",19:"K",20:"W",
21:"A",22:"B",23:"P",24:"K",25:"W",26:"P",27:"B",28:"B",29:"B",30:"H",
31:"H",32:"K",33:"H",34:"K",35:"K",36:"W",37:"A",38:"H",39:"K",40:"H",
41:"H",42:"H",43:"P",44:"B",45:"H",46:"H",47:"A",48:"B",49:"H",50:"W",
51:"H",52:"H",53:"B",54:"B",55:"H",56:"B",57:"H",58:"K",59:"K",60:"T",
61:"H",62:"H",63:"B",64:"H",65:"P",66:"B",67:"H",68:"B",69:"B",70:"P",
71:"K",72:"K",73:"K",74:"H",75:"H",76:"K",77:"B",78:"W",79:"K",80:"H",
81:"H",82:"K",83:"B",84:"H",85:"H",86:"H",87:"B",88:"H",89:"K",90:"W",
91:"B",92:"K",93:"B",94:"N",95:"K",96:"H",97:"W",98:"H",99:"B",100:"K",
101:"B",102:"A",103:"T",104:"H",105:"W",106:"H",107:"K",108:"K",109:"K",110:"B",
111:"K",112:"H",113:"B",114:"H",115:"K",116:"B",117:"W",118:"H",119:"P",120:"W",
121:"H",122:"A",123:"H",124:"W",125:"H",126:"H",127:"W",12
# … (2,787 characters in total)
```

**Call 4** (`completed`)

```python
stock_to_category={p["stock_code"]:category_names[assignments[int(p["row_id"])]] for p in products}
totals=defaultdict(Decimal)
product_totals=defaultdict(Decimal)
used=0
for r in revenues:
    cat=stock_to_category[r["stock_code"]]
    amount=Decimal(r["revenue"])
    assert amount == amount.quantize(Decimal("0.01"))
    totals[cat,r["country"]] += amount
    product_totals[r["stock_code"]] += amount
    used+=1
assert used == 4758
mismatches=[(p["stock_code"],product_totals[p["stock_code"]],p["revenue"]) for p in products if product_totals[p["stock_code"]]!=Decimal(p["revenue"])]
len(totals),sum(totals.values()),len(mismatches),mismatches[:5]
```

**Call 5** (`completed`)

```python
answer={
    "product_categories":[
        {"row_id":int(p["row_id"]),"category":stock_to_category[p["stock_code"]]}
        for p in sorted(products,key=lambda p:int(p["row_id"]))
    ],
    "revenue_by_category_and_country":[
        {"category":category,"country":country,"revenue":float(total)}
        for (category,country),total in sorted(totals.items())
    ],
}
encoded=json.dumps(answer,ensure_ascii=False,allow_nan=False)
assert len(answer["product_categories"])==300
assert len(answer["revenue_by_category_and_country"])==264
assert sum(Decimal(str(r["revenue"])) for r in answer["revenue_by_category_and_country"])==sum(Decimal(r["revenue"]) for r in revenues)
len(encoded), Counter(r["category"] for r in answer["revenue_by_category_and_country"])
```

**Call 6** (`completed`)

```python
for row in answer["product_categories"]:
    print(json.dumps(row)+",")
```

**Call 7** (`completed`)

```python
for row in answer["revenue_by_category_and_country"]:
    print(json.dumps(row)+",")
```

</details>

**Comparison** (one-shot side; the operators arm was not run in this run)

```json
{
  "notes": [
    "Bounded to the 300 highest-revenue products and their 4758 product x country revenue rows. Both approaches define their own gift taxonomy, so labels are compared with clustering agreement (ARI / NMI) rather than exact match; the revenue table is recomputed exactly from each approach's own labels to check the arithmetic."
  ],
  "oneshot": {
    "products_labeled": 300,
    "categories": {
      "Home & Garden": 108,
      "Kitchen & Dining": 69,
      "Bags & Accessories": 43,
      "Seasonal & Party": 25,
      "Wellness & Personal Care": 24,
      "Arts, Crafts & Stationery": 17,
      "Toys & Games": 13,
      "Non-gift Fees": 1
    },
    "revenue_check": {
      "cells_claimed": 264,
      "cells_expected": 264,
      "cells_with_expected_counterpart": 264,
      "cells_missing": 0,
      "cells_extra": 0,
      "exact_to_cent": 264,
      "within_0_5_percent": 264,
      "max_abs_error": 0.0,
      "total_revenue_claimed": 10328259.82,
      "total_revenue_expected": 10328259.82,
      "worst_cells": []
    }
  }
}
```

<details><summary>Examples</summary>

```json
{
  "label_pairs": []
}
```

</details>


## Product matching: offers to catalog

**Prompt:** “Match offers to the catalog for the same exact product despite different titles.”

**Inputs:** `product_offers` 400 rows × 7 columns (~21,355 tokens); `product_catalog` 598 rows × 6 columns (~31,847 tokens)

> 400 offers, each with exactly one true match among 598 catalog entries (WDC gold standard); every other catalog entry is a different product. 0 catalog entries that were the offers' own identical records were removed first. The cluster_id and true_catalog_id columns were removed from both inputs.

| | One-shot, no tools | One-shot + code interpreter (gpt-6-astra) |
|---|---|---|
| Tokens | 66,650 in / 17,349 out (reasoning 12,338) | 128,684 in / 17,411 out (reasoning 12,344) |
| Cached / cache-write input | 0 / 0 | 0 / 68,020 |
| Cost | **$1.53** · 347s | **$2.36** ($2.33 model + $0.030 container) · 436s · 15 tool calls |
| Status | `completed` | `completed` |

<details><summary>Code the model ran (15 calls)</summary>

**Call 1** (`completed`)

```python
import pandas as pd, json
offers=pd.read_csv('/mnt/data/file-HieAXim3LUov6w8ui9jQXC-product_offers.csv').fillna('')
cat=pd.read_csv('/mnt/data/file-XW3xXiHnEzC2LZCPnADm91-product_catalog.csv').fillna('')
print(len(offers),len(cat))
cm=cat.set_index('catalog_id')
om=offers.set_index('row_id')
matches={}
def put(d):
    for r,c in d.items():
        assert r in om.index and c in cm.index
        assert r not in matches
        matches[r]=c
def showcat(ids):
    for c in ids:
        print(c,cm.loc[c,'title'], '|',cm.loc[c,'description'])
def cats(s):
    # retrieval only, decisions by reading
    x=cat[(cat.title+' '+cat.description).str.contains(s,case=False,regex=True)]
    for _,r in x.iterrows(): print(r.catalog_id, r.title,'|',r.description)
def chunk(start,end):
    for r,row in om.loc[start:end].iterrows():
        print(r,row.offer_id, row.title,'|',row.description)
chunk(1,50)
```

**Call 2** (`completed`)

```python
put({1:7218865,2:17212553,3:18787691,4:54933824,5:28556391,6:15071592,
9:89829635,10:2353834,11:26740796,12:68284224,13:26099517,14:60131715,15:68374441,16:26060342,
17:60359264,18:78303938,19:43618856,20:51760904,21:16691545,22:3777655,23:22821574,24:67725254,
25:70265673,26:53299737,27:22154247,28:48093720,29:41453265,30:9138065,32:43227294,33:30151389,
34:58023892,35:22150807,36:85021449,37:47959304,38:71230707,39:35792541,40:9907839,41:80792673,
42:19499147,43:25537521,44:13845553,45:7309472,47:5089485,48:64928819,49:47385841})
chunk(51,100)
```

**Call 3** (`completed`)

```python
put({51:53125631,52:75251439,53:18451047,54:80918812,55:11096579,56:78241304,57:88715660,
58:17553753,59:78116281,60:46088087,61:3954683,62:67422473,63:63542010,64:20813006,
65:24366661,66:17631479,67:74983296,68:17236806,69:1446199,70:34986650,71:33088663,
73:78438382,74:9632109,75:35350872,76:43979051,78:14035013,79:20044617,80:47121877,
81:26567011,82:28956719,83:42544495,84:95558103,85:34298415,86:88950114,87:7916004,88:80240194,
89:77791574,90:86407819,91:37083729,92:21012462,93:67415262,95:53380330,96:75486692,
97:5576199,98:23699673,99:51766259,100:22463957})
chunk(101,150)
```

**Call 4** (`completed`)

```python
put({101:90951828,102:43900336,103:457768,104:5650508,105:8060977,106:40163790,107:14132157,
108:84301060,109:11658050,110:12333156,111:96980573,112:44201512,113:45664586,114:1291443,
115:54722844,116:89973207,117:9646081,118:8921864,120:2244475,122:23279983,123:15724231,
124:87945154,125:25817481,127:8387371,128:30114774,129:45187816,130:86739887,132:77697394,
133:13129556,134:7900674,135:10108569,136:23313027,137:70805675,138:22638951,139:41293728,
140:25251155,142:98866039,143:36157293,144:40180230,145:66271987,146:31552844,147:95351023,
148:61690853,149:84771715,150:68792553})
chunk(151,200)
```

**Call 5** (`completed`)

```python
put({151:35860022,152:46030392,153:18057818,154:72910194,155:18996777,156:4984777,
157:90938642,158:83528218,159:89749927,161:25607937,162:75852884,163:32932210,164:47478087,
165:60359264,166:36800658,167:10484404,169:54746544,170:33520763,172:11565762,173:78504460,
174:14979345,175:62380727,176:37431252,177:13546282,178:220243,179:64197093,180:23346990,
181:31517280,182:79883238,183:55201507,184:68519995,185:13950014,186:68865061,187:92746462,
189:56392806,190:91975856,191:1766796,192:8680758,193:64888074,194:54710391,195:25138178,
196:38788118,197:27761736,198:21266898,199:57306118})
chunk(201,250)
```

**Call 6** (`completed`)

```python
put({201:14737778,202:48390119,203:80932957,204:62345625,206:29457595,207:34472445,
208:81179017,209:71989322,210:36866967,211:34961217,213:59671298,214:8757303,215:1938345,
216:2071835,218:59929353,220:63587638,221:21367693,222:76532384,223:71130653,224:32421414,
225:91421108,226:13883364,228:46158383,229:24300732,230:41608931,231:84836014,232:40225828,
233:6032942,234:38415767,235:41978210,236:1823563,237:23336476,238:87392550,239:68075793,
240:72310020,241:98358005,243:68876228,244:20148664,245:2950848,246:68849162,247:25340730,
248:15506125,249:87653966,250:71942027})
chunk(251,300)
```

**Call 7** (`completed`)

```python
put({252:28830600,253:84407354,254:16234006,255:92349368,256:7406291,257:90202399,
258:40916385,261:56993303,262:65208205,263:92956756,264:52216082,265:87112730,266:69401250,
267:2251660,270:57728272,271:93018058,272:29653526,273:18824446,274:87238244,275:25369954,
276:38112967,277:6214990,278:42351173,279:87636038,281:15348795,282:43498383,283:23158552,
284:6988926,285:19673382,286:14864012,287:74965852,288:64191452,290:30120490,291:72310020,
292:47886079,294:71951492,295:2214493,296:58461375,297:5680655,298:27263164,299:8713688,300:74270481})
chunk(301,350)
```

**Call 8** (`completed`)

```python
put({301:29412015,302:51760904,303:18351112,304:93938575,305:38874870,306:86065131,
307:19735735,308:43259056,309:2089105,310:41174280,311:10005140,312:54362555,313:51385541,
314:41132816,315:59617619,316:94578154,317:46518287,318:10320617,319:88555676,
321:75242176,322:16282417,323:71514700,324:91421555,325:25758152,326:17390600,327:32292330,
328:33130244,329:97294565,331:41364197,332:42906184,333:44737753,334:7993978,335:41834837,
336:36616040,337:13684723,338:42545635,340:72004049,341:15393429,342:8401004,344:625440,
345:34298415,347:29316472,348:70351063,349:69281741,350:45677006})
chunk(351,400)
```

**Call 9** (`completed`)

```python
print(max(matches))
put({325:25758185,326:17390600,327:32292330,328:33130244,329:97294565,331:41364197,
332:42906184,333:44737753,334:7993978,335:41834837,336:36616040,337:13684723,338:42545635,
340:72004049,341:15393429,342:8401004,344:625440,345:34298415,347:29316472,348:70351063,
349:69281741,350:45677006})
chunk(351,400)
```

**Call 10** (`completed`)

```python
put({351:10492407,352:65657820,353:90938642,354:55645617,355:13125570,357:68444336,
358:21293905,359:25175603,360:71275365,362:31785582,363:9940059,364:13110023,365:94367933,
366:87567643,367:7114245,368:80380534,369:31785582,370:16338439,371:19543686,372:30633820,
373:18159593,374:58597448,375:88973968,376:45620476,378:22745274,379:27210717,380:10155621,
381:21918296,382:72029794,383:42191064,384:1425916,385:10899164,386:30523276,387:65837617,
388:49769546,390:25641839,391:80212720,393:15529664,394:26140067,395:13899039,396:75223780,
397:81830866,398:65504542,399:72955297,400:12706433})
print('Matched',len(matches))
print('Unmatched')
for r,row in om.iterrows():
    if r not in matches: print(r,row.offer_id,row.title)
```

**Call 11** (`completed`)

```python
cats(r'Privacy|3M -')
```

**Call 12** (`completed`)

```python
for r,c in sorted(matches.items()):
    print(f"{r} {om.loc[r,'title']} == {c} {cm.loc[c,'title']}")
```

**Call 13** (`completed`)

```python
cats(r'Jabra')
```

**Call 14** (`completed`)

```python
put({242:48868171,268:2493882,269:5576199})
len(matches)
```

**Call 15** (`completed`)

```python
result={"matches":[{"offer_id":int(om.loc[r,'offer_id']),"catalog_id":int(c)} for r,c in sorted(matches.items())]}
assert len({m['offer_id'] for m in result["matches"]})==len(matches)
print(json.dumps(result,separators=(',',':')))
```

</details>

**Comparison** (one-shot side; the operators arm was not run in this run)

```json
{
  "notes": [
    "400 offers, each with exactly one true match among 598 catalog entries (WDC gold standard); every other catalog entry is a different product. 0 catalog entries that were the offers' own identical records were removed first. The cluster_id and true_catalog_id columns were removed from both inputs."
  ],
  "gold_pairs": 400,
  "oneshot": {
    "pairs": 361,
    "tp": 349,
    "fp": 12,
    "fn": 51,
    "precision": 0.9668,
    "recall": 0.8725,
    "f1": 0.9172
  }
}
```

<details><summary>Examples</summary>

```json
{
  "oneshot_wrong": [
    {
      "offer_id": 5578260,
      "title": "Jabra Biz 2300 USB Duo Headset",
      "chosen": 47886079,
      "gold": 90972374
    },
    {
      "offer_id": 10096449,
      "title": "21 oz Standard Mouth Water Bottle",
      "chosen": 10899164,
      "gold": 28179113
    },
    {
      "offer_id": 29909599,
      "title": "Zebra Black Printer Ribbon for P310F, P310C, P420C, P520C & P720C ID Card Printe",
      "chosen": 90938642,
      "gold": 50368196
    },
    {
      "offer_id": 34868774,
      "title": "Audemars Piguet Royal Oak Offshore Chronograph 42mm",
      "chosen": 15071592,
      "gold": 23249222
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
      "offer_id": 51758990,
      "title": "Seamaster Planet Ocean 600m Automatic Chronometer Chronograph Men's Watch",
      "chosen": 25758185,
      "gold": 66020255
    },
    {
      "offer_id": 51855709,
      "title": "Western Digital Dysk MY PASSPORT 1TB 2,5 black WDBYVG0010BBK-WESN",
      "chosen": 34298415,
      "gold": 50944297
    }
  ]
}
```

</details>

