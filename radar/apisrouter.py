"""APIsRouter as a second backend for the AIsa calls, for those who hold an APIsRouter key
and no AIsa key. aisa.call() and aisa.chat() hand over to this module when
RADAR_BACKEND=apisrouter, or when no AIsa key is found and an APIsRouter key is.

Each AIsa tool the radar uses is mapped to an APIsRouter information operation, bought
(quote, request, poll, results) and reshaped into the answer AIsa would have given, so no
other module changes. Chat goes to APIsRouter's OpenAI-compatible endpoint.

Key: APISROUTER_API_KEY, or the file ~/.config/apisrouter/api-key.

What differs from AIsa:
- Reddit search sorts by relevance over all time only; posts are filtered to the asked
  time window here, so the weekly asks find fewer posts.
- Trends windows are read from the nearest preset range (30 days, 90 days, 12 months)
  and trimmed to the dates asked.
- The whois signal (new sites) is not mapped: APIsRouter takes three filter terms, the
  radar's query needs seven.
- A purchase takes 5 to 40 seconds; calls in a batch run three at a time.

Every purchase is cached for 24 hours by operation and input (RADAR_STATE or ./state,
apisrouter_cache/), so a rerun the same day does not pay twice, and logged to
apisrouter_spend.jsonl beside it.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import UA
from .aisa import AisaError

BASE = os.environ.get("APISROUTER_BASE", "https://api.apisrouter.com/v1/information")
CHAT = os.environ.get("APISROUTER_CHAT_URL", "https://api.apisrouter.com/v1/chat/completions")
WORKERS = 3
POLL_SECONDS = 300
REUSE_SECONDS = 24 * 3600
FINAL = ("completed", "failed", "cancelled", "awaiting_payment", "outcome_unknown", "awaiting_price_confirmation")
# Models AIsa serves that APIsRouter does not; RADAR_MODEL_ALIASES (JSON) adds or overrides.
MODEL_ALIASES = {"gpt-4.1-mini": "claude-haiku-4-5-20251001"}


class RouterError(AisaError):
    pass


def key(required: bool = True) -> str:
    k = os.environ.get("APISROUTER_API_KEY", "").strip()
    if not k:
        path = Path.home() / ".config" / "apisrouter" / "api-key"
        if path.exists():
            k = path.read_text().strip()
    if not k and required:
        raise RouterError("no APIsRouter key: set APISROUTER_API_KEY or write ~/.config/apisrouter/api-key")
    return k


def _state() -> Path:
    p = Path(os.environ.get("RADAR_STATE", "state")) / "apisrouter_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _req(method: str, path: str, body: dict | None = None, url: str | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        req = urllib.request.Request(url or BASE + path, data=data, method=method, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {key()}", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.load(res)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 5:
                try:
                    wait = min(60.0, max(1.0, float(exc.headers.get("Retry-After"))))
                except (TypeError, ValueError):
                    wait = 5.0 * (attempt + 1)
                time.sleep(wait)
                continue
            try:
                return json.load(exc)
            except ValueError:
                raise RouterError(f"HTTP {exc.code} on {path or url}") from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:   # IncompleteRead on a cut stream
            if attempt >= 2:
                raise RouterError(f"{type(exc).__name__}: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    raise RouterError(f"rate limited on {path or url}")


def _ok(doc: dict) -> bool:
    # An accepted request still executing answers code "ok" without status "success".
    return doc.get("status") == "success" or doc.get("code") == "ok"


def _fail(doc: dict) -> RouterError:
    return RouterError(" ".join(str(doc.get(k) or "") for k in ("code", "summary")).strip()[:200] or str(doc)[:200])


def _results(result_ref: str) -> list:
    records, offset = [], 0
    while True:
        doc = _req("GET", f"/results/{result_ref}?offset={offset}&limit=1000")
        if not _ok(doc):
            raise _fail(doc)
        page = doc.get("data") or {}
        got = page.get("records") or []
        records += got
        offset += len(got)
        if not got or offset >= page.get("total", 0):
            return records


def buy(ref: str, inp: dict) -> tuple[list, int]:
    """The result records of one purchase and its price in micro-USD (0 when cached)."""
    h = hashlib.sha256(json.dumps([ref, inp], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = _state() / f"{h[:32]}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < REUSE_SECONDS:
        return json.loads(cache.read_text()), 0
    doc = _req("POST", "/quotes", {"operation_ref": ref, "input": inp, "quantity": 1})
    if not _ok(doc):
        raise _fail(doc)
    quote = doc["data"]
    price = quote["maximum_amount"]
    idem = f"ntr-{h[:40]}-{int(time.time() // REUSE_SECONDS)}"
    doc = _req("POST", "/requests", {"quote_ref": quote["quote_ref"], "idempotency_key": idem, "max_charge_usd": price})
    if _ok(doc):
        req = doc["data"]
    elif doc.get("code") == "idempotency_conflict" and ((doc.get("data") or {}).get("existing") or {}).get("request_ref"):
        req = doc["data"]["existing"]      # bought earlier today and interrupted: continue that one
    else:
        raise _fail(doc)
    deadline = time.time() + POLL_SECONDS
    while req.get("status") not in FINAL:
        if time.time() > deadline:
            raise RouterError(f"{ref}: request {req.get('request_ref')} still running after {POLL_SECONDS}s")
        time.sleep(2)
        doc = _req("GET", f"/requests/{req['request_ref']}")
        if not _ok(doc):
            raise _fail(doc)
        req = doc["data"]
    if req["status"] != "completed":
        raise RouterError(f"{ref}: request {req['request_ref']} ended {req['status']}")
    records = _results(req["result_ref"])
    cache.write_text(json.dumps(records, ensure_ascii=False))
    with open(_state().parent / "apisrouter_spend.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "operation": ref,
                            "request_ref": req["request_ref"], "max_usd": price}) + "\n")
    return records, int(float(price) * 1_000_000)


# ---- reshaping: APIsRouter records -> the answer AIsa would have given ----------------

def _data(records: list) -> list:
    """The `data` rows of DataForSEO-style records, flattened."""
    out = []
    for r in records:
        d = r.get("data") if isinstance(r, dict) else None
        if isinstance(d, list):
            out += d
        elif isinstance(d, dict):
            out.append(d)
        elif isinstance(r, dict):
            out.append(r)
    return out


def _task(result: list) -> dict:
    return {"tasks": [{"result": result}]}


def _tweet_url(screen: str, tid: str) -> str:
    return f"https://x.com/{screen or 'i'}/status/{tid}" if tid else ""


def _tweet(t: dict, screen: str = "") -> dict:
    who = screen or t.get("screen_name") or (t.get("user_info") or {}).get("screen_name") \
        or (t.get("author") or {}).get("screen_name") or ""
    return {"text": t.get("text") or "", "likeCount": int(t.get("favorites") or 0), "createdAt": t.get("created_at") or "",
            "url": _tweet_url(who, str(t.get("tweet_id") or "")), "author": {"userName": who}}


def _timeline(records: list) -> list[dict]:
    out = []
    for r in records:
        d = (r or {}).get("data") or {}
        out += d.get("timeline") or []
    return out


def x_user(args: dict) -> tuple[dict, int]:
    handle = args.get("userName") or ""
    records, cost = buy("twitter-user-post-tweet.read.v1", {"screen_name": handle})
    # Retweets carry the original in retweeted_tweet; the account still said the words.
    tweets = [_tweet(t, handle) for t in _timeline(records)]
    return {"data": {"tweets": tweets}}, cost


def x_search(args: dict) -> tuple[dict, int]:
    records, cost = buy("twitter-search-timeline.search.v1",
                        {"keyword": args.get("query") or "", "search_type": args.get("queryType") or "Top"})
    return {"tweets": [_tweet(t) for t in _timeline(records)]}, cost


TIME_DAYS = {"hour": 1 / 24, "day": 1, "week": 7, "month": 31, "year": 366}


def _posts(records: list) -> list[dict]:
    """SubredditPost nodes from the Reddit app's search answer."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("__typename") == "SubredditPost" and o.get("id"):
                out.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(records)
    return out


def reddit(args: dict) -> tuple[dict, int]:
    records, cost = buy("reddit-dynamic-search.search.v1", {
        "allow_nsfw": "0", "language": "en-US", "need_format": False, "query": args.get("query") or "",
        "safe_search": "unset", "search_type": "post", "sort": "RELEVANCE", "time_range": "all"})
    days = TIME_DAYS.get(args.get("time") or "all")
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    posts, seen = [], set()
    for p in _posts(records):
        try:
            created = datetime.fromisoformat(str(p.get("createdAt")).replace("+0000", "+00:00"))
        except ValueError:
            created = None
        if since and (created is None or created < since):
            continue
        pid = str(p["id"]).removeprefix("t3_")
        if pid in seen:
            continue
        seen.add(pid)
        content = p.get("content") or {}
        posts.append({"id": pid, "title": p.get("postTitle") or p.get("title") or "",
                      "selftext": content.get("markdown") or content.get("preview") or "",
                      "score": int(p.get("score") or 0), "num_comments": int(p.get("commentCount") or 0),
                      "subreddit": (p.get("subreddit") or {}).get("name") or "", "permalink": p.get("permalink") or "",
                      "created_utc": created.timestamp() if created else 0, "over_18": bool(p.get("isNsfw"))})
    if (args.get("sort") or "top") == "top":
        posts.sort(key=lambda p: -p["score"])
    return {"posts": posts[:int(args.get("limit") or 25)]}, cost


def _count(v) -> int:
    """'105', '1.2K', '3M' -> int."""
    s = str(v or "0").replace(",", "").strip().upper()
    m = re.match(r"([\d.]+)\s*([KMB]?)", s)
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def youtube_comments(args: dict) -> tuple[dict, int]:
    records, cost = buy("youtube-video-comments.read.v1", {
        "video_id": args.get("video_id") or "", "need_format": True, "sort_by": "top",
        "language_code": "en", "country_code": "US"})
    comments = []
    for r in records:
        for c in ((r or {}).get("data") or {}).get("comments") or []:
            comments.append({**c, "like_count": _count(c.get("like_count"))})
    return {"data": {"comments": comments}}, cost


def _body(args: dict) -> dict:
    b = args.get("body") or [{}]
    return b[0] if isinstance(b, list) else b


def volume(args: dict) -> tuple[dict, int]:
    b = _body(args)
    inp = {"keywords": b.get("keywords") or [], "location_code": b.get("location_code") or 2840,
           "language_code": b.get("language_code") or "en"}
    records, cost = buy("search.google-ads.keyword-volume.v1", inp)
    rows = []
    for d in _data(records):
        if isinstance(d.get("items"), list):
            rows += d["items"]
        elif d.get("keyword"):
            rows.append(d)
    return _task(rows), cost


def _range(date_from: str, date_to: str) -> str:
    try:
        days = (date.today() - date.fromisoformat(date_from)).days
    except ValueError:
        return "past_90_days"
    return "past_30_days" if days <= 30 else "past_90_days" if days <= 90 else "past_12_months"


def trends(args: dict) -> tuple[dict, int]:
    b = _body(args)
    inp = {"keywords": b.get("keywords") or [], "time_range": _range(b.get("date_from") or "", b.get("date_to") or "")}
    records, cost = buy("search.search-trends.interest-history.v1", inp)
    items = []
    for d in _data(records):
        for item in d.get("items") or []:
            if item.get("type", "").endswith("graph"):
                lo, hi = b.get("date_from") or "", b.get("date_to") or "9999"
                item = {**item, "data": [p for p in item.get("data") or [] if lo <= p.get("date_from", "") <= hi]}
                items.append(item)
    return _task([{"items": items}]), cost


def serp(args: dict) -> tuple[dict, int]:
    b = _body(args)
    records, cost = buy("search.google.result-page.v1", {"keyword": b.get("keyword") or "",
                        "language_code": b.get("language_code") or "en", "location_code": b.get("location_code") or 2840})
    items = []
    for d in _data(records):
        items += d.get("items") or []
    return _task([{"items": items}]), cost


def difficulty(args: dict) -> tuple[dict, int]:
    b = _body(args)
    kws, items, cost = b.get("keywords") or [], [], 0
    for i in range(0, len(kws), 3):     # three keywords a purchase
        records, c = buy("search.google-organic.keyword-difficulty.v1", {"keywords": kws[i:i + 3],
                         "language_code": b.get("language_code") or "en", "location_code": b.get("location_code") or 2840})
        cost += c
        for r in records:
            for it in (r or {}).get("items") or []:
                items.append({"keyword": it.get("keyword"),
                              "keyword_properties": {"keyword_difficulty": it.get("keyword_difficulty")}})
    return _task([{"items": items}]), cost


def youtube_videos(query: str, limit: int = 10) -> tuple[list[str], int]:
    """Video ids for a query, most relevant first: stands in for GoAnyAPI's site:youtube.com search."""
    records, cost = buy("youtube-videos.search.v1", {"search_query": query, "language_code": "en", "country_code": "us"})
    ids = []

    def walk(o):
        if isinstance(o, dict):
            vid = o.get("video_id") or o.get("videoId")
            if isinstance(vid, str) and re.fullmatch(r"[\w-]{11}", vid):
                ids.append(vid)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(records)
    return list(dict.fromkeys(ids))[:limit], cost


TOOLS = {
    "get_twitter_user_last_tweets": x_user,
    "get_twitter_tweet_advanced_search": x_search,
    "get_reddit_search": reddit,
    "get_tikhub_youtube_web_v2_get_video_comments": youtube_comments,
    "post_dataforseo_keywords_gads_search_volume_live": volume,
    "post_dataforseo_keywords_trends_explore_live": trends,
    "post_dataforseo_serp_google_organic_live": serp,
    "post_dataforseo_labs_google_keyword_overview_live": difficulty,
}


def call(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """Same contract as aisa.call: ({call_id: data}, cost in micro-USD); failed calls are
    left out. Raises only when every call failed, like a failed AIsa batch."""
    def one(c: dict):
        fn = TOOLS.get(c["tool"])
        if fn is None:
            return c["call_id"], None, 0, f"{c['tool']} is not available on APIsRouter"
        try:
            data, cost = fn(c.get("arguments") or {})
            return c["call_id"], data, cost, ""
        except RouterError as exc:
            return c["call_id"], None, 0, str(exc)
    out, cost, errors = {}, 0, []
    with ThreadPoolExecutor(WORKERS) as pool:
        for cid, data, c, err in pool.map(one, calls):
            cost += c
            if data is not None:
                out[cid] = data
            else:
                errors.append(err)
    if calls and not out:
        raise RouterError("; ".join(sorted(set(errors)))[:200])
    return out, cost


def chat(model: str, system: str, user: str, max_tokens: int = 1500) -> str:
    aliases = {**MODEL_ALIASES, **json.loads(os.environ.get("RADAR_MODEL_ALIASES") or "{}")}
    body = _req("POST", "", {"model": aliases.get(model, model), "temperature": 0, "max_tokens": max_tokens, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}]}, url=CHAT, timeout=120)
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RouterError(f"unexpected chat answer: {str(body)[:160]}") from exc
