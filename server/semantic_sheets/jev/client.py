"""Jev (TypeSafe System One) client: retries with backoff, provider retry signals, shared rate limiting."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings


class JevError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, request_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.request_id = request_id


@dataclass
class JevResponse:
    answers: dict[str, dict[str, Any]]
    model: str
    input_tokens: int
    output_tokens: int
    request_id: str | None
    latency_ms: float
    attempts: int


class RateLimiter:
    """Token buckets for requests per minute and input tokens per second (one process; share via config)."""

    def __init__(self, rpm: int, tps: int):
        self.rpm = max(1, rpm)
        self.tps = max(1, tps)
        self._req_tokens = float(self.rpm)
        self._tok_tokens = float(self.tps)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        self._req_tokens = min(self.rpm, self._req_tokens + dt * self.rpm / 60.0)
        self._tok_tokens = min(self.tps, self._tok_tokens + dt * self.tps)

    async def acquire(self, est_tokens: int) -> None:
        est_tokens = max(1, min(est_tokens, self.tps))
        while True:
            async with self._lock:
                self._refill()
                if self._req_tokens >= 1 and self._tok_tokens >= est_tokens:
                    self._req_tokens -= 1
                    self._tok_tokens -= est_tokens
                    return
                need_req = max(0.0, (1 - self._req_tokens) * 60.0 / self.rpm)
                need_tok = max(0.0, (est_tokens - self._tok_tokens) / self.tps)
                wait = max(need_req, need_tok, 0.005)
            await asyncio.sleep(min(wait, 2.0))


_limiter: RateLimiter | None = None


def limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        cfg = settings()
        _limiter = RateLimiter(cfg.jev_requests_per_minute, cfg.jev_tokens_per_second)
    return _limiter


class JevClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        cfg = settings()
        self.api_key = api_key or cfg.typesafe_api_key
        self.base_url = (base_url or cfg.typesafe_base_url).rstrip("/")
        self.model = model or cfg.jev_model
        self.timeout = cfg.jev_timeout_seconds
        self.max_retries = cfg.jev_max_retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "JevClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, http2=False)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def system_one(self, state: Any, questions: dict[str, dict[str, Any]], *, est_tokens: int = 500,
                         model: str | None = None) -> JevResponse:
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY is not configured", retryable=False)
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        body = {"state": state, "model": model or self.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        attempt = 0
        t0 = time.monotonic()
        while True:
            attempt += 1
            await limiter().acquire(est_tokens)
            try:
                resp = await self._client.post(f"{self.base_url}/v1/systemone", json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt > self.max_retries:
                    raise JevError(f"provider unreachable: {e}", retryable=True) from e
                await asyncio.sleep(_backoff(attempt, None))
                continue
            rid = resp.headers.get("x-typesafe-request-id")
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage") or {}
                return JevResponse(
                    answers=data.get("answers", {}), model=data.get("model", body["model"]),
                    input_tokens=int(usage.get("input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0),
                    request_id=rid, latency_ms=(time.monotonic() - t0) * 1000, attempts=attempt)
            retryable = resp.status_code in (408, 429, 500, 502, 503, 504, 529)
            text = resp.text[:300]
            if retryable and attempt <= self.max_retries:
                await asyncio.sleep(_backoff(attempt, resp.headers))
                continue
            raise JevError(f"provider error {resp.status_code}: {text}", status=resp.status_code,
                           retryable=retryable, request_id=rid)


def _backoff(attempt: int, headers: Any) -> float:
    if headers is not None:
        ms = headers.get("retry-after-ms")
        if ms:
            try:
                return min(30.0, float(ms) / 1000.0)
            except ValueError:
                pass
        s = headers.get("retry-after")
        if s:
            try:
                return min(30.0, float(s))
            except ValueError:
                pass
    base = min(8.0, 0.5 * (2 ** (attempt - 1)))
    return base * (0.5 + random.random() * 0.5)


class FakeJevClient(JevClient):
    """Deterministic stand-in for tests and offline demos. Keyword overlap drives the answers."""

    def __init__(self, *args: Any, latency: float = 0.0, fail_every: int = 0, **kwargs: Any):
        super().__init__(api_key="fake", *args, **kwargs)
        self.latency = latency
        self.fail_every = fail_every
        self.calls = 0

    async def __aenter__(self) -> "FakeJevClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def system_one(self, state: Any, questions: dict[str, dict[str, Any]], *, est_tokens: int = 500,
                         model: str | None = None) -> JevResponse:
        self.calls += 1
        if self.fail_every and self.calls % self.fail_every == 0:
            raise JevError("simulated provider outage", status=503, retryable=True)
        if self.latency:
            await asyncio.sleep(self.latency)
        rows = _state_rows(state)
        answers: dict[str, dict[str, Any]] = {}
        for key, qd in questions.items():
            row_key = key.split(".", 1)[0] if "." in key and key.startswith("R") else None
            text = rows.get(row_key, "") if row_key else " ".join(rows.values())
            answers[key] = _fake_answer(qd, text)
        tokens = int(len(json.dumps(state)) * 0.28) + 40 * len(questions)
        return JevResponse(answers=answers, model="jev-fake", input_tokens=tokens, output_tokens=len(questions) * 10,
                           request_id=f"fake_{self.calls}", latency_ms=1.0, attempts=1)


def _state_rows(state: Any) -> dict[str, str]:
    if isinstance(state, dict) and isinstance(state.get("rows"), list):
        out = {}
        for r in state["rows"]:
            rid = str(r.get("row"))
            out[rid] = " ".join(str(v) for k, v in r.items() if k != "row" and v is not None)
        return out
    if isinstance(state, dict):
        return {"R0": " ".join(str(v) for v in state.values() if v is not None)}
    return {"R0": str(state)}


_WORD = re.compile(r"[a-z]{4,}")
STOP = {"does", "this", "that", "with", "from", "text", "about", "which", "what", "have", "their", "there", "into",
        "customer", "describe", "describes", "message", "consider", "only", "ignore", "other", "rows", "row", "every", "pair",
        "pairs", "left", "right"}


def _words(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in STOP}


def _fake_answer(qd: dict[str, Any], text: str) -> dict[str, Any]:
    t = _words(text)
    instr = qd.get("instructions") or ""
    if isinstance(instr, dict):
        instr = json.dumps(instr)
    kind = qd.get("type")
    if kind == "noul":
        crit = qd.get("criteria") or {}
        hint = _words(str(crit.get("true") or "")) | _words(instr)
        p = 0.97 if t & hint else 0.03
        return {"type": "noul", "noul": p}
    if kind == "choice":
        crit = qd.get("criteria") or {}
        best, best_n = None, 0
        for label, desc in crit.items():
            n = len(t & (_words(label.replace("_", " ")) | _words(str(desc or ""))))
            if n > best_n:
                best, best_n = label, n
        if best is None:
            best = "other" if "other" in crit else next(iter(crit))
        probs = {l: (0.9 if l == best else 0.1 / max(1, len(crit) - 1)) for l in crit}
        return {"type": "choice", "choice": best, "confidence": 0.9 if best_n else 0.5, "probabilities": probs}
    if kind == "score":
        levels = qd.get("criteria") or []
        n = len(levels)
        h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        score = (h % (n * 10)) / 10.0
        idx = min(n - 1, int(round(score)))
        probs = {str(i): (0.8 if i == idx else 0.2 / max(1, n - 1)) for i in range(n)}
        return {"type": "score", "score": round(score, 2), "confidence": 0.8,
                "legend": {str(i): l for i, l in enumerate(levels)}, "probabilities": probs}
    return {"type": kind or "noul", "noul": 0.5}


def make_client() -> JevClient:
    if settings().fake_jev:
        return FakeJevClient()
    return JevClient()
