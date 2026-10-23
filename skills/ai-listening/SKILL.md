---
name: ai-listening
description: Collect what is new in AI in the last 24 hours across X, Hugging Face, GitHub and sitemaps, deduplicated, as one report of names that are starting to surge. Use when the user asks what is new or trending in AI today, wants a daily AI listening report, or needs fresh names of models, products and repositories to feed keyword research. Runs the new-term-radar command line; needs an AISA_API_KEY.
---

# AI listening

Produce one report: the names of new AI models, products and repositories seen in the last 24 hours, where each was seen, and how strong the signal is. This is collection, not analysis. For search demand behind a name, hand the names to the `seo-new-keywords-research` skill.

## Before you run anything

1. Find the radar. Use `$NEW_TERM_RADAR_DIR` if set, else `~/new-term-radar`. If neither exists, clone it: `git clone https://github.com/zhuyansen/new-term-radar ~/new-term-radar`. Run every command from that directory. Python 3.10 or newer; no packages to install.
2. Check an AIsa key is available: `AISA_API_KEY`, or the AIsa CLI logged in (`ls ~/.aisa/key`). Without one, only the free signals run (Hugging Face, GitHub, sitemaps), no X posts are read, and nothing judges which terms are names. Say so in the report. A key comes from https://go.aisa.one/yansen-zhu.
3. If there is no `config.json`, copy `config.example.json` to `config.json`. Tell the user they are on the example list of X accounts and sitemaps and can edit it.

## Cost and time, say this first

A run reads every X account in `config.json`: about 0.003 USD and 10 seconds each, so the example list of 35 accounts is about 0.11 USD and 6 minutes. Tell the user the estimate before you start, and do not run it twice in a row: the second run reports only what changed since the first.

## Run

```bash
python -m radar daily --skip usecases,review,newsites --out listening.md
```

This reads X, Hugging Face, GitHub breakouts and the sitemaps, merges terms seen by several signals, and scores each as a name or a common word. It writes `listening.md` and `listening.json`.

The first run on a machine only builds snapshots (yesterday's X vocabulary, star counts, sitemap URLs do not exist yet), so it reports GitHub breakouts and little else. Say this plainly instead of presenting a thin report as a quiet day. From the second run on, every signal reports what is new.

If the user only wants the fastest-moving sources, `python -m radar sources --out listening.md` reads X and Hugging Face alone and writes nothing when there is no new name.

## Write the report

Read `listening.json` (key `leads`), not only the Markdown. For each lead you have `term`, `kinds` (which signals saw it), `signals` (one line per signal), `named` (0 to 1), `examples`, `posts` (text, url, likes) and `known`.

Give the user:

1. **New names, strongest first**: seen by two or more signals, then by name score. For each: the name, what it is in a few words (from the examples, do not guess), where it was seen with the number that shows its strength (accounts and likes, stars a day, trending rank), and one link (the post or the repository).
2. **Breakout repositories** from `breakout.repos` that are new today.
3. **What did not run**: any signal whose block holds `error` or `skipped`, and what the report lacks because of it.

Keep only leads with `named` of 0.5 or more unless the judge did not run. Leave out leads whose `known` is set: the user already covers those. Do not add names from your own knowledge: the report is what the radar saw.

`python -m radar leads --days 1` lists what was kept, for a quick look without a new run. It is free.

## What this cannot tell you

- Whether people search for these names. That is the next skill.
- Names that none of the configured accounts or sites mentioned.
- Anything about a name beyond the examples it was seen in.
