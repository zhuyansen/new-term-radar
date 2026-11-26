"""Where the radar's paid data and its model come from.

Data: through Glasser when GLASSER_API_KEY is set, for every call it sells (glasser.py), else through AIsa.
AIsa answers the rest: X posts (Glasser does not sell AIsa's X source) and Google searches with operators
(site:). Both hand back the provider's own answer, so a caller reads the same data whichever answered, and
the cost of either goes into the same totals.

Model (the name judge and the other small sorting jobs): any OpenAI-compatible endpoint when
RADAR_LLM_BASE_URL is set (RADAR_LLM_API_KEY, and RADAR_LLM_MODEL in place of the model config.json names),
else AIsa's chat. Glasser sells no model calls. With neither, chat() raises NoLLM, which the judge reads as
"skipped" and the report says so.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import UA, aisa, glasser
from .aisa import AisaError

LLM_TIMEOUT = aisa.CHAT_TIMEOUT
_LLM_COST_MICROS = 0


class NoLLM(AisaError):
    """No model to ask: neither RADAR_LLM_BASE_URL nor an AIsa key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AisaError("model endpoint redirect refused: the key stays where RADAR_LLM_BASE_URL points")


_OPENER = urllib.request.build_opener(_NoRedirect())


def _aisa_key() -> str:
    try:
        return aisa.key()
    except AisaError:
        return ""


def name() -> str:
    """Who answers the paid data calls this run, for the report: "Glasser + AIsa", "Glasser" or "AIsa"."""
    if glasser.key():
        return "Glasser + AIsa" if _aisa_key() else "Glasser"
    return "AIsa"


def _need_aisa(calls: list[dict]) -> None:
    """A clear error, before anything is bought, when AIsa must answer and there is no AIsa key."""
    if _aisa_key():
        return
    if glasser.key():
        tools = ", ".join(sorted({c["tool"] for c in calls}))
        raise AisaError(f"no AIsa key for {tools[:120]}, which Glasser does not sell: set AISA_API_KEY, "
                        "or log in with the AIsa CLI (`aisa login`)")
    raise AisaError("no key for the paid data: set GLASSER_API_KEY, or AISA_API_KEY (or log in with the AIsa CLI, `aisa login`)")


def call(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """One request of router calls ({"call_id", "tool", "arguments"}). Returns ({call_id: data}, cost in
    micro-USD). Calls that failed are left out; a request that failed raises an AisaError."""
    if not calls:
        return {}, 0
    to_glasser = [c for c in calls if glasser.key() and glasser.sells(c["tool"], c.get("arguments") or {})]
    to_aisa = [c for c in calls if c not in to_glasser]
    out, cost = {}, 0
    if to_aisa:                     # first: a missing AIsa key stops the request before Glasser bills any of it
        _need_aisa(to_aisa)
        out, cost = aisa.call(to_aisa)
    if to_glasser:
        got, spent = glasser.call(to_glasser)
        out, cost = {**out, **got}, cost + spent
    return out, cost


def call_all(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """call() in requests of aisa.BATCH calls. A failed request raises and stops the rest."""
    out, cost = {}, 0
    for i in range(0, len(calls), aisa.BATCH):
        data, c = call(calls[i:i + aisa.BATCH])
        out.update(data)
        cost += c
    return out, cost


def rest(path: str, task: dict) -> tuple[dict, int]:
    """One DataForSEO live task by its path: (its first result, cost in micro-USD)."""
    if glasser.key() and path in glasser.REST:
        return glasser.rest(path, task)
    _need_aisa([{"tool": path}])
    return aisa.rest(path, task)


def llm() -> str:
    """Which model answers chat(): "RADAR_LLM_BASE_URL", "AIsa", or "" for none."""
    if os.environ.get("RADAR_LLM_BASE_URL", "").strip():
        return "RADAR_LLM_BASE_URL"
    return "AIsa" if _aisa_key() else ""


def chat(model: str, system: str, user: str, max_tokens: int = 1500) -> str:
    """One chat completion from the model source llm() names. The model of RADAR_LLM_MODEL, when set,
    replaces `model` on that endpoint."""
    source = llm()
    if source == "AIsa":
        return aisa.chat(model, system, user, max_tokens)
    if not source:
        raise NoLLM("no model to ask: set RADAR_LLM_BASE_URL (any OpenAI-compatible endpoint, with RADAR_LLM_API_KEY "
                    "and RADAR_LLM_MODEL) or AISA_API_KEY")
    return _openai_chat(os.environ.get("RADAR_LLM_MODEL", "").strip() or model, system, user, max_tokens)


def _endpoint() -> str:
    base = os.environ["RADAR_LLM_BASE_URL"].strip().rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _openai_chat(model: str, system: str, user: str, max_tokens: int) -> str:
    global _LLM_COST_MICROS
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    llm_key = os.environ.get("RADAR_LLM_API_KEY", "").strip()
    if llm_key:
        headers["Authorization"] = f"Bearer {llm_key}"
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(_endpoint(), data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=LLM_TIMEOUT) as res:              # no redirects: the key stays where it was sent
            answer = json.load(res)
    except urllib.error.HTTPError as exc:
        raise AisaError(f"model HTTP {exc.code}: {' '.join(exc.read().decode('utf-8', 'replace').split())[:200]}") from None
    except (OSError, ValueError, AisaError) as exc:
        raise AisaError(f"model {type(exc).__name__}: {str(exc)[:160]}") from None
    try:
        text = answer["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AisaError(f"unexpected chat answer: {str(answer)[:160]}") from exc
    _LLM_COST_MICROS += aisa.estimate_chat_cost(model, answer, len(system) + len(user), len(text))
    return text


def take_chat_cost() -> int:
    """Returns and resets what the chat calls cost so far, in micro-USD (AIsa's and the endpoint's)."""
    global _LLM_COST_MICROS
    spent, _LLM_COST_MICROS = _LLM_COST_MICROS, 0
    return spent + aisa.take_chat_cost()
