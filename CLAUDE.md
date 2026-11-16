# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A daily radar for names that are starting to surge in AI (new models, products, repos), caught at the source (X, Hugging Face, GitHub, sitemaps, new domains, Reddit asks, payment-link sellers, …) before keyword tools list them, plus a "keyword table" that turns each name into the generic search terms behind it, sized and given a decision (prioritize / watch / skip). It ships as a CLI, an MCP server, three Agent Skills and a Claude Code plugin.

## Commands

The radar itself uses **only the Python standard library** (3.10+). `markdown` is only for `--mail`, `mcp` only for the MCP server.

```bash
pip install -r requirements.txt pytest mcp     # what CI installs
python -m pytest -q tests                      # full suite (no network, no paid calls)
python -m pytest -q tests/test_radar.py -k test_every_manifest_carries_the_same_version   # one test
```

Running from a clone (installed, the command is `new-term-radar` / `uvx new-term-radar`):

```bash
python -m radar daily --out report.md            # everything; also writes report.json
python -m radar sources --out sources.md         # ① X + Hugging Face only (meant for every few hours)
python -m radar daily --skip x,hf --out report.md
python -m radar keywords "name one" --repo "name one=owner/repo" --json
python -m radar leads [--days 7] [--json]        # free: what the store kept + review verdicts
python -m radar pains "keyword"
python -m radar history [pull|update|read|sort]
python -m radar mcp                              # MCP server on stdio
```

`daily`, `sources`, `keywords`, `pains`, `history` call **paid APIs** (AIsa; optionally GoAnyAPI, TrustMRR). Don't run them casually; tests and `leads` are free. Keys come from env: `AISA_API_KEY` (or `~/.aisa/key`), `GOANYAPI_API_KEY`, `TRUSTMRR_API_KEY`, `GSC_TOKEN_JSON`, `RESEND_API_KEY`/`EMAIL_FROM`/`RADAR_EMAIL_TO`. MCP server reads `RADAR_CONFIG` and `RADAR_STATE`.

## Architecture

- **Package layout quirk**: code lives in `radar/` so `python -m radar` works from a clone, but it is published as the package `new_term_radar` (pyproject maps `new_term_radar` → `radar/`; PyPI's `radar` name is taken). Use relative imports inside the package.
- **`radar/__main__.py`** is the orchestrator. Each signal is wrapped in `run(fn, skip)`: an exception becomes `{"error": ...}` in that signal's block (plus `seconds`), so one failing source never kills the report. Keep that contract: signal functions return a dict block; the report renders errors/skips in a status section.
- **One module per signal** (`breakout.py`, `sitemaps.py`, `new_sites.py`, `revenue.py`, `asks.py`, `sellers.py`, `feeds.py`, `money.py`, `gsc.py`, `sidelights.py`, …), numbered ①–⑩ in docs and the CLI docstring. Many are weekly (a `weekday` config key) and many are opt-in (`enabled`).
- **Lead flow**: signals → `report.leads()` merges terms across signals (kinds listed in `report.KINDS`) → `judge.py` (small model scores "is this a specific name?") → `store.py` (SQLite at `state/radar.db`) → `review.py` re-checks each lead in DataForSEO Trends after `review.after_days` (14) and computes hit rate per signal.
- **Keyword table** (`usecases.py`, the largest piece): evidence (posts/README) → model proposes generic phrases → screening call → Google Ads volume → Trends curves vs a benchmark keyword → decision. Extra per-row readings each live in their own module with their own config block: `competition.py` (page-one check), `wordage.py`, `markets.py`, `serpshape.py`, `curve.py` (12 months), `yours.py`, `dotcom.py`, `ai_answers.py`, `page_age.py`.
- **External access**: `aisa.py` (tool router, REST passthrough to DataForSEO, chat models) and `goanyapi.py` are the only paid gateways. Trends' last two days are unsettled; every reader drops them (`review.settled()`).
- **State**: `state/` holds `radar.db` plus per-source snapshots; a first run of a snapshot-based source only records and reports nothing.
- **Config**: `config.py` has `DEFAULTS`; `config.load()` merges `config.json` (cwd) over them, falling back to the bundled `radar/config.example.json` when no config exists.
- **Reports**: `report.py` renders Markdown; all user-visible text goes through `i18n.t(key, **kw)` with an `(en, zh)` tuple per key in `i18n.T`. Add both languages when adding report text.
- **`mcp_server.py`** exposes `recent_leads`, `keyword_table`, `run_radar`.

## Tests

All tests are in `tests/test_radar.py`. `tests/conftest.py` has an autouse fixture that **blocks every network/paid call** (`aisa._post`, `goanyapi.call`, RDAP lookups) and turns off weekly/opt-in signals by patching `config.DEFAULTS`. A new module that reaches the network must be stubbed there too, and new opt-in blocks should default to off in tests. Use the `cfg` fixture for a config with `_dir` set to `tmp_path`.

## Invariants enforced by tests

- **Version in many places**: `radar/__init__.py` `__version__`, `pyproject.toml`, `server.json` (twice), `plugin.json` (root, for Copilot), `.claude-plugin/plugin.json` (also pinned in its `mcpServers` args), `.claude-plugin/marketplace.json`, and the pinned `uvx new-term-radar==X` in every `skills/*/SKILL.md`. Bump them together (also README/README.zh-CN mentions and CHANGELOG).
- `config.example.json` (root) and `radar/config.example.json` must be identical.
- `README.md` must keep the `<!-- mcp-name: io.github.zhuyansen/new-term-radar -->` line; `server.json` description ≤ 100 chars.

## Conventions

- `README.md` and `README.zh-CN.md` are kept in parallel; document user-facing changes in both and in `CHANGELOG.md` (which records measured costs and observed behavior, not just features).
- The skills (`skills/*/SKILL.md`) instruct agents to state the cost and wait for the user's yes before any paid run, and never to post/publish/send on the user's behalf without explicit approval. Preserve those rules when editing skills or MCP server instructions.
- Releases: publishing a GitHub release runs `.github/workflows/publish.yml` (tests → PyPI via Trusted Publishing → MCP Registry). `.github/workflows/radar.yml` is the opt-in scheduled run (`RADAR_ENABLED` repo variable).
