"""The first words a new site ranks for (0.6): what a site a few weeks old has won, so a new site can win it too.

A domain registered in the last few months that already ranks on Google's first two pages has found searches a new
site can reach. For each site new this week, the most traffic first and SITES_MAX at most, DataForSEO Labs' ranked
keywords (through Glasser or AIsa's REST passthrough: about $0.012 a site, $0.00012 a word) give its WORDS words in the top TOP,
the most traffic first, in any country: a new site often starts outside the US (on 2026-10-27, one registered in
August ranked 5 to 11 in Taiwan for stock-price long-tails and for nothing in the US top 20). Labs keeps no day a site
started ranking for a word; a word new since Labs' previous check of its results started between the two (`since`).
"""
from __future__ import annotations

from . import aisa, backend

PATH = "dataforseo_labs/google/ranked_keywords/live"
TOP = 20                # positions that count: the first two pages
WORDS = 5               # words kept a site
SITES_MAX = 10          # sites asked a run
COUNTRIES = {2840: "US", 2826: "UK", 2124: "CA", 2036: "AU", 2356: "IN", 2276: "DE", 2250: "FR", 2724: "ES", 2380: "IT",
             2392: "JP", 2410: "KR", 2158: "TW", 2344: "HK", 2702: "SG", 2076: "BR", 2484: "MX", 2360: "ID", 2704: "VN",
             2764: "TH", 2608: "PH", 2458: "MY", 2528: "NL", 2616: "PL", 2792: "TR", 2804: "UA"}


def word_row(item: dict) -> dict:
    """{"word", "position", "volume", "kd", "country", "url", "since"} from one ranked keyword."""
    kd, el = item.get("keyword_data") or {}, item.get("ranked_serp_element") or {}
    s = el.get("serp_item") or {}
    new = (s.get("rank_changes") or {}).get("is_new")
    window = f"{(el.get('previous_updated_time') or '')[:10]}..{(el.get('last_updated_time') or '')[:10]}"
    return {"word": kd.get("keyword") or "", "position": s.get("rank_absolute"),
            "volume": (kd.get("keyword_info") or {}).get("search_volume"),
            "kd": (kd.get("keyword_properties") or {}).get("keyword_difficulty"),
            "country": COUNTRIES.get(kd.get("location_code"), str(kd.get("location_code") or "")),
            "url": s.get("relative_url") or "", "since": window if new else ""}


def words(domain: str) -> tuple[list[dict], int, int]:
    """(its WORDS words in the top TOP, the most traffic first; how many it has there in all; cost in micro-USD)."""
    result, cost = backend.rest(PATH, {"target": domain, "limit": WORDS, "order_by": ["ranked_serp_element.serp_item.etv,desc"],
                                    "filters": [["ranked_serp_element.serp_item.rank_absolute", "<=", TOP]]})
    return [word_row(i) for i in result.get("items") or []], int(result.get("total_count") or 0), cost


def add_words(rows: list[dict], max_sites: int = SITES_MAX) -> tuple[int, list[str]]:
    """Adds `words` and `top` (its words in the top TOP in all) to the first max_sites new rows. (cost, errors)"""
    cost, errors = 0, []
    for r in [r for r in rows if r.get("new")][:max_sites]:
        try:
            r["words"], r["top"], spent = words(r["domain"])
            cost += spent
        except aisa.AisaError as exc:
            errors.append(f"{r['domain']} words: {exc}")
    return cost, errors
