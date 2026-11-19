"""The generic terms behind the names: what people do with a new thing, as search terms.

A name ("SynthID Detector") has a short life as a keyword and belongs to someone. The
job it does ("ai image detector") is what people keep searching, and nobody owns it.
For the day's new names this step:

  1. collects evidence: popular posts that mention the name (AIsa, X advanced search)
     and, for a repository, its README;
  2. asks a model which jobs the evidence shows people doing, and the keyword an
     ordinary person would type for each job. A phrase built on another product's name
     ("photoshop alternative") is kept and marked `brand`: demand often hangs on the
     incumbent, and that is yours to weigh;
  2b. with a GoAnyAPI key (optional), also takes Google Trends' related queries for the
     name: what people really searched together with it. Queries that hold the name are
     its long-tails ("photocraft download"); the others join the phrases; those that are
     themselves names of other new things ("gpt image 2.5" beside "nano banana 2.1") are
     handed back as leads of kind "cosearch". With that key the names and the bigger
     generic terms also take their shape from a 30-day series for one country, which is
     settled to yesterday and shows moves the worldwide series flattens;
  3. screens them: a second call scores how well the name answers each phrase, and the
     loosely related ones are dropped (a keyword taken from a passing comparison);
  4. looks the rest up: Google Ads monthly searches (one batch call);
  5. reads DataForSEO Trends (worldwide, through AIsa) for the names and for the phrases with enough volume, twice
     each: alone, for the shape of the curve (when it started rising, its peak, what it
     is doing now), and next to a benchmark keyword ("gpts" by default), for the size.
     A generic term's searches a day come from its own Google Ads volume, moved to today
     along its own curve. The benchmark is sized from those terms, and a name, which has
     no volume data yet, is sized against the benchmark.

The result is one table, names and generic terms together, each with a decision
(table()). Phrases with measurable volume are also kept as leads of kind "usecase" and
go through the same 14-day review as the names.

What it cannot do: on the day a model is announced the posts are announcements, and
no job shows yet; each name is therefore read twice, the day it appears and
`recheck_days` later. Google Ads volumes run about two months behind, so they tell how
big a need is, not whether this release moved it; Trends tells that, but reads nothing
for terms under a few hundred searches a month. Most proposed phrases have no volume.

Then, for the rows rated prioritize or watch, three more readings, each switchable in the
config: the word's age (wordage.py: when it first drew searches; a term from the last half
year reads one step easier on page one), its markets (markets.py: searches and difficulty
in several countries, the best market, the top region) and, for the prioritized rows, the
shape of page one (serpshape.py: homepages against inner pages, platforms, exact titles).
Four more cost nothing: the twelve months of searches the volume call already carries
(curve.py: new since, peaked, falling, rising or steady), whether your own site ranks for the
row already (yours.py: from the page-one results and Search Console, so the action is an
update, an improvement or a new page), the forum threads and AI overview on page one
(competition.py) and, for the prioritized rows, whether the exact-match .com is free
(dotcom.py, RDAP).

Costs seen in October 2026: $0.003 per name for the posts, $0.09 for the volume batch
(up to 1,000 phrases), $0.0012 per Trends lookup (two per row of the table), plus the model's tokens.
The three readings above add about $0.41 a run plus $0.004 a row (see each module).
"""
from __future__ import annotations

import base64
import json
import os
import re
import statistics
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import UA, aisa, competition, curve, dotcom, goanyapi, markets, review, serpshape, snap, wordage, yours

POSTS_KEPT = 12
POST_CHARS = 400
README_CHARS = 3000
MIN_FAVES = 50
SEARCH_DAYS = 14
TRENDS_DAYS, RECENT_DAYS = 90, 7
MOMENTUM_MIN_BASE = 5      # of 100: the week before must reach this for a week-on-week ratio
NOW_DAYS = 3               # a peak in the last three settled days is still rising
HOLD_SHARE = 0.5           # at half its peak or more in those days, a term is holding
CALIBRATE_MIN = 1000       # monthly searches a term needs to help size the benchmark
NEW_BASE_MAX = 10          # a baseline under 10 (of 100) means the term barely existed before
MAX_PER_NAME = 6
STATE_DAYS = 30
ASKED_MAX = 8              # names read in one `keywords` call: each costs several paid lookups
SEARCH_TOOL = "get_twitter_tweet_advanced_search"
VOLUME_TOOL = "post_dataforseo_keywords_gads_search_volume_live"
SYSTEM = (
    "You help an SEO researcher who builds tool pages around new AI releases. You get the NAME of a new model, product "
    "or repository and EVIDENCE: popular posts about it and, for a repository, its README.\n"
    "Step 1: work out the concrete jobs people are doing with it, according to the evidence.\n"
    "Step 2: for each job, write the KEYWORD a keyword tool would list for it: the short head term people type into "
    "Google when they want that job done and have never heard of this product.\n"
    "A keyword is a noun phrase for a tool or a task, 2 to 4 words, lowercase: 'ai image detector', 'psd editor online', "
    "'run llm locally', 'photo to anime', 'ai dance video generator', 'remove background from video'. Not a sentence, "
    "not a feature description ('generate ultra detailed images', 'combine multiple reference images'), not a quality "
    "('fast image generation'), not news.\n"
    "Never use the NAME itself or the name of its maker. Prefer keywords with no names at all. When the evidence shows "
    "people comparing it to an established product or using it on a named platform, you may add up to two keywords "
    "built on that established name ('photoshop alternative', 'debloat macos') and set `brand` to true for them. Set "
    "`brand` to true for any keyword that contains a brand, product, company, operating system, person, studio, "
    "franchise or character name; false otherwise.\n"
    "Every keyword needs a short quote from the evidence showing people doing that job. No evidence of a concrete job "
    "(only an announcement, prices or benchmarks): return an empty list. Do not guess.\n"
    "Answer with JSON only: {\"what_it_is\": \"one sentence\", \"use_cases\": [{\"phrase\": \"...\", \"evidence\": "
    "\"short quote\", \"brand\": false}]}, at most 6, the most specific job first."
)


def due(names: list[str], seen: dict[str, dict], today: date, per_day: int, recheck_days: int) -> list[str]:
    """Names to read today: new ones first, then those read once `recheck_days` ago or earlier."""
    new = [n for n in names if n not in seen]
    cutoff = (today - timedelta(days=recheck_days)).isoformat()
    again = sorted((n for n, s in seen.items() if s["runs"] == 1 and s["first"] <= cutoff), key=lambda n: seen[n]["first"])
    return list(dict.fromkeys(new + again))[:per_day]


def _readme(repo: str) -> str:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{repo}/readme", headers=headers), timeout=30) as res:
            return base64.b64decode(json.load(res)["content"]).decode("utf-8", "replace")[:README_CHARS]
    except (OSError, KeyError, ValueError):
        return ""


def evidence(names: list[str], repos: dict[str, str], today: date,
             texts: dict[str, tuple[str, str]] | None = None) -> tuple[dict[str, dict], int]:
    """{name: {"posts": [...], "readme": str, "error": str}} and the cost in micro-USD.

    One request per name: the router runs a batch's calls one after another, a post
    search takes 15 to 25 seconds, and its gateway gives up at 60, so two names in one
    request timed out twice on 2026-10-13 while each alone answered. A name whose search
    fails goes on with its README and the related queries, and the table says so."""
    since = (today - timedelta(days=SEARCH_DAYS)).isoformat()
    out, cost = {}, 0
    for n in names:
        error, tweets = "", []
        try:
            data, spent = aisa.call([{"call_id": "s", "tool": SEARCH_TOOL, "arguments": {
                "query": f'"{n}" min_faves:{MIN_FAVES} -filter:replies since:{since}', "queryType": "Top"}}])
            cost += spent
            d = data.get("s") or {}
            tweets = d.get("tweets") or (d.get("data") or {}).get("tweets") or []
        except aisa.AisaError as exc:
            error = " ".join(re.sub(r"<[^>]+>", " ", str(exc)).split())[:80]
        posts = sorted(({"text": " ".join((t.get("text") or "").split()), "likes": int(t.get("likeCount") or 0),
                         "by": (t.get("author") or {}).get("userName") or "?", "url": t.get("url", "")} for t in tweets),
                       key=lambda p: -p["likes"])[:POSTS_KEPT]
        out[n] = {"posts": posts, "readme": _readme(repos[n]) if repos.get(n) else "", "repo": repos.get(n, ""), "error": error}
        if not out[n]["readme"] and (texts or {}).get(n):     # a product's own listing stands in for a README
            out[n]["repo"], out[n]["readme"] = texts[n]
    return out, cost


def parse(answer: str, name: str) -> list[dict]:
    """The model's use cases, cleaned: lowercased, 2 to 5 words, never the name itself, no repeats."""
    m = re.search(r"\{.*\}", answer, re.S)
    try:
        items = json.loads(m.group(0)).get("use_cases", []) if m else []
    except (ValueError, AttributeError):
        return []
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        phrase = " ".join(str(it.get("phrase", "")).lower().split()) if isinstance(it, dict) else ""
        if not 2 <= len(phrase.split()) <= 5 or name in phrase or phrase in seen:
            continue
        seen.add(phrase)
        out.append({"phrase": phrase, "evidence": " ".join(str(it.get("evidence", "")).split())[:160], "brand": bool(it.get("brand"))})
    return out[:MAX_PER_NAME]


def propose(model: str, name: str, ev: dict) -> list[dict]:
    if not ev["posts"] and not ev["readme"]:
        return []
    posts = "\n".join(f"- ({p['likes']} likes) @{p['by']}: {p['text'][:POST_CHARS]}" for p in ev["posts"])
    user = f"NAME: {name}\n\nPOSTS:\n{posts or '(none found)'}\n" + (f"\nREADME of {ev['repo']}:\n{ev['readme']}\n" if ev["readme"] else "")
    return parse(aisa.chat(model, SYSTEM, user, max_tokens=900), name)


SCREEN = (
    "You check keyword candidates for an SEO researcher. NAME is a new model, product or repository; EVIDENCE is what "
    "people posted about it. For each candidate keyword give three things.\n"
    "`score`, 0.0 to 1.0: how well NAME answers that search. Would someone who typed the keyword be served by a page "
    "about using NAME for it? High: the keyword names the main job NAME does, as the evidence shows it ('ai image "
    "detector' for a tool that tells whether an image is AI-made; 'photoshop alternative' for a free clone of that "
    "editor). Low: a side mention, a comparison someone made in passing ('chatgpt alternative' because one post "
    "compared two models), a broad category NAME is only one of thousands in ('ai coding assistant' for a general "
    "language model), or a job the evidence does not show.\n"
    "`brand`, true or false: the keyword contains a brand, product, company, operating system, person, studio, "
    "franchise or character name ('photoshop alternative', 'free up mac disk space').\n"
    "`name`, true or false: the keyword is itself the name of another specific product, model or tool, with or "
    "without its maker ('gpt image 2.5', 'gemini 4 argon', 'mistral large 4'), not a task or a kind of tool.\n"
    "`established`, true or false: only when `name` is true. True when you know that product to have existed for a "
    "year or more ('premiere pro', 'davinci resolve', 'adobe lightroom', 'google gemini'); false when it is new or "
    "you do not know it.\n"
    "Answer with JSON only, one object per candidate, in the same order: "
    "[{\"i\": 0, \"score\": 0.9, \"brand\": false, \"name\": false, \"established\": false}, ...]"
)
RELEVANT_MIN = 0.6
RELATED_KEPT = 12          # related queries per name that go to the screen
COSEARCH_KEPT = 5          # other names kept as leads per name
COUNTRY_WORKERS = 3        # one-country lookups in flight at once


def squash(text: str) -> str:
    return re.sub(r"[\s\-_.]+", "", text.lower())


def split_related(name: str, related: dict[str, list[str]]) -> tuple[list[str], list[dict]]:
    """Related queries sorted into the name's own long-tails ("photocraft download") and
    candidate phrases. One-word queries ("github", "google") say nothing and are dropped."""
    words = name.split()
    tails, cands, seen = [], [], set()
    for kind in ("rising", "top"):
        for q in related.get(kind, []):
            if q in seen or q == name:
                continue
            seen.add(q)
            if all(w in q.split() for w in words) or squash(name) in squash(q):
                tails.append(q)      # also "pdf craft" for "pdfcraft": the same name, typed apart
            elif len(q.split()) >= 2:
                cands.append({"phrase": q, "brand": False, "source": "searched", "rising": kind == "rising",
                              "evidence": f"searched on Google together with {name} ({kind})"})
    return tails, cands[:RELATED_KEPT]


def screen(model: str, name: str, ev: dict, cands: list[dict]) -> list[dict]:
    """The candidates with the model's view of each: `relevance`, `brand` (added to what
    the proposer said), `is_name`. A failed call returns them as they came, unscored: the
    screen narrows the table, it never empties it."""
    if not cands:
        return []
    posts = "\n".join(f"- ({p['likes']} likes) @{p['by']}: {p['text'][:POST_CHARS]}" for p in ev["posts"][:6])
    user = f"NAME: {name}\n\nEVIDENCE:\n{posts or ev['readme'][:1200]}\n\nCANDIDATES:\n" + json.dumps(
        [{"i": i, "keyword": c["phrase"]} for i, c in enumerate(cands)], ensure_ascii=False)
    try:
        answer = aisa.chat(model, SCREEN, user, max_tokens=600)
    except aisa.AisaError:
        return cands
    m = re.search(r"\[.*\]", answer, re.S)
    try:
        views = {int(x["i"]): x for x in json.loads(m.group(0))} if m else {}
    except (ValueError, KeyError, TypeError):
        return cands
    out = []
    for i, c in enumerate(cands):
        v = views.get(i)
        try:
            out.append(dict(c, relevance=float(v["score"]), brand=c["brand"] or bool(v.get("brand")), is_name=bool(v.get("name")),
                            established=bool(v.get("established")))
                       if v else c)
        except (KeyError, TypeError, ValueError):
            out.append(c)
    return out


def other_names(name: str, viewed: list[dict], reading: list[str] = ()) -> list[dict]:
    """Names of other NEW things rising in searches beside this one: leads of their own.
    Left out: a query sharing a word with the name, which is the same family ("google nano
    banana" beside "nano banana 2.1"); one that holds a name already being read today
    ("photocraft github" beside "pdfcraft"); and an established product the new thing is
    compared with ("premiere pro", "davinci resolve" beside "filmcraft")."""
    family = {w for w in name.split() if w.isalpha() and len(w) > 2}
    siblings = [squash(n) for n in reading if n != name]
    out, seen = [], set()
    for u in viewed:
        words = frozenset(u["phrase"].split())      # "adobe artcraft" and "artcraft adobe" are one name
        if (u.get("is_name") and u.get("rising") and not u.get("established") and words not in seen
                and not family & words and not any(s in squash(u["phrase"]) for s in siblings)):
            seen.add(words)
            out.append({"term": u["phrase"], "with": name})
    return out[:COSEARCH_KEPT]


def relevant(cands: list[dict]) -> list[dict]:
    """Candidates that pass the screen: not another thing's name, and related enough."""
    return [c for c in cands if not c.get("is_name") and c.get("relevance", 1.0) >= RELEVANT_MIN]


def volumes(phrases: list[str], location_code: int) -> tuple[dict[str, dict], int]:
    """{phrase: {"volume", "month"}} from Google Ads, one batch call."""
    if not phrases:
        return {}, 0
    data, cost = aisa.call([{"call_id": "vol", "tool": VOLUME_TOOL, "arguments": {"body": [{
        "keywords": phrases, "location_code": location_code, "language_code": "en"}]}}])
    out = {}
    for r in ((data.get("vol") or {}).get("tasks") or [{}])[0].get("result") or []:
        months = sorted((f"{m['year']}-{m['month']:02d}", int(m.get("search_volume") or 0)) for m in r.get("monthly_searches") or [])
        out[r["keyword"]] = {"volume": int(r.get("search_volume") or 0), "month": months[-1][0] if months else "",
                             "months": [list(m) for m in months]}     # oldest first, for the twelve-month curve (curve.py)
    return out, cost


WORLD = {"unsettled": review.UNSETTLED_DAYS, "window": 30, "factor": review.RISE_FACTOR, "days": 2}
# One country, 30 days, settled to yesterday (goanyapi.daily_series). The rise is judged in
# the last 10 days against the 20 before. An established term has a high baseline, so the
# bar is 1.5x for three days: "photoshop alternative" ran 77-100 against 50 for five days
# after PhotoCraft appeared, a real move that 2x would miss.
COUNTRY = {"unsettled": 0, "window": 10, "factor": 1.5, "days": 3}


def shape(points: list[tuple[date, int]], today: date, rule: dict = WORLD) -> dict:
    """What a term's own Trends curve says: is it new, when did it start rising, where is
    the peak, what is it doing now. `points` is the daily series on its own 0-100 scale.
    Today's value covers part of a day and is dropped, with the days `rule` calls
    unsettled; "now" means the last days that are left. A rise is judged inside the last
    `window` days against the level before them, so an old peak does not count, and it
    needs `days` days above the line: a term with few searches shows single-day spikes
    out of nothing ("debloat macos": 0, 0, 100, 0, 0)."""
    cut = today - timedelta(days=rule["unsettled"])
    points = [(d, v) for d, v in points if d < cut]
    top = max((v for _, v in points), default=0)
    if not top:
        return {"no_data": True}
    # Back onto 0-100: when a dropped day held the series' 100, the real days sit squeezed
    # under 10 and no threshold in points would mean anything.
    points = [(d, round(v * 100 / top)) for d, v in points]
    end = points[-1][0]
    start = end - timedelta(days=rule["window"])
    old = [v for d, v in points if d < start]
    window = [(d, v) for d, v in points if d >= start]
    base = statistics.mean(old) if old else 0.0
    peak_day, peak = max(window, key=lambda p: (p[1], p[0]))
    line = max(base * rule["factor"], base + review.RISE_MIN)
    above = [d for d, v in window if v >= line]
    rose = len(above) >= rule["days"]
    week, before = [v for _, v in points[-RECENT_DAYS:]], [v for _, v in points[-2 * RECENT_DAYS:-RECENT_DAYS]]
    out = {"no_data": False, "base": round(base, 1), "peak": peak, "peak_day": peak_day.isoformat(),
           "rose": rose, "new": rose and base < NEW_BASE_MAX,
           # the last settled week against the week before it; None when that week held too
           # little to divide by ("ai audio detector" read x43 on a week of near-zeros)
           "momentum": round(statistics.mean(week) / statistics.mean(before), 1)
           if before and statistics.mean(before) >= MOMENTUM_MIN_BASE else None}
    if not rose:
        return {**out, "now": "flat"}
    # The rise that holds the peak: walk back from the peak while each day is above the
    # line. The first day above it anywhere in the window can be an earlier, unrelated blip.
    started, up = peak_day, set(above)
    while started - timedelta(days=1) in up:
        started -= timedelta(days=1)
    out["started"] = started.isoformat()
    latest = max(v for d, v in window if d > end - timedelta(days=NOW_DAYS))
    out["now"] = "rising" if (end - peak_day).days < NOW_DAYS else "holding" if latest >= HOLD_SHARE * peak else "fading"
    return out


def country_series(rows: list[dict], c: dict) -> tuple[dict[str, list], int, list[str]]:
    """The one-country series for the rows worth it: every name, and the generic terms with
    at least `country_min_volume` searches a month, `country_max` of those at most. The cap
    is on the generic terms alone: when it covered names too, five names left room for three
    generic terms, and "photoshop alternative" fell back to the worldwide series and read
    flat the day after it had read rising. A lookup takes 20 to 55 seconds, so they run
    COUNTRY_WORKERS at a time."""
    generic = sorted((r for r in rows if r["kind"] == "generic" and r.get("volume", 0) >= c["country_min_volume"]),
                     key=lambda r: -r["volume"])[:c["country_max"]]
    picked = [r["phrase"] for r in rows if r["kind"] == "name"] + [r["phrase"] for r in generic]

    def one(phrase: str):
        try:
            return phrase, goanyapi.daily_series(phrase, c["related_geo"]), ""
        except goanyapi.GoAnyError as exc:
            return phrase, ([], 0), str(exc)
    out, cost, errors = {}, 0, []
    with ThreadPoolExecutor(max_workers=COUNTRY_WORKERS) as pool:
        for phrase, (series, spent), error in pool.map(one, picked):
            cost += spent
            if error:
                errors.append(f"{phrase}: {error}")
            else:
                out[phrase] = series
    return out, cost, errors


def _mean(values: list[int]) -> float:
    return statistics.mean(values) if values else 0.0


def _in_month(day: date, month: str) -> bool:
    return day.isoformat().startswith(month)


def own_per_day(points: list[tuple[date, int]], volume: int, month: str, today: date) -> int | None:
    """Searches a day now, from the term's own numbers: its Google Ads volume for `month`
    scaled by how its Trends level in the last RECENT_DAYS compares with that month."""
    points = review.settled(points, today)
    anchor = _mean([v for d, v in points if _in_month(d, month)])
    recent = _mean([v for _, v in points[-RECENT_DAYS:]])
    return round(volume / 30 * recent / anchor) if anchor and volume else None


def bench_daily(rows: list[dict]) -> float | None:
    """The benchmark's searches a day, worked back from the terms whose volume is known:
    in the month of the volume data, a term at a third of the benchmark's level with
    3,000 searches a day puts the benchmark at 9,000. The median over the terms with
    at least CALIBRATE_MIN searches a month; Trends is too coarse below that."""
    found = []
    for r in rows:
        pair, month = r.get("_pair") or [], r.get("month")
        if not month or r.get("volume", 0) < CALIBRATE_MIN:
            continue
        term = _mean([v for d, v, _ in pair if _in_month(d, month)])
        bench = _mean([b for d, _, b in pair if _in_month(d, month)])
        if term and bench:
            found.append(r["volume"] / 30 * bench / term)
    return statistics.median(found) if found else None


def size(pair: list[tuple[date, int, int]], today: date, daily: float | None) -> dict:
    """A term next to the benchmark on one scale: its peak against the benchmark's usual
    level, and, when the benchmark's size is known, searches a day now."""
    pair = review.settled(pair, today)
    level = _mean([b for _, _, b in pair])
    if not level:
        return {}
    window = pair[-WORLD["window"]:]
    out = {"peak_vs": max(v for _, v, _ in window), "bench_level": round(level, 1)}
    if daily:
        out["per_day"] = round(_mean([v for _, v, _ in pair[-RECENT_DAYS:]]) / level * daily)
    return out


def decide(s: dict, per_day: int | None, min_daily: int) -> str:
    """prioritize: rose, still up, and big enough. watch: rose but small; too early to
    read; or a term that did not move but has steady demand of that size (a need the
    new thing serves, not one it created). skip: fading, or flat and small."""
    big = (per_day or 0) >= min_daily
    if s.get("no_data"):
        return "watch"
    if s["now"] == "fading":
        return "skip"
    if s["now"] == "flat":
        return "watch" if big else "skip"
    return "prioritize" if big else "watch"


def _pairs(task: dict) -> list[tuple[date, int, int]]:
    try:
        item = task["result"][0]["items"][0]
    except (KeyError, IndexError, TypeError):
        return []
    rows = []
    for p in item.get("data", []):
        v = (p.get("values") or []) + [0, 0]
        rows.append((date.fromisoformat(p["date_from"]), int(v[0] or 0), int(v[1] or 0)))
    return rows


def fetch_series(keywords: list[str], benchmark: str, today: date) -> tuple[dict[str, dict], int]:
    """{keyword: {"solo": [(day, value)], "pair": [(day, value, benchmark value)]}}: two Trends lookups each."""
    window = {"date_from": (today - timedelta(days=TRENDS_DAYS)).isoformat(), "date_to": today.isoformat()}
    calls = []
    for i, k in enumerate(keywords):
        calls.append({"call_id": f"s{i}", "tool": review.TOOL, "arguments": {"body": [{"keywords": [k], **window}]}})
        calls.append({"call_id": f"p{i}", "tool": review.TOOL, "arguments": {"body": [{"keywords": [k, benchmark], **window}]}})
    data, cost = aisa.call_all(calls)
    task = lambda cid: ((data.get(cid) or {}).get("tasks") or [{}])[0]  # noqa: E731
    return {k: {"solo": review._series(task(f"s{i}")), "pair": _pairs(task(f"p{i}"))} for i, k in enumerate(keywords)}, cost


def measure(rows: list[dict], series: dict[str, dict], min_daily: int, today: date,
            country: dict[str, list] | None = None) -> float | None:
    """Fills each row with its shape, size and decision. Returns the benchmark's calibrated
    size. A row with a one-country series takes its shape from that (`trend_geo` says so)."""
    for r in rows:
        r["_pair"] = series.get(r["phrase"], {}).get("pair") or []
    daily = bench_daily(rows)
    for r in rows:
        solo = series.get(r["phrase"], {}).get("solo") or []
        local = (country or {}).get(r["phrase"])
        s = shape(local, today, COUNTRY) if local else shape(solo, today)
        if local and not s.get("no_data"):
            r["trend_geo"] = True
        elif local:
            s = shape(solo, today)
        z = size(r.pop("_pair"), today, daily)
        own = own_per_day(solo, r.get("volume", 0), r.get("month", ""), today)
        if own is not None:
            z["per_day"] = own      # a term's own volume is a better anchor than the benchmark
        r.update({**s, **z, "decision": decide(s, z.get("per_day"), min_daily)})
    return daily


def run(cfg: dict, state: Path, names: list[str], repos: dict[str, str], today: date | None = None,
        on_request: bool = False, texts: dict[str, tuple[str, str]] | None = None) -> dict:
    """names: today's named leads, most important first. repos: {name: "owner/repo"} where known.
    `on_request` is for names a person asked about (the `keywords` command, the MCP tool): all of
    them are read now, up to ASKED_MAX, and the read-twice record is left alone."""
    c = cfg["usecases"]
    today = today or date.today()
    path = state / "usecases.json.gz"
    seen: dict[str, dict] = {} if on_request else snap.load(path) or {}
    todo = list(dict.fromkeys(names))[:ASKED_MAX] if on_request else due(names, seen, today, c["per_day"], c["recheck_days"])
    if not todo:
        return {"names": [], "rows": [], "proposed": 0, "cost_micros": 0}
    ev, cost = evidence(todo, repos, today, texts)
    proposed: dict[str, dict] = {}
    asked, c0 = 0, 0
    tails: dict[str, list[str]] = {}
    cosearch: list[dict] = []
    related_errors: list[str] = []
    use_related = bool(goanyapi.key()) and c.get("related", True)
    for name in todo:
        cands = [dict(u, source="model") for u in propose(c["model"], name, ev[name])]
        if use_related:
            try:
                related, cost_r = goanyapi.related_queries(name, c["related_geo"])
            except goanyapi.GoAnyError as exc:
                related, cost_r = {}, 0
                related_errors.append(f"{name}: {exc}")
            c0 += cost_r
            tails[name], searched = split_related(name, related)
            have = {u["phrase"] for u in cands}
            cands += [u for u in searched if u["phrase"] not in have]
        asked += len(cands)
        viewed = screen(c["model"], name, ev[name], cands)
        cosearch += other_names(name, viewed, todo)
        for u in relevant(viewed):
            row = proposed.setdefault(u["phrase"], {**u, "term": u["phrase"], "kind": "generic", "names": []})
            row["names"].append(name)
            row["brand"] |= u["brand"]
            if u["source"] == "searched":
                row["source"] = "searched"
    bench = c["benchmark"].lower()
    vol, c2 = volumes(sorted(proposed), c["location_code"])
    generic = sorted((dict(r, **vol[p]) for p, r in proposed.items() if vol.get(p, {}).get("volume", 0) >= c["min_volume"]),
                     key=lambda r: -r["volume"])[:c["trends_max"]]
    rows = [{"term": n, "phrase": n, "kind": "name", "names": [], "brand": True, "evidence": "", **vol.get(n, {})} for n in todo] + generic
    series, c3 = fetch_series([r["phrase"] for r in rows], bench, today)
    local, c4, local_errors = country_series(rows, c) if goanyapi.key() and c.get("country_trends", True) else ({}, 0, [])
    daily = measure(rows, series, c["min_daily"], today, local)
    acted, first = [r for r in rows if r["decision"] in ("prioritize", "watch")], [r for r in rows if r["decision"] == "prioritize"]
    c6, age_errors = wordage.assess(acted, cfg, today)          # before the page-one check: a new term reads easier there
    c7, market_errors = markets.assess(acted, cfg, {c["location_code"]: vol})
    c5, comp_errors = competition.assess(rows, cfg, state) if cfg.get("competition", {}).get("max_rows") else (0, [])
    c8, serp_errors = serpshape.assess(first, cfg)              # prioritized before the page-one check could demote them
    curve.assess(rows, cfg)                                      # free: the twelve months the volume call carried
    _, own_errors = yours.assess(acted, cfg, today)              # after the page-one check: reads its ten results
    _, com_errors = dotcom.assess([r for r in rows if r["decision"] == "prioritize"], cfg)
    order = {"prioritize": 0, "watch": 1, "skip": 2}
    rows.sort(key=lambda r: (order[r["decision"]], -(r.get("per_day") or 0), -(r.get("volume") or 0)))
    if not on_request:
        for name in todo:
            s = seen.setdefault(name, {"first": today.isoformat(), "runs": 0})
            s["runs"] += 1
        cutoff = (today - timedelta(days=STATE_DAYS)).isoformat()
        snap.save(path, {n: s for n, s in seen.items() if s["first"] >= cutoff})
    return {"names": todo, "rows": rows, "proposed": asked, "relevant": len(proposed), "kept": len(generic), "benchmark": bench,
            "bench_daily": round(daily) if daily else None, "volume_month": next((r["month"] for r in generic if r.get("month")), ""),
            "no_evidence": [n for n in todo if not ev[n]["posts"] and not ev[n]["readme"]],
            "posts_error": "; ".join(f"{n}: {e['error']}" for n, e in ev.items() if e.get("error")),
            "related": use_related, "tails": {n: v for n, v in tails.items() if v}, "cosearch": cosearch,
            "related_errors": related_errors, "geo": c["related_geo"], "country_errors": local_errors,
            "competition_errors": comp_errors, "extra_errors": age_errors + market_errors + serp_errors + own_errors + com_errors,
            "cost_micros": cost + c0 + c2 + c3 + c4 + c5 + c6 + c7 + c8 + aisa.take_chat_cost()}


READ_KINDS = {"x", "hf", "breakout", "newsite", "revenue", "seller"}
READ_MIN = 0.8


def names_to_read(leads_today: list[dict], recent_sources: list[dict]) -> list[str]:
    """The leads worth reading for their generic terms: a thing someone released or built,
    not a word that surged. Terms that only the word-count signals found (new sitemap URLs,
    repos sharing a word) are words like "mcd" and "context protocol", or URL fragments
    like "kr.co.klisten.api": our first live run read five of those. A lead
    needs a signal that reports things and a name score of READ_MIN; unscored leads pass."""
    out = []
    for r in [*leads_today, *recent_sources]:
        named = r.get("named")
        if READ_KINDS & set(r.get("kinds", [])) and (named is None or named >= READ_MIN) and r["term"] not in out:
            out.append(r["term"])
    return out


def degraded(uc: dict) -> bool:
    """A source failed during this run, so the table holds less than it would have."""
    return bool(uc.get("posts_error") or uc.get("related_errors") or uc.get("country_errors") or uc.get("competition_errors"))
