"""The radar's memory: every lead it reported, in one SQLite file (state/radar.db).

record()          upserts today's leads.
due()             leads old enough to check in DataForSEO Trends and not checked yet.
save_verdict()    hit / miss / no_data and how many days the radar was ahead.
recent_sources()  X and Hugging Face leads the frequent job kept since yesterday.
summary()         hit rate and median lead per signal.

Lists are stored as JSON text; the file is small (one row per term) and safe to keep in
a CI cache or to commit.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path

NOTES_KEPT = 10
SUMMARY_WINDOW = 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  term        TEXT PRIMARY KEY,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  signals     TEXT NOT NULL DEFAULT '[]',
  notes       TEXT NOT NULL DEFAULT '[]',
  named       REAL,
  variants    TEXT NOT NULL DEFAULT '[]',
  reviewed_at TEXT,
  verdict     TEXT,
  lead_days   INTEGER,
  trends      TEXT
)"""


def connect(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_dir / "radar.db")
    conn.execute(SCHEMA)
    return conn


def _merge(old: str, new: list[str], keep: int | None = None) -> str:
    out = json.loads(old)
    out += [x for x in new if x not in out]
    return json.dumps(out[-keep:] if keep else out, ensure_ascii=False)


def record(conn: sqlite3.Connection, rows: list[dict], today: date | None = None) -> int:
    """rows: report leads with term, kinds (signal names), signals (report lines),
    optional named and variants."""
    day = (today or date.today()).isoformat()
    for r in rows:
        old = conn.execute("SELECT signals, notes, named, variants FROM leads WHERE term = ?", (r["term"],)).fetchone()
        kinds, notes, variants = sorted(set(r["kinds"])), list(r["signals"]), list(r.get("variants", []))
        if old is None:
            conn.execute("INSERT INTO leads (term, first_seen, last_seen, signals, notes, named, variants) VALUES (?,?,?,?,?,?,?)",
                         (r["term"], day, day, json.dumps(kinds), json.dumps(notes[-NOTES_KEPT:], ensure_ascii=False),
                          r.get("named"), json.dumps(variants, ensure_ascii=False)))
        else:
            named = r.get("named") if r.get("named") is not None else old[2]
            conn.execute("UPDATE leads SET last_seen = ?, signals = ?, notes = ?, named = ?, variants = ? WHERE term = ?",
                         (day, json.dumps(sorted(set(json.loads(old[0])) | set(kinds))), _merge(old[1], notes, NOTES_KEPT),
                          named, _merge(old[3], variants), r["term"]))
    conn.commit()
    return len(rows)


MAX_REVIEW_AGE_DAYS = 90


def due(conn: sqlite3.Connection, today: date, after_days: int, limit: int,
        max_age_days: int = MAX_REVIEW_AGE_DAYS) -> list[tuple[str, date, list[str]]]:
    oldest = (today - timedelta(days=max_age_days)).isoformat()
    cutoff = (today - timedelta(days=after_days)).isoformat()
    rows = conn.execute("SELECT term, first_seen, variants FROM leads WHERE reviewed_at IS NULL AND first_seen >= ? AND first_seen <= ? "
                        "ORDER BY first_seen, term LIMIT ?", (oldest, cutoff, limit)).fetchall()
    return [(t, date.fromisoformat(f), json.loads(v)) for t, f, v in rows]


def save_verdict(conn: sqlite3.Connection, term: str, verdict: str, lead_days: int | None, trends: dict,
                 today: date | None = None) -> None:
    conn.execute("UPDATE leads SET reviewed_at = ?, verdict = ?, lead_days = ?, trends = ? WHERE term = ?",
                 ((today or date.today()).isoformat(), verdict, lead_days, json.dumps(trends), term))
    conn.commit()


def recent_sources(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    since = ((today or date.today()) - timedelta(days=1)).isoformat()
    rows = conn.execute("SELECT term, signals, notes, named FROM leads WHERE last_seen >= ? ORDER BY last_seen DESC, term",
                        (since,)).fetchall()
    out = [{"term": t, "kinds": json.loads(k), "note": (json.loads(n) or [""])[-1], "named": named} for t, k, n, named in rows]
    return [r for r in out if {"x", "hf"} & set(r["kinds"])][:20]


def summary(conn: sqlite3.Connection, today: date | None = None) -> dict:
    since = ((today or date.today()) - timedelta(days=SUMMARY_WINDOW)).isoformat()
    rows = conn.execute("SELECT signals, verdict, lead_days FROM leads WHERE reviewed_at >= ? AND verdict IS NOT NULL",
                        (since,)).fetchall()
    pending = conn.execute("SELECT count(*) FROM leads WHERE reviewed_at IS NULL").fetchone()[0]
    by: dict[str, dict] = {}
    for kinds, verdict, lead in rows:
        for k in json.loads(kinds) or ["?"]:
            s = by.setdefault(k, {"checked": 0, "hit": 0, "leads": []})
            s["checked"] += 1
            if verdict == "hit":
                s["hit"] += 1
                s["leads"].append(lead)
    for s in by.values():
        leads = s.pop("leads")
        s["median_lead"] = statistics.median(leads) if leads else None
    return {"by_signal": by, "pending": pending, "window_days": SUMMARY_WINDOW}


def leads(conn: sqlite3.Connection, days: int = 7, today: date | None = None) -> list[dict]:
    """Leads seen in the last `days` days, newest first, with their review verdict when there is one."""
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT term, first_seen, last_seen, signals, notes, named, variants, verdict, lead_days FROM leads "
                        "WHERE last_seen >= ? ORDER BY last_seen DESC, first_seen DESC, term", (since,)).fetchall()
    return [{"term": t, "first_seen": f, "last_seen": l, "signals": json.loads(k), "note": (json.loads(n) or [""])[-1],
             "name_score": named, "variants": json.loads(v), "verdict": verdict, "days_ahead": ahead}
            for t, f, l, k, n, named, v, verdict, ahead in rows]
