# Proposed V-Trade tool descriptions

## Shared conventions

Discovery tools inspect only the open and tradeable markets included in the current cycle’s frozen market universe. Their results are reproducible as of the returned `as_of` cutoff and are not live market data.

Discovery cards contain indicative prices, not guaranteed executable quotes. Before trading, retrieve the full market with `get_market_details` and the relevant executable book with `get_orderbook`.

Paginated discovery results may contain:

* `next_cursor`: opaque cursor for the next page;
* `has_more`: whether another page exists;
* `payload_truncated`: whether the result had to be shortened to fit the tool-result limit.

When following a cursor, reuse the same tool and the same filtering arguments. The `limit` may be changed, but filters must remain unchanged.

Monetary discovery filters such as `min_liquidity` and `min_volume_24hr` use dollar-denominated values. Output fields ending in `_micros` use millionths of a dollar.

---

# Redundant info which can be generalized in the shared part :
- Optional `min_liquidity` and `min_volume_24hr` filters can exclude inactive or shallow markets.
- Retrieve the full resolution rules with `get_market_details` and executable liquidity with `get_orderbook` before trading.
- Discovery filters identify candidates, not positive expected value. Validate the thesis with current evidence, official resolution rules, and executable liquidity.

# Market discovery tools

## `get_newest_markets`

**Proposed description**

List open, tradeable markets created within the last `hours_back` hours
of the current cycle cutoff. The default lookback is 24 hours.

Use this tool to identify newly listed individual markets that may not yet
have been widely researched or efficiently priced.

Results should be ordered primarily by creation time from newest to oldest,
with volume and liquidity used only as secondary tie-breakers.

---

## `discover_by_time_remaining`

**Proposed description**

Find open, tradeable markets whose `closes_at` time is between `hours_min` and `hours_max` hours after the current cycle cutoff. Use this tool to locate markets approaching closure or markets within a specific trading horizon.

A market’s closing time is not necessarily its resolution or payout time; always inspect the full resolution rules

---

## `discover_events`

**Proposed description**

Search for groups of related markets by event. The optional `keyword` is matched case-insensitively against market questions and market metadata. Markets are grouped by `event_id`, and event groups are ordered by their aggregated 24-hour volume.

Each event contains compact discovery cards for its associated markets. Use `get_event_markets` to inspect an event more systematically.

---

## `list_top_events`

**Proposed description**

List groups of related markets ordered by their aggregated total historical volume. Use this tool to identify the largest or most established events in the frozen market universe.

Each event contains compact discovery cards for its associated markets. Use additional tools to inspect exact resolution conditions.

---

## `get_market_details`

**Proposed description**

Retrieve the complete frozen record for one market as of the current cycle cutoff. Supply exactly one of `market_ref`, `market_id`, or `slug`. Prefer the `market_ref` returned by discovery tools when available.

The result includes the market question, official resolution rules, opening and closing times, status, tradeability, volume, liquidity, metadata, and all outcomes with their venue token identifiers. Treat the resolution rules and outcome mapping as authoritative for side selection.

Prices contained in market metadata or outcomes are indicative snapshots, not executable quotes. Call `get_orderbook` before trading.

---

## `web_search`

**Proposed description**

Search the external web for current evidence relevant to an event, market thesis, probability estimate, resolution rule, forecast, or catalyst. The tool returns up to ten search results with available titles, snippets, URLs and publication timestamps.

Check the publication date and ensure that evidence was available before the cycle’s data cutoff. Prefer primary or authoritative sources, distinguish independent sources from repeated reporting, and actively search for evidence that could disconfirm the thesis.

Search-result snippets may be incomplete or misleading. Do not treat a snippet as sufficient evidence when the underlying claim is material to the trade, and never let external reporting override the market’s official resolution rules.

---

## `get_orderbook`

**Proposed description**

Retrieve the latest valid frozen order-book snapshot for one outcome as of the current cycle cutoff. Supply exactly one of `venue_token_id`, `token_id`, or `outcome_id`.

The result contains up to five bid and ask levels, the best bid, the best ask, observation timestamps and the snapshot identifier. Use asks to evaluate a BUY and bids to evaluate a SELL. Inspect the displayed depth rather than assuming the full requested size can execute at the best price.

The tool rejects missing, causally invalid or stale books. A missing or rejected result means that executable liquidity cannot currently be verified and the outcome should not be traded unless a later valid book is obtained.

---

## `discover_by_price_volatility`

**Proposed description**

Find open, tradeable markets whose recorded price volatility is at least `min_volatility`. Volatility is currently defined as the larger absolute value of the recorded one-hour and one-day price changes.

Results are ordered by volatility magnitude descending, then by total market volume.

Use this tool to identify markets with recent repricing or potential catalysts.

---

## `get_event_markets`

**Proposed description**

Retrieve the open, tradeable markets associated with one event. Supply the required `event_id`, which may match the internal event identifier or the venue event identifier.

Markets are ordered primarily by total historical volume. Use this tool after `discover_events`, `list_top_events` or `get_newest_events` to compare related outcomes and identify mutually related or correlated positions.

The returned cards are summaries only. Do not assume that similarly worded markets resolve under identical conditions.

---

## `get_newest_events`

**Proposed description**

List event groups ordered by the creation time of their newest associated market. Use this tool to identify newly added events that may not yet have been widely researched or efficiently priced.

Each event contains compact discovery cards for its associated markets. Newness does not imply positive expected value and may coincide with low liquidity or unclear resolution conditions.

---

## `get_all_active_markets`

**Proposed description**

List all open and tradeable markets included in the current cycle’s frozen universe.

Results are ordered primarily by total historical volume and liquidity. Use this tool for exhaustive or broad discovery when narrower discovery tools are not appropriate. If needed, you can follow pagination when `has_more` is true.

Returned prices are indicative only. Use `get_market_details` and `get_orderbook` before trading.

---

## `discover_by_volume_trend`

**Proposed description**

Find open, tradeable markets whose volume trend is classified as `increasing` or `decreasing`. The current classification compares the market’s 24-hour volume with one seventh of its recorded one-week volume.

A market is classified as increasing when its latest 24-hour volume is at least its average daily volume over the recorded week; otherwise it is classified as decreasing.

Results are ordered primarily by total historical volume, not by the strength of the trend. Use volume changes as an opportunity-discovery signal, not as sufficient evidence for a directional trade.

---

## `discover_by_competitive_score`

**Proposed description**

Find open, tradeable markets whose metadata `competitive` score is at least `min_score`.

The competitive score is supplied by the market-data source and should be treated as a discovery heuristic rather than a probability or expected-value estimate. Results are ordered by competitive score descending, then by total market volume, liquidity and market id for deterministic tie-breaking.

Inspect the market rules, outcome prices and order-book depth before deciding whether a competitive market offers a tradeable edge.

---

## `discover_by_date_range`

**Proposed description**

Find open, tradeable markets whose `closes_at` calendar date falls within the inclusive `start_date` and `end_date` range. Dates use the `YYYY-MM-DD` format. Either boundary may be omitted.

This tool filters by the stated market closing date, not necessarily by the final resolution, settlement or payout date. Inspect the resolution rules before relying on the date for trading decisions.

---

## `search_tags`

**Proposed description**

Search open, tradeable markets using a case-insensitive text query over their stored metadata. Use this tool to locate markets associated with a topic, category, label or tag that may not be easy to find through event names alone.

Results are ordered primarily by total market volume. Returned cards are summaries and must be followed by `get_market_details` and, when trading, `get_orderbook`.

Current implementation note: despite the tool name, matching is performed against the complete serialized metadata object and is not restricted to normalized tag names. Matches may therefore come from unrelated metadata fields.

---

# Account and portfolio tools

## `get_balance`

**Proposed description**

Return the calling agent’s current cash ledger balance and portfolio version. `cash_micros` is expressed in millionths of a dollar.

This result does not include the market value of open positions, total account value, unrealized profit or loss, or portfolio concentration. Use `get_portfolio` to inspect positions.

After calling `place_market_order`, prefer the `portfolio_after` state returned by that order result because it reflects the immediate post-execution state.

---

## `get_portfolio`

**Proposed description**

Retrieve the calling agent’s positive-share positions from an immutable portfolio snapshot associated with the current agent cycle and portfolio version. Each position includes market and outcome identifiers, the market question, share quantity, average cost, cost basis, realized P&L and last update time.

Results are ordered deterministically and may be paginated. When `has_more` is true, continue calling this tool with `next_cursor` until every page has been reviewed. A cursor is valid only for the same agent and cycle.

The result does not provide a current executable exit price or unrealized P&L. Use the position’s venue token identifier with `get_orderbook` to estimate liquidation value and exit liquidity.

---

## `get_closed_trades`

**Proposed description**

Return the calling agent’s most recent execution fills, ordered from newest to oldest. Each record includes the executed side, filled shares, execution price, gross value, fee and fill time.

Despite the tool name, the result represents fills rather than only fully closed trades or closed positions. It may include BUY fills that opened or increased a position and SELL fills that only partially reduced one.

The tool does not include rejected order attempts, unfilled remainders or the complete thesis associated with the trade. Compare fills with the current portfolio and settlements when reconstructing performance.

---

## `get_settlements`

**Proposed description**

Return the calling agent’s most recent settled position records, ordered from newest to oldest. Each record includes the settled share quantity, payout, realized P&L and settlement time.

Use this tool to verify that an outcome has been settled and to distinguish realized settlement results from unrealized position value. Settlement P&L is authoritative for completed positions.

Current implementation limitation: settlement records do not currently include the associated market, outcome or position identifier, which may make individual records difficult to associate with a thesis.

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

Search the calling agent’s active beliefs using an optional case-insensitive `keyword` substring and an optional exact `category`. Categories are `event_analysis`, `trading_strategy`, `market_sentiment`, `market_structure` and `risk_assessment`.

Use this tool when only a subset of memory is relevant to the current market or decision. An empty keyword and category return the available active beliefs up to the selected limit.

The search covers active beliefs only. It does not search inactive history, evidence contents or semantic similarity, and a missing result does not prove that the agent has never stored a related belief.

---

## `create_general_belief`

**Proposed description**

Store a durable belief or learned conclusion for use in later cycles. Supply concise `belief_content`, one valid category and a `confidence` value from 0 to 1 representing confidence in the claim.

Use this tool for information expected to remain useful beyond the current cycle, including event analysis, strategy lessons, market-structure observations and risk-management conclusions. Do not store temporary prices, balances, execution status or facts that will quickly become stale.

This tool creates a new belief rather than updating an existing one. Before creating a belief, check for duplicates or conflicts. Delete a superseded belief and create a corrected replacement when necessary.

Current implementation limitation: the tool does not accept structured evidence, sample size, expiration date, probability range or explicit invalidation conditions.

---

## `delete_general_belief`

**Proposed description**

Deactivate one belief belonging to the calling agent by its `belief_id`. Use this tool when a belief is stale, duplicated, contradicted by stronger evidence or replaced by a more accurate formulation.

Deletion is a soft deactivation for audit purposes; the historical record is retained and may still be retrieved with `get_general_beliefs` using `include_inactive: true`.

This tool cannot delete another agent’s belief and does not provide a way to reactivate a deactivated belief. Create a new corrected belief when the underlying conclusion changes.

---

# Planning tools

## `create_long_term_plan`

**Proposed description**

Create or replace the calling agent’s active long-term trading plan. Use the plan to store durable portfolio objectives, research priorities, strategic constraints, risk-management intentions and lessons that should guide multiple future cycles.

Creating a new long-term plan supersedes the previous active long-term plan. Write a coherent replacement rather than an incremental fragment, because only the latest active plan should be treated as current.

Do not include current balances, prices or position quantities unless they are clearly timestamped and necessary for context. Current account and tool results remain authoritative over plan text.

---

## `get_next_cycle_plan`

**Proposed description**

Retrieve the calling agent’s active next-cycle plan or plans. Use this tool to recover concrete follow-up tasks, events to monitor, pending thesis checks and conditions that were intentionally deferred from a previous cycle.

A next-cycle plan is a fallible historical instruction, not current state. Verify all referenced positions, prices, deadlines and events against current tools before acting.

This tool returns only next-cycle plans. It does not retrieve the active long-term plan.

---

## `create_next_cycle_plan`

**Proposed description**

Create or replace the calling agent’s active plan for a later cycle. The plan should record specific follow-up actions, catalysts, deadlines, invalidation conditions, positions to reassess and research that remains incomplete.

Creating a new next-cycle plan supersedes the previous active next-cycle plan. Write the complete intended replacement rather than a partial addition.

The optional `cycle_date` uses the `YYYY-MM-DD` format and is stored as midnight UTC metadata. It does not schedule the agent or guarantee execution on that date.

---

# Trading tool

## `place_market_order`

**Proposed description**

Submit and immediately evaluate a paper-market order against the current cycle’s frozen order book. `token_id` must be the venue token identifier for the exact outcome being bought or sold.

For BUY orders, `amount_type` defaults to `CASH`, meaning `amount` is a dollar budget. BUY orders may instead use `SHARES`. For SELL orders, `amount_type` defaults to `SHARES`; SELL with `CASH` is not supported.

`time_in_force` defaults to `IOC`:

* `IOC` executes available eligible liquidity immediately and cancels any unfilled remainder.
* `FOK` executes only if the complete requested quantity can be filled under the order constraints; otherwise it is rejected.

The optional `limit_price` restricts execution to acceptable displayed prices between 0 and 1. The optional `conviction` value is a 0-to-1 audit value and defaults to 0.5; it does not replace an explicit probability, expected-value or risk analysis.

Before calling this tool:

1. retrieve the complete market details and resolution rules;
2. verify the exact YES/NO outcome and token;
3. retrieve the current frozen order book;
4. calculate edge and expected P&L using executable depth;
5. confirm available cash or shares;
6. check existing exposure and risk limits.

The tool may return a rejection, a complete fill or a partial IOC fill. Always inspect the returned:

* `status`;
* `rejection_code` and message;
* `filled_shares`;
* `cancelled_shares`;
* `average_price`;
* `fee_micros`;
* `cash_delta_micros`;
* `portfolio_after`;
* affected position.

Never assume that submitting an order means it executed. After a rejection or partial fill, recalculate cash, exposure, remaining edge and liquidity before deciding whether to submit another order. Do not retry unchanged orders repeatedly.

---
