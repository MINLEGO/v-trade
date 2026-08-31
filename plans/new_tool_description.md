# Proposed V-Trade tool descriptions

## Shared conventions

Discovery, market-details, and order-book tools inspect the open and tradeable markets in the current cycle’s frozen market universe. Their results are reproducible as of the returned `as_of` cutoff and are decision inputs, not live execution guarantees. `place_market_order` attempts to refresh the execution context at order time and may fill partially or reject when the live context no longer supports the request.

Discovery cards contain indicative prices, not guaranteed executable quotes. Before trading, retrieve the full market with `get_market_details` and the relevant executable book with `get_orderbook`. Filters help identify candidates, but do not guarantee positive expected value. Validate your thesis with current evidence, official resolution rules, and executable liquidity.

Paginated discovery results may contain:

* `next_cursor`: opaque cursor for the next page;
* `has_more`: whether another page exists;
* `payload_truncated`: whether the result had to be shortened to fit the tool-result limit, which is usually 4000 tokens.

When following a cursor, reuse the same tool and the same filtering arguments. The `limit` may be changed, but filters must remain unchanged.

Monetary discovery filters such as `min_liquidity` and `min_volume_24hr` use dollar-denominated values, using these filters can help exclude inactive or shallow markets. Output fields ending in `_micros` use millionths of a dollar.

A market’s closing time is not necessarily its resolution or payout time — always inspect the full resolution rules before relying on it for trading decisions.

If a required market detail, resolution rule, executable book, or fee policy is missing, causally invalid, or stale, fail closed and do not trade. A `fee_policy` of `null` is a valid unavailable-data result, not a tool error; without it, fee-inclusive expected value cannot be verified.

For a taker estimate using `formula_version` `polymarket-v2-p-one-minus-p`, estimate the fee at each executable price as `shares * rate * price * (1 - price)` and sum across levels when a request crosses multiple prices. The source `exponent` is raw fee metadata and is not an additional multiplier unless the advertised formula version requires it. The fee returned by `place_market_order` is authoritative after execution and replaces the estimate.
---

# Market discovery tools

## `get_newest_markets`

**Proposed description**

List open, tradeable markets opened within the last `hours_back` hours of the current cycle cutoff.

Use this tool to identify newly listed individual markets that may not yet have been widely researched or efficiently priced.

Results will be ordered primarily by opening time from newest to oldest, with market ID as a secondary tie-breaker.

---

## `discover_by_time_remaining`

**Proposed description**

Find open, tradeable markets whose `closes_time` is between `hours_min` and `hours_max` hours after the current cycle cutoff. Use this tool to locate markets approaching closure or markets within a specific trading horizon.

Results are ordered by remaining time ascending, with the soonest-closing markets first.

---

## `discover_events`

**Proposed description**

Search for groups of related markets by event. The optional(s) `keyword` are matched case-insensitively against market questions and market metadata. Markets are grouped by `event_id`, and event groups are ordered by their aggregated 24-hour volume.

---

## `list_top_events`

**Proposed description**

List groups of related markets ordered by their aggregated total historical volume. Use this tool to identify the largest or most established events in the frozen market universe.

---

## `get_market_details`

**Proposed description**

Retrieve the complete frozen record for one market as of the current cycle cutoff.

The result includes the market question, official resolution rules, opening and closing times, status, tradeability, volume, liquidity, metadata, and all outcomes with their venue token identifiers. Treat the resolution rules and outcome mapping as authoritative for side selection.

Prices contained in market metadata or outcomes are indicative snapshots, not executable quotes. Call `get_orderbook` before trading.

---

## `web_search`

**Proposed description**

Search the external web for current evidence relevant to an event, market thesis, probability estimate, resolution rule, forecast, or catalyst. The tool returns up to ten search results with available titles, snippets, URLs and publication timestamps.

You can balance the number of result and their quality using `num_results` and `max_highlight_length`. Note that to avoid truncated results, max_highlight_length × num_results must not exceed 15000 character and will return an error otherwise. The optional `start_published_date` and `end_published_date` arguments define the publication-date range. Each accepts a non-negative number of days back from now or a `YYYY-MM-DD` date; they default to 30 and 0 respectively.

Check the publication date and ensure that evidence was available before the cycle’s data cutoff. Prefer primary or authoritative sources, distinguish independent sources from repeated reporting, and actively search for evidence that could disconfirm the thesis.

Search-result snippets may be incomplete or misleading. When the information is critical, try using `fetch_webpage` to retrieve more details and never let external reporting override the market’s official resolution rules.

---

## `fetch_webpage`

**Proposed description**

Retrieve the readable content of a specific public webpage URL. Use this tool when a search result, market rule, official announcement, report, dataset page or primary source must be inspected directly. Prefer the original or authoritative source when a search result points to one.

Optionally set `result_type` to `full_text` for the full page text or `highlights` for focused excerpts; When using `highlights`, `highlight_query` can guide the snippet selection; it must be omitted or set to `null` for `full_text`. `max_length` defaults to 4000 characters and cannot exceed 12000.

---

## `get_orderbook`

**Proposed description**

Retrieve the latest valid frozen reciprocal YES/NO order-book snapshot for one market as of the current cycle cutoff.

The result contains up to five bid and ask levels per outcome, observation timestamps, audit references, and `fee_policy`. Use asks to evaluate a BUY and bids to evaluate a SELL. Inspect the displayed depth rather than assuming the full requested size can execute at the best price.

`fee_policy` is either `null` or an object containing the applicable `contract_version`, `schedule_version`, `formula_version`, `participant_role`, multiplier values, event-override fields, effective and observation timestamps, cutoff, source tier, policy fingerprint, and audit metadata. A null policy means that fees cannot be estimated reliably; do not place an order for that market until a later result provides a usable policy.

Only the displayed levels are available for analysis; do not assume additional executable depth beyond what the result contains.

The tool rejects missing, causally invalid, or stale books. A missing or rejected result means that executable liquidity cannot currently be verified, and the market should not be traded unless a later valid book is obtained.

---

## `discover_by_price_volatility`

**Proposed description**

Find open, tradeable markets whose recorder price volatility is at
least `min_volatility_micros`. Volatility is the sample standard deviation of available
consecutive hourly close changes in the recent 24-hour window; missing or insufficient
price observations are not converted to zero.

Results are ordered by volatility magnitude descending, then by total market volume.

Use this tool to identify markets with recent repricing or potential catalysts.

---

## `get_event_markets`

**Proposed description**

Retrieve the open, tradeable markets associated with one event. Supply the required `event_ref`, which may match the internal event identifier or the venue event identifier.

Markets are ordered primarily by total historical volume. Use this tool after `discover_events`, `list_top_events` or `get_newest_events` to compare related outcomes and identify mutually related or correlated positions.

---

## `get_newest_events`

**Proposed description**

List event groups ordered by the opening time of their newest associated market. Use this tool to identify newly added events that may not yet have been widely researched or efficiently priced.

---

## `get_all_active_markets`

**Proposed description**

List all open and tradeable markets included in the current cycle’s frozen universe.

Results are ordered primarily by total historical volume and liquidity. Use this tool for exhaustive or broad discovery when narrower discovery tools are not appropriate.

---

## `discover_by_volume_trend`

**Proposed description**

Find open, tradeable markets whose volume trend is classified as `increasing`,
`decreasing`, `flat`, or `insufficient_data`. The current classification compares two
complete consecutive 24-hour windows from hourly market candlesticks and exposes
`volume_trend_delta = (recent - baseline) / baseline` when the baseline is non-zero.

A market is classified by comparing the two windows. If either required window is incomplete, the classification is `insufficient_data`; a zero baseline leaves the trend valid but the delta null.

---

## `discover_by_competitive_score`

**Proposed description**

Find open, tradeable markets whose competitive score is at least
`min_score`.

The score is a bounded Kalshi-native discovery heuristic combining reciprocal-book
spread, balanced near-midpoint depth, and `volume_24h_fp` activity. It should be
treated as a discovery heuristic rather than a probability or expected-value estimate.
Results are ordered by score descending.

---

## `discover_by_date_range`

**Proposed description**

Find open, tradeable markets whose `date_basis` calendar date falls within the inclusive `start_date` and `end_date` range. `date_basis` accepts `close_time` (the default) or `open_time`. Dates use the `YYYY-MM-DD` format. Either boundary may be omitted. Results are ordered by volume and liquidity descending.

---

## `search_tags`

**Proposed description**

Search open, tradeable markets using exact case-insensitive membership over their associated tags. Use this tool to locate markets associated with
a topic, category, label or tag that may not be easy to find through event names alone. Tags use OR semantics.

Results are ordered primarily by total market volume. Returned cards are summaries and must be followed by `get_market_details` and, when trading, `get_orderbook`.

---

# Account and portfolio tools

## `get_balance`

**Proposed description**

Return the calling agent’s current cash ledger balance and portfolio version. `cash_micros` is expressed in millionths of a dollar (microdollars).

This result covers cash only; it does not include open-position value, total account value, unrealized P&L, or concentration. Use `get_portfolio` to inspect positions.

 After an order, use its status, fills, and cash deltas, then call get_balance again when persisted account state is needed.

---

## `get_portfolio`

**Proposed description**

Retrieve the calling agent’s current positive-contract positions from the portfolio projection. Each position includes market and position identifiers, the market_question, the number of units held, cost basis, realized P&L, and last update time.

`contract_units` are exact hundredths-of-a-contract units. Monetary fields use integer microdollars. `gross_cost_basis_micros` is the gross acquisition cost; `entry_fees_micros` is reported separately and must not be subtracted twice.

This tool does not provide an average entry-price field, current executable exit price, unrealized P&L, total account value, or portfolio concentration. Use `market_ref` with `get_orderbook` to inspect exit liquidity. Positions with zero contracts are omitted; use `get_closed_trades` or `get_settlements` for completed-position history.

---

## `get_closed_trades`

**Proposed description**

Return positions that were fully closed through SELL executions, ordered from newest to oldest. Each record aggregates the complete position lifecycle, including entry and exit quantities, average prices, fees, realized P&L, and opening and closing times.

Partially sold positions are not included until their remaining shares reach zero. Positions closed through market settlement are returned separately by get_settlements.

---

## `get_settlements`

**Proposed description**

Return the calling agent’s most recent settled position records, ordered from newest to oldest. Each record includes the settled share quantity, payout, realized P&L, settlement time, market_question, winning outcome (nullable) and the outcome chosen by the agent.

Use this tool to verify that an outcome has been settled and to distinguish realized settlement results from unrealized position value. Settlement P&L is authoritative for completed positions.

---

# Belief and memory tools

## `get_general_beliefs`

**Proposed description**

Retrieve stored beliefs belonging to the calling agent. By default, only active beliefs are returned. Set `include_inactive` to true when reviewing historical, superseded or deleted beliefs.

Beliefs may concern a specific event, general trading strategy, market sentiment, market structure or risk management. Treat them as fallible historical conclusions rather than current facts. Verify time-sensitive beliefs against current evidence before using them.

Use this tool periodically to identify stale, duplicated or conflicting beliefs. Delete beliefs that are no longer supported rather than allowing contradictory memory to accumulate.

---

## `search_general_beliefs`

**Proposed description**

Search the calling agent’s beliefs using optionals case-insensitive `keyword` substrings and an optional exact `category`. Categories are `event_analysis`, `trading_strategy`, `market_sentiment`, `market_structure` and `risk_assessment`.

Use this tool when only a subset of memory is relevant to the current market or decision. An ommitted keyword and category return the available beliefs up to the selected limit, restricted to active beliefs by default.

When `include_inactive` is true, the search also covers inactive history. It does not search evidence contents or semantic similarity, and a missing result does not prove that the agent has never stored a related belief.

Results are ordered newest first and may be paginated. If so, make sure to keep `keyword`, `category` and `include_inactive` unchanged when following pagination.

---

## `create_general_belief`

**Proposed description**

Store a durable belief or learned conclusion for use in later cycles. Supply concise `belief_content`, one valid category and a `confidence` value from 0 to 1 representing confidence in the claim. You may also provide an optional `evidence` list containing concise source references or supporting observations.

Use this tool for information expected to remain useful beyond the current cycle, including event analysis, strategy lessons, market-structure observations and risk-management conclusions. Do not store temporary prices, balances, execution status or facts that will quickly become stale.

This tool creates a new belief rather than updating an existing one. Before creating a belief, check for duplicates or conflicts. Delete a superseded belief and create a corrected replacement when necessary.

Evidence is stored with the belief for later review; it is not searched by `search_general_beliefs`. The tool does not accept sample size, expiration date, probability range or explicit invalidation conditions.

---

## `delete_general_belief`

**Proposed description**

Deactivate one belief belonging to the calling agent by its `belief_id`. Use this tool when a belief is stale, duplicated, contradicted by stronger evidence or replaced by a more accurate formulation.

Deletion is a soft deactivation for audit purposes; the historical record is retained and may still be retrieved with `get_general_beliefs` or `search_general_beliefs` using `include_inactive: true`.

This tool does not provide a way to reactivate a deactivated belief, and deactivated beliefs may be completely deleted in the future. Create a new corrected belief when the underlying conclusion changes.

---

# Planning tools

## `create_long_term_plan`

**Proposed description**

Replace the calling agent’s active long-term trading plan. Use the plan to store durable portfolio objectives, research priorities, strategic constraints, risk-management intentions and the most important lessons that should guide multiple future cycles.

Creating a new long-term plan supersedes the previous active long-term plan. Write a coherent replacement rather than an incremental fragment, because only the latest active plan should be treated as current.

Do not include current balances, prices or position quantities unless they are clearly timestamped and necessary for context. Current account and tool results remain authoritative over plan text.

Call this tool only when a replacement is intended; repeated identical content is not a meaningful update.

---

## `create_next_cycle_plan`

**Proposed description**

Replace the plan that will be supplied to the agent at the beginning of its next cycle.

Use this tool near the end of the current cycle to preserve concrete follow-up actions, catalysts to monitor, positions to reassess, unresolved research, deadlines and explicit invalidation conditions. If the next cycle plan is not replaced, the agent will receive the same plan as this cycle.

Only one next-cycle plan exists. Calling this tool again during the same cycle replaces the previously created next-cycle plan; it does not append another plan.

Write the complete replacement plan rather than an incremental update. Do not repeat information already stored as durable general beliefs or long-term strategy unless it is directly relevant to the next cycle.

The optional cycle_date is descriptive scheduling metadata. It does not schedule the agent or guarantee execution on that date. Except for maintenance or other exceptional circumstances, the period between each cycle is 1 hour.

---

# Trading tool

## `place_market_order`

**Proposed description**

Submit and evaluate one order for the specified binary market using an
execution context refreshed at order time. The current cycle’s frozen order
book is decision evidence, not a fill guarantee. `market_ref` plus `outcome`
(`YES` or `NO`) identifies the requested contract.

`CASH` amounts are integer microdollars. `CONTRACTS` amounts are integer hundredths-of-a-contract units. You may submit a BUY in either unit type, but a SELL must only be done in `CONTRACTS` units.

* `IOC` executes available eligible liquidity immediately and rejects any unfilled remainder.
* `FOK` executes only if the complete requested quantity can be filled under the order constraints; otherwise it is rejected.
* `limit_price_micros` restricts execution prices to the interval between 0 and 1 000 000. For a BUY it is the maximum acceptable per-unit price; for a SELL it is the minimum acceptable per-unit price. It does not guarantee a fill and does not replace the fee, expected-value, or risk checks. Set the value to null to ignore price limits and accept any available liquidity.

Before calling this tool:

1. retrieve the complete market details and resolution rules;
2. verify the exact YES/NO outcome;
3. review the current frozen order book;
4. verify a non-null fee policy and calculate net edge and expected P&L using executable depth and the fee estimate;
5. confirm available cash or contracts;
6. check existing exposure and risk limits.

A result may be `REJECTED`, `PENDING`, `PARTIALLY_FILLED`, `FILLED`, or
`CANCELLED`. `PENDING` means that no fill is confirmed and reconciliation is
required; do not treat it as filled or submit another order for the affected
account until reconciliation is resolved.

Inspect `operation_id`, `status`, `reconciliation_state`, `error_code`,
`message`, contract-unit quantities, `fills`, cash deltas, fees, context IDs,
timestamps, and audit references. Reuse an idempotency key only for the same
request. Returned fees are authoritative.

Never assume that submitting an order means it executed. After a rejection or partial fill, recalculate cash, exposure, remaining edge and liquidity before deciding whether to submit another order. Do not retry unchanged orders repeatedly.
---
