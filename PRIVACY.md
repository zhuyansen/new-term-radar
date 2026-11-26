# Privacy

new-term-radar runs on your machine, or in your own GitHub Actions: the command line, the skills and the MCP server alike. Its author runs no server and collects nothing from it: no account, no telemetry, no analytics.

## What it reads, and where its requests go

Free, with no key:

- Hugging Face's trending models and Spaces (`huggingface.co`)
- GitHub's repository search and READMEs (`api.github.com`). A `GITHUB_TOKEN`, if you set one, goes only to GitHub and only raises the rate limit.
- the sitemaps listed in your `config.json` (the example config lists twelve AI tool, MCP and skill directories)

Through the providers whose keys you set. A request carries what the radar is looking up or judging (X account names, terms and names, and the public posts and pages they came from) and nothing else from your machine:

- Glasser (`api.glasser.ai`), with `GLASSER_API_KEY`: Google Ads volumes, DataForSEO Trends, Google's results, domain data, Reddit searches, YouTube comments and trending lists, X posts, passed on to the provider that answers them (DataForSEO, TikHub, ScrapeCreators, AnyAPI)
- AIsa (`tools.aisa.one`, `api.aisa.one`), with `AISA_API_KEY`, or the key the AIsa CLI stored at login: `site:` searches and the model that judges which terms are names; without a Glasser key, the data above too
- the model endpoint you set in `RADAR_LLM_BASE_URL`, with `RADAR_LLM_API_KEY`: the terms and posts the model reads, in place of AIsa's model
- GoAnyAPI (`api.goanyapi.com`), with `GOANYAPI_API_KEY`: related queries, one country's Trends series, searches, and Chrome Web Store reviews
- TrustMRR (`trustmrr.com`), with `TRUSTMRR_API_KEY`: verified revenue listings
- Google Search Console (`googleapis.com`), with your own Google token: the queries your own site got
- Resend (`api.resend.com`), with `RESEND_API_KEY`: only to email the report, to the addresses you put in `RADAR_EMAIL_TO`

Each key goes only to its own provider. What a provider keeps of a request is governed by that provider's own privacy policy. Without a key, the signals that need it are skipped and the report says so; a lookup that cannot run without one stops with a message naming the key.

## What it keeps, on your machine only

In `RADAR_STATE` (by default `./state`; the skills keep theirs under `~/new-term-radar`; in Claude Code, the plugin's own data folder):

- `radar.db`: the leads it kept and their reviews 14 days later
- the snapshots that tell what is new since the last run: X vocabulary, GitHub stars, Hugging Face's top lists, sitemap URLs, new sites, sellers and revenue listings
- caches of keyword tables and site ratings, so a repeated lookup costs nothing

Reports go where you write them (`--out`). Delete the folder to remove all of it. Keys are never written there; a Search Console token is the file you put next to `config.json`, or `GSC_TOKEN_JSON`.

## Contact

Questions or a problem: [open an issue](https://github.com/zhuyansen/new-term-radar/issues).
