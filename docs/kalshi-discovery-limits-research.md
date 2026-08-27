# Kalshi discovery and freeze limits

Research date: 2026-08-20  
Scope: GitHub issue [#4](https://github.com/MINLEGO/v-trade/issues/4), first-party Kalshi documentation/API references, and the closed [#3 French-host probe summary](https://github.com/MINLEGO/v-trade/issues/3#issuecomment-5349001703). No new probe data is inferred here.

## Recommendation

These are implementation-ready V-Trade defaults. Values described as recommendations are not Kalshi guarantees and remain owner gates where noted.

| Area | Recommended contract |
| --- | --- |
| Catalogue | Use `GET /markets?status=open&mve_filter=exclude&limit=1000` as the authoritative active-market scan. Locally normalize the returned open market to `active`, validate the ordinary binary YES/NO contract from its documented fields, and reject multivariate/scalar inputs; use `ticker` as the stable identity. Do not invent or parse a ticker-derived market type. `mve_filter=exclude` removes multivariate markets. |
| Supporting metadata | Use `GET /events` only for event metadata, with `status=open`, `with_nested_markets=false`, and `limit=200`; do not use event membership as the market universe because event status is derived from child markets. Fetch `GET /series/{series_ticker}` on demand for series rules/tags. |
| Discovery metrics | Capture batch `GET /markets/candlesticks` data at 60-minute intervals over 48 hours for the bounded discovery set. Compare complete recent and preceding 24-hour windows; use `volume_24h_fp` for current 24-hour volume; calculate volatility from consecutive hourly closes; and persist `insufficient_data` instead of fabricating a zero when a required window is incomplete. |
| Historical boundary | Read `GET /historical/cutoff` during every freeze. Route settled markets older than `market_settled_ts` to `GET /historical/markets`; do not expect them from live `/markets`. |
| Pages and cursors | Request the documented maxima: 1,000 markets/page and 200 events/page. Follow every returned cursor until it is empty. Do not add a smaller numeric page cap; bound traversal by the freeze deadline, reject a repeated cursor or malformed/duplicate page, and fail closed if the chain cannot finish. |
| Poll/cache cadence | Recommended defaults: one shared full-catalogue refresh at most once per 60 seconds; catalogue cache TTL 60 seconds; event/series metadata TTL 300 seconds. `min_updated_ts` is for metadata polling, not a replacement for the active `/markets` scan because Kalshi documents it as non-trading metadata change tracking. Orderbooks have no cross-cycle cache; reuse one response only within the same freeze. |
| Shortlist | Preserve the existing target of 20 prior historical outcomes plus 80 additional markets. The 20-item historical context and 80-market additional cap do not cap resolution tracking. Select additional markets at market level, sorted by `volume_fp` descending, `liquidity_dollars` descending, then `ticker` ascending. Retained markets do not consume the 80 slots. |
| Books | For each active retained or shortlisted market, call the public single-market endpoint `GET /markets/{ticker}/orderbook?depth=0` (all levels). Use a semaphore of 8 in-flight requests per cycle. This is the highest tested concurrency in issue #3, not a provider quota; keep the bulk-book endpoint out of v1 because the probe did not cover it and the documented bulk endpoint is an authenticated path. |
| Timeout/retry | Use the existing bounded transport shape: 15-second total request timeout, 5-second connect timeout, 3 attempts including the first, and capped exponential delays of 0.25s then 0.5s (never beyond a 2s cap or the freeze deadline). Retry transport timeouts/connection failures, 429, and transient 5xx responses; retry a 404 only for the documented post-creation visibility race. Do not retry 400/401/403, malformed JSON, or contract/schema errors. |
| Cutoff/failure | Publish a freeze atomically only after the complete catalogue, required metadata, retained/resolution reads, and every required book have succeeded. Use the existing V-Trade actual-completed-freeze `data_cutoff`; every artifact must have `observed_at <= data_cutoff`. A missing page, late response, repeated cursor, invalid payload, exhausted retry, or required book failure means no new freeze and no agent trading decision. Never substitute an expired cache, partial catalogue, or stale book. |

## Retention rules

- **Held:** Always retain the market record. If it is still `active`, include it in the frozen active set and fetch its book regardless of rank. If it is `closed`, `determined`, `disputed`, `amended`, or `finalized`, keep it out of active discovery but keep it in resolution tracking until V-Trade has reconciled the final payout.
- **Touched:** Include a previously touched market in active discovery while Kalshi still reports it as `active`, regardless of its rank. If it is no longer active, keep it in the resolution universe rather than silently dropping it.
- **Resolution:** The resolution universe is not bounded by 20 or 80. Kalshi documents `finalized` as the terminal, paid-out state and `settlement_ts` as populated after settlement; use that state plus local payout reconciliation before archiving a touched/held record. A `result` alone is not a reason to remove it.

The active catalogue should therefore be computed as:

```text
active held markets
+ active previously touched markets
+ top 80 other active, binary, non-MVE markets
```

Books are fetched only for that active union. Resolution synchronization remains separate and must continue for all known held/touched markets, using live or historical endpoints according to Kalshi's moving cutoff.

## Metric contract

The active v1 metric formula is `kalshi-market-metrics-v1`. Each selected market gets a
freeze-scoped snapshot backed by its market row, reciprocal book, and candle response.
`volume_trend_delta` is `(recent_volume - baseline_volume) / baseline_volume`, rendered
with ten decimal places; a zero baseline keeps the qualitative trend but produces a null
delta. Volatility is the sample standard deviation, in microdollars, of available
consecutive hourly close changes in the recent 24-hour window. The competitive heuristic
uses only Kalshi's two independent bid arrays: spread score `1/(1 + spread_cents/10)`,
balanced near-midpoint depth saturation, and 24-hour activity saturation. The resulting
score is bounded to `[0, 1]` and is not a probability.

## Evidence and limits of the evidence

Issue #3 reports that the intended French VPS reached unauthenticated markets/events/market/orderbook REST endpoints; cursor pages had distinct tickers; orderbook requests returned 10/10 HTTP 200 at bounded concurrency 1, 2, 4, and 8; no timeout, 4xx, 5xx, or 429 occurred; and no retry was exercised. It does not establish a sustained public rate ceiling, latency percentile, or retry delay.

Kalshi documents that single-market orderbooks are public and support `depth=0` for all levels. Its rate-limit documentation describes token buckets and exponential backoff for 429 responses but gives numeric tiers for authenticated requests; it currently documents no `Retry-After` or `X-RateLimit-*` header. The 60/300/15/5-second cadence and timeout values above are therefore conservative V-Trade defaults, not vendor-supplied limits.

## Deployment owner gates (not unresolved #4 defaults)

1. The 20 historical / 80 additional target, deterministic tie-break order, 60-second catalogue TTL, 300-second metadata TTL, 8 book workers, and 15s/5s/3-attempt retry policy are the v1 defaults resolved by #4. A sustained French-host run at the intended full shortlist size is still needed before treating 8 as a provider ceiling.
2. Decide whether `is_provisional=true` markets are admissible candidates or require a separate policy. Kalshi documents their possible later removal, but does not define V-Trade's desired treatment.
3. Confirm the long-term archive rule after `finalized` and local payout reconciliation, including how long raw resolution artifacts remain retained.
4. Keep the authenticated bulk-book endpoint and WebSocket path out of v1 until the owner explicitly approves credentials, a new probe, and a separate transport contract.

## First-party sources

- [Kalshi Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi Get Events](https://docs.kalshi.com/api-reference/events/get-events)
- [Kalshi Get Series List](https://docs.kalshi.com/api-reference/market/get-series-list)
- [Kalshi Understanding Pagination](https://docs.kalshi.com/getting_started/pagination)
- [Kalshi Market Lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)
- [Kalshi Orderbook Responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Kalshi Batch Get Market Candlesticks](https://docs.kalshi.com/api-reference/market/batch-get-market-candlesticks)
- [Kalshi Get Market Candlesticks](https://docs.kalshi.com/api-reference/market/get-market-candlesticks)
- [Kalshi Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi Historical Data](https://docs.kalshi.com/getting_started/historical_data)
- [Kalshi Get Historical Cutoff Timestamps](https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps)
- [Kalshi Get Multiple Market Orderbooks](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks)
