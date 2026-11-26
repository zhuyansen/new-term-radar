"""Signal ③: pages other sites added since the last run.

Reads the public sitemaps listed in config.json ("sitemaps"), keeps the last run's URLs
in a snapshot, and turns the new URLs into terms: a word that three or more new URLs
share, and that the site's older URLs rarely carried, is a surge.

    "sitemaps": {
      "glama":   {"index": "https://glama.ai/sitemap.xml", "shards": "/mcp-servers/\\\\d+\\\\.xml$", "pause": 2},
      "lobehub": {"index": "https://lobehub.com/sitemap/mcp.xml", "pause": 5, "strip_locale": true}
    }

`index` is a sitemap index when `shards` (a regex) picks the sub-sitemaps to read, or a
plain sitemap when `shards` is left out. `pause` is the seconds between requests to that
site: keep each site's robots.txt and crawl-delay. `strip_locale` drops a leading
/en/ or /zh-CN/ so one new entry in 17 languages counts once.

A site that fails is skipped for the day and keeps its snapshot, since the URLs it
lacks would come back tomorrow as "new".
"""
from __future__ import annotations

import gzip
import re
import time
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

from . import UA, snap
from .terms import by_source, surges

TIMEOUT = 40
RETRIES = 3
RETRY_PAUSE = 10
MIN_URLS = 3
TOP = 25
DEFAULT_PAUSE = 3
# A leading path segment that is a language code (/de/, /zh-CN/, /pt_BR/). Only real codes: a slug
# like /ai-blog/ or /rf-detr/ is a page, not a locale.
LANGS = ("en|zh|tw|ja|ko|fr|de|es|pt|it|ru|ar|tr|vi|th|id|nl|pl|sv|da|fi|no|nb|cs|hu|ro|uk|el|he|hi|ms|fa|bn|"
         "ur|ta|te|sk|bg|hr|sr|sl|lt|lv|et|ca|tl|fil|sw")
_LOCALE = rf"^(https?://[^/]+)/(?:{LANGS})(?:[-_][A-Za-z]{{2,4}})?(?=/)"
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                body = res.read()
            return (gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body).decode("utf-8", "replace")
        except (OSError, ValueError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(RETRY_PAUSE * (attempt + 1))
    return ""


def site_urls(index: str, shards: str | None, pause: float, strip_locale: bool = False) -> set[str]:
    files = [index] if not shards else \
        [u for u in _locations(fetch(index), "sitemapindex", "sitemap") if re.search(shards, u)]
    urls: set[str] = set()
    for i, u in enumerate(files):
        if i or shards:
            time.sleep(pause)
        urls.update(_locations(fetch(u), "urlset", "url"))
    return {re.sub(_LOCALE, r"\1", u) for u in urls} if strip_locale else urls


def slug_text(url: str) -> str:
    """The words a URL carries: its path after the domain, without ids or file suffixes."""
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
    path = re.sub(r"\b[0-9a-f]{8,}\b|\.(html|xml)$", " ", path)
    return re.sub(r"[/_\-]+", " ", path)


def diff(cfg: dict, state: Path) -> dict:
    """New URLs per site against the stored snapshot, then the terms that surge in them."""
    if not cfg["sitemaps"]:
        return {"skipped": True}
    report, recent, baseline = {}, [], []
    for i, (site, s) in enumerate(cfg["sitemaps"].items()):
        path = state / "sitemaps" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', site)}.json.gz"
        stored = snap.load(path)
        old = set(stored) if stored is not None else None
        pause = s.get("pause", DEFAULT_PAUSE)
        try:
            if i:
                time.sleep(pause)   # several entries can point at one host
            now = site_urls(s["index"], s.get("shards"), pause, bool(s.get("strip_locale")))
        except Exception as exc:  # noqa: BLE001 — one site down must not stop the others
            report[site] = {"error": str(exc)[:120]}
            continue
        if not now:     # blocked, empty or a format the reader misses: say so rather than wait forever
            report[site] = {"error": "no URLs read"}
            continue
        new = sorted(now - old) if old is not None else []
        report[site] = {"urls": len(now), "new": len(new), "first_run": old is None, "sample": new[:8]}
        recent += [(u, slug_text(u)) for u in new]
        baseline += [(u, slug_text(u)) for u in (old or set())]
        snap.save(path, sorted(now))
    ratio = len(recent) / max(len(baseline), 1)
    return {"sites": report, "terms": surges(by_source(recent), by_source(baseline), ratio, MIN_URLS, TOP)}


def _locations(xml: str, root_name: str, item_name: str) -> list[str]:
    root = ElementTree.fromstring(xml)
    namespace = _SITEMAP_NS if root.tag == _SITEMAP_NS + root_name else ""
    if root.tag != namespace + root_name:
        return []
    nodes = root.findall(f"{namespace}{item_name}/{namespace}loc")
    locations = [(node.text or "").strip() for node in nodes]
    return [location for location in locations if location]
