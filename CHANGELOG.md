# Changelog

## Unreleased

- **⑤ Review**: `rose()` now scans `first_rise` starting from `first_seen` (`after_pts`) rather than across the whole
  window including the pre-report baseline, fixing negative `lead_days` when a variant had a spike before it was
  reported. Synced the October 27 retro rules into `review.py`: pre-existing terms (`born_day < 2026-10-03 - 14d` or
  baseline `>= 25`) are marked `unclear` (`pre_existing` or `high_baseline`) and excluded from the hit-rate denominator;
  multi-source attribution tracks `first_source` vs `all_sources`.
- **② GitHub breakout**: `breakouts()` records each repo's `born` date (`created_at[:10]`), `shown()` preserves all
  newly emerged breakouts (`is_new`) before filling the top-15 display slice with older repos, and `breakouts()` supports
  optional shadow queries (`young_days`/`young_min_stars` for 3-day young repos and `late_days`/`late_min_stars` for
  older repos breaking out now).
- **Keyword table (`KGR`)**: `competition.py` computes `kgr_of(built, volume)` (`pages built / monthly searches`:
  `< 0.25` great, `<= 1.0` watch, `> 1.0` skip) and shows `KGR` in the `Page one` column when GoAnyAPI `pages_built`
  and monthly volume are present.
- **⑦ AI answers multi-run distribution & first pick**: `ai_answers.py` records `first_pick` (the first tool named) and
  supports `"runs": N` (`N >= 2`) to sample each engine multiple times, aggregating tool counts, cited-site counts,
  first-pick counts and empty-run `gap` rate (`>= 80%` empty runs).
- **Fixes**: `radar.__init__.UA` now uses `__version__`; `notify.send()` raises a clear `SystemExit` when
  `RESEND_API_KEY` or `EMAIL_FROM` is missing instead of `KeyError`; `mcp_server.run_radar()` raises `ToolError` on an
  invalid `command`; `pains` report and docs now match the four sources read (Chrome Web Store, App Store, Reddit and
  YouTube comments); cost summary notes `AIsa + GoAnyAPI`.

## 0.6.1 (2026-10-28)

- Nothing goes out without the user's yes. The three skills and the MCP server's instructions now say the radar
  only reads: before an agent posts, publishes, submits, emails or buys anything for the user on the strength of a
  result, it shows where it goes and the exact text or the cost, and waits for the user's yes to that one. A row
  marked `prioritize` is not that yes, and one yes covers one action. Nothing else changed.

## 0.6.0 (2026-10-27)

What was built after 0.5.0 on a separate line (it ran there as 0.5.3, 0.6.0 and 0.7.0), now published together with
0.5.1 and 0.5.2, and three changes to the new-sites signal and the page-one check.

### Fixed

- **③ New sites ran again**: AIsa's tool router checks arguments against a schema that types `filters` as objects, so
  the whois overview's DataForSEO filters have been refused there since about 2026-10-15 and the weekly signal failed.
  It goes through AIsa's REST passthrough now (`aisa.rest()`), which takes them as DataForSEO does. A query that fails
  is named; all of them failing raises.

### New sites and page one

- **The first words a new site ranks for** (`radar/site_words.py`, `words` in `new_sites`): each site new this run
  comes with its five words in Google's top 20, the most traffic first, in any country (DataForSEO Labs' ranked
  keywords, about $0.013 a site). On 2026-10-27 one site registered in August ranked 5 to 11 in Taiwan and for nothing
  in the US top 20. A word new since Labs' previous check shows the two checks it fell between.
- **How old page one's sites are, recorded only** (`radar/page_age.py`, `page_age` in `competition`): each page-one
  result's domain dated by its registration (RDAP, free, one request every half second, kept 30 days), and the results
  that entered the top 10 since the reading before. In report.json as each row's `page_age`; shown nowhere and used in
  no decision until later rankings show it says something.

### The keyword table: twelve months, your rank, threads, the .com, AI answers

Four free readings on the keyword table and a paid, opt-in one on ⑦ asks. Each has its own config block with `"enabled"`.

- **12 months** (`radar/curve.py`, `year_curve`): the volume call (`usecases.volumes`) now keeps each phrase's monthly searches, oldest first (`months`), and the table has a `12 months` column after `Now`: bars on the term's own scale from zero and a word, `new since` (barely searched a year ago), `peaked` (a burst past its top: twice the median month or more, at half of it or less now), `falling`, `rising`, `steady`, or `few searches` under 50 in its best month. Every row with Google Ads data; free.
- **Your rank** (`radar/yours.py`, `yours`): your sites (`yours.domains`, else the domain of `gsc.site`) are looked for in the page-one check's ten results and in Search Console (`gsc.positions()`: average position, impressions and clicks for the exact queries over `days`, one call, an RE2 filter). The decision cell ends with the action: `→ update yours` (page one by either), `→ improve yours` (Search Console average 11 to 30), `→ new page`; your pages are listed under the table. Prioritize and watch rows; free.
- **What else holds page one** (`radar/competition.py`): the page-one check keeps the forum threads on page one (`forums`: forum results and the "Discussions and forums" block), and the page-one cell marks an AI overview, an answer box, forums, videos and news. Under the table: a note on the clicks an AI overview takes (Pew Research Center, 2025) and the threads, best-of searches first. Free.
- **Exact-match .com** (`radar/dotcom.py`, `dot_com`): RDAP (Verisign) for the joined and hyphenated .com of the prioritized rows, names first, up to `max_rows`; registered with its date, or not registered. Free.
- **AI answers** (`radar/ai_answers.py`, `ai_answers`, off by default): ⑦'s jobs with searches are asked of ChatGPT (DataForSEO's LLM scraper: its interface's answer with search) and, optionally, Google's AI Mode, as "What is the best tool to {job}?"; the asks table gains an `AI answers name` column, and under it the sites each answer cites, ✅ when one is yours. About $0.004 an answer.
- `gsc._query()` takes dimension filters and returns positions.
- Tests: the RDAP lookup is blocked in tests like the paid calls; `dot_com` is off unless a test turns it on.

Measured on 2026-10-20: five "claude skills" terms through the new keyword readings, $0.11 (the volume call and the page-one check; the four readings themselves cost nothing); six AI answers, $0.024. Found on the way and fixed before this entry: AI Mode's reference names included "Reddit", so its tool names are now the bold names that open its list items; and a Search Console average of 89 with one impression no longer counts as a page to improve.

### The keyword table: word age, markets, page-one shape

Three readings added to the keyword table, for the rows that would be acted on. Each has its own config block and `"enabled": false` turns it off.

- **Word age** (`radar/wordage.py`, `word_age`): the first week with any interest in the last five years (DataForSEO Trends, `past_5_years`) is the column `First searched`; 🆕 = within `new_days` (180). The week still running and Trends' unsettled last two days are left out (`review.settled()`), so a "0, 100" tail cannot make a term look new. A new term's page one is read one step easier (crowded → mixed, mixed → open), so a rising new term is no longer demoted to watch for a crowded page one it has not had time to shape. Prioritize and watch rows; $0.0012 a row.
- **Markets** (`radar/markets.py`, `markets`): Google Ads monthly searches in each market of `codes` (US, DE, FR, JP by default, all languages), the keyword difficulty there (`difficulty`), `best` market (most searches per point of difficulty, or most searches when a difficulty is missing) and the Trends top region among the major markets (`top_region`). Prioritize and watch rows; about $0.41 a run with the defaults (four $0.09 volume calls, four difficulty calls at about $0.012) plus $0.0024 a row.
- **Page-one shape** (`radar/serpshape.py`, `serp_shape`): from the live top ten (US / en / desktop by default), homepages against inner pages, results on high-authority platforms (configurable), titles that hold the term, an exact-match homepage, and #1 and #2. Verdict `platform-held` (half or more on platforms), `beatable` (mostly inner pages, no exact-match homepage) or `contested`. Prioritize rows; reuses the page-one check's results, otherwise about $0.002 a term.
- The keyword table has three new columns (First searched, Markets, Page-one shape) in English and Chinese, a note on what they mean, and the #1 and #2 of the prioritized rows under it. Failures of these readings are listed under the table and leave the rest of it as it was.
- `competition.page_one()` takes a language and a device; `competition.difficulty()` takes a language; the page-one check keeps its ten results on the row (`competition.results`).

Measured on 2026-10-18 for two terms: $0.42 for the three readings.

### More signals

- **⑧ sellers**: Paddle Classic, Dodo Payments and Waffo Pancake beside Stripe, Polar, Lemon Squeezy, Gumroad and Creem;
  each new seller's domain registration and where its visitors come from, with the AI share (GoAnyAPI); a payment link
  on several new sites is one network's, not new sellers (1,465 keyword domains on one template shared a few links in
  September and October 2026); a builder's or host's default words (Notion, Framer, ReadMe, Gitea, a link shortener)
  are not the seller's own, and forums are not sellers.
- **⑧b builders**: sites just built with Lovable or v0 and newsletters just started on beehiiv, read off their badges;
  only the software among them becomes a lead.
- **⑨ just listed** (`radar/feeds.py`): Product Hunt, Show HN, OpenRouter, Civitai, the App Store's grossing charts,
  awesome lists, Google's new pages under Figma, There's An AI For That and the Chrome Web Store, trending searches, and
  rising searches next to root words (the paid ones weekly). A first run lists what each source itself dates as recent.
- **⑩ money** (`radar/money.py`, weekly, GoAnyAPI): the other sites on a paying tool site's AdSense account, and the
  advertisers new on your own terms.
- **Beside the names** (`radar/sidelights.py`): new sites carrying a name (crt.sh certificates, whois), its Wikipedia
  views, what Google knows it as (a name taken by something else is said so), and Discord servers; the searches around
  the paying products.
- **⑦ asks** also read X, Software Recommendations and Freelancer.com, drop old posts and name each post's source; the
  tool sites on page one for the day's terms go to ⑩, big sites and ad networks left out.
- **history**: `history update` adds the sellers that appeared since the history's newest day (weekly, about $0.5);
  `history sort --rules` sorts by keywords alone, free, and a later sort with the model redoes those.
- The status says how long each signal took when one took a minute or more.

## 0.5.2 (2026-10-26)

- The MCP server's failures reach the agent with their reason (no key, a refused call, a state folder it cannot write).

## 0.5.1 (2026-10-24)

- The skills run the pinned release from PyPI, ask before any paid call, and read fetched text as data; with no
  `config.json` the radar runs on the example it ships with. "DataForSEO Trends" where the radar reads it.

## 0.5.0 (2026-10-20)

- ⑧ sellers: the sites that just started taking money, new by the day each first linked to the platform; `history`.

## Earlier

See the git history.
