"""Markets: where a term is searched, and where it is easiest to rank for.

For the rows that would be acted on (prioritize and watch), in each market of `codes`
(US, DE, FR, JP by default): Google Ads monthly searches (the radar's volume source, one
call per market for all the rows; in the table's own market only what it has not looked up) and, with
`difficulty` on, the keyword difficulty there (DataForSEO Labs, one call per market;
often missing for an English term outside English markets). `best` is the market with the
most searches per point of difficulty when every market with searches has a difficulty,
else the one with the most searches: a ratio and a raw count are not compared.

With `top_region` on, one more Trends task per row (DataForSEO Trends' interest by region,
worldwide, 12 months) names the country where the term is most popular, on Trends' own
0-100 scale: a share of local searches, not a count. That share puts small places first
(Sint Maarten and Finland topped "photocraft" and "ai image detector" on 2026-10-18), so the
top region is chosen among the markets of LOCATIONS, `locations` and `codes` only.

The volumes are asked without a language, so they hold searches in every language a
market's users set; the difficulty needs one and takes the market's main language.

Costs seen in October 2026: $0.09 a volume call, about $0.012 a difficulty call, $0.0024
a region task. With the defaults that is four volume calls (the US one only when a name is
among the rows, which is nearly always) and four difficulty calls, $0.41 for two rows on
2026-10-18, plus $0.0024 a row.
"""
from __future__ import annotations

from . import aisa, backend, competition

VOLUME_TOOL = "post_dataforseo_keywords_gads_search_volume_live"
REGION_TOOL = "post_dataforseo_keywords_trends_subregion_interests_live"
# Google Ads location codes and each market's main language (for the difficulty lookup)
LOCATIONS = {"US": (2840, "en"), "GB": (2826, "en"), "CA": (2124, "en"), "AU": (2036, "en"), "IN": (2356, "en"),
             "DE": (2276, "de"), "FR": (2250, "fr"), "ES": (2724, "es"), "IT": (2380, "it"), "NL": (2528, "nl"),
             "BR": (2076, "pt"), "JP": (2392, "ja"), "KR": (2410, "ko")}


def location(code: str, extra: dict | None = None) -> tuple[int, str] | None:
    """(location code, language) for a market code, from LOCATIONS or the config's `locations`."""
    got = (extra or {}).get(code) or LOCATIONS.get(code)
    return (int(got[0]), str(got[1])) if got else None


def best_market(by: dict[str, dict]) -> str | None:
    """The market with the best searches per point of difficulty, or the most searches when
    a market with searches has no difficulty. None when no market has searches."""
    live = {k: v for k, v in by.items() if (v.get("volume") or 0) > 0}
    if not live:
        return None
    if all(v.get("kd") is not None for v in live.values()):
        return max(live, key=lambda k: (live[k]["volume"] / max(live[k]["kd"], 1), live[k]["volume"]))
    return max(live, key=lambda k: live[k]["volume"])


def volumes(phrases: list[str], loc: int) -> tuple[dict[str, int], int]:
    """{phrase: monthly searches} in one market, every language."""
    data, cost = backend.call([{"call_id": "vol", "tool": VOLUME_TOOL, "arguments": {"body": [{"keywords": phrases, "location_code": loc}]}}])
    found = ((data.get("vol") or {}).get("tasks") or [{}])[0].get("result") or []
    return {r["keyword"]: int(r.get("search_volume") or 0) for r in found if r.get("keyword")}, cost


def top_region(task: dict, pool: set[str] | None = None) -> dict | None:
    """{"id", "name", "value"} of the region with the highest interest in one region task,
    among the region codes in `pool` when given."""
    items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    # {"type": "subregion_interests", "interests": [{"keyword", "values": [{"geo_id", "geo_name", "value"}]}]}
    regions = [g for i in items for k in (i.get("interests") or []) if isinstance(k, dict)
               for g in (k.get("values") or []) if isinstance(g, dict)]
    scored = [(int(g.get("value") or 0), g) for g in regions]
    scored = [(v, d) for v, d in scored if v > 0 and (pool is None or str(d.get("geo_id") or "").upper() in pool)]
    if not scored:
        return None
    v, d = max(scored, key=lambda x: x[0])
    return {"id": d.get("geo_id") or d.get("location_code"), "name": d.get("geo_name") or d.get("location_name") or "?", "value": v}


def _volumes(rows: list[dict], codes: list[str], extra: dict, known: dict[int, dict]) -> tuple[dict, int, list[str]]:
    phrases = [r["phrase"] for r in rows]
    by, cost, errors = {}, 0, []
    for code in codes:
        loc = location(code, extra)
        if not loc:
            errors.append(f"market {code}: unknown, add it to markets.locations")
            continue
        have = {p: v["volume"] for p, v in known.get(loc[0], {}).items() if p in phrases and "volume" in v}
        missing = [p for p in phrases if p not in have]      # the names: the table looks up the generic terms only
        try:
            got, spent = volumes(missing, loc[0]) if missing else ({}, 0)
            by[code], cost = {**have, **got}, cost + spent
        except aisa.AisaError as exc:
            errors.append(f"volume {code}: {exc}")
    return by, cost, errors


def _difficulty(rows: list[dict], codes: list[str], extra: dict) -> tuple[dict, int, list[str]]:
    by, cost, errors = {}, 0, []
    for code in codes:
        loc = location(code, extra)
        if not loc:
            continue
        try:
            by[code], spent = competition.difficulty([r["phrase"] for r in rows], *loc)
            cost += spent
        except aisa.AisaError as exc:
            errors.append(f"difficulty {code}: {exc}")
    return by, cost, errors


def _regions(rows: list[dict], pool: set[str]) -> tuple[dict[str, dict | None], int, list[str]]:
    calls = [{"call_id": f"g{i}", "tool": REGION_TOOL, "arguments": {"body": [{"keywords": [r["phrase"]], "time_range": "past_12_months"}]}}
             for i, r in enumerate(rows)]
    try:
        data, cost = backend.call_all(calls)
    except aisa.AisaError as exc:
        return {}, 0, [f"top region: {exc}"]
    return {r["phrase"]: top_region(((data.get(f"g{i}") or {}).get("tasks") or [{}])[0], pool) for i, r in enumerate(rows)
            if f"g{i}" in data}, cost, []


def assess(rows: list[dict], cfg: dict, known: dict[int, dict] | None = None) -> tuple[int, list[str]]:
    """Adds `markets` = {"by": {code: {"volume", "kd"}}, "best", "top_region"} to each row.
    `known`: {location code: {phrase: {"volume"}}} already bought this run. (cost, errors)"""
    c = cfg["markets"]
    if not c.get("enabled") or not rows or not c.get("codes"):
        return 0, []
    extra = c.get("locations") or {}
    vols, cost, errors = _volumes(rows, c["codes"], extra, known or {})
    kds, spent, failed = _difficulty(rows, c["codes"], extra) if c.get("difficulty") else ({}, 0, [])
    pool = {k.upper() for k in [*LOCATIONS, *extra, *c["codes"]]}
    regions, spent_r, failed_r = _regions(rows, pool) if c.get("top_region") else ({}, 0, [])
    for r in rows:
        by = {code: {"volume": vols[code].get(r["phrase"], 0), "kd": kds.get(code, {}).get(r["phrase"])} for code in c["codes"] if code in vols}
        r["markets"] = {"by": by, "best": best_market(by), "top_region": regions.get(r["phrase"])}
    return cost + spent + spent_r, errors + failed + failed_r
