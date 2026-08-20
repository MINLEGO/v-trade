# Kalshi liquidity haircut analysis

Analysis version: kalshi-liquidity-haircut-analysis-v1  
Capture version: kalshi-liquidity-haircut-capture-v1  
Capture completed: 2026-08-19T23:52:13.093579Z  
Capture SHA-256 (canonical JSON): 61f96cc630ca628c677fc9cef99715fad027271196b089e2c30fbc250d0a956e

## Scope and evidence

This is the throwaway prototype requested by GitHub issue [#5](https://github.com/MINLEGO/v-trade/issues/5). It uses public Kalshi market metadata and individual orderbook_fp responses; it does not use credentials, the authenticated bulk-book endpoint, or production broker code.

- Markets sampled: **48**
- Side observations (YES and NO bid ladders): **96**
- Categories represented: **Commodities, Crypto, Economics, Elections**
- Price bands represented: **0.00-0.20, 0.20-0.40, 0.40-0.60, 0.60-0.80, 0.80-1.00**
- Empty-side rate: **0.291667**
- Sample target / category cap: **48 / 8**
- Markets endpoint: https://external-api.kalshi.com/trade-api/v2/markets?status=open&mve_filter=exclude&limit=1000
- Events endpoint: https://external-api.kalshi.com/trade-api/v2/events?status=open&with_nested_markets=false&limit=200
- Order-book endpoint: https://external-api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook?depth=0

The two API bid ladders are analyzed as separate contract sides. A BUY ask ladder is the complementary NO/YES bid ladder at 1 - price, so contract depth is not duplicated; the market spread is counted once from the two best bids.

## Prototype recommendation

**best-level-50pct-6x6** (prototype_recommendation_pending_owner_review). The existing rule violated the 50% retained-depth floor on 7 non-empty sampled side(s); the proposed alternative is the first conservative candidate in the documented preference order with no observed violation.

This recommendation is evidence from one captured sample, not a statistical guarantee and not a production configuration change. Human review must decide whether to version a production rule and schedule a new migration/configuration change.

## Candidate comparison

Captured raw depth is the sum of the candidate's first observed_levels; retained depth is the executable sum after the haircut and executable-level cap. The floor is evaluated only on non-empty sampled sides.

| Candidate | Observed / executable | Ignored best levels | Max haircut | Median retained | P10 retained | Floor violations | Median tail excluded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no-haircut-5x5 | 5 / 5 | 0 | 0 | 1.000000 | 1.000000 | 0 | 0.000000 |
| best-level-25pct-6x6 | 6 / 6 | 1 | 0.25 | 0.879728 | 0.750000 | 0 | 0.000000 |
| current-six-observed-five-effective | 6 / 5 | 1 | 0.50 | 0.879728 | 0.499984 | 7 | 0.000000 |
| best-level-50pct-6x6 | 6 / 6 | 1 | 0.50 | 0.879728 | 0.500000 | 0 | 0.000000 |
| top-two-50pct-7x7 | 7 / 7 | 2 | 0.50 | 0.674892 | 0.500000 | 0 | 0.000000 |

The current rule is included as a diagnostic baseline even when its five-level tail truncation violates the explicit 50% floor. Alternatives are not promoted to production by this artifact.

## Depth, spreads, ticks, and empty sides

| Metric | Value |
| --- | ---: |
| Full-ladder best-level fraction, median / p90 | 0.013582 / 0.597329 |
| Full-ladder depth, median contracts | 1492.735000 |
| Complementary best-bid spread, median | 0.010000 |
| Complementary spread, median ticks | 3.500000 |
| Minimum configured tick, median dollars | 0.010000 |
| Crossed books | 0 |

## By category

| Category | Markets | Side obs. | Empty-side rate | Best fraction median | Spread median | Tick median | Current retained median | Current floor violations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Commodities | 12 | 24 | 0.291667 | 0.060563 | 0.040000 | 0.010000 | 0.889702 | 1 |
| Crypto | 14 | 28 | 0.285714 | 0.012437 | 0.010000 | 0.010000 | 0.873420 | 2 |
| Economics | 12 | 24 | 0.291667 | 0.013304 | 0.010000 | 0.010000 | 0.923313 | 1 |
| Elections | 10 | 20 | 0.300000 | 0.010272 | 0.010000 | 0.005500 | 0.899878 | 3 |

## By price band

| Best-quote midpoint band | Markets | Side obs. | Empty-side rate | Best fraction median | Spread median | Tick median | Current retained median | Current floor violations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.00-0.20 | 10 | 20 | 0.300000 | 0.016058 | 0.010000 | 0.010000 | 0.782179 | 3 |
| 0.20-0.40 | 9 | 18 | 0.277778 | 0.013304 | 0.025000 | 0.010000 | 0.874781 | 1 |
| 0.40-0.60 | 9 | 18 | 0.277778 | 0.011874 | 0.010000 | 0.010000 | 0.941067 | 1 |
| 0.60-0.80 | 10 | 20 | 0.300000 | 0.008910 | 0.010000 | 0.010000 | 0.841090 | 0 |
| 0.80-1.00 | 10 | 20 | 0.300000 | 0.051902 | 0.025000 | 0.010000 | 0.856492 | 2 |

## Reproduction

The capture is preserved in docs/kalshi-liquidity-haircut-capture.json; rerun the offline analysis with:

    uv run python scripts/analyze_kalshi_liquidity_haircut.py --capture docs/kalshi-liquidity-haircut-capture.json --analysis-json docs/kalshi-liquidity-haircut-analysis.json --report docs/kalshi-liquidity-haircut-analysis.md

To refresh the public sample, add --fetch. A refresh replaces the capture artifact and must be reviewed as a new evidence snapshot.

## Non-goals

- No production haircut, migration, experiment, or broker behavior changes.
- No claim of out-of-sample protection, fill probability, or venue-performance equivalence.
- No authenticated access, order submission, WebSocket data, or real-money execution.
