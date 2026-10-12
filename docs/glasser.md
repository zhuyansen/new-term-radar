# Optional Glasser data backend

This is a **hybrid integration**, not a replacement for every AIsa operation. AIsa
remains the default. Select either supported capability independently in `config.json`:

```json
{
  "data_providers": {"trends": "glasser", "volume": "glasser"},
  "glasser": {"trends_location_code": 2840, "trends_language_code": "en"}
}
```

Supply your existing Glasser key through `GLASSER_API_KEY` in the environment. Never
put keys in `config.json`. Retain `AISA_API_KEY` for X and model calls (and new sites
when enabled). Omitted provider settings use AIsa; setting only `trends` or `volume`
to `glasser` leaves the other on AIsa. There is **no automatic provider fallback**.
Unknown provider names and unsupported capability settings fail before the CLI runs.

For GitHub Actions, add this line under `env` in the existing **Run the radar** step,
with an existing repository secret of the same name. This integration does not
create or configure any credentials:

```yaml
GLASSER_API_KEY: ${{ secrets.GLASSER_API_KEY }}
```

| Capability | Selection | What stays on AIsa |
| --- | --- | --- |
| Review and keyword-table Trends (solo + benchmark) | `data_providers.trends` | AIsa when omitted or `aisa` |
| Keyword-table Google Ads search volume | `data_providers.volume` | AIsa when omitted or `aisa` |
| X account posts and keyword evidence search | AIsa only | All X calls |
| Domain WHOIS discovery / new sites | AIsa only | Existing nested filters and ordering |
| Name judge, keyword generation and relevance screen | AIsa only | All chat/model calls |

Glasser Trends requires a location and language. Its defaults here are **US (2840),
English (`en`)**, not worldwide. Volume retains `usecases.location_code` (US by
default) and English. When comparing sources, keep their geographic scope aligned;
use a separate `--state` directory when changing the provider or geography so old
review statistics are not mixed with a different source. The adapter uses **Google
Trends**, never DataForSEO's separate clickstream Trends product.

The full keyword table still requires AIsa for X evidence and models. Glasser alone
can run the review of existing leads; without AIsa, explicitly skip `x`, `judge`,
`newsites`, and `usecases` as appropriate. Selecting Glasser does not remove those
AIsa requirements. The repository's existing AIsa new-sites limitation remains.

## Requests, limits and failures

Each logical task sends one `POST https://api.glasser.ai/v1/runs` with Bearer
authentication, a fresh `Idempotency-Key`, provider `dataforseo`, the exact endpoint
below, and a **single task object** as `input` (not AIsa's `body` array).

| Operation | Endpoint | Version | Supported input |
| --- | --- | --- | --- |
| Trends | `/v3/keywords_data/google_trends/explore/live` | 4 | 1–5 keywords; `date_from`, `date_to`; explicit location/language; `item_types: ["google_trends_graph"]` |
| Search volume | `/v3/keywords_data/google_ads/search_volume/live` | 2 | 1–100 keywords; location/language |

Versions select a supported contract; **they do not lock the price**. Inspect
current terms and your key's policy before enabling real calls. These are billable
operations, including some empty results. The AIsa prices in the README do not apply
to Glasser. The adapter sums Glasser's `charge_usd` using `Decimal` into the radar's
micro-USD cost field; it never substitutes DataForSEO's upstream `cost` field.

All calls in a batch are validated before any Run is created. Unknown fields and
oversized requests raise an error instead of dropping parameters or splitting the
request into more billable executions. A volume list over 100 keywords must be
reduced by the caller. No pagination or data-source substitution is implicit.

POSTs are **never automatically retried**. An ambiguous transport failure includes
the original idempotency key; inspect the Run in Glasser before manually rerunning.
If retrying that exact HTTP request yourself, retain its original body and key.
A new CLI invocation creates new keys and may incur new charges. In-progress Runs
are polled via `GET /v1/runs/{id}`, up to 30 polls, without another POST. A poll limit
or network failure is not a cancellation; the Run may still finish and be charged.
The initial request has a 90-second socket timeout; subsequent polling has a
90-second time budget as well as the poll-count cap.
Redirects are rejected to keep the Glasser key on the intended service. Provider
response bodies and keys are not copied into error messages.

A `COMPLETED` Run is accepted only with a successful provider HTTP status and
successful DataForSEO envelope/task status codes. Failures raise an explicit error
and do not fall back to AIsa. The review therefore leaves affected leads pending,
rather than storing `no_data` for a failed fetch. A successful explicit empty result
is allowed. Unexpected result structures, wrong keyword order, non-daily/gapped or
missing Trends values are errors: they are not interpolated or converted to zeros.
Successful empty graph data is allowed; the existing radar records it as `no_data`.

For failed batches, the exception includes known completed charges and, when
available, the failing Run's id/charge; the normal per-signal cost field is not a
billing ledger. The report can truncate long errors. Use the Glasser Run history
for complete billing information, including failures, unresolved or pending Runs.

## Unimplemented operations

- **X latest posts and advanced search:** ScrapeCreators' `user-tweets` returns
  popular posts, not the latest posts needed by the radar. TikHub documents `Top`
  and `Latest`, but the inspected public contract does not establish the required
  advanced search operators and a stable output mapping. No speculative adapter is
  included. X continues to use AIsa explicitly.
- **WHOIS discovery:** Glasser's published WHOIS input allows `limit`, `offset`,
  `offset_token`, and `tag`, but not the radar's required `filters` or `order_by`.
  Omitting those would change which domains are found. The AIsa call is unchanged;
  Glasser WHOIS is not offered by this adapter.
- **Chat:** no verified OpenAI-compatible Glasser chat contract is implemented.
  Model selection remains controlled by `judge.model` and `usecases.model` on AIsa.

## Validation and sources

Validated with synthetic fixtures derived from public documentation and mocked
HTTP responses. **No authenticated Glasser inspection, billable Run, or live
end-to-end integration test was performed.** These tests establish request mapping,
parsing and failure behavior; they do not establish live endpoint availability,
key permissions, data completeness, comparability or current prices. The two
Glasser endpoint pages publish no output schema, so the parser checks the fields
required by the radar against DataForSEO's documented native response.

Run `python -m pytest -q tests`. Tests prohibit real socket connections, strip live
AIsa/Glasser keys and use dummy keys only. Existing AIsa tests remain part of the
suite. The upstream test workflow runs Python 3.10 and 3.12.

Official references inspected on 2026-10-12:

- [Glasser API overview](https://glasser.ai/docs/api-reference/overview)
- [Create a Run: statuses, idempotency, output and charges](https://glasser.ai/docs/api-reference/runs/create)
- [Get a Run](https://glasser.ai/docs/api-reference/runs/get)
- [Glasser Google Trends contract](https://glasser.ai/endpoints/dataforseo/compare-interest-in-several-keywords-over-time-on-google-trends-dataforseo-3aba33ba)
- [Glasser Google Ads volume contract](https://glasser.ai/endpoints/dataforseo/look-up-monthly-search-volume-and-cpc-for-a-list-of-keywords-dataforseo-8369c1cd)
- [DataForSEO Google Trends response](https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/)
- [DataForSEO Google Ads volume response](https://docs.dataforseo.com/v3/keywords_data/google_ads/search_volume/live/)
- [Glasser WHOIS contract and input restriction](https://glasser.ai/endpoints/dataforseo/domain-analytics-overview-live-39c61dbd)
- [ScrapeCreators user tweets limitation](https://docs.scrapecreators.com/v1/twitter/user-tweets/)
- [Glasser TikHub search contract](https://glasser.ai/endpoints/tikhub/x-search-6f55f3a3)
- [TikHub OpenAPI](https://api.tikhub.io/openapi.json)
