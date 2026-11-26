"""The radar's review: check every lead in DataForSEO Trends two weeks later.

review()  takes up to `per_day` leads first seen `after_days` ago or earlier and not yet
          checked, asks DataForSEO Trends (through Glasser or AIsa) about each search variant, and
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

import re
import sqlite3
import statistics
from datetime import date, timedelta

from . import backend, store

BEFORE_DAYS = 30
MAX_VARIANTS = 3      # each costs one Trends task ($0.0012 in October 2026)
RISE_FACTOR, RISE_MIN = 2.0, 10
RISE_SHARE = 0.25     # the rise starts where a series first covers this share of base -> peak
TOOL = "post_dataforseo_keywords_trends_explore_live"
# Google has not settled the last two days of a daily series: they come back as 0 and
# then 100 for unrelated terms ("ai image detector": ..., 5, 5, 0, 100 on 2026-10-12),
# which reads as a rise. Every reader drops them (settled()).
UNSETTLED_DAYS = 2


EARLY_DAYS = 1
OUTCOMES = ("early", "late", "unclear", "miss", "no_data")
RECORDING_START = date(2026, 10, 3)
PRE_EXISTING_DAYS = 1
BORN_NOTE = re.compile(r"★(\d+) \((?:均 )?(\d+)/")


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
    after_pts = [(d, v) for d, v in points if d >= first_seen]
    after = [v for _, v in after_pts]
    base = statistics.mean(before) if before else 0.0
    peak_after = max(after, default=0)
    out = {"base": round(base, 1), "peak_after": peak_after, "no_data": False,
           "rose": peak_after >= max(base * RISE_FACTOR, base + RISE_MIN)}
    if out["rose"]:
        line = base + RISE_SHARE * (peak_after - base)
        out["first_rise"] = next(d for d, v in after_pts if v >= max(line, 1) and v > base).isoformat()
        earliest = next(d for d, v in points if v >= max(line, 1) and v > base).isoformat()
        if earliest != out["first_rise"]:
            out["earliest_rise"] = earliest
    return out


def verdict(per_variant: dict[str, dict], first_seen: date) -> tuple[str, int | None]:
    risen = [v for v in per_variant.values() if v.get("rose")]
    if risen:
        first = min(date.fromisoformat(v["first_rise"]) for v in risen)
        return "hit", (first - first_seen).days
    if all(v.get("no_data") for v in per_variant.values()):
        return "no_data", None
    return "miss", None


def born_day(born: str | None, notes: list[str] | None, first_seen: date) -> date | None:
    """The day the repository a breakout lead came from was created, when known."""
    if born:
        return date.fromisoformat(born)
    for note in notes or []:
        m = BORN_NOTE.search(note)
        if m and int(m.group(2)):
            return first_seen - timedelta(days=round(int(m.group(1)) / int(m.group(2))))
    return None


def earliest_rise(trends: dict | None) -> date | None:
    """The earliest day a variant crossed the rise line (including pre-existing baseline rises)."""
    days = [v.get("earliest_rise") or v.get("first_rise") for v in (trends or {}).values()
            if isinstance(v, dict) and (v.get("earliest_rise") or v.get("first_rise"))]
    return date.fromisoformat(min(days)) if days else None


def unclear(first_seen: date, rise: date | None, born: date | None = None) -> str:
    """Why a hit's lead_days cannot tell how early the radar was, or '' when it is clear."""
    if first_seen == RECORDING_START:
        return "recording_start"
    if rise and born and (born - rise).days > PRE_EXISTING_DAYS:
        return "pre_existing"
    return ""


def outcome(said: str, lead_days: int | None, vague: bool | str = False) -> str:
    """A stored verdict classified into early / late / unclear / miss / no_data."""
    if said == "hit":
        if vague:
            return "unclear"
        return "early" if lead_days is not None and lead_days >= EARLY_DAYS else "late"
    return "no_data" if said == "no_data" else "miss"


def tally(rows: list[tuple]) -> dict[str, dict]:
    """{signal: {"checked", "early", "late", "unclear", "miss", "no_data", "median_lead"}} from
    (signals, verdict, lead_days[, vague]) rows; median_lead is over early hits."""
    by: dict[str, dict] = {}
    for kinds, said, lead_days, *vague in rows:
        got = outcome(said, lead_days, bool(vague and vague[0]))
        for k in kinds or ["?"]:
            s = by.setdefault(k, {"checked": 0, **dict.fromkeys(OUTCOMES, 0), "leads": []})
            s["checked"] += 1
            s[got] += 1
            if got == "early" and lead_days is not None:
                s["leads"].append(lead_days)
    for s in by.values():
        leads = s.pop("leads")
        s["median_lead"] = statistics.median(leads) if leads else None
    return by


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
    data, cost = backend.call_all(calls)
    checked = []
    for ids, term, first_seen, keys in plan:
        if not all(cid in data for cid in ids):
            if (today - first_seen).days > c["after_days"] + 14:
                store.save_verdict(conn, term, "no_data", None, {}, today)
            continue   # stays due until the retry window expires
        per = {k: rose(settled(_series((data[cid].get("tasks") or [{}])[0]), today), first_seen) for cid, k in zip(ids, keys)}
        v, lead = verdict(per, first_seen)
        store.save_verdict(conn, term, v, lead, per, today)
        row = {"term": term, "first_seen": first_seen.isoformat(), "verdict": v, "lead_days": lead}
        if v == "hit":
            why = unclear(first_seen, earliest_rise(per))
            if why:
                row["unclear"] = why
        checked.append(row)
    return {"checked": checked, "cost_micros": cost, "after_days": c["after_days"]}
