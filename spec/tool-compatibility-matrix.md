# PredictionArena tool compatibility matrix

All 27 names below are preserved exactly at the AI-facing boundary. Argument keys are
trace evidence from the 50-cycle sample; types, optionality, defaults, bounds, response
envelopes and authorization are V-Trade `inferred` contracts unless a later primary
source establishes them.

| AI-facing name | Function | Observed argument keys | V-Trade authorization |
|---|---|---|---|
| `get_newest_markets` | discovery | hours_back, limit, min_liquidity, min_volume_24hr | frozen market cache; creation date descending |
| `discover_by_time_remaining` | discovery | hours_min, hours_max, limit, min_liquidity | frozen market cache |
| `discover_events` | discovery | keyword (string or tuple[str]), limit, min_liquidity, min_volume_24hr | frozen market cache |
| `list_top_events` | discovery | limit, min_liquidity, min_volume_24hr | frozen market cache |
| `get_market_details` | discovery | slug | frozen market cache |
| `web_search` | research | query, max_highlight_length, num_results, start_published_date, end_published_date | configured research provider only |
| `fetch_webpage` | research | url, result_type, highlight_query, max_length | configured Exa contents provider only |
| `get_orderbook` | discovery | token_id | cutoff-compatible archived snapshot |
| `discover_by_price_volatility` | discovery | limit, min_liquidity, min_volatility | frozen market cache |
| `get_event_markets` | discovery | event_id | frozen market cache |
| `get_newest_events` | discovery | limit, min_liquidity | frozen market cache |
| `get_all_active_markets` | discovery | limit, min_liquidity, min_volume_24hr | frozen market cache |
| `discover_by_volume_trend` | discovery | limit, min_liquidity, trend | frozen market cache |
| `discover_by_competitive_score` | discovery | limit, min_liquidity, min_score | frozen market cache |
| `discover_by_date_range` | discovery | start_date, end_date, limit, min_liquidity | frozen market cache |
| `search_tags` | discovery | query (string or tuple[str]) | frozen market cache |
| `get_balance` | account | none | calling agent only |
| `get_portfolio` | account | none | calling agent only |
| `get_closed_trades` | account | limit | calling agent only |
| `get_settlements` | account | limit | calling agent only |
| `get_general_beliefs` | knowledge | cursor, limit, include_inactive | calling agent only; newest-first paginated result |
| `search_general_beliefs` | knowledge | cursor, keyword (string or tuple[str]), category, include_inactive, limit | calling agent only; newest-first paginated result |
| `create_general_belief` | knowledge | belief_content, category, confidence, evidence | calling agent only |
| `delete_general_belief` | knowledge | belief_id | calling agent only; deactivate, never erase |
| `create_long_term_plan` | knowledge | plan_content | calling agent only |
| `create_next_cycle_plan` | knowledge | plan_content, cycle_date | calling agent only |
| `place_market_order` | trading | token_id, side, amount, conviction | calling agent; frozen decision context plus target-market live quote, fee, metadata and depth refresh; deterministic validation |

Trace counts and examples remain in `docs/predictionarena-cycle-analysis.json`; they are
evidence fixtures, not runtime defaults.

For `discover_events`, `search_tags`, and `search_general_beliefs`, the keyword-bearing
argument accepts either the legacy string or a non-empty tuple/JSON array of strings.
Multiple values use OR semantics: matches for each keyword are merged into one paginated
result, with events and markets emitted only once.

Account position outputs preserve gross `cost_basis_micros` and `average_cost` and add
`entry_fees_micros` for buy fees still attached to open shares. Closed-trade P&L is net
of `total_fees_micros`; settlement P&L is net of the position’s remaining entry fees.
