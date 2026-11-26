"""Every seller a payment platform has, back to 2019: the history behind signal ⑧.

    python -m radar history pull   [--platform stripe] [--max-usd 20]
    python -m radar history update [--platform stripe] [--max-usd 3]
    python -m radar history read   [--workers 32]
    python -m radar history sort   [--max-usd 30] [--rules]
    python -m radar history        [--out sellers.csv]

`pull` reads every site that links to each platform today, one row per site with the day it
FIRST linked: DataForSEO's one-per-domain backlinks, oldest first, 1,000 a call ($0.06 on
2026-10-14). That day matched the site's referring-domain first_seen for every Polar site checked
(1,704). Stripe had about 143,000 such sites, Gumroad 140,000, Lemon Squeezy 18,000, Polar and
Creem about 2,000 each: about $18 for all five. Sites that took their link down are not in it, and
before mid-2021 the link graph saw few Stripe links. A pull stops at --max-usd and picks up where
it stopped on the next run.

`update` adds the sellers that appeared since the history's newest day: each platform's newest
links first, until a page reaches back past that day (an old seller's new page is skipped, about
two thirds of the rows; about $0.5 a week for all platforms). Run it weekly and the history grows
into a series: who started selling each week, and who added a second platform.

`read` fetches each seller's home page title and description (free, `--workers` at a time;
about 10 sites a second), `sort` has a small model sort the ones that answered by kind (an
estimated $0.005 for 60 sites; `--rules` sorts by keywords alone, free, and a later sort with the model
redoes those), and `history` alone counts sellers per month, platform and kind
and, with --out, writes every seller to a CSV. Only links that take money count as sellers (a
Gumroad product page, not a profile; see sellers.PLATFORMS); the rest are kept with pays = 0.

The rows are DataForSEO's: use them for research, do not republish them.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import backend, sellers

PAGE = 1000
KIND_BATCH = 60
KIND_MICROS = 5000          # an estimate: the chat endpoint does not report its price
READ_CHUNK = 500
READ_BYTES = 64_000      # off the wire: a gzipped head holds the title and description
SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (platform TEXT, host TEXT, first_seen TEXT, page TEXT, link TEXT, rank INTEGER, pays INTEGER,
    title TEXT, description TEXT, read_on TEXT, kind TEXT, kind_by TEXT, PRIMARY KEY (platform, host));
CREATE TABLE IF NOT EXISTS pulls (platform TEXT PRIMARY KEY, token TEXT, rows INTEGER, cost_micros INTEGER, done INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS updates (day TEXT, platform TEXT, since TEXT, read INTEGER, new INTEGER, paying INTEGER, elsewhere INTEGER,
    cost_micros INTEGER, error TEXT, PRIMARY KEY (day, platform));
CREATE INDEX IF NOT EXISTS sites_host ON sites (host);    -- read and sort update by host: without it each update scans the table
"""


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def connect(state: Path) -> sqlite3.Connection:
    db = sqlite3.connect(state / "sellers_history.db")
    db.executescript(SCHEMA)
    return db


def page(platform: str, token: str, order: str = "asc") -> tuple[list[dict], str, int, str]:
    """One call: (sites, the token for the next page, cost in micro-USD, error)."""
    body = {"target": sellers.PLATFORMS[platform][0], "mode": "one_per_domain", "limit": PAGE,
            "order_by": [f"first_seen,{order}"], "backlinks_status_type": "live"}
    if token:
        body["search_after_token"] = token      # every other field must stay as it was
    data, cost = backend.call([{"call_id": "h", "tool": sellers.TOOL, "arguments": {"body": [body]}}])
    task = ((data.get("h") or {}).get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        return [], token, cost, str(task.get("status_message") or "no answer")[:160]
    res = sellers.result(data, "h")
    return res.get("items") or [], res.get("search_after_token") or "", cost, ""


def row(platform: str, item: dict) -> tuple:
    host = (item.get("domain_from") or "").lower()
    link_host, path = sellers.split(item.get("url_to") or "")
    pays = sellers.PLATFORMS[platform][1](link_host, path) and not sellers.shared(host)
    return (platform, host, str(item.get("first_seen") or "")[:10], item.get("url_from") or "", item.get("url_to") or "",
            int(item.get("domain_from_rank") or 0), int(bool(pays)))


def progress(db: sqlite3.Connection, platform: str) -> list:
    got = db.execute("SELECT token, rows, cost_micros, done FROM pulls WHERE platform = ?", (platform,)).fetchone()
    return list(got) if got else ["", 0, 0, 0]


def pull_one(db: sqlite3.Connection, platform: str, budget: int) -> dict:
    """Pages of one platform until it is done or `budget` micro-USD are spent; saved after every page."""
    token, rows, cost, done = progress(db, platform)
    spent, error = 0, ""
    while not done and spent < budget:
        items, nxt, c, error = page(platform, token)
        spent, cost = spent + c, cost + c
        if not error:
            db.executemany("INSERT OR IGNORE INTO sites (platform, host, first_seen, page, link, rank, pays) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           [row(platform, i) for i in items if i.get("domain_from")])
            rows, token, done = rows + len(items), nxt, int(len(items) < PAGE or not nxt)
        db.execute("INSERT OR REPLACE INTO pulls VALUES (?, ?, ?, ?, ?, ?)", (platform, token, rows, cost, done, error))
        db.commit()
        log(f"{platform}: {rows:,} sites, ${cost / 1e6:.2f}" + (f", error: {error}" if error else ""))
        if error:
            break
    return {"rows": rows, "done": bool(done), "cost_micros": cost, "spent_micros": spent, "error": error}


def pull(db: sqlite3.Connection, platforms: list[str], max_micros: int) -> dict:
    out, spent = {}, 0
    for platform in platforms:
        out[platform] = pull_one(db, platform, max_micros - spent)
        spent += out[platform]["spent_micros"]
    return {"step": "pull", "platforms": out, "cost_micros": spent}


UPDATE_MARGIN = 3       # days before the history's newest day an update reads back to (the link graph lags)


def update_one(db: sqlite3.Connection, platform: str, budget: int, today: str) -> dict:
    """The sellers of one platform the history lacks, newest links first, back to its newest day less UPDATE_MARGIN."""
    newest = db.execute("SELECT max(first_seen) FROM sites WHERE platform = ?", (platform,)).fetchone()[0] or "2019-01-01"
    since = (date.fromisoformat(newest[:10]) - timedelta(days=UPDATE_MARGIN)).isoformat()
    known = {h for (h,) in db.execute("SELECT host FROM sites WHERE platform = ?", (platform,))}
    elsewhere = {h for (h,) in db.execute("SELECT DISTINCT host FROM sites WHERE platform != ? AND pays = 1", (platform,))}
    token, cost, read_rows, new, error = "", 0, 0, [], ""
    while cost < budget:
        items, token, spent, error = page(platform, token, "desc")
        cost += spent
        if error:
            break
        for item in items:
            host = (item.get("domain_from") or "").lower()
            if host and host not in known:
                known.add(host)
                new.append(row(platform, item))
        read_rows += len(items)
        oldest = min((str(i.get("first_seen") or "")[:10] for i in items), default="")
        if not token or len(items) < PAGE or oldest < since:
            break
    db.executemany("INSERT OR IGNORE INTO sites (platform, host, first_seen, page, link, rank, pays) VALUES (?, ?, ?, ?, ?, ?, ?)", new)
    result = {"since": since, "read": read_rows, "new": len(new), "paying": sum(r[6] for r in new),
              "elsewhere": sum(1 for r in new if r[6] and r[1] in elsewhere), "cost_micros": cost, "error": error}
    db.execute("INSERT OR REPLACE INTO updates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (today, platform, since, read_rows, len(new),
               result["paying"], result["elsewhere"], cost, error))
    db.commit()
    log(f"{platform}: {len(new):,} new sites since {since} ({result['paying']:,} take money), ${cost / 1e6:.2f}")
    return result


def update(db: sqlite3.Connection, platforms: list[str], max_micros: int, today: date | None = None) -> dict:
    """New sellers on each platform since the history's newest day; `elsewhere` counts the new ones that already
    sold through another platform (a second checkout, or a move)."""
    day, out, spent = (today or date.today()).isoformat(), {}, 0
    for platform in platforms:
        out[platform] = update_one(db, platform, max_micros - spent, day)
        spent += out[platform]["cost_micros"]
    return {"step": "update", "platforms": out, "cost_micros": spent}


def read(db: sqlite3.Connection, workers: int) -> dict:
    """Title and description of every seller's home page not read yet."""
    hosts = [h for (h,) in db.execute(
        "SELECT host FROM sites WHERE pays = 1 AND read_on IS NULL GROUP BY host ORDER BY max(first_seen) DESC")]   # newest first
    answered, today = 0, date.today().isoformat()
    with ThreadPoolExecutor(workers) as pool:
        for start in range(0, len(hosts), READ_CHUNK):
            chunk = hosts[start:start + READ_CHUNK]
            words = list(pool.map(lambda h: sellers.page_words(h, READ_BYTES), chunk))
            db.executemany("UPDATE sites SET title = ?, description = ?, read_on = ? WHERE host = ?",
                           [(t, d, today, h) for h, (t, d) in zip(chunk, words)])
            db.commit()
            answered += sum(1 for t, d in words if t or d)
            log(f"read {start + len(chunk):,}/{len(hosts):,} sites, {answered:,} answered")
    return {"step": "read", "read": len(hosts), "answered": answered}


def sort(db: sqlite3.Connection, model: str, max_micros: int, workers: int = 8) -> dict:
    """Kinds for the sellers whose page answered: one model call per `KIND_BATCH` until the estimated budget, or
    keyword rules alone (free) when `model` is empty. With a model, the ones sorted by rules before are sorted again."""
    todo = "(kind IS NULL OR kind_by = 'rules')" if model else "kind IS NULL"
    rows = [{"host": h, "title": t or "", "description": d or ""} for h, t, d in db.execute(
        f"SELECT host, max(title), max(description) FROM sites WHERE pays = 1 AND {todo} AND (title != '' OR description != '') "
        "GROUP BY host ORDER BY max(first_seen) DESC")]
    batches = [rows[i:i + KIND_BATCH] for i in range(0, len(rows), KIND_BATCH)]
    if model:
        batches = batches[:max(0, max_micros) // KIND_MICROS]
    done, by_rules = 0, 0
    with ThreadPoolExecutor(workers) as pool:
        for got, error in pool.map(lambda b: sellers.kinds(model, b), batches):
            by = "rules" if error or not model else "model"
            db.executemany("UPDATE sites SET kind = ?, kind_by = ? WHERE host = ?", [(k, by, h) for h, k in got.items()])
            db.commit()
            done, by_rules = done + len(got), by_rules + (len(got) if by == "rules" else 0)
            if done % 6000 < KIND_BATCH or done == len(rows):
                log(f"sorted {done:,}/{len(rows):,} sites" + (f", {by_rules:,} by keyword rules" if by_rules else ""))
    return {"step": "sort", "sorted": done, "by_rules": by_rules, "left": len(rows) - done,
            "cost_micros": len(batches) * KIND_MICROS if model else 0}


NETWORK_MIN = 5          # one payment link on this many sites is a network's (templates, link farms), not a seller's


def mark_networks(db: sqlite3.Connection) -> int:
    """Flag the sites whose payment link also sits on NETWORK_MIN or more other sites; returns how many."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(sites)")}
    if "network" not in cols:
        db.execute("ALTER TABLE sites ADD COLUMN network INTEGER DEFAULT 0")
    db.execute("""UPDATE sites SET network = (link IN (SELECT link FROM sites WHERE pays = 1 GROUP BY link
                                                       HAVING count(DISTINCT host) >= ?))""", (NETWORK_MIN,))
    db.commit()
    return db.execute("SELECT count(DISTINCT host) FROM sites WHERE network = 1").fetchone()[0]


def clean_defaults(db: sqlite3.Connection) -> int:
    """Blank the builders' default words read before sellers.own_words; a site left with no words of its own
    loses the kind sorted from them. Returns how many sites changed."""
    rows = db.execute("SELECT DISTINCT host, coalesce(title, ''), coalesce(description, '') FROM sites "
                      "WHERE pays = 1 AND (title != '' OR description != '')").fetchall()
    changed = [(h, *sellers.own_words(t, d)) for h, t, d in rows if sellers.own_words(t, d) != (t, d)]
    ai = ", ai = NULL" if "ai" in {r[1] for r in db.execute("PRAGMA table_info(sites)")} else ""   # the Jev pass adds it
    for h, t, d in changed:
        db.execute("UPDATE sites SET title = ?, description = ? WHERE host = ?", (t, d, h))
        if not t and not d:
            db.execute(f"UPDATE sites SET kind = NULL, kind_by = NULL{ai} WHERE host = ?", (h,))
    db.commit()
    return len({h for h, _, _ in changed})


def drop_shared(db: sqlite3.Connection) -> int:
    """Sites anyone posts on (sellers.shared), stored as sellers before the rule knew them, set to pays = 0."""
    hosts = [h for (h,) in db.execute("SELECT DISTINCT host FROM sites WHERE pays = 1") if sellers.shared(h)]
    db.executemany("UPDATE sites SET pays = 0 WHERE host = ?", [(h,) for h in hosts])
    db.commit()
    return len(hosts)


def summary(db: sqlite3.Connection, out: Path | None = None) -> dict:
    """Sellers per month and platform, per year and kind, how far each pull got; every seller to `out` as CSV.
    Builders' default words are cleared first; sites on a network's payment link are left out of the counts and
    marked in the CSV."""
    drop_shared(db)
    defaults = clean_defaults(db)
    networked = mark_networks(db)
    q = lambda sql: [list(r) for r in db.execute(sql)]  # noqa: E731
    result = {"step": "report",
              "pulls": q("SELECT platform, rows, done, cost_micros, error FROM pulls ORDER BY platform"),
              "sellers": q("SELECT platform, count(*), sum(pays) FROM sites GROUP BY platform ORDER BY platform"),
              "networked": networked, "defaults": defaults,
              "by_month": q("SELECT substr(first_seen, 1, 7) m, platform, count(*) FROM sites WHERE pays = 1 AND network = 0 GROUP BY m, platform ORDER BY m"),
              "by_year_kind": q("SELECT substr(first_seen, 1, 4) y, coalesce(kind, CASE WHEN read_on IS NULL THEN 'unread' "
                                "WHEN title != '' OR description != '' THEN 'unsorted' ELSE 'no answer' END) k, count(*) "
                                "FROM sites WHERE pays = 1 AND network = 0 GROUP BY y, k ORDER BY y, k")}
    if out:
        cols = ["platform", "host", "first_seen", "kind", "title", "description", "page", "link", "rank", "network"]
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(db.execute(f"SELECT {', '.join(cols)} FROM sites WHERE pays = 1 ORDER BY first_seen DESC"))
        result["csv"] = str(out)
    return result


def text(result: dict) -> str:
    """A short Markdown view of what a step did, or of the whole history."""
    if result["step"] != "report":
        return "\n".join(f"{k}: {v}" for k, v in result.items() if k != "step")
    lines = ["| Platform | Sites read | Sellers (link takes money) | Pull done | Cost |", "|---|---|---|---|---|"]
    pulls = {p: (rows, done, cost) for p, rows, done, cost, _ in result["pulls"]}
    for p, n, paying in result["sellers"]:
        rows, done, cost = pulls.get(p, (0, 0, 0))
        lines.append(f"| {sellers.LABEL.get(p, p)} | {n:,} | {paying or 0:,} | {'yes' if done else 'no'} | ${cost / 1e6:.2f} |")
    years = sorted({y for y, _, _ in result["by_year_kind"]})
    kinds = sorted({k for _, k, _ in result["by_year_kind"]})
    cell = {(y, k): n for y, k, n in result["by_year_kind"]}
    if years:
        lines += ["", "| Year | " + " | ".join(kinds) + " |", "|---" * (len(kinds) + 1) + "|"]
        lines += [f"| {y} | " + " | ".join(f"{cell.get((y, k), 0):,}" for k in kinds) + " |" for y in years]
    return "\n".join(lines + (["", f"CSV: {result['csv']}"] if result.get("csv") else []))
