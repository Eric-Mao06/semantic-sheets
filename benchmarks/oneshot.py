"""One-shot baseline: hand the whole CSV and the same request to gpt-6-astra (reasoning effort high) and ask for
the final answer directly as JSON. No tools, no code execution, one request per scenario."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from common import ASTRA_MODEL, ASTRA_REASONING, astra_cost, estimate_tokens
from scenarios import Prepared, Scenario

INSTRUCTIONS = """You are given one or more complete data tables as CSV text and a request from a spreadsheet user.
Carry out the request yourself over every row of the data (read every row; do not sample, skip or truncate).
Judge meaning, not keywords. Where the request needs numbers (counts, sums, prices), compute them exactly from the data.
Refer to rows only by the values in their identifier columns (row_id, offer_id, catalog_id, listing_id, stock_code) exactly as given.
Respond with a single JSON object that matches the required schema, and nothing else."""

MAX_OUTPUT_TOKENS = 120_000
POLL_SECONDS = 5


@dataclass
class OneShotResult:
    model: str = ASTRA_MODEL
    reasoning_effort: str = ASTRA_REASONING
    status: str = "not_run"
    output: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    latency_seconds: float | None = None
    input_tokens_estimate: int | None = None
    response_id: str | None = None
    error: str | None = None
    raw_text_head: str | None = None

    def summary(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["output_summary"] = _summarize_output(self.output)
        d.pop("output")
        return d


def _summarize_output(out: dict[str, Any] | None) -> dict[str, Any] | None:
    if not out:
        return None
    return {k: (f"list[{len(v)}]" if isinstance(v, list) else v) for k, v in out.items()}


def build_input(scenario: Scenario, prep: Prepared) -> str:
    parts = [f"Request: {scenario.prompt}", f"Output: {scenario.oneshot_hint}"]
    for t in prep.tables:
        parts.append(f"Table `{t.name}` ({t.row_count} rows; columns: {', '.join(t.columns)}), CSV follows:\n```csv\n{t.path.read_text(encoding='utf-8')}\n```")
    return "\n\n".join(parts)


def run_oneshot(scenario: Scenario, prep: Prepared, timeout_seconds: int = 3 * 3600) -> OneShotResult:
    res = OneShotResult()
    text = build_input(scenario, prep)
    res.input_tokens_estimate = estimate_tokens(text) + estimate_tokens(INSTRUCTIONS)
    client = OpenAI(timeout=600, max_retries=2)
    t0 = time.time()
    try:
        resp = client.responses.create(
            model=ASTRA_MODEL,
            reasoning={"effort": ASTRA_REASONING},
            instructions=INSTRUCTIONS,
            input=text,
            text={"format": {"type": "json_schema", "name": f"{scenario.key}_answer", "schema": scenario.oneshot_schema, "strict": True}},
            max_output_tokens=MAX_OUTPUT_TOKENS,
            background=True,
            store=True,
        )
        res.response_id = resp.id
        while resp.status in ("queued", "in_progress") and time.time() - t0 < timeout_seconds:
            time.sleep(POLL_SECONDS)
            resp = client.responses.retrieve(resp.id)
        res.latency_seconds = round(time.time() - t0, 1)
        res.status = resp.status or "unknown"
        usage = getattr(resp, "usage", None)
        if usage is not None:
            res.usage = {
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None),
                "total_tokens": usage.total_tokens,
            }
            res.cost = astra_cost(res.usage)
        if resp.status == "incomplete":
            res.error = f"incomplete: {getattr(getattr(resp, 'incomplete_details', None), 'reason', None)}"
        elif resp.status == "failed":
            res.error = f"failed: {getattr(getattr(resp, 'error', None), 'message', None)}"
        out_text = getattr(resp, "output_text", "") or ""
        res.raw_text_head = out_text[:400]
        if out_text:
            try:
                res.output = json.loads(out_text)
            except json.JSONDecodeError as e:
                res.error = (res.error or "") + f" invalid JSON: {e}"
        if resp.status in ("queued", "in_progress"):
            res.error = "timed out waiting for the background response"
    except Exception as e:  # noqa: BLE001
        res.latency_seconds = round(time.time() - t0, 1)
        res.status = "error"
        res.error = f"{type(e).__name__}: {str(e)[:800]}"
    return res
