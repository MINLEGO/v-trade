# PredictionArena tool compatibility matrix

All 29 names below are preserved exactly at the AI-facing boundary. Argument keys are
trace evidence from the 50-cycle sample; types, optionality, defaults, bounds, response
envelopes and authorization are V-Trade `inferred` contracts unless a later primary
source establishes them.

| AI-facing name | Function | Observed argument keys | V-Trade authorization |
|---|---|---|---|
| `discover_hot_markets` | discovery | hours_back, limit, min_liquidity, min_volume_24hr | frozen market cache |
| `discover_by_time_remaining` | discovery | hours_min, hours_max, limit, min_liquidity | frozen market cache |
| `discover_events` | discovery | keyword, limit, min_liquidity, min_volume_24hr | frozen market cache |
| `list_top_events` | discovery | limit, min_liquidity, min_volume_24hr | frozen market cache |
| `get_market_details` | discovery | slug | frozen market cache |
| `web_search` | research | query | configured research provider only |
| `get_orderbook` | discovery | token_id | cutoff-compatible archived snapshot |
| `browse_markets_by_volume` | discovery | limit, min_liquidity, min_volume_24hr | frozen market cache |
| `discover_by_price_volatility` | discovery | limit, min_liquidity, min_volatility | frozen market cache |
| `get_event_markets` | discovery | event_id | frozen market cache |
| `get_newest_events` | discovery | limit, min_liquidity | frozen market cache |
| `get_all_active_markets` | discovery | limit, min_liquidity, min_volume_24hr | frozen market cache |
| `discover_by_volume_trend` | discovery | limit, min_liquidity, trend | frozen market cache |
| `discover_by_competitive_score` | discovery | limit, min_liquidity, min_score | frozen market cache |
| `discover_by_date_range` | discovery | start_date, end_date, limit, min_liquidity | frozen market cache |
| `search_tags` | discovery | query | frozen market cache |
| `get_balance` | account | none | calling agent only |
| `get_portfolio` | account | none | calling agent only |
| `get_open_orders` | account | none | calling agent only |
| `get_closed_trades` | account | limit | calling agent only |
| `get_settlements` | account | limit | calling agent only |
| `get_general_beliefs` | knowledge | limit, include_inactive | calling agent only |
| `search_general_beliefs` | knowledge | keyword, category, limit | calling agent only |
| `create_general_belief` | knowledge | belief_content, category, confidence | calling agent only |
| `delete_general_belief` | knowledge | belief_id | calling agent only; deactivate, never erase |
| `create_long_term_plan` | knowledge | plan_content | calling agent only |
| `get_next_cycle_plan` | knowledge | none | calling agent only |
| `create_next_cycle_plan` | knowledge | plan_content, cycle_date | calling agent only |
| `place_market_order` | trading | token_id, side, amount, conviction | calling agent, frozen snapshot, deterministic validation |

Trace counts and examples remain in `docs/predictionarena-cycle-analysis.json`; they are
evidence fixtures, not runtime defaults.

