"""Shared helpers for the one-shot-vs-operators benchmark: pricing, token estimates, small metrics."""
from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "data" / "samples"
BENCH_DATA_DIR = ROOT / "data" / "benchmark"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# OpenAI list prices for gpt-6-astra, USD per 1M tokens, standard tier, read from the OpenAI pricing page on
# 2026-09-21. Requests above LONG_CONTEXT_INPUT_TOKENS input tokens are repriced for the whole request.
# Reasoning tokens are billed as output tokens.
ASTRA_MODEL = "gpt-6-astra"
ASTRA_REASONING = "high"
ASTRA_PRICES = {
    "short": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
    "long": {"input": 20.00, "cached_input": 2.00, "output": 75.00},
}
LONG_CONTEXT_INPUT_TOKENS = 272_000

# Planner (orchestrator) candidates, USD per 1M tokens. OpenRouter list prices read 2026-09-21.
PLANNER_PRICES = {
    "gpt-6-astra": ASTRA_PRICES["short"],
    "z-ai/glm-5.3-flash": {"input": 0.15, "cached_input": 0.15, "output": 0.50},
    "deepseek/deepseek-v4.1-flash": {"input": 0.30, "cached_input": 0.30, "output": 1.20},  # Together endpoint
}

# TypeSafe Jev 1.13: charged per input token, output tokens free (docs.typesafe.ai/models, 2026-09-21).
JEV_MODEL = "jev-1.13.0"
JEV_PRICE_PER_MTOK_INPUT = 0.042


def planner_cost(model: str, usage: dict[str, Any]) -> dict[str, Any]:
    """Cost of one planner call. Uses the provider-reported cost when present, else the list-price table."""
    reported = usage.get("cost_usd")
    if reported:
        return {"usd": round(float(reported), 6), "source": "provider_reported", "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "reasoning_tokens": usage.get("reasoning_tokens")}
    if model == "gpt-6-astra":
        return {**astra_cost(usage), "source": "list_price"}
    p = PLANNER_PRICES.get(model)
    if p is None:
        return {"usd": None, "source": "unknown_model", "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "reasoning_tokens": usage.get("reasoning_tokens")}
    cost = int(usage.get("input_tokens") or 0) / 1e6 * p["input"] + int(usage.get("output_tokens") or 0) / 1e6 * p["output"]
    return {"usd": round(cost, 6), "source": "list_price", "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "reasoning_tokens": usage.get("reasoning_tokens"), "prices_per_mtok": p}


def astra_cost(usage: dict[str, Any]) -> dict[str, Any]:
    """Cost of one Responses API call from its usage block."""
    input_tokens = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    tier = "long" if input_tokens > LONG_CONTEXT_INPUT_TOKENS else "short"
    p = ASTRA_PRICES[tier]
    uncached = max(0, input_tokens - cached)
    cost = uncached / 1e6 * p["input"] + cached / 1e6 * p["cached_input"] + output_tokens / 1e6 * p["output"]
    return {
        "usd": round(cost, 6),
        "tier": tier,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "prices_per_mtok": p,
    }


def jev_cost(input_tokens: int) -> float:
    return round(input_tokens / 1e6 * JEV_PRICE_PER_MTOK_INPUT, 6)


def estimate_tokens(text: str) -> int:
    """Rough pre-flight estimate (about 4 characters per token for English CSV text). Measured usage is
    what gets reported; this only decides whether a table plausibly fits a one-shot request."""
    return int(math.ceil(len(text) / 4))


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=_default), encoding="utf-8")


def _default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return str(o)


# ------------------------------------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------------------------------------


def prf(predicted: set, gold: set) -> dict[str, Any]:
    tp = len(predicted & gold)
    precision = tp / len(predicted) if predicted else None
    recall = tp / len(gold) if gold else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else (0.0 if predicted or gold else None)
    return {"tp": tp, "fp": len(predicted - gold), "fn": len(gold - predicted), "precision": _r(precision), "recall": _r(recall), "f1": _r(f1)}


def set_agreement(a: set, b: set) -> dict[str, Any]:
    inter = a & b
    union = a | b
    return {"both": len(inter), "only_a": len(a - b), "only_b": len(b - a), "jaccard": _r(len(inter) / len(union)) if union else None}


def cohens_kappa(labels_a: dict[Any, str], labels_b: dict[Any, str]) -> dict[str, Any]:
    keys = sorted(set(labels_a) & set(labels_b))
    if not keys:
        return {"n": 0, "agreement": None, "kappa": None}
    agree = sum(1 for k in keys if labels_a[k] == labels_b[k])
    po = agree / len(keys)
    ca = Counter(labels_a[k] for k in keys)
    cb = Counter(labels_b[k] for k in keys)
    pe = sum(ca[label] * cb.get(label, 0) for label in ca) / (len(keys) ** 2)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {"n": len(keys), "agreement": _r(po), "kappa": _r(kappa)}


def adjusted_rand_index(labels_a: dict[Any, str], labels_b: dict[Any, str]) -> float | None:
    keys = sorted(set(labels_a) & set(labels_b))
    n = len(keys)
    if n < 2:
        return None
    table: Counter = Counter((labels_a[k], labels_b[k]) for k in keys)
    rows: Counter = Counter(labels_a[k] for k in keys)
    cols: Counter = Counter(labels_b[k] for k in keys)
    comb = lambda x: x * (x - 1) / 2  # noqa: E731
    sum_ij = sum(comb(v) for v in table.values())
    sum_a = sum(comb(v) for v in rows.values())
    sum_b = sum(comb(v) for v in cols.values())
    total = comb(n)
    expected = sum_a * sum_b / total if total else 0.0
    max_index = (sum_a + sum_b) / 2
    if max_index == expected:
        return 1.0
    return _r((sum_ij - expected) / (max_index - expected))


def normalized_mutual_information(labels_a: dict[Any, str], labels_b: dict[Any, str]) -> float | None:
    keys = sorted(set(labels_a) & set(labels_b))
    n = len(keys)
    if n == 0:
        return None
    joint: Counter = Counter((labels_a[k], labels_b[k]) for k in keys)
    pa: Counter = Counter(labels_a[k] for k in keys)
    pb: Counter = Counter(labels_b[k] for k in keys)
    mi = 0.0
    for (a, b), c in joint.items():
        p = c / n
        mi += p * math.log(p / ((pa[a] / n) * (pb[b] / n)))
    ha = -sum((c / n) * math.log(c / n) for c in pa.values())
    hb = -sum((c / n) * math.log(c / n) for c in pb.values())
    denom = math.sqrt(ha * hb)
    return _r(mi / denom) if denom else (1.0 if not mi else None)


def spearman(rank_a: list[Any], rank_b: list[Any]) -> dict[str, Any]:
    """Spearman correlation between two orderings, computed on the items present in both lists."""
    common = [x for x in rank_a if x in set(rank_b)]
    if len(common) < 3:
        return {"n": len(common), "rho": None}
    pos_b = {x: i for i, x in enumerate(rank_b)}
    a_ranks = list(range(len(common)))
    b_ranks = [pos_b[x] for x in common]
    b_sorted = sorted(range(len(common)), key=lambda i: b_ranks[i])
    b_rank_pos = [0] * len(common)
    for r, i in enumerate(b_sorted):
        b_rank_pos[i] = r
    n = len(common)
    d2 = sum((a_ranks[i] - b_rank_pos[i]) ** 2 for i in range(n))
    rho = 1 - 6 * d2 / (n * (n * n - 1))
    return {"n": n, "rho": _r(rho)}


def top_k_overlap(rank_a: list[Any], rank_b: list[Any], k: int) -> dict[str, Any]:
    a, b = set(rank_a[:k]), set(rank_b[:k])
    return {"k": k, "overlap": len(a & b)}


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def head(items: Iterable[Any], n: int = 8) -> list[Any]:
    out = []
    for x in items:
        out.append(x)
        if len(out) >= n:
            break
    return out
