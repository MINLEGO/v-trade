# Kalshi official API research for the V-Trade migration

Research date: 2026-08-19  
Source policy: only first-party Kalshi documentation, agreements, and schedules were used.

Documentation timestamp caveat: the fixed-point page visible during this research labels itself
"Last Updated: August 20, 2026", one day after the requested research date. Its published schema is
reported below because it is the current official documentation surface, but the implementation
freeze should verify those fields against the live API/OpenAPI schema before coding.

## Executive conclusion

Kalshi can support a read-only, real-market-data paper broker without production trading
credentials: its production REST API exposes series, events, markets, and individual order books
without authentication. This is materially different from its WebSocket API, whose handshake
always requires an API key even for public-data channels. The production REST root is
`https://external-api.kalshi.com/trade-api/v2`; the production WebSocket endpoint is
`wss://external-api-ws.kalshi.com/trade-api/ws/v2`.
([market-data quick start](https://docs.kalshi.com/getting_started/quick_start_market_data),
[environments](https://docs.kalshi.com/getting_started/api_environments),
[WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets))

The geographic position is narrower than a simple "Kalshi is available in France" statement.
The Member Agreement effective June 17, 2026 lists France as a Restricted Jurisdiction for
**trading Event Contracts**. The same section expressly says those restrictions do not, by
themselves, prohibit membership or non-trading access to the platform, subject to applicable law,
Kalshi policy, and Kalshi's discretion. The official international-access help page likewise
defers eligibility to that agreement. Therefore, public REST ingestion for paper trading is
contractually distinguishable from live trading, but future live Event Contract execution from
France cannot be treated as available.
([Member Agreement, section VI](https://kalshi.com/docs/kalshi-member-agreement.pdf),
[international access](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states))

For V-Trade, the safe destination is consequently:

- public REST snapshots and polling for the paper phase;
- a venue-neutral agent order contract implemented by a paper broker now and translatable to the
  Kalshi V2 order API later;
- no production Kalshi credentials, signing, or WebSocket dependency in the French paper
  deployment;
- a hard owner/legal/venue-eligibility gate before any real Event Contract broker is enabled.

## Geographic and authentication boundary

### Confirmed

- France is explicitly prohibited for trading Kalshi Event Contracts under the current Member
  Agreement. The restriction is about trading Event Contracts, not an unconditional ban on
  non-trading access. Kalshi can still grant, deny, condition, suspend, or revoke platform access
  at its discretion.
  ([Member Agreement, pages 2-3](https://kalshi.com/docs/kalshi-member-agreement.pdf))
- Public REST market data can be queried without authentication. Kalshi's own guide demonstrates
  unauthenticated calls for a series, open markets/events, and an individual market order book.
  ([market-data quick start](https://docs.kalshi.com/getting_started/quick_start_market_data))
- Authenticated REST calls require `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, and
  `KALSHI-ACCESS-SIGNATURE`. The signature is RSA-PSS/SHA-256 over
  `timestamp + HTTP_METHOD + path`, excluding query parameters.
  ([authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests))
- Every WebSocket connection requires those credentials during the handshake. Public-data
  channels (`ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, and
  `multivariate`) do not add channel-level authorization, but they still require the authenticated
  session. The order-book channel is also authenticated.
  ([WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets),
  [order-book updates](https://docs.kalshi.com/websockets/orderbook-updates))
- The bulk endpoint `GET /markets/orderbooks` accepts up to 100 tickers but is authenticated. The
  single-market `GET /markets/{ticker}/orderbook` endpoint is explicitly unauthenticated.
  ([multiple order books](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks),
  [order-book responses](https://docs.kalshi.com/getting_started/orderbook_responses))

### Not established by official sources

- The documentation does not guarantee network reachability of public REST hosts from every
  French IP, hosting provider, or future deployment region.
- The public documentation does not publish a separate unauthenticated REST rate-limit budget.
  The documented token budgets are account tiers for authenticated requests.
- Non-trading access being permitted by the Member Agreement is not legal advice on French law and
  is not an assurance that Kalshi will issue or retain an account/API key for a French resident.

These remain real deployment gates. They should be verified with a production-host connectivity
probe from the intended VPS and, for any authenticated or live phase, written confirmation of
eligibility rather than a mock credential or assumed exception.

## Canonical market model

Kalshi has a three-level discovery hierarchy:

1. a **series**, the recurring template and rules family;
2. an **event**, the user-facing occurrence and collection of markets;
3. a **market**, one binary YES/NO contract.

Typical tickers look hierarchical, but Kalshi documents exceptions and explicitly says clients
must not parse ticker strings. Integrations should retain `series_ticker`, `event_ticker`, and the
market `ticker` from response fields as separate opaque identifiers.
([Kalshi glossary](https://docs.kalshi.com/getting_started/terms))

An event may contain multiple binary markets. `mutually_exclusive` is an event property, so an
event can represent a multi-option question through several separate binary contracts rather than
a single multi-token market. Event payloads also expose `settlement_sources`, category, and nested
markets when requested. `GET /events` excludes multivariate events; those are retrieved through
`GET /events/multivariate`.
([Get Events](https://docs.kalshi.com/api-reference/events/get-events),
[Get Event](https://docs.kalshi.com/api-reference/events/get-event),
[Get Multivariate Events](https://docs.kalshi.com/api-reference/events/get-multivariate-events))

Multivariate events are dynamically created combo events. Their markets carry
`mve_collection_ticker` and `mve_selected_legs`, where each leg identifies an event ticker,
market ticker, side, and YES settlement value. Creating a combo market is an authenticated POST;
the API index says it must occur before that market can be traded or looked up and is limited to
5,000 creations per week. Multivariate lifecycle updates also use a distinct WebSocket channel.
([multivariate collection](https://docs.kalshi.com/api-reference/multivariate/get-multivariate-event-collection),
[create multivariate market](https://docs.kalshi.com/api-reference/multivariate/create-market-in-multivariate-event-collection),
[API documentation index](https://docs.kalshi.com/llms.txt))

**Migration implication:** make the opaque market ticker the venue market reference and retain
event/series references as grouping metadata. Do not map a whole Kalshi event to one V-Trade
binary market. Standard binary markets and multivariate combo markets should be distinct ingestion
classes; excluding MVE from the first paper baseline is the lowest-risk choice because discovery
may require an authenticated mutation and its lifecycle is separate.

## Lifecycle, trading eligibility, and resolution

REST market statuses are:

| REST status | Meaning |
| --- | --- |
| `initialized` | Created but not yet open |
| `active` | Open for trading |
| `inactive` | Exchange-paused, not closed |
| `closed` | Trading closed; awaiting determination |
| `determined` | Result known; settlement timer running |
| `disputed` | Result challenged |
| `amended` | Re-determined after dispute |
| `finalized` | Settlement paid; terminal |

The list filter vocabulary is coarser: `unopened`, `open`, `paused`, `closed`, and `settled` map to
those response states. Event status filters are derived from child markets rather than a single
event state, so an event can match more than one filter. `open_time`, `close_time`,
`expected_expiration_time`, and `latest_expiration_time` have different meanings; `close_time` may
move, and `can_close_early` allows an earlier close. `expiration_time` is deprecated.
([market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle))

There is a documentation inconsistency to test: the lifecycle page documents `paused` as a market
list filter, while the `GET /markets` reference prose enumerates only `unopened`, `open`, `closed`,
and `settled`. The raw per-market `inactive` status is the safer source of truth; do not assume the
server-side `paused` filter works until probed.
([market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle),
[Get Markets](https://docs.kalshi.com/api-reference/market/get-markets))

The result can be `yes`, `no`, or `scalar`. During `determined`, the result can still be disputed;
only `finalized` means settlement has completed, and then `settlement_ts` is populated. YES or NO
holders receive $1 per winning contract, net positions are settled automatically, and timing may
vary with source availability and review. Simple binary settlement has no settlement fee; the
documentation notes special rounding/fee behavior for sub-cent scalar settlement.
([market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle),
[market settlement](https://docs.kalshi.com/getting_started/market_settlement))

**Migration implication:** discovery should admit only `active` markets, while the resolution
universe must retain held markets through `closed`, `determined`, `disputed`, and `amended`. Paper
cash must not be credited merely because `result` appeared; settlement should finalize only after
the venue reports `finalized`/`settlement_ts`. If the first contract is binary-only, reject scalar
markets fail-closed rather than assuming a $0/$1 payout.

## Prices, quantities, ticks, and the order book

Current API fields use fixed-point strings:

- price fields ending in `_dollars` support up to four decimal places;
- quantities ending in `_fp` support up to two decimal places and a 0.01-contract minimum;
- every market publishes `price_ranges`, an array of `{start, end, step}` bands that is the source
  of truth for valid prices;
- `GET /markets` exposes `volume_24h_fp` separately from cumulative `volume_fp`; the active
  discovery card preserves the former as exact contract units rather than deriving it from a
  stale or differently scoped cumulative value;
- `price_level_structure` is descriptive only and must not drive validation.

Ticks are not globally one cent. Published structures include $0.01, $0.005, $0.002, $0.001, and
$0.0001 steps, including tapered grids. Whole-cent integer fields cannot faithfully represent all
markets.
([fixed-point representation](https://docs.kalshi.com/getting_started/fixed_point_migration))

The REST order book returns aggregated bid levels for both YES and NO, not explicit asks. Each
level is `[price_dollars, count_fp]`; arrays are ascending and their last element is the best bid.
The complementary asks are derived as:

- best YES ask = $1 - best NO bid;
- best NO ask = $1 - best YES bid.

Thus a YES bid at price `x` is a NO ask at `1-x`, and vice versa.
([order-book responses](https://docs.kalshi.com/getting_started/orderbook_responses))

The batch market-candlestick endpoint accepts up to 100 comma-separated market tickers
and returns candlesticks grouped by market. Hourly candles expose a traded `price`
aggregate, `volume_fp`, and an optional synthetic continuity candle when requested;
the v1 adapter disables that synthetic candle and uses the 48 real hourly observations
for freeze-scoped volatility and volume-trend calculations. The resulting competitive
score uses the two independent YES/NO bid arrays and never double-counts derived asks.
([batch market candlesticks](https://docs.kalshi.com/api-reference/market/batch-get-market-candlesticks),
[market candlesticks](https://docs.kalshi.com/api-reference/market/get-market-candlesticks))

The WebSocket order-book stream sends a snapshot followed by sequenced deltas. By default its NO
levels use NO-leg prices, but `use_yes_price: true` requests a unified YES-price scale. Kalshi says
that unified view will become the future default, so clients should set the flag explicitly while
it exists and persist sequence numbers/timestamps.
([order direction](https://docs.kalshi.com/getting_started/order_direction),
[order-book updates](https://docs.kalshi.com/websockets/orderbook-updates))

**Migration implication:** preserve V-Trade's integer-micro dollar accounting, parse decimal
strings exactly, and validate against each market's dynamic `price_ranges`. Also introduce an
exact integer representation for hundredths of a contract even if agents initially submit only
whole contracts, because public volume and future fills may be fractional. Build a canonical
two-sided book internally so the agent does not need to understand reciprocal Kalshi bids.

## Pagination, rate limits, polling, and streaming

List endpoints use opaque cursor pagination. The usual default is 100, but maximum page size is
endpoint-specific: for example, `GET /markets` currently allows 1,000 while
`GET /events/multivariate` allows 200. A client must follow the returned cursor until it is empty
and must not impose one global maximum.
([pagination](https://docs.kalshi.com/getting_started/pagination),
[Get Markets](https://docs.kalshi.com/api-reference/market/get-markets),
[Get Multivariate Events](https://docs.kalshi.com/api-reference/events/get-multivariate-events))

Authenticated requests consume independent read/write token buckets. Most calls cost 10 tokens;
the authoritative account-specific exceptions are exposed by `GET /account/endpoint_costs`.
Basic budgets are 200 read and 100 write tokens per second, rising by tier. A 429 has no
`Retry-After` or `X-RateLimit-*` headers; Kalshi directs clients to use exponential backoff. Batch
orders cost the sum of their items rather than one request token charge.
([rate limits and tiers](https://docs.kalshi.com/getting_started/rate_limits))

For paper trading without credentials, bounded REST polling is the documented path. The design
should cache series/event metadata, page incrementally with server filters such as `status=open`
and `min_updated_ts` where compatible, fetch only selected individual books, and apply bounded
concurrency plus backoff. Authenticated WebSockets can later improve timeliness, but they cannot
be a prerequisite for the French read-only baseline.

## Fees

The July 7, 2026 official schedule defines the general taker fee as:

`round_up(M * 0.07 * C * P * (1-P))`

and the maker-fee formula, where applicable, as:

`round_up(M * 0.0175 * C * P * (1-P))`

Here `P` is contract price in dollars, `C` is contract count, and `M` is the applicable multiplier.
Maker fees apply only to listed series with a nonzero maker multiplier; maker orders are charged
only if ultimately executed, not when canceled. The schedule contains series-specific maker/taker
multipliers and says there is no settlement fee under its general settlement-fee section.
([official July 2026 fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf))

Fee policy is time-varying and layered. Kalshi publishes `GET /series/fee_changes` with scheduled
series changes and `GET /events/fee_changes` for event overrides; a null event override means the
override was cleared. Market/event payloads also expose fee override and waiver fields.
([series fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes),
[event fee changes](https://docs.kalshi.com/api-reference/events/get-event-fee-changes),
[Get Events](https://docs.kalshi.com/api-reference/events/get-events))

**Migration implication:** do not encode a single permanent fee rate. Freeze the effective
schedule/version plus series multiplier, event override, waiver, maker/taker role, price, quantity,
and rounding inputs with each paper execution. If any effective component cannot be established,
reject execution rather than silently treating the fee as zero.

## Historical data

Kalshi partitions live and historical storage, targeting roughly three months in the live tier.
`GET /historical/cutoff` returns moving cutoffs for market settlement, trades, orders, and
positions. Older markets/candlesticks and public trades must be fetched from their corresponding
`/historical/...` endpoints; old events and series remain on their normal endpoints. A complete
history may require querying both sides of the cutoff and merging them. Historical list endpoints
remain cursor-paginated.
([historical data](https://docs.kalshi.com/getting_started/historical_data),
[historical trades](https://docs.kalshi.com/api-reference/historical/get-historical-trades),
[historical markets](https://docs.kalshi.com/api-reference/historical/get-historical-markets))

Public trade records include trade ID, market ticker, fixed-point count, YES/NO prices, timestamp,
and block-trade indication. Candlesticks are available at 1-minute, 1-hour, and 1-day intervals and
contain YES bid/ask, traded price, volume, and open interest aggregates.
([Get Trades](https://docs.kalshi.com/api-reference/market/get-trades),
[market candlesticks](https://docs.kalshi.com/api-reference/market/get-market-candlesticks))

**Migration implication:** a resolution synchronizer cannot rely on `GET /markets` alone. It must
read the live/historical cutoff, keep held markets independently of discovery limits, route old
settled markets to the historical endpoint, and deduplicate by opaque market ticker and venue
timestamps/IDs.

## Agent-facing order contract and future real execution

Kalshi's current V2 order endpoint is `POST /portfolio/events/orders`. It quotes a single YES-price
book: `bid` means buy YES and `ask` means sell YES; selling YES is economically equivalent to
buying NO at the complementary price. It supports `fill_or_kill`, `immediate_or_cancel`, and
`good_till_canceled`, optional expiration, `post_only`, `cancel_order_on_pause`, `reduce_only`,
self-trade prevention, subaccounts, order groups, and exchange routing. The response reports
immediate fill count, remaining count, matching-engine timestamp, and average fill price.
([Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2))

The V2 `client_order_id` is the retry/idempotency key: Kalshi recommends generating it before
submission and rejects duplicate submissions. Authenticated response and WebSocket state can lag,
so a live broker must reconcile create/cancel responses, order updates, fills, and portfolio state
rather than assume the submitted intent is the final execution.
([create-order quick start](https://docs.kalshi.com/getting_started/quick_start_create_order),
[API documentation index](https://docs.kalshi.com/llms.txt))

The agent-facing contract can remain the same in paper and live modes if it is semantic rather
than Kalshi-shaped:

- opaque `market_ref` plus outcome (`YES`/`NO`), action (`BUY`/`SELL`), exact quantity, optional
  limit, and a venue-neutral time-in-force;
- canonical `IOC` (Kalshi's name for fill-and-kill/FAK behavior), `FOK`, and optionally `GTC`;
- broker-returned accepted/rejected status, zero or more fills, remaining quantity, effective fees,
  and a stable client mutation ID;
- the same price/tick/risk validation before either paper simulation or live submission.

The paper adapter should translate the semantic order into the canonical two-outcome book and
simulate against the same snapshots. A future live adapter alone should translate it into Kalshi's
YES-price `bid`/`ask`, `client_order_id`, and V2 flags. Authentication, RSA signing, eligibility,
venue reconciliation, and keys remain below that broker boundary and invisible to the agent.

## Decisions and unresolved gates for the migration map

1. **Recommended baseline universe:** ordinary binary markets only; explicitly exclude
   multivariate combos and scalar settlement until each has a separately tested accounting and
   discovery contract.
2. **Recommended data plane:** unauthenticated REST, including bounded individual-book fetches;
   do not require WebSocket or the authenticated bulk-book endpoint for paper mode.
3. **Recommended identifiers:** opaque market ticker as `market_ref`, with opaque event and series
   tickers retained; never infer relationships by splitting tickers.
4. **Recommended numerics:** integer micro-dollars for money/prices and exact hundredths-of-contract
   units for quantities; dynamically validate the venue's `price_ranges`.
5. **Recommended resolution gate:** pay paper positions only on `finalized`, not `determined`.
6. **Recommended fee gate:** capture effective scheduled policy and round exactly; missing policy
   is an execution rejection, not a zero-fee fallback.
7. **Future live gate:** a real Kalshi broker is architecturally supported but must remain disabled
   until France/hosting eligibility, authenticated API access, signing, reconciliation, and risk
   controls are independently approved and tested.
8. **Operational unknown:** measure public REST reachability, throttling, payloads, and latency from
   the intended French VPS. Official docs do not establish those deployment-specific facts.
