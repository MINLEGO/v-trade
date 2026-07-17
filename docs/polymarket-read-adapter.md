# Polymarket read adapter

Checked against the official public APIs on 2026-07-16.

## Implemented contract

- Gamma `GET /events/keyset` with opaque `after_cursor` / `next_cursor` pagination;
- Gamma `GET /markets/keyset`, capped at the current documented maximum of 100 rows;
- CLOB public `GET /book?token_id=...` on demand;
- strict Event, Market and Outcome normalization, including JSON-string parsing for
  `outcomes`, `clobTokenIds` and `outcomePrices`;
- explicit market `status` and `tradeable` derivation from `active`, `closed`,
  `enableOrderBook`, `acceptingOrders` and valid 1:1 CLOB token mappings;
- display prices kept as indicative values only; executable bid/ask comes exclusively
  from an archived CLOB book;
- resolution ingestion only when singular `umaResolutionStatus` is `resolved`, the
  outcome prices are exactly one `1` and all remaining `0`, and the source timestamp is
  no later than the observation cutoff;
- bounded retries (three attempts by default), bounded exponential backoff, response
  byte limits, request-count bounds and a conservative client-side rate limiter;
- raw response archival before normalization through the `ArtifactStore` port.

Gamma and CLOB reads are public and unauthenticated. Production artifact persistence is
not optional: `SupabaseArtifactStore` validates that the configured bucket exists and is
private, then gzip-archives content-addressed raw bytes. Runtime never falls back to the
local fixture store.

## Cutoff rule

Every live response receives an `observed_at` timestamp only after its bytes have been
received. That timestamp is the earliest cycle cutoff allowed to consume the normalized
record. When a Gamma `updatedAt` or CLOB book timestamp is slightly ahead because of
provider/local clock skew, the effective cutoff advances to that source timestamp; it
is never backdated to local receive time. The accepted skew is strictly bounded by the
versioned `maximum_source_clock_skew_seconds` setting (five seconds in v1), and larger
skews are rejected as look-ahead violations. The versioned discovery cache excludes every page newer than a requested
`as_of`; naive timestamps are rejected. Current live endpoints are never used to
reconstruct an older historical cutoff.

## Contract evidence

`spec/fixtures/polymarket/manifest.json` records four bounded live responses: one events
page, one active-markets page, one order book and one exact resolved market. Replay tests
use those byte-exact local responses with network disabled. The recorder is
`scripts/record_polymarket_contracts.py` and has a five-page maximum when locating an
exact `1/0` resolved example.

Official references:

- <https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination>
- <https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination>
- <https://docs.polymarket.com/api-reference/market-data/get-order-book>
- <https://docs.polymarket.com/api-reference/rate-limits>
- <https://docs.polymarket.com/concepts/resolution>

## Known limits

- The in-process discovery cache is suitable for a single worker and deterministic
  cutoffs; database repository wiring is a later orchestration step. Migration `0002`
  adds the append-only page observation and normalized tradeability fields needed for it.
- Event pages can legitimately contain legacy/non-CLOB nested markets with no token
  mapping. They remain non-tradeable and retain their unmapped outcome names in metadata;
  no token identifier is invented.
- WebSocket `market_resolved` ingestion is not required for v1 polling and is not
  implemented in this increment.
