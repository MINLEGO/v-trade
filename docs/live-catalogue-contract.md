# Live catalogue and freshness contract

Status: approved by the owner on 2026-09-06. This specifies `vtrade-kalshi-live-v1`;
it does not change the running `vtrade-kalshi-v1` implementation.

Decision ticket: [Define the live catalogue and freshness contract](https://github.com/MINLEGO/v-trade/issues/32).

Canonical resolution: [approved contract](https://github.com/MINLEGO/v-trade/issues/32#issuecomment-5555810956).
This file is the local working copy; the issue resolution is authoritative.

## Agreed behavior

- A decision cycle starts only after its complete market preload succeeds. A partial
  load or an older generation cannot substitute for that preload. Independent
  reconciliation and position tracking are not blocked by this discovery gate.
- Preload compact discovery data once. Do not globally fetch market rules, books,
  candles, or fee policies. These are fetched for selected markets on demand.
- Cycle membership and the observations used for discovery filtering and ranking
  remain those selected at cycle start. Later creations enter the global catalogue
  for future cycles. Refreshing a result does not recompute the global ranking.
- Attempt a provider refresh for selected results on each call; concurrent identical
  requests may share work. If refresh fails or cannot run within its budget, valid
  older observations may serve read-only responses with their actual age.
- Most read tools need not expose the technical refresh failure. Missing data must
  not become a fabricated value or an authoritative empty result. Execution
  rejection and materially partial results remain explicit.
- A refreshed candidate that no longer matches the query is skipped. Continue down
  the initial candidate ordering within a bounded refresh budget. If that budget
  prevents filling the page, return fewer results and identify the partial selection.
  Direct market reads may show a closed market.
- An individual response uses a stable selection of observations. Observations that
  arrive after selection do not mutate that response.
- Age is measured from observation capture, never catalogue publication. An age
  range may describe a set; mixed market/book observations expose separate times
  when needed. `as_of` is response assembly time and does not rejuvenate inputs.
- The shared explanation of initial-catalogue ranking belongs in the system prompt.
  Tool-specific descriptions are appropriate only if the rule applies to a small
  surface (the owner's exception: fewer than roughly three to four tools).
- Orders require their own validated, sufficiently fresh execution context. Dated
  discovery data or a read fallback cannot authorize an order.

## Approved compact projection

The following exact projection and missing-value rules are approved.
These are internal semantic fields, not approval of the future tool schemas.

| Group | Retained values |
| --- | --- |
| Identity | Exact market reference, event reference, ordinary-binary type; series reference only when explicitly supplied |
| Search text | Market title, subtitle, YES/NO labels supplied by the listing |
| Lifecycle | Payload status, creation time, opening time, closing time |
| Discovery metrics | Total volume, 24-hour volume, open interest, indicative liquidity when supplied |
| Indicative prices | Listing YES/NO best bid/ask and last price when supplied |
| Provenance | Per-row capture time; source update time when supplied; generation identity |

Normalize monetary and quantity values using exact domain units. A missing optional
value remains unknown, never zero. A ranking that requires it excludes that row from
that metric's candidate set; incomplete coverage must not be described as exhaustive.
Invalid identity, ambiguous binary eligibility, or an unparseable row fails generation
validation rather than silently dropping potentially eligible markets.

No extra per-market request is made merely to fill this projection. Event titles,
categories, series tags, rules, and derived candle/book metrics are outside this
preload unless present directly in the listing. References are never inferred by
parsing another reference. Global discovery dependent on those extra fields must be
explicitly resolved in the tool-surface ticket, including any required shared index.

## Generation and incremental protocol

Carry forward the resolved preload research: exhaust separate `unopened`, `open`,
and `paused` scans with MVE excluded, then perform a fully paginated creation catch-up
from before scan start. Check payload eligibility locally. Publish only after the
whole build, catch-up, and validation succeed. Publication is atomic locally, not
a claim that the venue supplied a simultaneous snapshot.

Approved mechanics:

- Key deduplication by exact market reference. Keep the last captured valid complete
  row for a duplicate, then apply eligibility filtering; a later closed observation
  must not resurrect an earlier open row. Do not splice fields from different rows.
- Record scan start, completion, and publication. `catalog_observed_at` is publication
  time; retain each row's original capture time across publication and reuse.
- Use a status-free creation scan with MVE excluded. Query from at least one whole
  second before the previously successful scan-start watermark. After complete
  success, advance to this scan's start, not completion or the largest returned
  creation timestamp. An empty successful scan may advance it; a failed scan may not.
- Failed or repeated-cursor scans publish no partial changes and advance no watermark.
  The previous published generation remains intact for consultation, but does not
  satisfy a new cycle's required successful preload.
- Later increments publish new global generations, without changing an active cycle.
  Scheduling/coalescing of these increments belongs to runtime and budget decisions.
- A metadata-update stream, if used, has a separate watermark. It cannot establish
  trading-data freshness. Neither creation catch-up nor deduplication guarantees
  zero omissions while the venue changes during pagination.

## Remaining work already owned by downstream tickets

- Tool surface: approve retained/modified/removed tools, additional discovery indexes,
  derived global rankings, exact response schemas, and pagination over a stable
  initial candidate order with bounded refresh/backfill.
- Execution: numeric freshness limits for market, book, and fees; causal validation
  and the order-time fail-closed rules. No numeric freshness threshold is invented here.
- Provider budgets: refresh/backfill limits, timeouts, retries, and degraded-operation
  budgets. Read fallback permission does not imply unlimited requests or retries.
- Runtime and persistence: refresh scheduling, cycle orchestration, retained evidence,
  physical storage, generation retention, and crash recovery.

No tool removal or modification is approved by this contract. No product code,
deployment, or database change is part of this decision ticket.
