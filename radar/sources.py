"""Signal ①, the sources: where a new name is said first.

x_new    names in the last day's posts of the accounts in config.json ("x_accounts",
         read through AIsa) that none of those accounts has used in the last VOCAB_DAYS.
         The vocabulary is kept in the state directory; the first run only builds it.
hf_new   models that entered Hugging Face's trending top HF_TOP since the last run (the
         public API, no key). Quantised and fine-tuned copies of one model are grouped
         under its base name.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import UA, backend, snap
from .i18n import t
from .terms import STOP, _drop_nested, names

LOOKBACK_HOURS = 30          # meant to run every 8 hours; a name enters the vocabulary the run it is
                             # reported, so the overlap repeats nothing and covers missed runs
VOCAB_DAYS = 60
X_MIN_LIKES = 100            # a name only one account used needs this much attention,
PER_POST = 3                 # and must look like a name; one post yields at most this many
HF_TOP, HF_FETCH = 50, 100   # entering the top 50 against the last run's top 100
TOP = 25


def accounts(cfg: dict) -> list[str]:
    x = cfg["x_accounts"]
    return list(dict.fromkeys(x.get("labs", []) + x.get("bloggers", [])))


def _posted(post: dict) -> datetime | None:
    try:
        return datetime.strptime(post["createdAt"], "%a %b %d %H:%M:%S %z %Y")
    except (KeyError, ValueError):
        return None


def novel_names(posts: list[dict], vocab: dict[str, str], now: datetime) -> list[dict]:
    """Names in recent posts that are not in the vocabulary, with who said them.
    `posts` items: {"account", "text", "likes", "url", "created"}."""
    since = now - timedelta(hours=LOOKBACK_HOURS)
    found: dict[str, dict] = {}
    for p in posts:
        if p["created"] is None or p["created"] < since or p["text"].startswith("RT @"):
            continue
        strong = names(p["text"], strong=True)
        for term in names(p["text"]):
            if term in vocab:
                continue
            row = found.setdefault(term, {"term": term, "sources": set(), "likes": 0, "examples": [], "posts": [], "strong": False})
            if p["account"] not in row["sources"]:
                row["likes"] += p["likes"]
                row["examples"].append(f"@{p['account']}: {' '.join(p['text'].split())[:80]}")
                # The whole post and its link, for the mail: the reader checks the source in one click.
                row["posts"].append({"account": p["account"], "text": " ".join(p["text"].split()),
                                     "url": p.get("url", ""), "likes": p["likes"]})
            row["sources"].add(p["account"])
            row["strong"] |= term in strong
    rows = [r for r in found.values()
            if len(r["sources"]) >= 2 or (r["strong"] and r["likes"] >= X_MIN_LIKES)]
    # One viral post must not fill the report: keep its PER_POST longest single-account names.
    per_post: dict[str, int] = {}
    kept = []
    for r in sorted(rows, key=lambda r: (-len(r["sources"]), -len(r["term"]))):
        if len(r["sources"]) == 1:
            post = r["examples"][0]
            if per_post.get(post, 0) >= PER_POST:
                continue
            per_post[post] = per_post.get(post, 0) + 1
        kept.append(r)
    for r in kept:
        r.pop("strong")
        r["recent"] = len(r["sources"])
        r["note"] = t("note_x", n=r["recent"], likes=r["likes"])
        r["sources"] = sorted(r["sources"])
    kept.sort(key=lambda r: (-r["recent"], -r["likes"]))
    return _drop_nested(kept)[:TOP]


def x_new(cfg: dict, state: Path) -> dict:
    handles = accounts(cfg)
    if not handles:
        return {"skipped": True}
    data, cost = backend.call_all([{"call_id": h, "tool": "get_twitter_user_last_tweets", "arguments": {"userName": h}}
                                for h in handles])
    posts = []
    for h in handles:
        for tw in ((data.get(h) or {}).get("data") or {}).get("tweets") or []:
            posts.append({"account": h, "text": tw.get("text") or "", "likes": int(tw.get("likeCount") or 0),
                          "url": tw.get("url", ""), "created": _posted(tw)})
    path = state / "x_vocab.json.gz"
    vocab: dict[str, str] | None = snap.load(path)
    first_run = vocab is None
    rows = [] if first_run else novel_names(posts, vocab, datetime.now(timezone.utc))
    # Every name in every fetched post joins the vocabulary, old posts included, so
    # tomorrow only what is new tomorrow is reported.
    vocab = vocab or {}
    today, cutoff = date.today().isoformat(), (date.today() - timedelta(days=VOCAB_DAYS)).isoformat()
    for p in posts:
        for term in names(p["text"]):
            vocab[term] = today
    if posts:   # a failed fetch keeps yesterday's vocabulary
        snap.save(path, {term: d for term, d in vocab.items() if d >= cutoff})
    return {"accounts": len(handles), "answered": len(data), "posts": len(posts), "cost_micros": cost,
            "first_run": first_run, "terms": rows}


_HF_NOISE = re.compile(r"[-_.](gguf|ggml|awq|gptq|fp8|fp16|bf16|int[48]|mlx|exl2|onnx|uncensored|abliterated|"
                       r"instruct|chat|it|base|preview|turbo|lora|q\d\w*|\d+(\.\d+)?b|a\d+(\.\d+)?b)(?=[-_.]|$)", re.I)


def base_name(model_id: str) -> str:
    """'abenzerps/Qwen-Image-2.1-Uncensored-GGUF' -> 'qwen image 2.1'."""
    name = model_id.split("/")[-1]
    for _ in range(4):   # suffixes stack: -27B-Instruct-GGUF
        name = _HF_NOISE.sub("", name)
    return re.sub(r"[-_]+", " ", name).strip().lower()


SPACE_NOISE = {"demo", "webgpu", "studio", "space", "spaces", "playground", "gradio", "app", "online", "zerogpu", "gpu", "chat"}


def _trending(what: str) -> list[dict]:
    req = urllib.request.Request(f"https://huggingface.co/api/{what}?sort=trendingScore&direction=-1&limit={HF_FETCH}",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as res:
        return json.load(res)


def _entered(items: list[dict], before: list[str], what: str) -> list[dict]:
    """The terms of the items that entered the top HF_TOP since the last run, grouped by base name."""
    seen = set(before)
    grouped: dict[str, dict] = {}
    for rank, m in enumerate(items[:HF_TOP], 1):
        if m["id"] in seen:
            continue
        term = base_name(m["id"])
        if what == "spaces":            # a demo's name minus the demo words: 'lightonocr 3 demo' -> 'lightonocr 3'
            term = " ".join(w for w in term.split() if w not in SPACE_NOISE)
        if not term or term in STOP:
            continue
        g = grouped.setdefault(term, {"term": term, "rank": rank, "copies": 0, "examples": []})
        g["copies"] += 1
        g["examples"].append(f"{m['id']} ({m.get('pipeline_tag') or m.get('sdk') or '?'}, ♥{m.get('likes', 0)})")
    rows = sorted(grouped.values(), key=lambda g: g["rank"])
    for g in rows:
        g["recent"] = g["copies"]
        g["note"] = t("note_hf" if what == "models" else "note_hf_space", rank=g["rank"]) + \
            (t("note_hf_copies", n=g["copies"]) if g["copies"] > 1 else "")
    return rows


def hf_new(cfg: dict, state: Path) -> dict:
    """Models, and Spaces (the demos people try), that entered Hugging Face's trending top HF_TOP since
    the last run. One public request each; each list keeps its own snapshot."""
    out = {"first_run": False, "terms": []}
    for what, file in (("models", "hf_top.json.gz"), ("spaces", "hf_spaces_top.json.gz")):
        items = _trending(what)
        path = state / file
        before = snap.load(path)
        if items:
            snap.save(path, [m["id"] for m in items])
        out[what] = len(items)
        if before is None:
            out["first_run"] = True
            continue
        have = {r["term"]: r for r in out["terms"]}
        for r in _entered(items, before, what):
            if r["term"] in have:       # a model and its demo both new: one lead, both examples
                have[r["term"]]["examples"] += r["examples"]
            else:
                out["terms"].append(r)
    out["terms"] = out["terms"][:TOP]
    return out
