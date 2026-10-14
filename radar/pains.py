"""What people complain about: the gaps a new tool can fill.

`python -m radar pains "<keyword>"` reads, for a kind of tool:
- low-star Chrome Web Store reviews (1 to `max_stars`) of the extensions Google shows for the
  keyword (GoAnyAPI: a site search, 2 credits; reviews, 3 credits a page), or of the ones you name;
- Reddit posts that complain about the job or the tools for it (AIsa, $0.0019 a query);
- comments under the top tutorial videos for it (AIsa, $0.001 a page of 20);
and a small model groups them into the gaps they point at: what the tools fail to do, what is
too hard, too slow or too dear, what people wish existed. Each gap has a count, quotes and the
phrase someone with that problem would search.

The daily run does the tutorial part for the leads marked ⚡ (a widely shared walkthrough of a
manual workflow): what its viewers ask for is the one-click tool.

App Store, Google Play and Trustpilot reviews are not reachable: AIsa runs synchronous calls
only, and DataForSEO serves those reviews as tasks to post now and fetch later.
"""
from __future__ import annotations

import json
import re

from . import aisa, goanyapi, social

EXTENSION = re.compile(r"(?:chromewebstore\.google\.com/detail|chrome\.google\.com/webstore/detail)/(?:[^/?#]+/)?([a-p]{32})")
VIDEO = re.compile(r"youtube\.com/watch\?v=([\w-]{11})")
MAX_ITEMS = 120
SYSTEM = (
    "You read complaints and questions about one kind of tool: low-star Chrome Web Store reviews, Reddit posts and "
    "comments under tutorial videos. Group them into the GAPS they point at: what existing tools fail to do, what is "
    "too hard or too slow, what costs too much, what people wish existed or ask to be automated. For each gap give "
    "a short label (under 10 words), its kind ('missing', 'too hard', 'quality', 'price' or 'wants a tool'), the "
    "indexes of the items that support it, and the 2 to 6 words someone with this problem would type into Google. "
    "Skip praise, thanks and anything off topic. Answer with JSON only: "
    "[{\"gap\": \"...\", \"kind\": \"missing\", \"items\": [0, 3], \"phrase\": \"...\"}, ...]"
)
KINDS = ("missing", "too hard", "quality", "price", "wants a tool")


def parse(answer: str, items: list[dict]) -> list[dict]:
    m = re.search(r"\[.*\]", answer, re.S)
    try:
        raw = json.loads(m.group(0)) if m else []
    except ValueError:
        return []
    out = []
    for g in raw if isinstance(raw, list) else []:
        idx = [i for i in (g.get("items") or []) if isinstance(i, int) and 0 <= i < len(items)] if isinstance(g, dict) else []
        if not idx or not str(g.get("gap") or "").strip():
            continue
        out.append({"gap": " ".join(str(g["gap"]).split()), "kind": g.get("kind") if g.get("kind") in KINDS else "missing",
                    "count": len(idx), "phrase": " ".join(str(g.get("phrase") or "").lower().split()),
                    "sources": sorted({items[i]["source"] for i in idx}), "quotes": [items[i] for i in idx[:2]]})
    return sorted(out, key=lambda g: -g["count"])


def group(model: str, items: list[dict]) -> list[dict]:
    items = items[:MAX_ITEMS]
    if not items:
        return []
    user = json.dumps([{"i": i, "source": x["source"], "text": x["text"][:300]} for i, x in enumerate(items)], ensure_ascii=False)
    return parse(aisa.chat(model, SYSTEM, user, max_tokens=2500), items)


def chrome(keyword: str, extensions: list[str], c: dict) -> tuple[list[dict], int, list[str]]:
    """Low-star reviews of the given extensions, or of those Google shows for the keyword."""
    items, cost, errors = [], 0, []
    if not extensions:
        try:
            found, cost = goanyapi.serp(f"site:chromewebstore.google.com {keyword}")
            extensions = list(dict.fromkeys(m.group(1) for r in found for m in [EXTENSION.search(r["url"])] if m))
        except goanyapi.GoAnyError as exc:
            return [], 0, [f"extension search: {exc}"]
    for ext in extensions[:c["extensions"]]:
        cursor = ""
        for _ in range(c["review_pages"]):
            try:
                rows, cursor, spent = goanyapi.chrome_reviews(ext, cursor)
            except goanyapi.GoAnyError as exc:
                errors.append(f"reviews of {ext}: {exc}")
                break
            cost += spent
            items += [{"source": "chrome", "text": r["text"], "rating": r["rating"], "url": f"https://chromewebstore.google.com/detail/{ext}"}
                      for r in rows if r["text"] and (r["rating"] or 5) <= c["max_stars"]]
            if not cursor:
                break
    return items, cost, errors


def reddit(keyword: str, c: dict) -> tuple[list[dict], int, list[str]]:
    items, cost, errors, seen = [], 0, [], set()
    for template in c["reddit_queries"]:
        try:
            posts, spent = social.reddit(template.format(k=keyword), time=c["reddit_time"], sort="relevance", limit=25)
        except aisa.AisaError as exc:
            errors.append(f"reddit: {exc}")
            continue
        cost += spent
        for p in posts:
            if p["id"] not in seen:
                seen.add(p["id"])
                items.append({"source": "reddit", "text": f"{p['title']}. {p['text'][:250]}", "score": p["score"], "url": p["url"]})
    return items, cost, errors


def tutorials(term: str, c: dict) -> tuple[list[dict], int, list[str]]:
    """Comments under the top tutorial videos for a term (GoAnyAPI finds the videos; on the
    APIsRouter backend without a GoAnyAPI key, APIsRouter's YouTube search does)."""
    if goanyapi.key():
        try:
            found, cost = goanyapi.serp(f"site:youtube.com {term} tutorial")
        except goanyapi.GoAnyError as exc:
            return [], 0, [f"video search: {exc}"]
        ids = list(dict.fromkeys(m.group(1) for r in found for m in [VIDEO.search(r["url"])] if m))[:c["videos"]]
    elif aisa.backend() == "apisrouter":
        from . import apisrouter
        try:
            ids, cost = apisrouter.youtube_videos(f"{term} tutorial", c["videos"])
        except aisa.AisaError as exc:
            return [], 0, [f"video search: {exc}"]
    else:
        return [], 0, []
    items, errors = [], []
    for vid in ids:
        try:
            comments, spent = social.youtube_comments(vid)
        except aisa.AisaError as exc:
            errors.append(f"comments of {vid}: {exc}")
            continue
        cost += spent
        items += [{"source": "youtube", "text": x["text"], "likes": x["likes"], "url": f"https://www.youtube.com/watch?v={vid}"}
                  for x in comments if len(x["text"]) > 20]
    return items, cost, errors


def run(cfg: dict, keyword: str, extensions: list[str] | None = None) -> dict:
    """The pain points for a kind of tool, from reviews, Reddit and tutorial comments."""
    c, items, cost, errors = cfg["pains"], [], 0, []
    for fn in (lambda: chrome(keyword, list(extensions or []), c) if goanyapi.key() or extensions else ([], 0, ["no GOANYAPI_API_KEY: Chrome reviews skipped"]),
               lambda: reddit(keyword, c), lambda: tutorials(keyword, c)):
        got, spent, failed = fn()
        items, cost, errors = items + got, cost + spent, errors + failed
    try:
        gaps = group(c["model"], items)
    except aisa.AisaError as exc:
        gaps, errors = [], errors + [f"model: {exc}"]
    by = {s: sum(1 for x in items if x["source"] == s) for s in ("chrome", "reddit", "youtube")}
    return {"keyword": keyword, "items": len(items), "by_source": by, "gaps": gaps, "errors": errors, "cost_micros": cost}


def tutorial_asks(cfg: dict, leads: list[dict]) -> dict:
    """For the leads marked ⚡: what the viewers of its tutorials ask for."""
    c = cfg["tutorials"]
    out, cost, errors = [], 0, []
    for lead in [r for r in leads if r.get("one_click")][:c["max_leads"]]:
        items, spent, failed = tutorials(lead["term"], c)
        cost, errors = cost + spent, errors + failed
        try:
            gaps = group(c["model"], items)
        except aisa.AisaError as exc:
            gaps, errors = [], errors + [f"model: {exc}"]
        if gaps:
            out.append({"term": lead["term"], "comments": len(items), "gaps": gaps[:5]})
    return {"leads": out, "errors": errors, "cost_micros": cost}
