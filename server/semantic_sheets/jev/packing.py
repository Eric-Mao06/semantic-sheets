"""Turn plan questions and projected rows into Jev requests, and interpret answers.

Row isolation: every question is tied to one row through an explicit row reference. Cache keys exclude
thresholds and packet neighbours, so threshold edits and repacking reuse raw outputs.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from ..db import canonical_json, sha256_text
from ..plan.compile import ROW_OVERHEAD_TOKENS, TOKENS_PER_CHAR
from ..plan.schema import (
    PROMPT_LAYOUT_VERSION,
    BooleanQuestion,
    CategoryQuestion,
    ScoreQuestion,
    SemanticMatch,
)

QuestionT = BooleanQuestion | CategoryQuestion | ScoreQuestion


def normalize_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return v
    if isinstance(v, (int, bool)):
        return v
    return str(v)


def project_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    return {c: normalize_value(row.get(c)) for c in columns}


def row_is_missing(projected: dict[str, Any]) -> bool:
    return all(v is None for v in projected.values())


def row_tokens(projected: dict[str, Any]) -> int:
    return int(len(json.dumps(projected, ensure_ascii=False)) * TOKENS_PER_CHAR) + ROW_OVERHEAD_TOKENS


def question_spec(q: QuestionT) -> dict[str, Any]:
    """The part of a question that changes the model's answer (thresholds excluded)."""
    if q.kind == "boolean":
        return {"kind": "boolean", "instruction": q.instruction, "criteria": q.criteria}
    if q.kind == "category":
        return {"kind": "category", "instruction": q.instruction,
                "labels": [{"name": l.name, "description": l.description} for l in q.effective_labels()]}
    return {"kind": "score", "instruction": q.instruction, "levels": list(q.levels)}


def cache_key(workspace_id: str, model: str, spec: dict[str, Any], projected: dict[str, Any]) -> str:
    return sha256_text(canonical_json({
        "ws": workspace_id, "model": model, "layout": PROMPT_LAYOUT_VERSION, "q": spec, "input": projected}))


def jev_question(q: QuestionT, prefix: str = "") -> dict[str, Any]:
    instr = prefix + q.instruction
    if q.kind == "boolean":
        d: dict[str, Any] = {"type": "noul", "instructions": instr}
        if q.criteria:
            d["criteria"] = {k: v for k, v in q.criteria.items() if v is not None}
        return d
    if q.kind == "category":
        return {"type": "choice", "instructions": instr,
                "criteria": {l.name: l.description for l in q.effective_labels()}}
    return {"type": "score", "instructions": instr, "criteria": list(q.levels)}


@dataclass
class Packet:
    rows: list[tuple[int, dict[str, Any]]]  # (row_id, projected)
    est_tokens: int = 0


def make_packets(rows: list[tuple[int, dict[str, Any]]], q_tokens: int, pack_rows: int, state_budget: int,
                 total_budget: int) -> tuple[list[Packet], list[int]]:
    """Greedy token-aware packing. Returns packets and row ids that cannot fit even alone."""
    packets: list[Packet] = []
    too_long: list[int] = []
    cur = Packet(rows=[])
    for rid, proj in rows:
        rt = row_tokens(proj)
        if rt > state_budget or rt + q_tokens > total_budget:
            too_long.append(rid)
            continue
        if cur.rows and (len(cur.rows) >= pack_rows or cur.est_tokens + rt + q_tokens > total_budget
                         or cur.est_tokens + rt > state_budget):
            packets.append(cur)
            cur = Packet(rows=[])
        cur.rows.append((rid, proj))
        cur.est_tokens += rt + q_tokens
    if cur.rows:
        packets.append(cur)
    return packets, too_long


def build_request(packet: Packet, questions: list[QuestionT]) -> tuple[Any, dict[str, dict[str, Any]], dict[str, tuple[int, str]]]:
    """Return (state, jev_questions, key_map) where key_map maps question keys to (row_id, question name)."""
    key_map: dict[str, tuple[int, str]] = {}
    if len(packet.rows) == 1:
        rid, proj = packet.rows[0]
        state = {"row": "R0", **proj}
        qs = {}
        for q in questions:
            qs[f"R0.{q.name}"] = jev_question(q, "Consider only the row with row=R0. ")
            key_map[f"R0.{q.name}"] = (rid, q.name)
        return state, qs, key_map
    state_rows = []
    qs = {}
    for i, (rid, proj) in enumerate(packet.rows):
        ref = f"R{i}"
        state_rows.append({"row": ref, **proj})
        for q in questions:
            key = f"{ref}.{q.name}"
            qs[key] = jev_question(q, f"Consider only the row with row={ref} and ignore every other row. ")
            key_map[key] = (rid, q.name)
    return {"rows": state_rows}, qs, key_map


def interpret(q: QuestionT, raw: dict[str, Any] | None, *, status: str | None = None, error: str | None = None) -> dict[str, Any]:
    """Map a raw answer to the question's output columns. Raw output is always retained."""
    n = q.name
    if raw is None:
        st = status or ("failed" if error else "missing_input")
        out = {f"{n}.value": None, f"{n}.status": st, f"{n}.raw": json.dumps({"error": error}) if error else None}
        if q.kind == "boolean":
            out[f"{n}.p"] = None
        elif q.kind == "category":
            out[f"{n}.confidence"] = None
        else:
            out[f"{n}.score"] = None
            out[f"{n}.confidence"] = None
        return out
    rawjson = json.dumps(raw, ensure_ascii=False)
    if q.kind == "boolean":
        p = raw.get("noul")
        if p is None:
            return interpret(q, None, error="malformed answer")
        p = float(p)
        if p >= q.thresholds.true_min:
            value, st = True, "ok"
        elif p <= q.thresholds.false_max:
            value, st = False, "ok"
        else:
            value, st = None, "uncertain"
        return {f"{n}.value": value, f"{n}.p": p, f"{n}.status": st, f"{n}.raw": rawjson}
    if q.kind == "category":
        choice = raw.get("choice")
        if choice is None:
            return interpret(q, None, error="malformed answer")
        conf = raw.get("confidence")
        conf = float(conf) if conf is not None else None
        st = "ok"
        if choice == "insufficient_evidence" or (conf is not None and conf < q.min_confidence):
            st = "uncertain"
        return {f"{n}.value": str(choice), f"{n}.confidence": conf, f"{n}.status": st, f"{n}.raw": rawjson}
    score = raw.get("score")
    if score is None:
        return interpret(q, None, error="malformed answer")
    score = float(score)
    idx = max(0, min(len(q.levels) - 1, int(round(score))))
    conf = raw.get("confidence")
    return {f"{n}.score": score, f"{n}.value": q.levels[idx], f"{n}.confidence": float(conf) if conf is not None else None,
            f"{n}.status": "ok", f"{n}.raw": rawjson}


# ---- semantic match -------------------------------------------------------------------------------------

def match_spec(step: SemanticMatch) -> dict[str, Any]:
    return {"kind": "match", "instruction": step.instruction, "left_columns": step.left_columns,
            "right_columns": step.right_columns}


def match_pair_input(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {"left": left, "right": right}


def build_match_request(pairs: list[tuple[str, dict[str, Any]]], instruction: str) -> tuple[Any, dict[str, dict[str, Any]]]:
    """pairs: (key, {"left": {...}, "right": {...}}). One noul question per pair."""
    if len(pairs) == 1:
        key, pair = pairs[0]
        state = {"pair": "P0", "left": pair["left"], "right": pair["right"]}
        return state, {f"P0": {"type": "noul", "instructions": "Consider only the pair with pair=P0. " + instruction}}
    state_pairs = []
    qs = {}
    for i, (key, pair) in enumerate(pairs):
        ref = f"P{i}"
        state_pairs.append({"pair": ref, "left": pair["left"], "right": pair["right"]})
        qs[ref] = {"type": "noul", "instructions": f"Consider only the pair with pair={ref} and ignore every other pair. " + instruction}
    return {"pairs": state_pairs}, qs
