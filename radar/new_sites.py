"""Signal ③b: new sites that already draw search traffic.

A word that catches on gets sites built for it: ai-hailuo.com was registered in August
2026 for the Hailuo video model and ranked for 258 keywords within weeks. DataForSEO's
domain whois overview (through Glasser, or AIsa's REST passthrough) finds such domains: registered AND first seen by
DataForSEO within `window_days`, with estimated organic traffic over `min_etv` and at
least `min_keywords` ranking keywords. Both dates are needed: whois creation dates get
rewritten (dagbladet.no, a 2020 site, showed a July 2026 "created"). The keyword floor
drops gambling and parked domains, which rank for a handful of brand terms.

One query per entry of config.json's new_sites.queries (a name and a SQL LIKE pattern
on the domain, e.g. "%ai%"), about $0.13 each, so the radar runs this once a week
(new_sites.weekday, 0 = Monday). Each site is reported once; the words of its name go
to the name judge with the other leads ("hailuo").

Each site new this run comes with the first words it ranks for in Google's top 20 (radar/site_words.py,
`words` in config.json's new_sites: sites asked a run, 0 for none; about $0.013 a site).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from . import aisa, backend, site_words, snap
from .i18n import t
from .terms import STOP

PATH = "domain_analytics/whois/overview/live"      # Glasser, or AIsa's REST passthrough: AIsa's router refuses these filters
LIMIT = 30
DOMAIN_STOP = {"com", "net", "org", "app", "io", "co", "online", "free", "tool", "tools", "the", "my", "get",
               "official", "site", "hub", "pro", "best", "video", "videos"}


def name_terms(domain: str) -> list[str]:
    """'ai-hailuo.com' -> ['hailuo']; 'livephoto.video' -> ['livephoto']."""
    label = domain.rsplit(".", 1)[0] if domain.count(".") >= 1 else domain
    words = [w for w in re.split(r"[-_.]+", label.lower()) if w]
    return [w for w in words if w not in STOP and w not in DOMAIN_STOP and len(w) >= 4 and not w.isdigit()]


def keep(item: dict, min_etv: int = 1000, min_keywords: int = 20) -> bool:
    organic = (item.get("metrics") or {}).get("organic") or {}
    return (organic.get("etv") or 0) > min_etv and (organic.get("count") or 0) >= min_keywords


def site_row(item: dict, query: str) -> dict:
    organic = (item.get("metrics") or {}).get("organic") or {}
    return {"domain": item["domain"], "query": query, "created": str(item.get("created_datetime"))[:10],
            "first_seen": str(item.get("first_seen"))[:10], "etv": int(organic.get("etv") or 0),
            "keywords": int(organic.get("count") or 0)}


def _task(c: dict, today: date, pattern: str) -> dict:
    since = (today - timedelta(days=c["window_days"])).isoformat() + " 00:00:00 +00:00"
    return {"limit": LIMIT, "order_by": ["metrics.organic.etv,desc"],
            "filters": [["created_datetime", ">", since], "and", ["first_seen", ">", since], "and",
                        ["metrics.organic.etv", ">", c["min_etv"]], "and", ["domain", "like", pattern]]}


def new_sites(cfg: dict, state: Path, today: date | None = None) -> dict:
    c = cfg["new_sites"]
    today = today or date.today()
    sites: dict[str, dict] = {}
    cost, answered, errors = 0, 0, []
    for name, pattern in c["queries"].items():
        try:
            result, spent = backend.rest(PATH, _task(c, today, pattern))
        except aisa.AisaError as exc:
            errors.append(f"{name}: {exc}")
            continue
        cost, answered = cost + spent, answered + 1
        for item in result.get("items") or []:
            if keep(item, c["min_etv"], c["min_keywords"]) and item["domain"] not in sites:
                sites[item["domain"]] = site_row(item, name)
    if errors and not answered:
        raise aisa.AisaError("; ".join(errors)[:300])
    path = state / "new_sites.json.gz"
    seen: dict[str, str] = snap.load(path) or {}
    rows = sorted(sites.values(), key=lambda r: -r["etv"])
    for r in rows:
        r["new"] = r["domain"] not in seen
        seen.setdefault(r["domain"], today.isoformat())
    if answered:
        snap.save(path, seen)
    if c.get("words", site_words.SITES_MAX):
        spent, failed = site_words.add_words(rows, c.get("words", site_words.SITES_MAX))
        cost, errors = cost + spent, errors + failed
    terms = [{"term": w, "recent": 1, "examples": [f"{r['domain']} · {r['keywords']} keywords"],
              "note": t("note_site", domain=r["domain"], created=r["created"], etv=r["etv"], keywords=r["keywords"])}
             for r in rows if r["new"] for w in name_terms(r["domain"])]
    return {"queries": len(c["queries"]), "answered": answered, "cost_micros": cost, "sites": rows, "terms": terms, "errors": errors}
