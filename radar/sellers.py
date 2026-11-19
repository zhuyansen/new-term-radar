"""Signal ⑧: sites that just started taking money.

A product's first payment link says more than a launch post: someone built a thing and asked
for money for it. Payment links are ordinary links on the seller's own page, so the public link
graph (DataForSEO through AIsa) shows them, each with the day it was first seen:

- Stripe Payment Links (buy.stripe.com) and Polar checkout links (buy.polar.sh): every link is one;
- Lemon Squeezy store checkouts (<store>.lemonsqueezy.com/checkout or /buy);
- Gumroad product pages (.../l/<product>); profile pages are left out, spam networks use them;
- Creem payment pages (creem.io/payment/...);
- Paddle Classic checkouts (buy.paddle.com), Dodo Payments product links (checkout.dodopayments.com/buy/...) and
  Waffo Pancake's permanent product links (checkout.waffo.ai/{store}/{product}).

A new link is not a new seller: a site that has sold for years puts the same link on every new
page, and each page is a link first seen today. Of the 2,000 newest Stripe links on 2026-10-14,
two thirds came from sites that linked to Stripe before. So the day that counts is the day the
SITE first linked to the platform (its referring domain's first_seen), and only sites whose first
link is among the newest are kept.

Once a week (sellers.weekday, Monday by default) this reads the newest `per_platform` links and
the newest `per_platform` sites of each platform ($0.056 a platform on 2026-10-14), keeps the links
of new sites, drops the ones reported before and sites where anyone can post (GitHub, Reddit,
X ...), and keeps up to `max_leads` new sellers, busiest first when a GoAnyAPI key gives their
visits ($0.004 a site). A seller's link sits on its own home,
pricing or product page; the first real run showed what else carries payment links: user pages on
big platforms (framer.com templates, pub.dev packages, Flathub apps, forum threads, newsletters,
news articles). So the linking page must be a short, non-content path, the site must rank
`rank_max` or lower (DataForSEO's 0 to 1000 scale), and above `max_visits` a month it is a
platform, not a new seller. The home pages of the newest `pool` candidates are read, a small model
sorts them by kind (keyword rules when it cannot), and AI and software come first, then templates,
courses and the rest; within a kind, the busiest first. Each seller's home-page title and
description (read free) go to the keyword table as evidence, as for ⑥: the table reads the job
people pay for.

What it cannot see: checkouts created by code at the moment of payment (most of Stripe's
volume), and revenue. A link means the site sells, not that it sells well.
"""
from __future__ import annotations

import html
import json
import re
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from . import UA, aisa, goanyapi, snap
from .i18n import t

TOOL = "post_dataforseo_backlinks_backlinks_live"
DOMAINS = "post_dataforseo_backlinks_referring_domains_live"
NON_STORE = {"www", "app", "docs", "help", "status", "affiliates", "blog", "api", "assets", "discord"}
PLATFORMS = {
    "stripe": ("buy.stripe.com", lambda host, path, *_: True),
    "polar": ("buy.polar.sh", lambda host, path, *_: True),
    "lemonsqueezy": ("lemonsqueezy.com", lambda host, path, *_: host.endswith(".lemonsqueezy.com")
                     and host.split(".")[0] not in NON_STORE and path.split("/")[1:2] in (["checkout"], ["buy"])),
    "gumroad": ("gumroad.com", lambda host, path, *_: path.startswith("/l/")),
    "creem": ("creem.io", lambda host, path, *_: path.startswith("/payment/") or path.startswith("/test/payment/")),
    "paddle": ("buy.paddle.com", lambda host, path, *_: True),          # Paddle Classic checkouts: 266 sites on 2026-10-14
    # Merchants of record for indie and AI software, like Polar and Creem (2026-10-15: Dodo 480 linking sites;
    # Waffo Pancake's permanent links, checkout.waffo.ai/{store}/{product}, none yet in the link graph)
    "dodo": ("checkout.dodopayments.com", lambda host, path, *_: path.startswith("/buy/")),
    "waffo": ("checkout.waffo.ai", lambda host, path, *_: path.count("/") >= 2),
}
# Sites that show what they were built with: the badge of an AI app builder, or a newsletter's "Powered by" link.
# Told apart from posts about the tool by the badge's own anchor or address (2026-10-14: 75 of the newest 100
# sites linking to lovable.dev carried "Edit with"; beehiiv's link names the publication).
BUILDERS = {
    "lovable": ("lovable.dev", lambda host, path, anchor: anchor.startswith("edit with")),
    "v0": ("v0.app", lambda host, path, anchor: anchor.startswith("built with")),
    "beehiiv": ("beehiiv.com", lambda host, path, anchor: path.startswith("/powered-by")),
}
LABEL = {"stripe": "Stripe", "polar": "Polar", "lemonsqueezy": "Lemon Squeezy", "gumroad": "Gumroad", "creem": "Creem",
         "paddle": "Paddle", "dodo": "Dodo Payments", "waffo": "Waffo Pancake", "lovable": "Lovable", "v0": "v0", "beehiiv": "beehiiv"}
SOURCES = {"Direct": "direct", "Search": "search", "SearchPaid": "search", "Social": "social", "SocialPaid": "social",
           "Referrals": "referrals", "Affiliate": "referrals", "Mail": "mail", "DisplayAds": "ads", "GenAI": "AI"}
# Where anyone can post: the site is not the seller.
SHARED = {"github.com", "reddit.com", "x.com", "twitter.com", "youtube.com", "medium.com", "substack.com", "linktr.ee",
          "producthunt.com", "indiehackers.com", "facebook.com", "instagram.com", "tiktok.com", "discord.com", "google.com",
          "linkedin.com", "pinterest.com", "quora.com", "bit.ly", "beacons.ai", "bento.me", "t.me", "telegram.me"}
PAGE_BYTES, WORKERS = 300_000, 4
KINDS = ["ai", "template", "course", "content", "physical", "other", "donation"]      # the order sellers are listed in
KIND_RULES = [
    ("ai", r"\b(ai|gpt|llm|agents?|automat\w*|saas|apps?|software|api|bots?|tools?|generator|assistant|extension|plugin|mcp|"
           r"workflow|boilerplate|builder|analytics|mac|ios|android|cli|sdk|transcri\w+)\b"),
    ("template", r"\b(templates?|themes?|ui kit|framer|figma|notion|webflow|icons?|fonts?|mockups?|design system|components?|"
                 r"presets?|luts?|add-?ons?|3d|assets?)\b"),
    ("course", r"\b(courses?|training|academy|certification|coach\w*|workshops?|masterclass|mentor\w*|consult\w*|lessons?|class|exam)\b"),
    ("content", r"\b(e-?books?|guides?|recipes?|books?|newsletters?|community|membership|podcasts?|blog|magazine)\b"),
    ("physical", r"\b(shop|store|merch|plush\w*|apparel|prints?|jewel\w*|candles?|coffee|bikes?|shoes?|clothing|toys?|swag)\b"),
    ("donation", r"\b(donat\w*|news|church|charity|foundation|non-?profit|ministry|journal|association)\b"),
]
KIND_SYSTEM = (
    "You sort websites that just put up a payment link. From each one's domain, title and description choose its kind: "
    "'ai' (an AI product or any software: app, SaaS, browser extension, API, developer tool, agent), 'template' (templates, "
    "themes, UI kits, icons, fonts, 3D models, design assets), 'course' (courses, training, coaching, consulting), 'content' "
    "(ebooks, guides, newsletters, communities, memberships), 'physical' (physical goods), 'donation' (donations, news sites, "
    "churches, charities, nonprofits) or 'other'.\nAnswer with JSON only, one object per site, in order: [{\"i\": 0, \"kind\": \"ai\"}, ...]"
)
CONTENT = re.compile(r"/(t|p|posts?|story\w*|listings?\w*|profiles?|threads?|forum\w*|packages?|apps|themes|marketplace|questions|"
                     r"news|blog|articles?|wiki|view-image|galler\w*|events?|calendar|members?|users?|u|r|discussions?)(/|$)|"
                     r"\.(php|jsp|page)\b|\d{4,}", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
DESCRIPTION = re.compile(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']*)", re.I)
# A builder's or host's own words, not the seller's. On 2026-10-15, 301 Notion pages that sell through Gumroad read
# "Notion" / "A collaborative AI workspace..." and had been sorted as AI products; short-link domains read their
# shortener's home page, ReadMe docs and Gitea or Forgejo forges their software's.
DEFAULT_TITLE = re.compile(r"(notion|home|untitled|lovable app|rebrandly)$|switchy: shorten|url shortener, branded short links|"
                           r"your source for social news and networking|"
                           r"adobe portfolio \| build your own|developer platform \| .* docs$", re.I)
DEFAULT_DESCRIPTION = re.compile(r"a collaborative ai workspace, built on your company context|made with framer|a website made with "
                                 r"typedream|lovable generated project|welcome to your team.s developer platform|gitea \(git with a "
                                 r"cup of tea\)|forgejo is a self-hosted|quickly and simply build a personalized website|kliqqi is an "
                                 r"open source content management|discover switchy, the easiest|home$", re.I)


def own_words(title: str, description: str) -> tuple[str, str]:
    """A page's title and description minus its builder's defaults: blank where that is all there is."""
    return ("" if DEFAULT_TITLE.match(title.strip()) else title, "" if DEFAULT_DESCRIPTION.match(description.strip()) else description)


def split(url: str) -> tuple[str, str]:
    m = re.match(r"https?://([^/?#]+)([^?#]*)", url or "")
    return (m.group(1).lower(), m.group(2) or "/") if m else ("", "/")


FORUM = {"community", "forum", "forums", "discourse", "discuss", "talk", "answers"}   # anyone posts there: about 500 hosts
                                                                                    # had counted as sellers (2026-10-15)


def shared(host: str) -> bool:
    parts = host.split(".")
    return parts[0] in FORUM or any(".".join(parts[i:]) in SHARED for i in range(len(parts) - 1))


def own_page(url_from: str) -> bool:
    """A home, pricing or product page of the site itself, not a post, thread or listing on it."""
    _, path = split(url_from)
    return len([x for x in path.split("/") if x]) <= 2 and not CONTENT.search(path)


def result(data: dict, call_id: str) -> dict:
    """The one result of a DataForSEO call, {} when it has none."""
    return (((data.get(call_id) or {}).get("tasks") or [{}])[0].get("result") or [{}])[0] or {}


def links(platform: str, limit: int, rank_max: int = 350, table: dict = PLATFORMS, own: bool = True) -> tuple[list[dict], int]:
    """The newest site per linking domain that carries a payment link of this platform (or a builder's badge)."""
    target, is_payment = table[platform]
    data, cost = aisa.call([{"call_id": "b", "tool": TOOL, "arguments": {"body": [{
        "target": target, "mode": "one_per_domain", "limit": limit, "order_by": ["first_seen,desc"], "backlinks_status_type": "live"}]}}])
    out = []
    for i in result(data, "b").get("items") or []:
        host, path = split(i.get("url_to") or "")
        seller = (i.get("domain_from") or "").lower()
        rank = i.get("domain_from_rank") or 0
        anchor = " ".join((i.get("anchor") or "").lower().split())
        if seller and is_payment(host, path, anchor) and not shared(seller) and rank <= rank_max \
                and (not own or own_page(i.get("url_from") or "")):
            out.append({"host": seller, "platform": platform, "first_seen": str(i.get("first_seen") or "")[:10],
                        "page": i.get("url_from") or f"https://{seller}/", "rank": rank, "link": i.get("url_to") or ""})
    return out, cost


def started(platform: str, limit: int, table: dict = PLATFORMS) -> tuple[dict[str, str], int]:
    """{site: day} for the sites whose first link to this platform is among the newest `limit`."""
    data, cost = aisa.call([{"call_id": "d", "tool": DOMAINS, "arguments": {"body": [{
        "target": table[platform][0], "limit": limit, "order_by": ["first_seen,desc"], "backlinks_status_type": "live"}]}}])
    return {i["domain"].lower().removeprefix("www."): str(i.get("first_seen") or "")[:10]
            for i in result(data, "d").get("items") or [] if i.get("domain")}, cost


def first_day(host: str, sites: dict[str, str]) -> str:
    """The day `host` first linked to the platform when it is one of `sites`, else "".
    DataForSEO sometimes names a site by its parent domain (app.example.com as example.com)."""
    parts = host.removeprefix("www.").split(".")
    return next((sites[d] for d in (".".join(parts[i:]) for i in range(len(parts) - 1)) if d in sites), "")


def page_words(host: str, limit: int = PAGE_BYTES) -> tuple[str, str]:
    """(title, description) of the seller's home page, reading at most `limit` bytes off the wire
    (gzip when the site offers it); empty when it does not answer."""
    try:
        req = urllib.request.Request(f"https://{host}/", headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read(limit)
            if res.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, PAGE_BYTES)   # a cut stream decodes as far as it goes
            text = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a site that does not answer keeps its domain only
        return "", ""
    title = TITLE.search(text)
    desc = DESCRIPTION.search(text)
    clean = lambda s: " ".join(html.unescape(s).split())[:200]  # noqa: E731
    return own_words(clean(title.group(1)) if title else "", clean(desc.group(1)) if desc else "")


def term_of(host: str, title: str) -> str:
    """The product's name: the title's first part ("Pestora — pest control software"), else the domain's."""
    head = re.split(r"\s+[|\-–—:·]\s+", title)[0].strip() if title else ""
    if head and 1 <= len(head.split()) <= 4 and len(head) <= 40:
        return head.lower()
    label = host.split(".")
    return (label[-3] if len(label) > 2 and label[-2] in ("framer", "notion", "carrd", "webflow", "vercel", "netlify", "github")
            else label[-2] if len(label) >= 2 else host).lower()


def kind_by_rules(r: dict) -> str:
    text = f"{r.get('title', '')} {r.get('description', '')} {r['host']}".lower()
    return next((k for k, rx in KIND_RULES if re.search(rx, text)), "other")


def kinds(model: str, rows: list[dict]) -> tuple[dict[str, str], str]:
    """{host: kind} from one model call; keyword rules for whatever it leaves out, and all of it when it fails."""
    out, error = {}, ""
    if rows and model:
        user = json.dumps([{"i": i, "domain": r["host"], "title": r.get("title", ""), "description": r.get("description", "")[:160]}
                           for i, r in enumerate(rows)], ensure_ascii=False)
        try:
            answer = aisa.chat(model, KIND_SYSTEM, user)
            m = re.search(r"\[.*\]", answer, re.S)
            for it in json.loads(m.group(0)) if m else []:
                i = it.get("i") if isinstance(it, dict) else None
                if isinstance(i, int) and 0 <= i < len(rows) and it.get("kind") in KINDS:
                    out[rows[i]["host"]] = it["kind"]
        except (aisa.AisaError, ValueError) as exc:
            error = f"kinds: {str(exc)[:120]}"
    return {r["host"]: out.get(r["host"]) or kind_by_rules(r) for r in rows}, error


def traffic_info(data: dict) -> dict:
    """visits, registered, top_source and ai_share from a GoAnyAPI visits answer."""
    series = [int(float(v)) for _, v in sorted((data.get("EstimatedMonthlyVisits") or {}).items()) if v not in (None, "")]
    shares: dict[str, float] = {}
    for key, value in (data.get("TrafficSources") or {}).items():
        try:
            shares[SOURCES.get(key, "other")] = shares.get(SOURCES.get(key, "other"), 0.0) + float(value or 0)
        except (TypeError, ValueError):
            continue
    registered = str((data.get("DateData") or {}).get("registration") or "")[:10]
    return {"visits": series[-1] if series else None, "registered": registered if re.fullmatch(r"\d{4}-\d{2}-\d{2}", registered) else "",
            "top_source": max(shares, key=shares.get) if sum(shares.values()) > 0 else "",
            "ai_share": round(shares.get("AI", 0.0), 3) if sum(shares.values()) > 0 else None}


def visits(hosts: list[str]) -> tuple[dict[str, dict], int]:
    """{host: traffic_info} for the hosts GoAnyAPI answered ($0.004 a site)."""
    if not goanyapi.key():
        return {}, 0

    def one(h: str):
        try:
            data, cost = goanyapi.call("traffic", domain=h, month=3)
        except goanyapi.GoAnyError:
            return h, None, 0
        return h, traffic_info(data), cost
    out, cost = {}, 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for h, info, c in pool.map(one, hosts):
            cost += c
            if info is not None:
                out[h] = info
    return out, cost


def fresh(c: dict, seen: dict[str, str], table: dict = PLATFORMS, own: bool = True) -> dict:
    """The newest payment links of sites that just started linking to each platform, minus the ones reported
    before. `old` counts the links on new pages of sites that sold already."""
    out = {"found": {}, "read": 0, "old": 0, "cost": 0, "errors": []}
    for platform in c["platforms"]:
        try:
            rows, spent = links(platform, c["per_platform"], c["rank_max"], table, own)
            out["cost"] += spent
            sites, spent = started(platform, c["per_platform"], table)
            out["cost"] += spent
        except aisa.AisaError as exc:
            out["errors"].append(f"{platform}: {exc}")
            continue
        out["read"] += len(rows)
        for r in rows:
            day = first_day(r["host"], sites)
            out["old"] += not day
            if day and r["host"] not in seen:
                out["found"].setdefault(r["host"], dict(r, first_seen=day))
    # One payment link on several new sites is a network's: 1,465 keyword domains with one "Complete Guide" template
    # shared a few Stripe and Gumroad links in September and October 2026 (one link on 175 sites). Badges are
    # shared by design (every Lovable site links the same way), so this is for payment links only.
    if table is not PLATFORMS:
        return out
    hosts_of: dict[str, set] = {}
    for h, r in out["found"].items():
        hosts_of.setdefault(r.get("link") or h, set()).add(h)
    networked = {h for hs in hosts_of.values() if len(hs) > 1 for h in hs}
    out["networked"] = len(networked)
    out["found"] = {h: r for h, r in out["found"].items() if h not in networked}
    return out


def sellers(cfg: dict, state: Path, today: date | None = None) -> dict:
    c = cfg["sellers"]
    today = today or date.today()
    path = state / "sellers.json.gz"
    seen: dict[str, str] = snap.load(path) or {}
    first = not seen
    got = fresh(c, seen)
    found, cost, errors, read = got["found"], got["cost"], got["errors"], got["read"]
    pool = sorted(found.values(), key=lambda r: (r["first_seen"], r["rank"]), reverse=True)[:c["pool"]]
    with ThreadPoolExecutor(WORKERS) as workers:
        words = dict(zip([r["host"] for r in pool], workers.map(page_words, [r["host"] for r in pool])))
    for r in pool:
        r["title"], r["description"] = words.get(r["host"], ("", ""))
        r["term"] = term_of(r["host"], r["title"])
    sorted_kinds, kind_error = kinds(c["model"], pool)
    for r in pool:
        r["kind"] = sorted_kinds[r["host"]]
    order = lambda r: (KINDS.index(r["kind"]), -r["rank"])  # noqa: E731
    new = sorted(pool, key=order)[:c["max_leads"] * 2]
    want_visits = bool(c.get("visits", True) and goanyapi.key())
    sizes, spent = visits([r["host"] for r in new]) if c.get("visits", True) else ({}, 0)
    cost += spent + aisa.take_chat_cost()
    for r in new:
        info = sizes.get(r["host"]) or {}
        r.update(visits=info.get("visits"), registered=info.get("registered", ""), top_source=info.get("top_source", ""),
                 ai_share=info.get("ai_share"))
    new = [r for r in new if (r["visits"] is not None and r["visits"] <= c["max_visits"])
           or (r["visits"] is None and (not want_visits or r["host"] in sizes))]
    new = sorted(new, key=lambda r: (KINDS.index(r["kind"]), -(r["visits"] or 0), -r["rank"]))[:c["max_leads"]]
    for host in found:
        seen.setdefault(host, today.isoformat())
    if found:
        snap.save(path, seen)
    terms = [{"term": r["term"], "recent": 1, "about": (f"https://{r['host']}/", f"{r['title']}. {r['description']}".strip(". ")),
              "examples": [f"{r['host']} · {LABEL[r['platform']]}" + (f" · {r['visits']:,} visits/month" if r["visits"] else "")],
              "note": t("note_seller", via=LABEL[r["platform"]], day=r["first_seen"], kind=t("sk_" + r["kind"]))} for r in new]
    return {"links": read, "old": got["old"], "new": len(found), "networked": got.get("networked", 0), "first_run": first, "sellers": new, "terms": terms,
            "errors": errors, "kind_error": kind_error, "cost_micros": cost, "platforms": list(c["platforms"])}


def builders(cfg: dict, state: Path, today: date | None = None) -> dict:
    """What people just built with an AI app builder, and the newsletters that just started (weekly, with ⑧)."""
    c = cfg["builders"]
    today = today or date.today()
    path = state / "builders.json.gz"
    seen: dict[str, str] = snap.load(path) or {}
    first = not seen
    got = fresh(c, seen, BUILDERS, own=False)          # a badge sits on every page of the site, deep ones too
    found = got["found"]
    pool = sorted(found.values(), key=lambda r: (r["first_seen"], r["rank"]), reverse=True)[:c["pool"]]
    with ThreadPoolExecutor(WORKERS) as workers:
        words = dict(zip([r["host"] for r in pool], workers.map(page_words, [r["host"] for r in pool])))
    for r in pool:
        r["title"], r["description"] = words.get(r["host"], ("", ""))
        r["term"] = term_of(r["host"], r["title"])
    for host in found:
        seen.setdefault(host, today.isoformat())
    if found:
        snap.save(path, seen)
    answered = [r for r in pool if r["title"] or r["description"]]
    sorted_kinds, kind_error = kinds(cfg["sellers"]["model"], answered)
    for r in answered:
        r["kind"] = sorted_kinds[r["host"]]
    by_kind = {k: sum(1 for r in answered if r["kind"] == k) for k in KINDS}
    rows = sorted(answered, key=lambda r: KINDS.index(r["kind"]))[:c["max_leads"]]
    # Most of what people make with an app builder is a business's website (a pizzeria, a lawn service, 2026-10-15):
    # only the software among them is a lead; the rest is the context of what the builder is used for.
    terms = [{"term": r["term"], "recent": 1, "about": (f"https://{r['host']}/", f"{r['title']}. {r['description']}".strip(". ")),
              "examples": [f"{r['host']} · {LABEL[r['platform']]}"], "note": t("note_builder", via=LABEL[r["platform"]], day=r["first_seen"])}
             for r in rows if r["kind"] == "ai"]
    return {"links": got["read"], "old": got["old"], "new": len(found), "first_run": first, "sites": rows, "terms": terms,
            "by_kind": by_kind, "kind_error": kind_error, "errors": got["errors"], "cost_micros": got["cost"] + aisa.take_chat_cost()}
