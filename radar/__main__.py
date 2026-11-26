"""new-term-radar: terms that are starting to surge, earliest signal first.

  python -m radar daily    [--config config.json] [--state state] [--out report.md] [--skip x,hf,...] [--new-sites] [--mail]
  python -m radar sources  [--config config.json] [--state state] [--out sources.md] [--mail]

Signals:
  ① x         names the accounts you follow said for the first time (AIsa)
  ① hf        models new to Hugging Face's trending top 50
  ② breakout  one new GitHub repo taking off on its own
  ③ sitemaps  pages other sites added since the last run
  ③ newsites  domains registered in the last 90 days that already rank (weekly; Glasser or AIsa)
  ⑥ revenue   products people pay for, growing fastest or newly listed (weekly; TrustMRR key)
  ⑦ asks      what people ask for on Reddit ("is there an AI tool that…"), sized (weekly; Glasser or AIsa)
  ⑧ sellers   sites that just put up a payment link: Stripe, Polar, Lemon Squeezy, Gumroad, Creem, Paddle (weekly; Glasser or AIsa)
  ⑧ builders  sites just built with Lovable or v0, newsletters just started on beehiiv (weekly; Glasser or AIsa)
  ⑨ feeds     just listed: Product Hunt, Show HN, OpenRouter, Civitai, App Store grossing, awesome lists,
              Google's new pages under Figma / There's An AI For That / Chrome Web Store, trending searches,
              rising searches next to root words (daily; the paid ones weekly)
  ⑩ money     the other sites of a paying product's AdSense account; new advertisers on your terms (weekly; GoAnyAPI)
  ④ gsc       queries your own site got impressions for, for the first time (optional)
  ⑤ review    leads from 14 days ago checked in DataForSEO Trends; hit rate per signal (Glasser or AIsa)
  usecases    the generic terms behind today's names, sized against a benchmark keyword (Glasser or AIsa; the posts: AIsa)

  python -m radar keywords "name one" "name two" [--repo "name one=owner/repo"] [--json]
  python -m radar leads    [--days 7] [--json]
  python -m radar pains    "keyword" [--extension ID] [--json]   what people complain about for a kind of tool
  python -m radar history  [pull|update|read|sort] [--platform stripe] [--max-usd 20] [--out sellers.csv]
                           every seller on the payment platforms of ⑧, back to 2019 (about $18 for all; see radar/seller_history.py)
  python -m radar mcp      the MCP server on stdio (needs the `mcp` package)

`keywords` builds the keyword table for names you give it, now (about $0.05 a name plus
$0.09 a call for the volume lookup). `leads` lists what the radar has kept, with the
review's verdicts and the hit rate per signal; it costs nothing.

`sources` runs only ① (they change by the hour) and writes a report only when there is a
name or an error. `daily` runs everything; give it `--skip x,hf` when `sources` runs
apart, and it lists the ① leads of the last day from the database instead.

A small model scores every term: the name of a specific new thing, or a common word?
Names lead the report and are kept in state/radar.db for the review.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from . import asks, breakout, config, feeds, gsc, money, new_sites, notify, pains, report, review, revenue, seller_history, sellers, \
    sidelights, sitemaps, sources, store, usecases
from .i18n import t

SKIPPABLE = {"x", "hf", "breakout", "sitemaps", "newsites", "revenue", "asks", "sellers", "gsc", "review", "judge", "usecases", "tutorials",
             "feeds", "builders", "money", "sidelights"}


def run(fn, skip: bool = False) -> dict:
    """One signal failing still leaves a report: its error takes its place. Each block keeps how long it took."""
    if skip:
        return {"skipped": True}
    start = time.monotonic()
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        out = {"error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:160]}"}
    if isinstance(out, dict):
        out["seconds"] = round(time.monotonic() - start)
    return out


def _finish(args, text: str, subject: str) -> None:
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    if args.mail:
        print(notify.send(subject, text))


def cmd_sources(args, cfg: dict, state: Path) -> int:
    blocks = {"x": run(lambda: sources.x_new(cfg, state), "x" in args.skip),
              "hf": run(lambda: sources.hf_new(cfg, state), "hf" in args.skip)}
    lead, judged = report.leads(cfg, blocks)
    conn = store.connect(state)
    extra = {"judge": judged, "record": run(lambda: {"recorded": store.record(conn, report.to_record(cfg, lead))})}
    errors = any(b.get("error") for b in (*blocks.values(), *extra.values()))
    if report.named(cfg, lead) or errors:
        _finish(args, report.sources(cfg, lead, blocks, extra), f"New-term radar · sources {date.today()}")
    else:
        print(t("sources_quiet") + "; ".join(f"{k} {v.get('posts', v.get('models', t('dash')))}" for k, v in blocks.items()))
    return 0


def cmd_daily(args, cfg: dict, state: Path) -> int:
    weekly = args.new_sites or date.today().weekday() == cfg["new_sites"]["weekday"]
    blocks = {
        "x": run(lambda: sources.x_new(cfg, state), "x" in args.skip),
        "hf": run(lambda: sources.hf_new(cfg, state), "hf" in args.skip),
        "breakout": run(lambda: breakout.breakouts(cfg, state), "breakout" in args.skip),
        "sitemap": run(lambda: sitemaps.diff(cfg, state), "sitemaps" in args.skip),
        "newsite": run(lambda: new_sites.new_sites(cfg, state), "newsites" in args.skip or not weekly),
        "revenue": run(lambda: revenue.products(cfg, state), "revenue" in args.skip or not (
            args.revenue or date.today().weekday() == cfg["revenue"]["weekday"])),
        "ask": run(lambda: asks.asks(cfg, state), "asks" in args.skip or not (
            args.asks or date.today().weekday() == cfg["asks"]["weekday"])),
        "seller": run(lambda: sellers.sellers(cfg, state), "sellers" in args.skip or not (
            args.sellers or date.today().weekday() == cfg["sellers"]["weekday"])),
        "builder": run(lambda: sellers.builders(cfg, state), "builders" in args.skip or not (
            args.sellers or date.today().weekday() == cfg["builders"]["weekday"])),
        "feed": run(lambda: feeds.feeds(cfg, state, weekly=args.feeds), "feeds" in args.skip),
    }
    found = run(lambda: gsc.gsc_new(cfg), "gsc" in args.skip)
    lead, judged = report.leads(cfg, blocks)
    conn = store.connect(state)
    extra = {"judge": judged, "record": run(lambda: {"recorded": store.record(conn, report.to_record(cfg, lead))})}
    # What the viewers of a widely shared walkthrough ask for: the one-click tool behind a ⚡ lead.
    extra["tutorials"] = run(lambda: pains.tutorial_asks(cfg, report.named(cfg, lead)),
                             "tutorials" in args.skip or not cfg["tutorials"]["max_leads"])
    # With ① run apart, the daily report lists what that job kept since yesterday.
    extra["recent_sources"] = run(lambda: {"rows": store.recent_sources(conn)}, not {"x", "hf"} <= args.skip)
    # The names to read for their generic terms: things, not words that surged (usecases.names_to_read).
    names = usecases.names_to_read(report.to_record(cfg, lead), extra["recent_sources"].get("rows", []))
    repos = {r["term"]: r["repo"] for r in lead if r.get("repo")}
    about = {r["term"]: r["about"] for r in lead if r.get("about")}      # a paying product's own description
    extra["usecases"] = run(lambda: usecases.run(cfg, state, list(dict.fromkeys(names)), repos, texts=about),
                            "usecases" in args.skip or not cfg["usecases"]["per_day"])
    run(lambda: store.record(conn, report.usecase_leads(extra["usecases"])))
    # Side evidence spends its whois lookups on the names people search most, by the table's own estimate.
    extra["sidelights"] = run(lambda: sidelights.evidence(cfg["sidelights"], by_searches(list(dict.fromkeys(names)), extra["usecases"])),
                              "sidelights" in args.skip)
    # ⑩ waits for the keyword table: an AdSense ID belongs to an established site, and the tool sites already on
    # page one for the day's terms are the established ones; ⑥'s and ⑧'s products come after them.
    blocks["money"] = run(lambda: money.money(cfg, state, page_one_tools(extra["usecases"], cfg["money"]["max_dr"]) + paying_sites(blocks)),
                          "money" in args.skip or not (args.sellers or args.revenue or date.today().weekday() == cfg["money"]["weekday"]))
    extra["tails"] = run(lambda: sidelights.tails(cfg["sidelights"], paying_names(blocks)),
                         "sidelights" in args.skip or not paying_names(blocks))
    rev = run(lambda: review.review(cfg, conn), "review" in args.skip)
    summ = run(lambda: store.summary(conn))
    text = report.daily(cfg, lead, blocks, found, rev, summ, extra)
    if args.out:
        Path(args.out).with_suffix(".json").write_text(json.dumps(
            {"leads": lead, **blocks, "gsc": found, "review": rev, "summary": summ, "keywords": extra["usecases"]}, ensure_ascii=False, indent=1, default=str))
    _finish(args, text, f"New-term radar {date.today()}")
    return 0


def paying_sites(blocks: dict) -> list[str]:
    """Domains of this week's paying products: ⑥'s websites, then ⑧'s sellers."""
    sites = [urlsplit(r["about"][0] if "//" in r["about"][0] else "//" + r["about"][0]).netloc
             for r in blocks.get("revenue", {}).get("terms", []) if r.get("about") and "." in r["about"][0]]
    return [s.removeprefix("www.") for s in sites + [x["host"] for x in blocks.get("seller", {}).get("sellers", [])] if s]


def page_one_tools(uc: dict, max_dr: float = 60) -> list[str]:
    """The tool sites on page one for the terms worth acting on, most established first, up to a rating of
    `max_dr`: above it are github.com, apps.microsoft.com and media groups, not someone's portfolio."""
    tools = [x for r in uc.get("rows", []) if r.get("decision") in ("prioritize", "watch")
             for x in (r.get("competition") or {}).get("tools", []) if x.get("dr") is None or x["dr"] <= max_dr]
    return list(dict.fromkeys(x["domain"] for x in sorted(tools, key=lambda x: -(x.get("dr") or 0))))


def by_searches(names: list[str], uc: dict) -> list[str]:
    """The names sorted by the keyword table's estimated daily searches; names it did not read keep their order after."""
    per_day = {r["phrase"]: r.get("per_day") or 0 for r in uc.get("rows", []) if r.get("kind") == "name"}
    return sorted(names, key=lambda n: -per_day.get(n, -1))


def paying_names(blocks: dict) -> list[str]:
    return [r["term"] for k in ("revenue", "seller") for r in blocks.get(k, {}).get("terms", [])]


def keyword_table(cfg: dict, state: Path, names: list[str], repos: dict[str, str]) -> tuple[dict, str]:
    """The keyword table for names someone asked about: (the data, the table in Markdown)."""
    uc = usecases.run(cfg, state, [n.lower().strip() for n in names if n.strip()], {k.lower(): v for k, v in repos.items()}, on_request=True)
    return uc, "\n".join(report.keyword_table(cfg, uc)).strip() + "\n"


def kept_leads(state: Path, days: int) -> dict:
    conn = store.connect(state)
    return {"leads": store.leads(conn, days), "hit_rates": store.summary(conn)}


def cmd_keywords(args, cfg: dict, state: Path) -> int:
    repos = dict(r.split("=", 1) for r in args.repo)
    uc, text = keyword_table(cfg, state, args.names, repos)
    out = json.dumps(uc, ensure_ascii=False, indent=1, default=str) if args.json else text
    if args.out:
        Path(args.out).write_text(out)
    print(out)
    return 0


def cmd_pains(args, cfg: dict) -> int:
    keyword = " ".join(args.names).strip()
    if not keyword:
        raise SystemExit('pains: give a keyword, e.g. python -m radar pains "ai headshot"')
    result = pains.run(cfg, keyword, args.extension)
    out = json.dumps(result, ensure_ascii=False, indent=1, default=str) if args.json else report.pains(result)
    if args.out:
        Path(args.out).write_text(out)
    print(out)
    return 0


def cmd_history(args, cfg: dict, state: Path) -> int:
    step = (args.names or ["report"])[0]
    db = seller_history.connect(state)
    if step == "pull":
        result = seller_history.pull(db, args.platform or cfg["sellers"]["platforms"], int((args.max_usd or 20) * 1e6))
    elif step == "update":
        result = seller_history.update(db, args.platform or cfg["sellers"]["platforms"], int((args.max_usd or 3) * 1e6))
    elif step == "read":
        result = seller_history.read(db, args.workers)
    elif step == "sort":
        result = seller_history.sort(db, "" if args.rules else cfg["sellers"]["model"], int((args.max_usd or 30) * 1e6))
    elif step == "report":
        result = seller_history.summary(db, Path(args.out) if args.out else None)
    else:
        raise SystemExit("history: pull, update, read, sort or report")
    print(json.dumps(result, ensure_ascii=False, indent=1) if args.json else seller_history.text(result))
    return 0


def cmd_leads(args, state: Path) -> int:
    data = kept_leads(state, args.days)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
        return 0
    for r in data["leads"]:
        verdict = f" · {r['verdict']}" + (f" ({r['days_ahead']} days ahead)" if r["days_ahead"] is not None else "") if r["verdict"] else ""
        print(f"{r['first_seen']}  {r['term']}  [{', '.join(r['signals'])}]{verdict}  {r['note']}")
    for kind, s in data["hit_rates"]["by_signal"].items():
        print(f"hit rate {kind}: {s['hit']}/{s['checked']}")
    print(f"{len(data['leads'])} leads in the last {args.days} days; {data['hit_rates']['pending']} awaiting review")
    return 0


def serve_mcp() -> int:
    """`new-term-radar mcp`: the stdio MCP server under the package's own name, so a
    client that only knows the PyPI name can start it (`uvx --with mcp new-term-radar mcp`)."""
    try:
        from . import mcp_server
    except ImportError:
        print('The MCP server needs the `mcp` package: pip install "new-term-radar[mcp]"', file=sys.stderr)
        return 1
    mcp_server.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    if (sys.argv[1:] if argv is None else argv)[:1] == ["mcp"]:
        return serve_mcp()
    prog = "python -m radar" if __package__ == "radar" else "new-term-radar"  # installed, the package has another name
    p = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["daily", "sources", "keywords", "leads", "pains", "history"])
    p.add_argument("names", nargs="*", help="for `keywords`: the names to read; for `history`: pull, read, sort or report")
    p.add_argument("--repo", action="append", default=[], metavar="NAME=OWNER/REPO", help="for `keywords`: a name's GitHub repository")
    p.add_argument("--days", type=int, default=7, help="for `leads`: how far back")
    p.add_argument("--json", action="store_true", help="for `keywords` and `leads`: print JSON")
    p.add_argument("--config", help="default: ./config.json, or the example the radar ships with when there is none")
    p.add_argument("--state", default="state", help="directory for radar.db and the snapshots")
    p.add_argument("--out", help="write the report (Markdown) here; `daily` also writes a .json beside it")
    p.add_argument("--skip", default="", help="comma-separated: " + ",".join(sorted(SKIPPABLE)))
    p.add_argument("--new-sites", action="store_true", help="run the weekly new-site signal today")
    p.add_argument("--revenue", action="store_true", help="run the weekly revenue signal today (needs TRUSTMRR_API_KEY)")
    p.add_argument("--asks", action="store_true", help="run the weekly Reddit asks signal today")
    p.add_argument("--sellers", action="store_true", help="run the weekly new-sellers, builders and money signals today")
    p.add_argument("--feeds", action="store_true", help="run the weekly feeds (Google's new pages, rising root words) today")
    p.add_argument("--extension", action="append", default=[], metavar="ID", help="for `pains`: a Chrome extension to read the reviews of")
    p.add_argument("--mail", action="store_true", help="mail the report (see radar/notify.py)")
    p.add_argument("--platform", action="append", choices=sorted(sellers.PLATFORMS), help="for `history pull` and `update`: one platform (default: all)")
    p.add_argument("--max-usd", type=float, help="for `history pull` (default 20), `update` (default 3) and `sort` (default 30): stop at this spend")
    p.add_argument("--workers", type=int, default=32, help="for `history read`: home pages fetched at a time")
    p.add_argument("--rules", action="store_true", help="for `history sort`: keyword rules only, free; a later sort with the model redoes them")
    args = p.parse_args(argv)
    args.skip = {s for s in args.skip.split(",") if s}
    unknown = args.skip - SKIPPABLE
    if unknown:
        p.error(f"--skip: unknown {sorted(unknown)}")
    state = Path(args.state)
    if args.command == "leads":
        return cmd_leads(args, state)
    if args.command == "keywords" and not args.names:
        p.error("keywords: give at least one name")
    cfg = config.load(Path(args.config) if args.config else None)
    if "judge" in args.skip:
        cfg["judge"]["model"] = ""
    state.mkdir(parents=True, exist_ok=True)
    if args.command == "keywords":
        return cmd_keywords(args, cfg, state)
    if args.command == "pains":
        return cmd_pains(args, cfg)
    if args.command == "history":
        return cmd_history(args, cfg, state)
    return cmd_sources(args, cfg, state) if args.command == "sources" else cmd_daily(args, cfg, state)


if __name__ == "__main__":
    sys.exit(main())
