"""One-shot baseline: hand the whole CSV and the same request to gpt-6-astra (reasoning effort high) and ask for
the final answer directly as JSON. One request per scenario, in two variants:

- ``text``  (default): no tools, no code execution. The model reads the CSV in its context and answers.
- ``tools``: the same inline CSV and prompt, plus the hosted code interpreter with the CSV files mounted in the
  container, so the model can run Python for the exact work (counting, summing, joining) while still reading
  every row for the semantic judgements. Still a single Responses API request; the tool loop runs server-side."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

from common import ASTRA_MODEL, ASTRA_REASONING, CONTAINER_MINIMUM_SECONDS, CONTAINER_SESSION_SECONDS, CONTAINER_SESSION_USD, astra_cost, estimate_tokens
from scenarios import Prepared, Scenario

INSTRUCTIONS = """You are given one or more complete data tables as CSV text and a request from a spreadsheet user.
Carry out the request yourself over every row of the data (read every row; do not sample, skip or truncate).
Judge meaning, not keywords. Where the request needs numbers (counts, sums, prices), compute them exactly from the data.
Refer to rows only by the values in their identifier columns (row_id, offer_id, catalog_id, listing_id, stock_code) exactly as given.
Respond with a single JSON object that matches the required schema, and nothing else."""

TOOLS_ADDENDUM = """
You also have a Python code interpreter. The same tables are mounted in it as CSV files (the exact path is given with
each table). Use code for anything exact: counting, summing, grouping, joining, sorting and assembling the final JSON. Semantic judgements (what a message means, whether two titles are the same product, which category fits)
are yours to make by reading the rows; do not reduce them to keyword matching in code."""

VARIANTS = ("text", "tools")
MAX_OUTPUT_TOKENS = 120_000
POLL_SECONDS = 5


@dataclass
class OneShotResult:
    model: str = ASTRA_MODEL
    reasoning_effort: str = ASTRA_REASONING
    variant: str = "text"
    status: str = "not_run"
    output: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    latency_seconds: float | None = None
    input_tokens_estimate: int | None = None
    response_id: str | None = None
    error: str | None = None
    raw_text_head: str | None = None
    tool_calls: int = 0
    tool_trace: list[dict[str, Any]] = field(default_factory=list)  # code the model ran, in order (heads only)
    container_id: str | None = None
    file_ids: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["output_summary"] = _summarize_output(self.output)
        d.pop("output")
        return d


def _summarize_output(out: dict[str, Any] | None) -> dict[str, Any] | None:
    if not out:
        return None
    return {k: (f"list[{len(v)}]" if isinstance(v, list) else v) for k, v in out.items()}


def oneshot_filename(variant: str) -> str:
    return "oneshot.json" if variant == "text" else f"oneshot_{variant}.json"


def build_input(scenario: Scenario, prep: Prepared, mounts: dict[str, str] | None = None) -> str:
    """mounts: table name -> path inside the code interpreter container (tools variant only)."""
    parts = [f"Request: {scenario.prompt}", f"Output: {scenario.oneshot_hint}"]
    for t in prep.tables:
        mount = f"; the same file is mounted in the code interpreter at `{mounts[t.name]}`" if mounts else ""
        parts.append(f"Table `{t.name}` ({t.row_count} rows; columns: {', '.join(t.columns)}{mount}), CSV follows:\n```csv\n{t.path.read_text(encoding='utf-8')}\n```")
    return "\n\n".join(parts)


def container_path(file_id: str, filename: str) -> str:
    # The hosted container exposes uploaded files as /mnt/data/<file_id>-<original filename>.
    return f"/mnt/data/{file_id}-{filename}"


def _upload_tables(client: OpenAI, prep: Prepared) -> list[str]:
    ids = []
    for t in prep.tables:
        with open(t.path, "rb") as fh:
            ids.append(client.files.create(file=fh, purpose="user_data").id)
    return ids


def _usage_dict(usage: Any) -> dict[str, Any]:
    ind = getattr(usage, "input_tokens_details", None)
    outd = getattr(usage, "output_tokens_details", None)
    to_dict = lambda o: (o.model_dump() if hasattr(o, "model_dump") else dict(o)) if o is not None else {}  # noqa: E731
    ind_d, outd_d = to_dict(ind), to_dict(outd)
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": ind_d.get("cached_tokens") or 0,
        "cache_write_tokens": ind_d.get("cache_write_tokens") or ind_d.get("cache_creation_tokens") or 0,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": outd_d.get("reasoning_tokens"),
        "total_tokens": usage.total_tokens,
        "input_tokens_details": ind_d,
        "output_tokens_details": outd_d,
    }


def _tool_trace(resp: Any) -> tuple[int, list[dict[str, Any]], str | None]:
    n = 0
    trace: list[dict[str, Any]] = []
    container = None
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) != "code_interpreter_call":
            continue
        n += 1
        container = getattr(item, "container_id", None) or container
        outs = []
        for o in getattr(item, "outputs", None) or []:
            if getattr(o, "type", None) == "logs":
                outs.append(str(getattr(o, "logs", ""))[:400])
            else:
                outs.append(f"<{getattr(o, 'type', 'output')}>")
        code = getattr(item, "code", None) or ""
        trace.append({"code": code[:1200], "code_chars": len(code), "outputs": outs[:4], "status": getattr(item, "status", None)})
    return n, trace, container


def container_cost(latency_seconds: float | None) -> dict[str, Any]:
    """Hosted code interpreter: one 1 GB container per response, billed per CONTAINER_SESSION_SECONDS with a
    CONTAINER_MINIMUM_SECONDS minimum. The container lives at most as long as the response, so latency bounds it."""
    secs = max(float(latency_seconds or 0.0), CONTAINER_MINIMUM_SECONDS)
    sessions = max(1, math.ceil(secs / CONTAINER_SESSION_SECONDS))
    return {"usd": round(sessions * CONTAINER_SESSION_USD, 6), "sessions": sessions, "session_usd": CONTAINER_SESSION_USD, "billed_seconds": secs}


def run_oneshot(scenario: Scenario, prep: Prepared, variant: str = "text", timeout_seconds: int = 3 * 3600) -> OneShotResult:
    if variant not in VARIANTS:
        raise ValueError(f"unknown one-shot variant {variant!r}; known: {VARIANTS}")
    res = OneShotResult(variant=variant)
    instructions = INSTRUCTIONS + (TOOLS_ADDENDUM if variant == "tools" else "")
    client = OpenAI(timeout=600, max_retries=2)
    t0 = time.time()
    try:
        kwargs: dict[str, Any] = {}
        mounts = None
        if variant == "tools":
            res.file_ids = _upload_tables(client, prep)
            mounts = {t.name: container_path(fid, t.path.name) for t, fid in zip(prep.tables, res.file_ids)}
            kwargs["tools"] = [{"type": "code_interpreter", "container": {"type": "auto", "file_ids": res.file_ids}}]
            kwargs["tool_choice"] = "auto"
            kwargs["include"] = ["code_interpreter_call.outputs"]
        text = build_input(scenario, prep, mounts)
        res.input_tokens_estimate = estimate_tokens(text) + estimate_tokens(instructions)
        resp = client.responses.create(
            model=ASTRA_MODEL,
            reasoning={"effort": ASTRA_REASONING},
            instructions=instructions,
            input=text,
            text={"format": {"type": "json_schema", "name": f"{scenario.key}_answer", "schema": scenario.oneshot_schema, "strict": True}},
            max_output_tokens=MAX_OUTPUT_TOKENS,
            background=True,
            store=True,
            **kwargs,
        )
        res.response_id = resp.id
        while resp.status in ("queued", "in_progress") and time.time() - t0 < timeout_seconds:
            time.sleep(POLL_SECONDS)
            resp = client.responses.retrieve(resp.id, **({"include": kwargs["include"]} if "include" in kwargs else {}))
        res.latency_seconds = round(time.time() - t0, 1)
        res.status = resp.status or "unknown"
        if variant == "tools":
            res.tool_calls, res.tool_trace, res.container_id = _tool_trace(resp)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            res.usage = _usage_dict(usage)
            res.cost = astra_cost(res.usage, turns=res.tool_calls + 1)
        if variant == "tools":
            cc = container_cost(res.latency_seconds)
            res.cost = {**res.cost, "model_usd": res.cost.get("usd"), "container": cc, "usd": round((res.cost.get("usd") or 0.0) + cc["usd"], 6)}
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


def load_result(path: Path) -> OneShotResult | None:
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))
    d.pop("output_summary", None)
    return OneShotResult(**{k: v for k, v in d.items() if k in OneShotResult.__dataclass_fields__})
