"""The radar's review: check every lead in Google Trends two weeks later.

review()  takes up to `per_day` leads first seen `after_days` ago or earlier and not yet
          checked, asks DataForSEO Trends (through AIsa) about each search variant, and
          stores hit / miss / no_data plus how many days the radar was ahead.

Each variant is its own call: in one shared task the values share a 0-100 scale and a
small term next to a big one reads 0, and the live endpoint runs only the first task of
a request's body, so tasks cannot be batched into one call either. A variant "rose" when
its peak after first_seen clears its own baseline (the mean before first_seen) by
RISE_FACTOR and RISE_MIN; a word with an older meaning ("strata": strata rock, strata
title) has a high baseline and does not rise, while "niko1221 strata" goes from 0 to
something.

This is verification, not discovery: it tells you which signals to trust.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from . import data as data_backend, store

BEFORE_DAYS = 30
MAX_VARIANTS = 3      # each costs one Trends task ($0.0012 in October 2026)
RISE_FACTOR, RISE_MIN = 2.0, 10
RISE_SHARE = 0.25     # the rise starts where a series first covers this share of base -> peak
TOOL = "post_dataforseo_keywords_trends_explore_live"
# Google has not settled the last two days of a daily series: they come back as 0 and
# then 100 for unrelated terms ("ai image detector": ..., 5, 5, 0, 100 on 2026-10-12),
# which reads as a rise. Every reader drops them (settled()).
UNSETTLED_DAYS = 2


def settled(points: list[tuple], today: date) -> list[tuple]:
    """A daily Trends series without its last UNSETTLED_DAYS days. Rows are (day, value, ...)."""
    cut = today - timedelta(days=UNSETTLED_DAYS)
    return [p for p in points if p[0] < cut]


def rose(points: list[tuple[date, int]], first_seen: date) -> dict:
    """One variant's Trends series judged against its own baseline."""
    values = [v for _, v in points]
    if not values or max(values) == 0:
        return {"rose": False, "no_data": True}
    before = [v for d, v in points if d < first_seen]
    after = [v for d, v in points if d >= first_seen]
    base = statistics.mean(before) if before else 0.0
    peak_after = max(after, default=0)
    peak = max(values)
    out = {"base": round(base, 1), "peak_after": peak_after, "no_data": False,
           "rose": peak_after >= max(base * RISE_FACTOR, base + RISE_MIN)}
    if out["rose"]:
        line = base + RISE_SHARE * (peak - base)
        out["first_rise"] = next(d for d, v in points if v >= max(line, 1) and v > base).isoformat()
    return out


def verdict(per_variant: dict[str, dict], first_seen: date) -> tuple[str, int | None]:
    risen = [v for v in per_variant.values() if v.get("rose")]
    if risen:
        first = min(date.fromisoformat(v["first_rise"]) for v in risen)
        return "hit", (first - first_seen).days
    if all(v.get("no_data") for v in per_variant.values()):
        return "no_data", None
    return "miss", None


def _series(task: dict) -> list[tuple[date, int]]:
    try:
        item = task["result"][0]["items"][0]
    except (KeyError, IndexError, TypeError):
        return []
    return [(date.fromisoformat(p["date_from"]), int((p.get("values") or [0])[0] or 0)) for p in item.get("data", [])]


def plan_calls(due: list[tuple], today: date, after_days: int = 14) -> tuple[list[dict], list[tuple]]:
    """One Trends call per search variant of each due lead, over its review window."""
    calls, plan = [], []
    for i, (term, first_seen, variants) in enumerate(due):
        keys = (list(variants) or [term])[:MAX_VARIANTS]
        window = {"date_from": (first_seen - timedelta(days=BEFORE_DAYS)).isoformat(),
                  "date_to": min(first_seen + timedelta(days=after_days), today).isoformat()}
        ids = [f"r{i}v{j}" for j in range(len(keys))]
        calls += [{"call_id": cid, "tool": TOOL, "arguments": {"body": [{"keywords": [k], **window}]}}
                  for cid, k in zip(ids, keys)]
        plan.append((ids, term, first_seen, keys))
    return calls, plan


def review(cfg: dict, conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    c = cfg["review"]
    due = store.due(conn, today, c["after_days"], c["per_day"])
    if not due:
        return {"checked": [], "cost_micros": 0, "after_days": c["after_days"]}
    calls, plan = plan_calls(due, today, c["after_days"])
    data, cost = data_backend.call_all(calls, cfg, capability="trends")
    checked = []
    for ids, term, first_seen, keys in plan:
        if not all(cid in data for cid in ids):
            continue   # stays due; the next run takes it again
        per = {k: rose(settled(_series((data[cid].get("tasks") or [{}])[0]), today), first_seen) for cid, k in zip(ids, keys)}
        v, lead = verdict(per, first_seen)
        store.save_verdict(conn, term, v, lead, per, today)
        checked.append({"term": term, "first_seen": first_seen.isoformat(), "verdict": v, "lead_days": lead})
    return {"checked": checked, "cost_micros": cost, "after_days": c["after_days"]}
