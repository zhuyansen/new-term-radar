"""What AI assistants recommend for a job people ask for (⑦), and which sites they cite.

For the ⑦ rows that have Google searches, up to `max_rows`, this asks each engine of `engines`
the job as a question (`prompt`, "What is the best tool to {job}?") and keeps who the answer
names and which sites it cites:

- chatgpt: the answer ChatGPT's own interface gives, with its web search (DataForSEO's LLM
  scraper): the brands it names (`brand_entities`, as written in the answer) and its sources;
- google: Google's AI Mode for the same question: it has no brand list, so the names are the
  bold ones that open its list items ("- **[Knowt AI PowerPoint Summarizer](…)** : …"), which is
  how it writes up the tools it recommends; and the sites its answer references. The names its
  references go by are not used: they include "Reddit".

An answer that names no tool is a gap a new tool can fill in AI search; the cited sites are the
pages AI search reads for the job, the ones to be listed on or compared against. One of your
sites (`yours.domains`, or the Search Console property) among them is marked. About $0.004 an
answer for either engine (October 2026); off by default: set `"enabled": true` to pay for it.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from . import aisa, yours

PATHS = {"chatgpt": "ai_optimization/chat_gpt/llm_scraper/live/advanced",
         "google": "serp/google/ai_mode/live/advanced"}
KEPT = 8
WORKERS = 3          # answers asked at once; each takes 10 to 60 seconds


def _host(domain: str | None) -> str:
    return re.sub(r"^www\.", "", (domain or "").lower())


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))[:KEPT]


STOP_WORDS = frozenset({
    "what", "is", "the", "best", "tool", "tools", "to", "for", "and", "or", "in", "of", "on", "with", "by", "from",
    "a", "an", "how", "can", "i", "use", "using", "that", "which", "are", "top", "software", "app", "apps", "ai",
})


def extract_fan_out_queries(result: dict) -> list[str]:
    """Web search queries ChatGPT issued before answering (`fan_out_queries` or `search_queries`)."""
    raw = result.get("fan_out_queries") or result.get("search_queries") or []
    out: list[str] = []
    for q in raw:
        text = (q.get("query") or q.get("keyword") or "") if isinstance(q, dict) else (q if isinstance(q, str) else "")
        text = " ".join(text.strip().split())
        if text and text not in out:
            out.append(text)
    return out


def fan_out_modifiers(queries: list[str], job: str = "", limit: int = KEPT) -> list[tuple[str, int]]:
    """High-frequency co-occurring modifier tokens in `queries` that are not part of `job`."""
    job_words = set(re.findall(r"[a-z0-9]+", (job or "").lower())) | STOP_WORDS
    counts: dict[str, int] = {}
    for q in queries:
        seen_in_q: set[str] = set()
        for tok in re.findall(r"[a-z0-9]+", (q or "").lower()):
            if len(tok) < 2 or tok in job_words or tok in seen_in_q:
                continue
            seen_in_q.add(tok)
            counts[tok] = counts.get(tok, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def read_chatgpt(result: dict) -> dict:
    """Names as the answer writes them (the entity's title can be a category: "AI assistant" for ChatGPT)."""
    names = (re.sub(r"[*_`\[\]\\]", "", b.get("markdown") or b.get("title") or "").strip() for b in result.get("brand_entities") or [])
    out = {"names": _unique(names), "sites": _unique(_host(s.get("domain")) for s in result.get("sources") or [])}
    queries = extract_fan_out_queries(result)
    if queries:
        out["fan_out_queries"] = queries
    return out


LISTED = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\*\*(.+?)\*\*", re.M)
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def listed_names(markdown: str) -> list[str]:
    """The bold names that open an answer's list items, links and code marks taken off."""
    return [LINK.sub(r"\1", m).strip(" :`") for m in LISTED.findall(markdown or "")]


def read_google(result: dict) -> dict:
    refs, names = [], []
    for item in result.get("items") or []:
        refs += item.get("references") or []
        names += listed_names(item.get("markdown") or "")
        for element in item.get("items") or []:
            refs += element.get("references") or []
    return {"names": _unique(names), "sites": _unique(_host(r.get("domain")) for r in refs)}


READ = {"chatgpt": read_chatgpt, "google": read_google}


def _ask(job: str, engine: str, c: dict) -> tuple[str, str, dict | None, int, str | None]:
    task = {"keyword": c["prompt"].format(job=job), "location_code": c["location_code"], "language_code": c["language_code"]}
    try:
        result, cost = aisa.rest(PATHS[engine], task)
    except aisa.AisaError as exc:
        return job, engine, None, 0, f"{job} ({engine}): {exc}"
    return job, engine, READ[engine](result), cost, None


def _merge_runs(runs_got: list[dict]) -> tuple[dict, dict]:
    """Combines `runs_got` for one engine into {"names", "sites"} (ordered by frequency then first appearance)
    and distribution stats {"runs", "counts", "site_counts", "first_picks", "empty"}."""
    name_counts: dict[str, int] = {}
    site_counts: dict[str, int] = {}
    first_picks: dict[str, int] = {}
    empty = 0
    for got in runs_got:
        names = got.get("names") or []
        sites = got.get("sites") or []
        if not names:
            empty += 1
        else:
            first_picks[names[0]] = first_picks.get(names[0], 0) + 1
        for n in names:
            name_counts[n] = name_counts.get(n, 0) + 1
        for s in sites:
            site_counts[s] = site_counts.get(s, 0) + 1
    ordered_names = [k for k, _ in sorted(name_counts.items(), key=lambda kv: -kv[1])][:KEPT]
    ordered_sites = [k for k, _ in sorted(site_counts.items(), key=lambda kv: -kv[1])][:KEPT]
    all_queries = [q for got in runs_got for q in (got.get("fan_out_queries") or [])]
    merged: dict = {"names": ordered_names, "sites": ordered_sites}
    if all_queries:
        merged["fan_out_queries"] = list(dict.fromkeys(all_queries))[:KEPT * 2]
    dist = {"runs": len(runs_got), "counts": name_counts, "site_counts": site_counts,
            "first_picks": first_picks, "empty": empty}
    return merged, dist


def assess(rows: list[dict], cfg: dict) -> tuple[int, list[str]]:
    """Adds `ai` = {"prompt", "by": {engine: {"names", "sites"}}, "yours", "first_pick", "gap", ...}
    to the first `max_rows` rows. When `runs` > 1, asks each engine `runs` times and records
    `distribution` across runs. (cost, errors)"""
    c = cfg.get("ai_answers") or {}
    if not c.get("enabled") or not rows:
        return 0, []
    runs = max(1, int(c.get("runs", 1)))
    engines = [e for e in c["engines"] if e in PATHS]
    errors = [f"unknown engine: {e}" for e in c["engines"] if e not in PATHS]
    picked = {r["phrase"]: r for r in rows[:c["max_rows"]]}
    mine, cost = yours.own_domains(cfg), 0
    per_job: dict[str, dict[str, list[dict]]] = {j: {} for j in picked}
    tasks = [(j, e) for j in picked for e in engines for _ in range(runs)]
    with ThreadPoolExecutor(WORKERS) as pool:
        for job, engine, got, spent, error in pool.map(lambda p: _ask(*p, c), tasks):
            cost += spent
            if error:
                errors.append(error)
                continue
            per_job[job].setdefault(engine, []).append(got)
    for job, by_eng in per_job.items():
        if not by_eng:
            continue
        ai: dict = {"prompt": c["prompt"].format(job=job), "by": {}, "yours": False}
        dist_by: dict[str, dict] = {}
        total_runs, empty_runs = 0, 0
        for engine, runs_got in by_eng.items():
            merged, dist = _merge_runs(runs_got)
            ai["by"][engine] = merged
            dist_by[engine] = dist
            total_runs += dist["runs"]
            empty_runs += dist["empty"]
            ai["yours"] |= any(yours.is_mine(s, mine) for s in merged["sites"]) if mine else False
        ai["first_pick"] = {e: g["names"][0] for e, g in ai["by"].items() if g["names"]}
        ai["gap"] = (empty_runs / total_runs >= 0.8) if total_runs else False
        all_fan_out = [q for eng_runs in by_eng.values() for got in eng_runs for q in (got.get("fan_out_queries") or [])]
        if all_fan_out:
            ai["fan_out_queries"] = list(dict.fromkeys(all_fan_out))[:KEPT * 2]
            mods = fan_out_modifiers(all_fan_out, job=job)
            if mods:
                ai["fan_out_modifiers"] = [{"token": tok, "count": cnt} for tok, cnt in mods]
        if runs > 1:
            ai["runs"] = runs
            ai["distribution"] = dist_by
        picked[job]["ai"] = ai
    return cost, errors
