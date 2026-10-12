"""config.json: what to watch. Copy config.example.json and edit it.

Everything has a default except the X accounts and the sitemaps, which only you can
choose. Secrets are never read from this file; they come from the environment.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import data, i18n, terms

DEFAULTS = {
    "lang": "en",
    "data_providers": dict(data.DEFAULTS),
    "glasser": {"trends_location_code": 2840, "trends_language_code": "en"},
    "x_accounts": {"labs": [], "bloggers": []},
    "sitemaps": {},
    "topics": {},
    "known_terms": [],
    "stop_words": [],
    "judge": {"model": "gpt-4.1-mini", "min_score": 0.5},
    "breakout": {"window_days": 14, "min_stars": 300, "velocity_min": 200, "delta_min": 400},
    "new_sites": {"queries": {"ai": "%ai%"}, "weekday": 0, "window_days": 90, "min_etv": 1000, "min_keywords": 20},
    "review": {"after_days": 14, "per_day": 5},
    "usecases": {"per_day": 5, "model": "claude-sonnet-4-6", "min_volume": 50, "trends_max": 10, "recheck_days": 3,
                 "location_code": 2840, "benchmark": "gpts", "min_daily": 300},
    "gsc": {},
}


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} not found: copy config.example.json to {path.name} and edit it")
    raw = json.loads(path.read_text())
    cfg = {}
    for k, default in DEFAULTS.items():
        v = raw.get(k, default)
        cfg[k] = {**default, **v} if isinstance(default, dict) and default and isinstance(v, dict) else v
    cfg["_dir"] = path.resolve().parent
    data.validate(cfg)
    i18n.set_lang(cfg["lang"])
    terms.add_stop(cfg["stop_words"])
    return cfg


def known(cfg: dict, term: str) -> str:
    """The entry of known_terms that already covers this term, if any: what you have a
    page for, or never want reported."""
    for k in cfg["known_terms"]:
        k = k.lower()
        if k and (k in term or term in k):
            return k
    return ""
