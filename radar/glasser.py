"""Glasser's Run API (glasser.ai): the radar's first choice for paid data when GLASSER_API_KEY is set.

Glasser resells most of what the radar reads from the same providers AIsa resells, and hands back the
provider's own answer, so a caller reads the same data whichever was asked: every DataForSEO call (Google's
results, Ads volumes, DataForSEO Trends, Labs, backlinks, whois, the ChatGPT scraper, AI Mode), TikHub
(YouTube comments, the Douyin, TikTok and Reddit trending lists) and ScrapeCreators' Reddit search. Not sold
here, so backend.py asks AIsa for them: X (AIsa's X tools are twitterapi.io, which Glasser does not sell) and
searches with operators (site:, inurl:): Glasser refuses those.

One Run a call, with its own Idempotency-Key: a Run still queued or running is polled by its id, and a
request that failed on the way (a timeout, a 5xx, a rate limit) is sent once more with the same key, which
Glasser never bills twice. The key goes to api.glasser.ai only: a redirect is refused.

Prices in Glasser's catalog (inspect, 2026-11-26), the same as AIsa's for the same calls or a little under:
Google's page one $0.002, Ads volumes $0.09 a call, a Trends task $0.0012 ($0.0024 by region), keyword
difficulty about $0.012, backlinks $0.024 plus $0.000036 a row, whois $0.12 plus $0.0012 a row, a site's
ranked keywords $0.012 plus $0.00012 a row, a ChatGPT answer $0.004, a Reddit search $0.00188, a TikHub
call $0.001. A Run that finds nothing is billed like one that does; a task the provider refuses is not.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import UA
from .aisa import AisaError

BASE = os.environ.get("GLASSER_BASE_URL", "https://api.glasser.ai") + "/v1/runs"
TIMEOUT = 240
POLL_SECONDS = 2
MAX_POLLS = 60
RETRY_WAIT_MAX = 30          # seconds: the longest wait for a rate limit before the one retry
PENDING = ("QUEUED", "RUNNING")
OK = 20000
# Refusals every Run would get: a bad key, no balance, a retired contract version (409), an unknown endpoint.
FATAL = (401, 402, 403, 404, 409)
# Search operators Glasser does not sell (its Ads volume schema lists the same ones).
OPERATOR = re.compile(r"\b(site|inurl|intitle|allintitle|allinurl|filetype|related|cache):", re.I)


def _dataforseo(endpoint: str, version: int) -> tuple:
    """A DataForSEO live endpoint: the router's {"body": [task]} goes as the task itself."""
    return ("dataforseo", endpoint, version, lambda args: (args.get("body") or [{}])[0], None)


def _same(provider: str, endpoint: str, version: int, adapt=None) -> tuple:
    """An endpoint that takes the router's arguments as they are."""
    return (provider, endpoint, version, lambda args: dict(args), adapt)


def _reddit_search(args: dict) -> dict:
    """AIsa's Reddit search arguments, in ScrapeCreators' names: `time` is `timeframe`; a page is 25 posts."""
    return {"query": args["query"], **({"sort": args["sort"]} if args.get("sort") else {}),
            **({"timeframe": args["time"]} if args.get("time") else {})}


def _reddit_posts(output: dict, args: dict) -> dict:
    """ScrapeCreators' answer is the one AIsa gave; a post without `permalink` (a trimmed answer) gets it from `url`."""
    for p in output.get("posts") or []:
        if isinstance(p, dict) and not p.get("permalink") and str(p.get("url") or "").startswith("https://www.reddit.com/"):
            p["permalink"] = p["url"][len("https://www.reddit.com"):]
    return output


def _x_time(epoch) -> str:
    """X's own date format, the one twitterapi.io gives and the callers parse: "Fri Jul 17 19:56:23 +0000 2026"."""
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _x_post(t: dict, author: str, likes: str) -> dict:
    return {"id": str(t.get("id") or ""), "text": t.get("text") or "", "url": t.get("url") or "",
            "likeCount": int(t.get(likes) or 0), "createdAt": _x_time(t.get("createdUtc")),
            "author": {"userName": t.get(author) or ""}}


def _x_rows(output: dict, key: str) -> list[dict]:
    """AnyAPI's rows: output.data.<key>; an answer without them is not a shape the radar can read."""
    data = (output.get("output") or {}).get("data") if isinstance(output.get("output"), dict) else None
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        if (output.get("output") or {}).get("found") is False:
            return []
        raise GlasserError(f"AnyAPI's answer has no output.data.{key}: not the shape the radar reads")
    return [t for t in data[key] if isinstance(t, dict)]


def _x_search(output: dict, args: dict) -> dict:
    """AnyAPI's X search as twitterapi.io's advanced search: {"tweets": [...]}."""
    return {"tweets": [_x_post(t, "authorUsername", "likeCount") for t in _x_rows(output, "items")]}


def _x_timeline(output: dict, args: dict) -> dict:
    """AnyAPI's Posts tab as twitterapi.io's last tweets: {"data": {"tweets": [...]}}, the account's own posts
    only (a repost carries another author). AnyAPI leaves authorHandle blank on a timeline (the paid Run of
    2026-11-26 for @OpenAI: 18 posts, every authorHandle empty, every authorId set), so when no row names its
    author, the account is the authorId most rows share, and its handle is the one asked for."""
    handle = str(args.get("userName") or "")
    rows = _x_rows(output, "tweets")
    if any(str(t.get("authorHandle") or "") for t in rows):
        own = [t for t in rows if str(t.get("authorHandle") or "").lower() == handle.lower()]
    else:
        ids = Counter(str(t.get("authorId") or "") for t in rows if t.get("authorId"))
        owner, n = ids.most_common(1)[0] if ids else ("", 0)
        own = [t | {"authorHandle": handle} for t in rows
               if owner and 2 * n > len(rows) and str(t.get("authorId") or "") == owner]
    return {"data": {"tweets": [_x_post(t, "authorHandle", "likes") for t in own]}}


# The AIsa tool each caller names -> (provider, endpoint, contract version checked on 2026-11-26 (a retired
# one answers 409), the input made from the router's arguments, and None when the provider's answer is what
# AIsa gave, else what turns it into that: adapt(answer, arguments)).
ENDPOINTS = {
    "post_dataforseo_serp_google_organic_live": _dataforseo("/v3/serp/google/organic/live/advanced", 17),
    "post_dataforseo_serp_google_organic_live_regular": _dataforseo("/v3/serp/google/organic/live/regular", 8),
    "post_dataforseo_labs_google_keyword_overview_live": _dataforseo("/v3/dataforseo_labs/google/keyword_overview/live", 9),
    "post_dataforseo_keywords_gads_search_volume_live": _dataforseo("/v3/keywords_data/google_ads/search_volume/live", 8),
    "post_dataforseo_keywords_trends_explore_live": _dataforseo("/v3/keywords_data/dataforseo_trends/explore/live", 7),
    "post_dataforseo_keywords_trends_subregion_interests_live":
        _dataforseo("/v3/keywords_data/dataforseo_trends/subregion_interests/live", 7),
    "post_dataforseo_backlinks_backlinks_live": _dataforseo("/v3/backlinks/backlinks/live", 10),
    "post_dataforseo_backlinks_referring_domains_live": _dataforseo("/v3/backlinks/referring_domains/live", 9),
    "get_reddit_search": ("scrapecreators", "/v1/reddit/search", 4, _reddit_search, _reddit_posts),
    "get_tikhub_youtube_web_v2_get_video_comments": _same("tikhub", "/api/v1/youtube/web_v2/get_video_comments", 5),
    "get_tikhub_douyin_app_v3_fetch_hot_search_list": _same("tikhub", "/api/v1/douyin/app/v3/fetch_hot_search_list", 5),
    "get_tikhub_tiktok_web_fetch_trending_searchwords": _same("tikhub", "/api/v1/tiktok/web/fetch_trending_searchwords", 4),
    "post_tikhub_tiktok_ads_get_trends_hashtag_list": _same("tikhub", "/api/v1/tiktok/ads/get_trends_hashtag_list", 5),
    "get_tikhub_reddit_app_fetch_trending_searches": _same("tikhub", "/api/v1/reddit/app/fetch_trending_searches", 2),
    # X through AnyAPI (see the note above UNVERIFIED)
    "get_twitter_user_last_tweets": ("anyapi", "/twitter.user_posts", 1, lambda args: {"handle": args["userName"]}, _x_timeline),
    "get_twitter_tweet_advanced_search": ("anyapi", "/twitter.search", 1,
                                          lambda args: {"query": args["query"], "queryType": args.get("queryType") or "Latest"},
                                          _x_search),
}
# AIsa's DataForSEO passthrough paths (aisa.rest) -> the same endpoints.
REST = {
    "domain_analytics/whois/overview/live": ("/v3/domain_analytics/whois/overview/live", 8),
    "dataforseo_labs/google/ranked_keywords/live": ("/v3/dataforseo_labs/google/ranked_keywords/live", 10),
    "ai_optimization/chat_gpt/llm_scraper/live/advanced": ("/v3/ai_optimization/chat_gpt/llm_scraper/live/advanced", 9),
    "serp/google/ai_mode/live/advanced": ("/v3/serp/google/ai_mode/live/advanced", 8),
}
# X through AnyAPI. AnyAPI names the fields its own way (likes, createdUtc in seconds, authorHandle) and the
# adapters turn them into twitterapi.io's (likeCount, createdAt, author.userName). One paid Run of each on
# 2026-11-26 confirmed the shape ($0.00045 for @OpenAI's posts, $0.00085 for a Top search): the search read as
# AIsa's; the timeline left every authorHandle blank, which _x_timeline handles. A tool written from a published
# schema but not yet run goes here first, off by default, until one paid Run confirms it.
UNVERIFIED: dict = {}


class GlasserError(AisaError):
    """A Glasser Run that failed or could not be read. An AisaError, so the callers that already report
    AIsa's failures report these the same way."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GlasserError("Glasser redirect refused: the key stays on api.glasser.ai")


_OPENER = urllib.request.build_opener(_NoRedirect())


def key() -> str:
    return os.environ.get("GLASSER_API_KEY", "").strip()


def sells(tool: str, args: dict) -> bool:
    """Whether this call goes to Glasser: a mapped tool, and no search operator in its keywords."""
    if tool not in ENDPOINTS:
        return False
    tasks = args.get("body") if isinstance(args.get("body"), list) else [args]
    words = [w for t in tasks if isinstance(t, dict) for w in [t.get("keyword"), t.get("query"), *(t.get("keywords") or [])]]
    return not any(OPERATOR.search(str(w)) for w in words if w)


class _Refused(GlasserError):
    def __init__(self, message: str, status: int, retry_after: float = 0.0):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def _request(url: str, body: dict | None = None, request_id: str = "") -> dict:
    headers = {"Authorization": f"Bearer {key()}", "Accept": "application/json", "User-Agent": UA}
    if body is not None:
        headers.update({"Content-Type": "application/json", "Idempotency-Key": request_id})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as res:
            answer = json.load(res)
    except urllib.error.HTTPError as exc:
        reason, wait = _reason(exc)
        raise _Refused(f"Glasser HTTP {exc.code}: {reason}", exc.code, wait) from None
    except (OSError, ValueError) as exc:
        raise _Refused(f"Glasser {type(exc).__name__}; the Run may still have been billed", 0) from None
    if not isinstance(answer, dict) or answer.get("error"):
        raise GlasserError(f"Glasser error: {str((answer or {}).get('error'))[:160]}")
    return answer


def _reason(exc: urllib.error.HTTPError) -> tuple[str, float]:
    """The message Glasser gives, with its detail, and how long it asks to wait; never the request: it carries the key."""
    wait = 0.0
    try:
        wait = float(exc.headers.get("Retry-After") or 0) if exc.headers else 0.0
    except (TypeError, ValueError):
        pass
    try:
        err = json.loads(exc.read().decode("utf-8", "replace")).get("error") or {}
    except (ValueError, AttributeError):
        return "no detail", wait
    if not isinstance(err, dict):
        return str(err)[:160], wait
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    for ms in (err.get("retry_after_ms"), details.get("retry_after_ms")):
        try:
            wait = max(wait, float(ms) / 1000) if ms is not None else wait
        except (TypeError, ValueError):
            pass
    detail = details.get("issues")
    return str(err.get("message") or err.get("code") or err)[:160] + (f" ({str(detail)[:160]})" if detail else ""), wait


def _create(body: dict) -> dict:
    """POST a Run; a rate limit, a 5xx or a broken connection is tried once more with the same Idempotency-Key."""
    request_id = str(uuid.uuid4())
    try:
        return _request(BASE, body, request_id)
    except _Refused as exc:
        if exc.status not in (0, 429) and exc.status < 500:
            raise
        time.sleep(min(exc.retry_after or POLL_SECONDS, RETRY_WAIT_MAX))
        return _request(BASE, body, request_id)


def _settle(run: dict) -> dict:
    """Poll a queued or running Run by its id until it ends or MAX_POLLS runs out."""
    run_id = str(run.get("id") or "")
    for _ in range(MAX_POLLS):
        if run.get("status") not in PENDING:
            return run
        time.sleep(POLL_SECONDS)
        try:
            run = _request(f"{BASE}/{urllib.parse.quote(run_id, safe='')}")
        except _Refused as exc:          # a look that failed on the way is a look; the next one may answer
            if exc.status not in (0, 429) and exc.status < 500:
                raise
    raise GlasserError(f"Run {run_id} still {run.get('status')}: look it up in Glasser before running it again")


def _charge(run: dict) -> int:
    try:
        return int((Decimal(str(run["charge_usd"])) * 1_000_000).to_integral_value())
    except (KeyError, InvalidOperation):
        raise GlasserError(f"Run {run.get('id')}: no readable charge_usd") from None


def run(provider: str, endpoint: str, version: int, task: dict) -> tuple[dict, int]:
    """One Run: (the provider's own answer, cost in micro-USD). A DataForSEO task the provider refused (not
    billed) comes back as its answer, as AIsa gives it; a Run that failed or a provider error raises."""
    done = _settle(_create({"provider": provider, "endpoint": endpoint, "endpoint_version": version, "input": task}))
    if done.get("status") != "COMPLETED":
        failure = done.get("failure")
        why = (failure.get("message") or failure.get("kind")) if isinstance(failure, dict) else failure
        raise GlasserError(f"Run {done.get('id')} ended {done.get('status')}" + (f": {str(why)[:120]}" if why else ""))
    provider_answer = done.get("provider_response") or {}
    status = provider_answer.get("http_status")
    if not isinstance(status, int) or not 200 <= status < 300:
        raise GlasserError(f"Run {done.get('id')}: {provider} answered HTTP {status}"
                           + (f" ({str(provider_answer.get('error'))[:120]})" if provider_answer.get("error") else ""))
    output = done.get("output")
    if not isinstance(output, dict):
        output = provider_answer.get("error") if isinstance(provider_answer.get("error"), dict) else {}
    return output, _charge(done)


def run_tool(tool: str, args: dict, table: dict | None = None) -> tuple[dict, int]:
    """One router call through Glasser: (the data AIsa's router would have given, cost in micro-USD)."""
    provider, endpoint, version, to_input, adapt = (table or ENDPOINTS)[tool]
    output, cost = run(provider, endpoint, version, to_input(args))
    return (adapt(output, args) if adapt else output), cost


def call(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """aisa.call() through Glasser, one Run a call: ({call_id: data}, cost in micro-USD). A call that failed
    is left out, as AIsa's router leaves out a failed call; a refusal every Run would get (a bad key, no
    balance, a retired version) raises, and so does a request whose every call failed, with the first reason."""
    out, cost, errors = {}, 0, []
    for c in calls:
        try:
            out[c["call_id"]], spent = run_tool(c["tool"], c.get("arguments") or {})
            cost += spent
        except _Refused as exc:
            if exc.status in FATAL or exc.status == 429 or exc.status >= 500 or exc.status == 0:
                raise
            errors.append(f"{c['tool']}: {exc}")
        except GlasserError as exc:
            errors.append(f"{c['tool']}: {exc}")
    if errors and not out:
        raise GlasserError(errors[0][:300])
    return out, cost


def rest(path: str, task: dict) -> tuple[dict, int]:
    """aisa.rest() through Glasser: (the task's first result, cost in micro-USD). Any status but Ok raises, as
    from AIsa's passthrough."""
    endpoint, version = REST[path]
    output, cost = run("dataforseo", endpoint, version, task)
    t = (output.get("tasks") or [{}])[0] or {}
    if output.get("status_code") != OK or t.get("status_code") != OK:
        raise GlasserError(str(t.get("status_message") or output.get("status_message") or output or "no answer")[:200])
    return (t.get("result") or [{}])[0] or {}, cost
