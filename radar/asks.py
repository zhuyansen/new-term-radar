"""Signal ⑦: what people ask for on Reddit ("is there an AI tool that…").

A request is demand said out loud. Once a week (asks.weekday, Monday by default) this searches
Reddit for the ways people ask for a tool (`patterns`, $0.0019 a query through AIsa), keeps the
posts of the last week, and a small model sorts each: a request for something that does a job,
a complaint about a tool, a solution someone shares, or other. For requests and complaints it
writes the job as the words a person would search ("convert powerpoint slides to notes"). The
jobs are sized with Google Ads volumes and the ones with searches are checked for room on page
one; each of those becomes a lead. Most asks are too specific to have searches yet ("add b roll
automatically"); the best scored of those are listed too, as ideas rather than keywords.
With `ai_answers` on, the jobs with searches are also put to ChatGPT (and Google's AI Mode)
as a question, to see which tools the answers name and which sites they cite
(radar/ai_answers.py).

Posts are read for research: quote them, do not republish them.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import UA, ai_answers, aisa, competition, snap, social, usecases
from .i18n import t

CHUNK = 20
X_TOOL = "get_twitter_tweet_advanced_search"
SYSTEM = (
    "You read Reddit posts for a researcher who builds small AI tools. For each post decide its kind: 'request' "
    "(someone asks for a tool or a way to get a job done), 'complaint' (someone is unhappy with a tool or a manual "
    "process), 'solution' (someone shares a tool or a method) or 'other'. For a request or a complaint, write the "
    "JOB as the 2 to 6 words an ordinary person would type into Google to find a tool for it, without brand names "
    "('convert powerpoint slides to notes', 'add b roll automatically', 'reconstruct audio frequencies'). Say "
    "whether an AI tool could plausibly do the job (ai: true or false).\n"
    "Only real jobs a person needs done in their work or life count. Use 'other' for: playing a video game "
    "(automating things inside Factorio, RimWorld or any game), jokes, satire and circlejerk subreddits, opinions "
    "or debates about AI in general, and complaints with no task behind them.\n"
    "Answer with JSON only, one object per post, in the same order: "
    "[{\"i\": 0, \"kind\": \"request\", \"job\": \"...\", \"ai\": true}, ...]"
)


def parse(answer: str, n: int) -> dict[int, dict]:
    m = re.search(r"\[.*\]", answer, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        return {}
    out = {}
    for it in items if isinstance(items, list) else []:
        try:
            i = int(it["i"])
        except (KeyError, TypeError, ValueError):
            continue
        job = " ".join(str(it.get("job") or "").lower().split())
        if 0 <= i < n and it.get("kind") in ("request", "complaint") and it.get("ai") and 2 <= len(job.split()) <= 6:
            out[i] = {"kind": it["kind"], "job": job}
    return out


def x_posts(c: dict) -> tuple[list[dict], int]:
    """Posts on X that ask for a tool (AIsa's advanced search, $0.003, media credits), last week, `x_min_likes` or more."""
    since = (date.today() - timedelta(days=7)).isoformat()
    data, cost = aisa.call([{"call_id": "x", "tool": X_TOOL, "arguments": {
        "query": f"{c['x_query']} lang:en min_faves:{c['x_min_likes']} -filter:replies since:{since}", "queryType": "Top"}}])
    return [{"id": f"x:{p['id']}", "title": p.get("text") or "", "text": "", "subreddit": "x", "score": int(p.get("likeCount") or 0),
             "url": p.get("url") or "", "created": x_time(p.get("createdAt") or "")} for p in (data.get("x") or {}).get("tweets") or []], cost


def x_time(stamp: str) -> float:
    """Seconds since 1970 from X's "Fri Jul 17 19:56:23 +0000 2026", 0 when it does not parse."""
    try:
        return datetime.strptime(stamp, "%a %b %d %H:%M:%S %z %Y").timestamp()
    except ValueError:
        return 0.0


def stack_questions(c: dict) -> tuple[list[dict], int]:
    """Last week's questions on Software Recommendations, where every question asks for a tool, and on the sites
    in `stack_sites` the ones whose title says automate (free API). Stack Overflow itself is quiet now: one
    "automate" title in the 30 days to 2026-10-15; Software Recommendations had 9 questions that week."""
    since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    asks_for = [("questions", site, {}) for site in c["stack_all"]]
    asks_for += [("search", site, {"intitle": words}) for site in c["stack_sites"] for words in c["stack_titles"]]
    out = []
    for path, site, extra in asks_for:
        url = (f"https://api.stackexchange.com/2.3/{path}?" + urllib.parse.urlencode(
            {"order": "desc", "sort": "creation", "fromdate": since, "site": site, "pagesize": 50, **extra}))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as res:
            raw = res.read()
        body = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)     # the API gzips whether asked or not
        out += [{"id": f"se:{q['question_id']}", "title": html.unescape(q.get("title") or ""), "text": "", "subreddit": site,
                 "score": max(int(q.get("score") or 0), c["min_score"]) if site in c["stack_all"] else int(q.get("score") or 0),
                 "url": q.get("link") or "", "created": float(q.get("creation_date") or 0)} for q in body.get("items", [])]
    return out, 0


def freelancer_jobs(c: dict) -> tuple[list[dict], int]:
    """Last week's jobs on Freelancer.com that pay someone to do by hand what a tool could do (its public API, free).
    It stands in for Upwork, whose job pages Google does not list and whose RSS feeds closed in 2024. A job's
    bid count is its score: 124 freelancers bid on "Claude AI & Canva Graphic Automation Expert Needed"."""
    since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    out, seen = [], set()
    for words in c["freelancer_queries"]:
        url = "https://www.freelancer.com/api/projects/0.1/projects/active/?" + urllib.parse.urlencode(
            {"query": words, "limit": 50, "from_time": since, "full_description": "false"})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as res:
            projects = json.load(res).get("result", {}).get("projects", [])
        for p in projects:
            if p["id"] in seen:                          # one job answers several of the searches
                continue
            seen.add(p["id"])
            rate = float((p.get("currency") or {}).get("exchange_rate") or 1)
            top = (p.get("budget") or {}).get("maximum") or (p.get("budget") or {}).get("minimum") or 0
            out.append({"id": f"fl:{p['id']}", "title": p.get("title") or "", "text": " ".join((p.get("preview_description") or "").split())[:300],
                        "subreddit": "freelancer", "score": int((p.get("bid_stats") or {}).get("bid_count") or 0),
                        "budget_usd": round(float(top) * rate), "url": f"https://www.freelancer.com/projects/{p.get('seo_url') or p['id']}",
                        "created": float(p.get("time_submitted") or 0)})
    return out, 0


EXTRA = {"x": x_posts, "stackexchange": stack_questions, "freelancer": freelancer_jobs}
NOT_REDDIT = {"x": "X", "freelancer": "Freelancer"}


def where(source: str, stack_sites: set[str] = frozenset({"softwarerecs", "stackoverflow", "superuser", "webapps"})) -> str:
    """How a post's source reads in the report: r/PhD, X, Freelancer, Stack Exchange · softwarerecs."""
    return NOT_REDDIT.get(source) or (f"Stack Exchange · {source}" if source in stack_sites else f"r/{source}")


def collect(c: dict) -> tuple[list[dict], int, list[str]]:
    """This week's posts for every pattern, once each, above the score floor; then the other places people ask."""
    posts, cost, errors, seen = [], 0, [], set()
    for q in c["patterns"]:
        try:
            got, spent = social.reddit(q, time=c["time"], sort="top", limit=25)
        except aisa.AisaError as exc:
            errors.append(f"{q}: {exc}")
            continue
        cost += spent
        for p in got:
            if p["id"] not in seen and p["score"] >= c["min_score"] and "circlejerk" not in p["subreddit"].lower():
                seen.add(p["id"])
                posts.append(p)
    for name in c.get("extra_sources", []):
        try:
            got, spent = EXTRA[name](c)
        except (OSError, ValueError, KeyError, aisa.AisaError) as exc:
            errors.append(f"{name}: {str(exc)[:120]}")
            continue
        cost += spent
        posts += [p for p in got if p["id"] not in seen and p["score"] >= c["min_score"]]
    # Reddit's "week" is a ranking window, not a filter: posts from 2020 came back on 2026-10-15. The date decides.
    oldest = datetime.now(timezone.utc).timestamp() - c["max_age_days"] * 86400
    return [p for p in posts if (not p.get("created") and p.get("subreddit") != "x") or (p.get("created") or 0) >= oldest], cost, errors


def classify(model: str, posts: list[dict]) -> tuple[list[dict], list[str]]:
    """The posts that ask for a job an AI tool could do, each with its job phrase."""
    kept, errors = [], []
    for start in range(0, len(posts), CHUNK):
        chunk = posts[start:start + CHUNK]
        user = json.dumps([{"i": i, "title": p["title"], "text": p["text"][:300], "subreddit": p["subreddit"]}
                           for i, p in enumerate(chunk)], ensure_ascii=False)
        try:
            for i, v in parse(aisa.chat(model, SYSTEM, user), len(chunk)).items():
                kept.append({**chunk[i], **v})
        except aisa.AisaError as exc:
            errors.append(str(exc)[:160])
    return kept, errors


def asks(cfg: dict, state: Path, today: date | None = None) -> dict:
    c = cfg["asks"]
    today = today or date.today()
    posts, cost, errors = collect(c)
    kept, failed = classify(c["model"], posts)
    jobs: dict[str, dict] = {}
    for p in kept:
        j = jobs.setdefault(p["job"], {"phrase": p["job"], "kind": "generic", "posts": [], "score": 0, "subreddits": []})
        j["posts"].append(p)
        j["score"] += p["score"]
        if p["subreddit"] not in j["subreddits"]:
            j["subreddits"].append(p["subreddit"])
    vol, spent = usecases.volumes(sorted(jobs), c["location_code"]) if jobs else ({}, 0)
    cost += spent
    rows = sorted((dict(j, volume=vol.get(k, {}).get("volume", 0), decision="watch") for k, j in jobs.items()
                   if vol.get(k, {}).get("volume", 0) >= c["min_volume"]), key=lambda r: (-r["score"], -r["volume"]))[:c["max_leads"]]
    spent, comp_errors = competition.assess(rows, cfg, state) if rows and cfg["competition"]["max_rows"] else (0, [])
    cost += spent
    spent, ai_errors = ai_answers.assess(rows, cfg)              # opt-in: what ChatGPT / AI Mode recommend for the job
    cost += spent
    path = state / "asks.json.gz"
    seen: dict[str, str] = snap.load(path) or {}
    for r in rows:
        r["again"] = r["phrase"] in seen
        seen.setdefault(r["phrase"], today.isoformat())
    snap.save(path, seen)
    terms = [{"term": r["phrase"], "recent": len(r["posts"]),
              "examples": [f"{where(p['subreddit'])} ({p['score']}): {p['title'][:90]}" for p in r["posts"][:2]],
              "note": t("note_ask", n=len(r["posts"]), subs=", ".join(where(s) for s in r["subreddits"][:3]))}
             for r in rows if not r["again"]]
    quiet = sorted((dict(j, volume=vol.get(k, {}).get("volume", 0)) for k, j in jobs.items()
                    if vol.get(k, {}).get("volume", 0) < c["min_volume"]), key=lambda j: (-len(j["posts"]), -j["score"]))[:c["max_leads"]]   # many askers over one viral thread
    return {"read": len(posts), "asks": len(kept), "unsized": len(jobs) - len(rows), "rows": rows, "quiet": quiet,
            "terms": terms, "errors": errors + failed + comp_errors + ai_errors, "cost_micros": cost + aisa.take_chat_cost()}
