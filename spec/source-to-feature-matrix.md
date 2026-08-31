# vtrade-kalshi-v1 source-to-feature matrix

The active matrix describes the Kalshi paper release. Historical source material is
linked only from the read-only archive and is not an active configuration, fixture,
or runtime input.

| Feature or rule | Classification | Active evidence |
|---|---|---|
| Ordinary binary YES/NO market | vtrade_deviation | `src/vtrade/domain/types.py`, `src/vtrade/kalshi.py` |
| Opaque series/event/market/outcome identity | vtrade_deviation | `SeriesKey`, `EventKey`, `MarketKey`, `OutcomeKey` |
| Exact fixed-point prices and money | vtrade_deviation | integer microdollar parsers and contract-unit parsers |
| Dynamic per-market price grid | vtrade_deviation | `PriceGrid` and adapter normalization tests |
| Reciprocal canonical order book | vtrade_deviation | `build_canonical_order_book` and adapter tests |
| Complete opaque-cursor catalogue traversal | vtrade_deviation | public REST adapter tests and probe script |
| Live/historical routing and cutoff causality | vtrade_deviation | adapter and fixture-manifest checks |
| Freeze-scoped market metrics | vtrade_deviation | `market_metrics.py`, batched candlestick capture, metric persistence |
| `volume_24h_fp` and exact series tags | vtrade_deviation | Kalshi normalization and series metadata snapshots |
| Six-level best-level haircut | vtrade_deviation | `best-level-haircut-v1` audit and floor tests |
| IOC/FOK semantic paper order | vtrade_deviation | order request/result and execution tests |
| Exact fee rounding and cent alignment | vtrade_deviation | fee policy and calculation tests |
| Fill-only balanced accounting | vtrade_deviation | ledger and portfolio replay tests |
| Exact 15% concentration limit | vtrade_deviation | risk predicate and broker tests |
| FINALIZED-only idempotent settlement | vtrade_deviation | settlement engine and persistence tests |
| Independent agent schedules and isolated balances | vtrade_deviation | runtime/bootstrap contracts |
| Provider-independent readiness | vtrade_deviation | API/admin/runtime tests and deployment shape |
| Content-addressed raw evidence | inferred | artifact and fixture manifest validation |
| Paper-only real-data simulation | owner decision | active configuration and disabled real adapter |

Every active row is subject to the nine-migration persistence boundary, frozen artifact
hashes, the active-venue sweep, and the external evidence gates in the release matrix.
