"""Client for AIsa, the radar's one paid dependency: its tool router (X posts, Google
Trends and whois data through DataForSEO) and its OpenAI-compatible chat endpoint (the
name judge). One key: AISA_API_KEY, or the one the AIsa CLI stores when you log in.

The tool router runs a batch's calls one by one, about 10 s each, so batches are kept to
BATCH calls and the timeout is long. Every call is billed: a failed batch is never
retried here, so one bad day costs one batch at most.

Prices seen in October 2026: an account's last 20 posts $0.003, one Trends task $0.0012,
one whois overview query about $0.13.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import UA

ROUTER = os.environ.get("AISA_ROUTER_BASE_URL", "https://tools.aisa.one") + "/v1/tool-router/aisa-batch-use"
CHAT = os.environ.get("AISA_API_BASE_URL", "https://api.aisa.one") + "/v1/chat/completions"
# DataForSEO through AIsa's REST passthrough. The tool router checks arguments against a schema that types
# `filters` as objects, so DataForSEO's own filter syntax ([field, op, value] arrays joined by "and") is refused
# there (whois overview, backlinks; reported to AIsa on 2026-10-15). The passthrough takes it as DataForSEO does.
REST = os.environ.get("AISA_API_BASE_URL", "https://api.aisa.one") + "/apis/v1/dataforseo/"
MAX_CALLS = 20      # the router's own limit per batch
BATCH = 5           # what call_all sends at once, about 50 s a batch
TIMEOUT = 240
CHAT_TIMEOUT = 90


class AisaError(RuntimeError):
    pass


def key() -> str:
    """AISA_API_KEY, else the key the AIsa CLI stored at login (~/.aisa)."""
    k = os.environ.get("AISA_API_KEY", "").strip()
    if k:
        return k
    home = Path.home() / ".aisa"
    try:
        if (home / "key").exists():
            return (home / "key").read_text().strip()
        if (home / "tokens.json").exists():
            return json.loads((home / "tokens.json").read_text()).get("accessToken", "") or _no_key()
    except (OSError, ValueError):
        pass
    return _no_key()


def _no_key() -> str:
    raise AisaError("no AIsa key: set AISA_API_KEY, or log in with the AIsa CLI (`aisa login`)")


def _post(url: str, body, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key()}",
        "User-Agent": UA, "x-aisa-source": "new-term-radar"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        raise AisaError(f"HTTP {exc.code}: {' '.join(exc.read().decode('utf-8', 'replace').split())[:200]}") from exc
    except (OSError, ValueError) as exc:
        raise AisaError(f"{type(exc).__name__}: {exc}") from exc


def call(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """Run one batch of tool calls. Returns ({call_id: data}, cost in micro-USD). Calls
    that failed are left out of the dict; only a failed request raises."""
    if not calls:
        return {}, 0
    if len(calls) > MAX_CALLS:
        raise AisaError(f"{len(calls)} calls in one batch, the cap is {MAX_CALLS}")
    body = _post(ROUTER, {"calls": calls}, TIMEOUT)
    if "error" in body:
        raise AisaError(str(body["error"].get("message") or body["error"])[:200])
    out, cost = {}, 0
    for r in body.get("results", []):
        cost += int(r.get("customer_cost_micros_usd") or 0)
        if r.get("successful"):
            out[r["call_id"]] = r.get("data") or {}
    return out, cost


def call_all(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """call() in batches of BATCH. A failed batch raises and stops the rest."""
    out, cost = {}, 0
    for i in range(0, len(calls), BATCH):
        data, c = call(calls[i:i + BATCH])
        out.update(data)
        cost += c
    return out, cost


def rest(path: str, task: dict) -> tuple[dict, int]:
    """One DataForSEO live task through the passthrough: (its first result, cost in micro-USD)."""
    body = _post(REST + path, [task], TIMEOUT)
    t = (body.get("tasks") or [{}])[0]
    if body.get("status_code") != 20000 or t.get("status_code") != 20000:
        raise AisaError(str(t.get("status_message") or body.get("status_message") or body)[:200])
    return (t.get("result") or [{}])[0] or {}, int(round(float(body.get("cost") or t.get("cost") or 0) * 1e6))


# Rates in micro-USD per token (1 USD = 1e6 micro-USD; so $3/1M tokens = 3.0 micro-USD/token).
CHAT_RATES: dict[str, tuple[float, float]] = {
    "sonnet": (3.0, 15.0),
    "haiku": (0.8, 4.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o-mini": (0.15, 0.6),
}
DEFAULT_CHAT_RATE = (0.4, 1.6)
_CHAT_COST_MICROS = 0


def estimate_chat_cost(model: str, body: dict, prompt_chars: int = 0, answer_chars: int = 0) -> int:
    """Micro-USD for one chat completion, from explicit cost, usage tokens, or char estimate."""
    for k in ("customer_cost_micros_usd", "cost_micros"):
        if body.get(k) is not None:
            return int(body[k])
    if body.get("cost") is not None:
        return int(round(float(body["cost"]) * 1e6))
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    if usage.get("cost") is not None:
        return int(round(float(usage["cost"]) * 1e6))
    in_rate, out_rate = next((r for tag, r in CHAT_RATES.items() if tag in (model or "").lower()), DEFAULT_CHAT_RATE)
    in_tok = int(usage.get("prompt_tokens") or (max(1, prompt_chars // 4) if prompt_chars else 0))
    out_tok = int(usage.get("completion_tokens") or (max(1, answer_chars // 4) if answer_chars else 0))
    return int(round(in_tok * in_rate + out_tok * out_rate))


def take_chat_cost() -> int:
    """Returns and resets the accumulated chat cost in micro-USD."""
    global _CHAT_COST_MICROS
    spent, _CHAT_COST_MICROS = _CHAT_COST_MICROS, 0
    return spent


def chat(model: str, system: str, user: str, max_tokens: int = 1500) -> str:
    global _CHAT_COST_MICROS
    body = _post(CHAT, {"model": model, "temperature": 0, "max_tokens": max_tokens, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}]}, CHAT_TIMEOUT)
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AisaError(f"unexpected chat answer: {str(body)[:160]}") from exc
    _CHAT_COST_MICROS += estimate_chat_cost(model, body, len(system) + len(user), len(text))
    return text
