"""Signal ⑨: what was just listed where people launch, rank and talk about things.

Each feed reads one list and the radar keeps what it has not seen before (state/feeds.json.gz);
the first run of a feed only takes its snapshot. Names go to the leads like any other signal.

Free:
- Product Hunt's Atom feed of featured launches;
- Show HN on Hacker News (Algolia's API), posts with `hn_points` or more;
- OpenRouter's model list and Civitai's newest checkpoints: new model names;
- the App Store's top-grossing apps per genre (Apple's RSS; the legacy feed, which has grossing
  and genres, first, the marketing-tools one when it fails);
- new entries in awesome lists on GitHub (`awesome`: raw README URLs).

Paid, small:
- `google_new`: pages Google indexed in the last week under a path (DataForSEO through AIsa, about
  $0.02 a path, AIsa media credits): Figma community plugins, There's An AI For That tool pages,
  Chrome Web Store items. The sites themselves are not touched: Figma answers bots with 403 and
  There's An AI For That with a Cloudflare challenge.
- `trending`: trending searches on Douyin and TikTok, trending TikTok hashtags and Reddit's
  trending searches (TikHub through AIsa, $0.001 each), kept only when they name a tool, an
  effect, a template or AI.
- `rising`: the root-word method. For each seed ("ai generator", "ai detector", "ai converter" ...)
  the searches Google Trends calls rising next to it this month (GoAnyAPI, 6 credits a seed).
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from . import UA, aisa, goanyapi, snap
from .i18n import t

TIMEOUT = 20
SERP = "post_dataforseo_serp_google_organic_live_regular"
# "AI换脸" has no word boundary between I and 换, so the Latin words are fenced by letters, not by \b
TOOLISH = re.compile(r"(?<![a-z])(ai|app|tool|generator|filter|effect|template|prompt|editor|maker|bot|gpt|agent|plugin|extension|"
                     r"converter|translator|avatar)s?(?![a-z])|特效|模板|滤镜|工具|生成|剪辑|一键|智能|插件", re.I)
LABEL = {"producthunt": "Product Hunt", "hn": "Show HN", "openrouter": "OpenRouter", "civitai": "Civitai", "appstore": "App Store",
         "awesome": "awesome list", "google_new": "Google, new this week", "trending": "trending", "rising": "rising with"}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/xml;q=0.9, */*;q=0.8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def recent(stamp: str, days: int = 2) -> bool:
    """Whether an ISO date or datetime falls in the last `days` days."""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days < days


def clean(text: str, n: int = 200) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())[:n]


def producthunt(c: dict) -> tuple[list[dict], int]:
    xml = get("https://www.producthunt.com/feed").decode("utf-8", "replace")
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        link = re.search(r'<link[^>]+href="([^"]+)"', entry)
        content = re.search(r"<content[^>]*>(.*?)</content>", entry, re.S)
        published = re.search(r"<published>(.*?)</published>", entry)
        if title and link:
            out.append({"key": link.group(1), "name": clean(title.group(1), 80), "url": link.group(1),
                        "about": clean(html.unescape(content.group(1)) if content else ""), "fresh": recent(published.group(1) if published else "")})
    return out, 0


def show_hn(c: dict) -> tuple[list[dict], int]:
    since = int(time.time()) - 2 * 86400
    url = ("https://hn.algolia.com/api/v1/search_by_date?tags=show_hn&hitsPerPage=100&numericFilters="
           f"created_at_i>{since},points>={c['hn_points']}")
    out = []
    for h in json.loads(get(url)).get("hits", []):
        m = re.match(r"show hn:\s*(.+?)(?:\s+[–—-]\s+|:\s+|$)", h.get("title") or "", re.I)
        if m:
            out.append({"key": h["objectID"], "name": m.group(1).strip()[:80], "url": h.get("url") or
                        f"https://news.ycombinator.com/item?id={h['objectID']}", "about": clean(h.get("title") or ""),
                        "score": h.get("points") or 0, "fresh": True})          # the query asks for the last two days
    return out, 0


def openrouter(c: dict) -> tuple[list[dict], int]:
    since = time.time() - 7 * 86400
    models = json.loads(get("https://openrouter.ai/api/v1/models")).get("data", [])
    return [{"key": m["id"], "name": (m.get("name") or m["id"]).split(": ", 1)[-1][:80], "url": f"https://openrouter.ai/{m['id']}",
             "about": clean(m.get("description") or ""), "fresh": True} for m in models if (m.get("created") or 0) >= since], 0


def civitai(c: dict) -> tuple[list[dict], int]:
    data = json.loads(get("https://civitai.com/api/v1/models?sort=Newest&types=Checkpoint&limit=50"))
    return [{"key": str(m["id"]), "name": m["name"][:80], "url": f"https://civitai.com/models/{m['id']}",
             "about": clean(m.get("description") or ""),
             "fresh": recent(((m.get("modelVersions") or [{}])[0] or {}).get("publishedAt") or "")} for m in data.get("items", [])], 0


def appstore(c: dict) -> tuple[list[dict], int]:
    out = []
    for genre in c["app_genres"]:
        try:
            feed = json.loads(get(f"https://itunes.apple.com/us/rss/topgrossingapplications/limit=50/genre={genre}/json"))["feed"]
            for e in feed.get("entry", []):
                out.append({"key": e["id"]["attributes"]["im:id"], "name": e["im:name"]["label"][:80],
                            "url": e["id"]["label"], "about": clean(e.get("summary", {}).get("label", ""))})
        except (OSError, ValueError, KeyError):
            data = json.loads(get("https://rss.marketingtools.apple.com/api/v2/us/apps/top-free/50/apps.json"))
            out += [{"key": a["id"], "name": a["name"][:80], "url": a["url"], "about": ""} for a in data["feed"]["results"]]
            break
    return out, 0


ENTRY = re.compile(r"^\s*[-*]\s+\[([^\]]{2,80})\]\((https?://[^)\s]+)\)\s*[-–—:]?\s*(.*)$", re.M)


def awesome(c: dict) -> tuple[list[dict], int]:
    out = []
    for url in c["awesome"]:
        text = get(url).decode("utf-8", "replace")
        out += [{"key": link, "name": clean(name, 80), "url": link, "about": clean(desc)} for name, link, desc in ENTRY.findall(text)]
    return out, 0


def google_new(c: dict) -> tuple[list[dict], int]:
    """Pages Google indexed in the last seven days under each path."""
    out, cost = [], 0
    for path in c["google_new"]:
        data, spent = aisa.call([{"call_id": "g", "tool": SERP, "arguments": {"body": [{
            "keyword": f"site:{path}", "location_code": 2840, "language_code": "en", "depth": 100, "search_param": "tbs=qdr:w"}]}}])
        cost += spent
        res = ((((data.get("g") or {}).get("tasks") or [{}])[0].get("result") or [{}])[0]) or {}
        for i in res.get("items") or []:
            if i.get("type") == "organic" and i.get("url"):
                name = re.split(r"\s+[|–—-]\s+", i.get("title") or "")[0]
                out.append({"key": i["url"], "name": clean(name, 80), "url": i["url"], "about": clean(i.get("description") or ""),
                            "where": path, "fresh": True})                   # indexed in the last seven days
    return out, cost


TRENDING = {
    "douyin": ("get_tikhub_douyin_app_v3_fetch_hot_search_list", {}, lambda d: [w.get("word") for k in ("word_list", "trending_list")
                                                                                 for w in (d.get("data") or {}).get(k) or []]),
    "tiktok": ("get_tikhub_tiktok_web_fetch_trending_searchwords", {}, lambda d: [w.get("trendingSearchWord") for w in d.get("trending_search_words") or []]),
    "tiktok_tags": ("post_tikhub_tiktok_ads_get_trends_hashtag_list", {"country_code": "US", "time_range": 7, "limit": 50},
                    lambda d: [w.get("hashtagName") for w in d.get("items") or []]),
    "reddit": ("get_tikhub_reddit_app_fetch_trending_searches", {"need_format": True},
               lambda d: [w.get("displayQuery") for w in d.get("trendingQueries") or []]),
}


def trending(c: dict) -> tuple[list[dict], int]:
    calls = [{"call_id": k, "tool": TRENDING[k][0], "arguments": TRENDING[k][1]} for k in c["trending"]]
    data, cost = aisa.call(calls)
    out = []
    for k in c["trending"]:
        words = TRENDING[k][2](((data.get(k) or {}).get("data")) or {})
        out += [{"key": f"{k}:{w}", "name": w[:80], "url": "", "about": "", "where": k, "fresh": True} for w in words if w and TOOLISH.search(w)]
    return out, cost


def rising(c: dict) -> tuple[list[dict], int]:
    """The rising searches next to each root word ("ai generator", "ai detector" ...): GoAnyAPI Trends, 6 credits a seed,
    four seeds at a time."""
    out, cost = [], 0
    with ThreadPoolExecutor(4) as pool:
        answers = list(pool.map(goanyapi.related_queries, c["seeds"]))
    for seed, (groups, spent) in zip(c["seeds"], answers):
        cost += spent
        head = [w for w in seed.split() if w != "ai"] or seed.split()
        # Trends answered "ai generator" with "best pizza near me" and "how to bake a cake" on 2026-10-15: a rising
        # search that does not share the seed's word is the day's general news, not something people build tools for
        out += [{"key": q, "name": q, "url": "", "about": "", "where": seed, "fresh": True} for q in groups.get("rising", [])
                if q != seed and any(w in q.split() for w in head)]
    return out, cost


FEEDS = {"producthunt": producthunt, "hn": show_hn, "openrouter": openrouter, "civitai": civitai, "appstore": appstore,
         "awesome": awesome, "google_new": google_new, "trending": trending, "rising": rising}


def feeds(cfg: dict, state: Path, today: date | None = None, weekly: bool = False) -> dict:
    """Every enabled feed; the `weekly` ones (paid searches) only on `weekday` or when asked."""
    c = cfg["feeds"]
    today = today or date.today()
    weekly = weekly or today.weekday() == c["weekday"]
    path = state / "feeds.json.gz"
    seen: dict[str, str] = snap.load(path) or {}
    out = {"read": {}, "new": {}, "first_run": [], "errors": [], "cost_micros": 0, "terms": []}
    for name in c["enabled"]:
        if name in c["weekly"] and not weekly:
            continue
        try:
            items, cost = FEEDS[name](c)
        except (OSError, ValueError, KeyError, TypeError, aisa.AisaError, goanyapi.GoAnyError) as exc:
            out["errors"].append(f"{name}: {str(exc)[:120]}")
            continue
        out["cost_micros"] += cost
        out["read"][name] = len(items)
        first = not any(k.startswith(name + ":") for k in seen)
        fresh = []
        for i in items:
            key = f"{name}:{i['key']}"
            if key not in seen:
                fresh.append(i)
                seen[key] = today.isoformat()
        if first:               # the snapshot is taken; what the source itself dates as recent is news already
            out["first_run"].append(name)
            fresh = [i for i in fresh if i.get("fresh")]
            if not fresh:
                continue
        fresh = sorted(fresh, key=lambda i: -(i.get("score") or 0))[:c["max_per_feed"]]
        out["new"][name] = fresh
        out["terms"] += [{"term": i["name"].lower(), "recent": 1, "about": (i["url"], i["about"]) if i["url"] else None,
                          "examples": [f"{LABEL[name]}{' · ' + i['where'] if i.get('where') else ''}: {i['name']}"],
                          "note": t("note_feed", where=LABEL[name])} for i in fresh]
    snap.save(path, seen)
    out["terms"] = [{k: v for k, v in r.items() if v is not None} for r in out["terms"]]
    return out
