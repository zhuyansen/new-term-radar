"""Is this term the name of a new thing, or a common word?

The signals surface whatever is new in the text they read: "Opus 5.5" and "HyperFrames",
but also "Lots", "Trained" and "voice notes". A small model reads each term with a few
places it appeared and scores it from 0 to 1; the report leads with the names.

One chat call per CHUNK terms through AIsa's OpenAI-compatible endpoint, the model set in
config.json (judge.model). Without a key, or when the call fails, the terms keep no
score and the report lists them all: the judge sorts the report, it never blocks it.
"""
from __future__ import annotations

import json
import re

from . import aisa

CHUNK = 25
SYSTEM = (
    "You sort terms for a radar that looks for new product names. For each term, score from 0.0 to 1.0 how likely "
    "it is the NAME of one specific product, model, tool, library, company or technique, as opposed to a common "
    "word, a generic category, a feature description or a fragment of a sentence. `examples` are places where the "
    "term appeared (posts, repository descriptions, model ids, URL paths); use them.\n"
    "Names: 'opus 5.5', 'hyperframes', 'seedance', 'qwen image 2.1', 'strata' (when the examples show a project "
    "called Strata).\n"
    "Not names: 'dashboard', 'voice notes', 'pdf', 'lots', 'trained', 'video generator', 'new release', 'today'.\n"
    "A known company or product family alone ('google', 'claude') is a name, but an old one: score it 0.5.\n"
    "Answer with JSON only, one object per term, in the same order: [{\"i\": 0, \"score\": 0.9}, ...]"
)


def parse(answer: str, n: int) -> dict[int, float]:
    """{index: score} from the model's answer; indexes outside 0..n-1 and bad scores are dropped."""
    m = re.search(r"\[.*\]", answer, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        return {}
    out = {}
    for it in items if isinstance(items, list) else []:
        try:
            i, score = int(it["i"]), float(it["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n and 0.0 <= score <= 1.0:
            out[i] = score
    return out


def score(rows: list[dict], model: str) -> dict:
    """Sets r["named"] on each row the model scored. Returns {"judged": n} or {"error": ...}."""
    if not model:
        return {"judged": 0, "skipped": True}
    judged = 0
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start:start + CHUNK]
        user = json.dumps([{"i": i, "term": r["term"], "examples": [e[:160] for e in r.get("examples", [])[:3]]}
                           for i, r in enumerate(chunk)], ensure_ascii=False)
        try:
            scores = parse(aisa.chat(model, SYSTEM, user), len(chunk))
        except aisa.AisaError as exc:
            spent = aisa.take_chat_cost()
            return {"judged": judged, "error": str(exc)[:160], **({"cost_micros": spent} if spent else {})}
        for i, s in scores.items():
            chunk[i]["named"] = s
        judged += len(scores)
    spent = aisa.take_chat_cost()
    return {"judged": judged, **({"cost_micros": spent} if spent else {})}
