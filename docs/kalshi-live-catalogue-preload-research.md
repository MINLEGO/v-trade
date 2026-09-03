# Kalshi live catalogue preload and incremental refresh research

Research date: 2026-09-03

Scope: GitHub issue [#31](https://github.com/MINLEGO/v-trade/issues/31), current first-party Kalshi
documentation, a credential-free local-host measurement, and the repository's existing frozen
public-API fixture. Neither measurement is treated as a venue guarantee or as evidence from the
intended French VPS.

## Decision summary

A full preload remains necessary for global local search or ranking because the documented
`GET /markets` query surface has no sorting parameter. Use the endpoint maximum of 1,000 rows,
follow every opaque cursor, and build a compact generation locally. Kalshi explicitly warns that
data can change between page requests, so the result is an observed interval, not an atomic
snapshot. ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets),
[pagination guide](https://docs.kalshi.com/getting_started/pagination))

The useful non-closed, non-MVE universe requires three initial traversals: `status=unopened`,
`status=open`, and `status=paused`, each with `mve_filter=exclude`. Only one status may be supplied
per request. The intended payload states are `initialized`, `active`, and `inactive`, but the live
measurement observed rows that had already become `closed` inside the `open` and `paused` cursor
chains. Merge by ticker and validate the payload state locally before publication. Closed and
finalized markets stay outside this catalogue and can be fetched on demand for portfolio,
reconciliation, or settlement workflows. ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets),
[market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle))

`min_created_ts` is suitable for discovering additions but not for maintaining existing rows:
it returns items created **after** a Unix timestamp. `min_updated_ts` returns metadata changes
only and explicitly excludes trading changes. Therefore neither filter replaces direct refreshes
of the markets selected by a tool or used to validate an order. ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets))

The safe incremental shape is an overlapping, fully paginated `min_created_ts` query with no
status filter, `mve_filter=exclude`, local status filtering, and ticker deduplication. Advance the
watermark only after the entire cursor chain succeeds. This is a V-Trade mitigation, not a Kalshi
guarantee of zero omissions.

## Official API contract

| Concern | Documented behavior | Consequence for V-Trade |
| --- | --- | --- |
| Page size | `limit` defaults to 100 and has a maximum of 1,000. | Always request 1,000 for exhaustive scans; the total request count is `ceil(rows / 1000)` per status at that observation. |
| Cursor | The first request omits `cursor`; each following request reuses the returned cursor until it is empty/null. | Treat the cursor as opaque, reject repeats, and publish no new generation from an incomplete chain. |
| Ordering | No `sort` or `order` parameter appears in the documented query surface, and no implicit ordering guarantee is stated. | Global sorting and filtering must happen locally after exhaustive traversal. |
| Status | Filters are `unopened`, `open`, `paused`, `closed`, and `settled`; only one may be supplied per request. | Preload the three useful non-closed statuses separately, merge by ticker, then retain only payload states `initialized`, `active`, and `inactive`. |
| MVE | `mve_filter=only` selects multivariate markets and `mve_filter=exclude` excludes them. The changelog says omission returns all markets. | Send `mve_filter=exclude` explicitly on every catalogue and incremental request. |
| Creation filter | `min_created_ts` means created after the supplied Unix timestamp. It may be combined with `status=unopened`, `status=open`, or no status; `paused` is absent from the compatibility matrix. | Increment without a status filter, then retain only `initialized`, `active`, and `inactive` locally. A live `min_created_ts+paused` request returned HTTP 200, but relying on an undocumented combination is unwarranted. |
| Update filter | `min_updated_ts` tracks non-trading metadata changes only. It is incompatible with all filters except `mve_filter=exclude` (and a documented conditional `series_ticker` combination). | Run it as a separate optional metadata-repair stream; never use it as a price, volume, or order-context freshness signal. |

The endpoint-specific facts above come from the current
[Get Markets reference](https://docs.kalshi.com/api-reference/market/get-markets). The MVE default
is also stated in Kalshi's [API changelog](https://docs.kalshi.com/changelog). The generic
pagination guide says that list data can change between requests and recommends refresh logic;
it does not turn a cursor chain into a snapshot.
([pagination guide](https://docs.kalshi.com/getting_started/pagination))

There is a documentation inconsistency worth testing rather than normalizing away: the prose at
the top of `Get Markets` lists `unopened`, `open`, `closed`, and `settled`, while the parameter enum
and lifecycle page also include `paused`. The lifecycle page is explicit that `paused` matches
payload status `inactive`. ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets),
[market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle))

## Pagination consistency and watermark risks

Kalshi documents that data may change between requests. It does not document any of the following:

- an atomic or immutable snapshot bound to the first cursor;
- stable result ordering across pages;
- absence of duplicate or missing tickers while markets are created or change status;
- cursor lifetime;
- permission to resume a cursor with different filters.

Accordingly, a complete traversal proves only that every returned page was consumed. It does not
prove that the union equals the venue state at one instant. The implementation should record both
the first and last observation times. Use completion time for the published generation's
`catalog_observed_at`, and expose or retain the scan start/duration so consumers can judge the
age spread within that generation.

For `min_created_ts`, the word “after” makes the lower bound exclusive, while the input is Unix
seconds and returned `created_time` values can contain sub-second precision. To avoid a same-second
boundary omission, retain an overlap (at least the preceding whole second), deduplicate by exact
ticker, and move the durable watermark only after all pages succeed. Record the request watermark,
scan start, scan completion, page count, row count, and duplicate count. This overlap policy is an
engineering inference from the documented filter semantics, not a server-side completeness
guarantee. ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets))

The initial preload should therefore finish with an immediate incremental catch-up starting from
before the preload began. Every later catalogue-using tool should first complete the same
incremental protocol. A failed refresh may leave the prior generation available with its original
`catalog_observed_at`; it must not partially mutate the published generation.

## Current credential-free measurement

On 2026-09-03 from 17:06:50.584 to 17:09:19.279 UTC, a read-only sequential probe ran from the
local Codex host against the public production endpoint. It used `limit=1000`,
`mve_filter=exclude`, a 30-second request timeout, no credentials, no retries, and did not preserve
raw payloads. All 193 preload requests returned HTTP 200.

| Scan | Pages | Rows before cross-scan deduplication | Raw bytes | Elapsed | Per-page median / maximum |
| --- | ---: | ---: | ---: | ---: | ---: |
| `unopened` | 73 | 72,634 | 135,660,975 | 73.749 s | 479 / 1,363 ms |
| `open` | 118 | 117,187 | 248,517,334 | 72.615 s | 234 / 476 ms |
| `paused` | 2 | 1,795 | 4,092,302 | 0.942 s | 231 / 251 ms |
| **Total** | **193** | **191,616** | **388,270,611 (370.29 MiB)** | **147.306 s** | — |

There were no duplicate tickers within an individual cursor chain and no MVE markers. The probe
did not retain all ticker sets after each scan, so the 191,616-row sum is deliberately reported
before cross-scan deduplication. Payload-state validation found:

- `unopened`: 72,634 `initialized`;
- `open`: 117,186 `active` and 1 `closed`;
- `paused`: 1,690 `inactive` and 105 `closed`.

The mismatches demonstrate why query filters cannot substitute for local state validation across a
multi-minute scan. They are consistent with concurrent lifecycle changes, but the probe does not
prove their cause.

A deliberately non-normative 22-field projection serialized to 143,007,679 bytes (136.38 MiB),
compared with 370.29 MiB of raw API JSON. The streaming probe's Python allocation peak was
76,134,115 bytes (72.61 MiB), but it retained only ticker sets and one parsed page at a time. That
peak is **not** a measurement of a fully materialized compact cache; the exact field contract and
resident-memory budget remain downstream decisions.

An immediate catch-up from the Unix-second watermark taken before preload found 17 newly created
binary markets, all `initialized`, in one 35,725-byte page and 0.116 seconds. A separate
`min_updated_ts` request found 219 metadata-updated markets in one 472,817-byte page and 0.185
seconds; its payload included `active`, `initialized`, `determined`, and `finalized` rows, confirming
the need for local lifecycle filtering. The first `open` page repeated after the scan had identical
ticker membership and order in this single run. This observation neither proves cursor stability
nor contradicts Kalshi's warning that data may change between requests.

Two filter probes exposed a documentation/runtime boundary:

- `min_created_ts+status=paused+mve_filter=exclude` returned HTTP 200 with no rows;
- `min_updated_ts+status=open+mve_filter=exclude` returned HTTP 400 and stated that
  `min_updated_ts` is incompatible with filters other than `series_ticker` and
  `mve_filter=exclude`.

The first response shows only that the server accepted that empty probe; it does not establish
correct or durable `paused` incremental semantics. The documented, status-free creation query plus
local filtering remains the safer contract.

## Existing repository measurement

The repository's ready fixture corpus was captured on 2026-08-24 from the credential-free public
endpoint. Recomputing the catalogue statistics from
[`probe-manifest.json`](../spec/fixtures/kalshi/probe-manifest.json) and its raw market pages gives:

| Metric | Dated observation |
| --- | ---: |
| Query | `status=open&mve_filter=exclude&limit=1000` |
| Market pages | 96 |
| Rows / unique tickers | 95,366 / 95,366 |
| Duplicate tickers | 0 |
| Payload states | 95,366 `active` |
| Market types | 95,366 `binary` |
| Rows with non-empty MVE markers | 0 |
| Raw response bytes | 201,099,797 bytes (191.78 MiB) |
| Per-page response size | 639,226 to 3,099,876 bytes; mean 2,094,790 bytes |
| Sum of page request durations | 20.111 seconds |
| Per-page request duration | 123 to 477 ms; mean 209 ms |
| First-to-last page observation span | 21.035 seconds |
| HTTP/retry result | 96 HTTP 200 responses; zero retries |

These figures cover only the `open` traversal, not `unopened` or `paused`. They show that a raw
full catalogue is already about 192 MiB and that this traversal spans roughly 21 seconds during
which Kalshi says list data may change. They do not establish current row count, current latency,
a memory budget, or a provider service-level objective.

The current probe already follows every cursor and rejects a repeated cursor
([`probe_kalshi_public_rest.py`](../scripts/probe_kalshi_public_rest.py)). The current adapter's
synthetic regression traverses 95,366 rows while retaining only a top-two shortlist
([`test_kalshi.py`](../tests/test_kalshi.py)); it proves bounded semantic retention for the old
freeze model, not the memory cost of retaining a compact 95,000-row live catalogue.

Neither the repository fixture nor the local live probe currently establishes:

- resident memory for the exact compact-row contract;
- cross-status duplicate count for the current three-scan preload;
- completeness under concurrent creation/status movement during pagination;
- anonymous throttling behavior.

## Rate limits

Kalshi documents token buckets for authenticated traffic. `GET` operations use the Read bucket;
most requests cost 10 tokens, while `GET /account/endpoint_costs` is authoritative for current
non-default costs. Documented Read refill budgets range from 200 tokens/s for Basic to 10,000
tokens/s for Prestige, and `GET /account/limits` exposes the caller's effective refill rate and
capacity. A 429 carries no `Retry-After` or `X-RateLimit-*` header, and Kalshi recommends
exponential backoff. ([rate limits and tiers](https://docs.kalshi.com/getting_started/rate_limits),
[account limits](https://docs.kalshi.com/api-reference/account/get-account-api-limits))

`GET /markets` is a public endpoint that can be tested without authentication, but the official
documentation publishes no numeric anonymous quota. Therefore the authenticated tier table is
not a guarantee for the credential-free catalogue path, and the prior 96-page success is only a
dated observed envelope. ([making your first request](https://docs.kalshi.com/getting_started/making_your_first_request))

## Implementation-ready measurement protocol

Before freezing the cache schema and budgets, repeat the credential-free sequential probe on the
intended French host and preserve only its small report, not all raw market payloads. The report
should:

1. record a watermark immediately before network activity;
2. traverse `unopened`, `open`, and `paused` independently with
   `mve_filter=exclude&limit=1000`;
3. report rows, unique tickers, duplicates, status/type distributions, bytes, total duration,
   request latency distribution, response codes, retries, and cursor anomalies;
4. project exactly the proposed compact fields and measure serialized size plus process peak
   memory;
5. run a fully paginated `min_created_ts` catch-up from the overlapped start watermark with no
   status filter, filter locally, and report additions/duplicates;
6. run `min_updated_ts` separately to characterize metadata churn without treating it as trading
   freshness;
7. probe and record the server response for the undocumented/unsupported combinations
   `min_created_ts+status=paused` and `min_updated_ts+status=open`;
8. repeat the first `open` page and report membership/order differences as evidence of change,
   without interpreting equality as an ordering guarantee.

The exact compact field list belongs to the downstream catalogue-contract decision. At minimum,
this research implies that raw response objects must not be retained wholesale and that cache
publication must be generation-based: build, validate, catch up, then atomically swap.

## First-party sources

- [Kalshi Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi Understanding Pagination](https://docs.kalshi.com/getting_started/pagination)
- [Kalshi Market Lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)
- [Kalshi Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi Get Account API Limits](https://docs.kalshi.com/api-reference/account/get-account-api-limits)
- [Kalshi Making Your First Request](https://docs.kalshi.com/getting_started/making_your_first_request)
- [Kalshi API Changelog](https://docs.kalshi.com/changelog)
