"""Signal ②: one new repo taking off on its own, anywhere on GitHub.

A wave of repos sharing a word shows up days after the idea started. Strata
(Niko1221/Strata) was one repo, born 2026-09-24, at 7.7k stars nine days later; its name
was being searched from 9/26 while no second repo carried it. This signal watches the
repo itself.

GitHub search: repos created in the last `window_days` with at least `min_stars`, two
pages. A repo breaks out when it averages `velocity_min` stars a day since birth, or
gained `delta_min` since the last run's snapshot. For new breakouts the owner's display
name is fetched too, because people search it ("niko strata", not "niko1221").

A repo stays above the bar for days. It becomes a lead term once, the day it first
breaks out (remembered for FLAGGED_DAYS); the report's table lists all of them.
A GitHub token (GITHUB_TOKEN) is optional and only raises the rate limit.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import UA, snap
from .i18n import t

PAGES = 2
TOP = 15
FLAGGED_DAYS = 30
API = "https://api.github.com"


def _get(path: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=30) as res:
        return json.load(res)


def repo_term(name: str) -> str:
    """'universal-modder' -> 'universal modder'; 'OpenDots' -> 'opendots'."""
    return re.sub(r"[-_.]+", " ", name).strip().lower()


def _speed(row: dict) -> int:
    return -max(row["velocity"], row["delta"] or 0)


def score(repos: list[dict], before: dict[str, int] | None, now: datetime,
          velocity_min: int = 200, delta_min: int = 400) -> list[dict]:
    """Repos that break out, fastest first. `repos` items are GitHub search results."""
    out = []
    for r in repos:
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        age = max((now - created).total_seconds() / 86400, 1.0)
        stars = r["stargazers_count"]
        velocity = stars / age
        delta = stars - before[r["full_name"]] if before and r["full_name"] in before else None
        if velocity < velocity_min and (delta is None or delta < delta_min):
            continue
        owner, name = r["full_name"].split("/", 1)
        out.append({"repo": r["full_name"], "owner": owner, "name": name, "stars": stars,
                    "velocity": round(velocity), "delta": delta, "age_days": round(age, 1),
                    "born": created.date().isoformat(), "description": (r.get("description") or "")[:120]})
    out.sort(key=_speed)
    return out[:TOP]


def shown(rows: list[dict]) -> list[dict]:
    """At most TOP rows, fastest first: the first-time breakouts (the TOP fastest of them),
    then the fastest of those reported before. Rows need 'new'."""
    new = [r for r in rows if r["new"]][:TOP]
    return sorted(new + [r for r in rows if not r["new"]][:TOP - len(new)], key=_speed)


def variants(row: dict, display: str) -> list[str]:
    """The ways people end up searching a repo: its name, the owner's handle with it,
    and the owner's display name with it (Strata: 'strata', 'niko1221 strata', 'niko strata')."""
    term = repo_term(row["name"])
    out = [term, f"{row['owner'].lower()} {term}"]
    first = (display or "").split(" ")[0].lower()
    if first and first != row["owner"].lower() and first.isascii() and first.isalpha():
        out.append(f"{first} {term}")
    return out


def _search(query: str) -> list[dict]:
    q = urllib.parse.quote(query)
    repos: list[dict] = []
    for page in range(1, PAGES + 1):
        items = _get(f"/search/repositories?q={q}&sort=stars&order=desc&per_page=100&page={page}").get("items", [])
        repos += items
        if len(items) < 100:
            break
    return repos


def breakouts(cfg: dict, state: Path) -> dict:
    c = cfg["breakout"]
    today_d = date.today()
    since = (today_d - timedelta(days=c["window_days"])).isoformat()
    repos = _search(f"created:>={since} stars:>={c['min_stars']}")
    seen = {r["full_name"] for r in repos}
    if c.get("young_days") and c.get("young_min_stars"):
        young_since = (today_d - timedelta(days=c["young_days"])).isoformat()
        try:
            for r in _search(f"created:>={young_since} stars:>={c['young_min_stars']}"):
                if r["full_name"] not in seen:
                    repos.append(r)
                    seen.add(r["full_name"])
        except (OSError, ValueError):
            pass
    if c.get("late_days") and c.get("late_min_stars"):
        late_from = (today_d - timedelta(days=c["late_days"])).isoformat()
        late_to = (today_d - timedelta(days=c["window_days"] + 1)).isoformat()
        try:
            for r in _search(f"created:{late_from}..{late_to} stars:>={c['late_min_stars']}"):
                if r["full_name"] not in seen:
                    repos.append(r)
                    seen.add(r["full_name"])
        except (OSError, ValueError):
            pass
    path = state / "gh_stars.json.gz"
    before = snap.load(path)
    all_rows = []
    now = datetime.now(timezone.utc)
    for r in repos:
        all_rows += score([r], before, now, c["velocity_min"], c["delta_min"])
    all_rows.sort(key=_speed)
    if repos:
        snap.save(path, {r["full_name"]: r["stargazers_count"] for r in repos})
    flagged_path = state / "gh_flagged.json.gz"
    flagged: dict[str, str] = snap.load(flagged_path) or {}
    today = today_d.isoformat()
    for row in all_rows:
        row["new"] = row["repo"] not in flagged
    rows = shown(all_rows)
    for row in rows:
        flagged.setdefault(row["repo"], today)
    cutoff = (today_d - timedelta(days=FLAGGED_DAYS)).isoformat()
    snap.save(flagged_path, {k: v for k, v in flagged.items() if v >= cutoff})
    terms = []
    for row in (r for r in rows if r["new"]):
        try:
            display = _get(f"/users/{row['owner']}").get("name") or ""
        except OSError:
            display = ""
        row["variants"] = variants(row, display)
        gain = t("note_breakout_gain", delta=row["delta"]) if row["delta"] is not None else ""
        terms.append({"term": row["variants"][0], "recent": 1, "variants": row["variants"], "repo": row["repo"],
                      "born": row.get("born", ""),
                      "note": t("note_breakout", repo=row["repo"], stars=row["stars"], velocity=row["velocity"], gain=gain),
                      "examples": [f"{row['repo']}: {row['description']}"]})
    return {"searched": len(repos), "first_run": before is None, "repos": rows, "terms": terms,
            "window_days": c["window_days"]}
