# vtrade-kalshi-v1 compatibility statement

## Active scope

The active release supports only paper execution over ordinary binary Kalshi markets.
It admits exactly YES and NO outcomes, opaque market references, exact integer
microdollar prices, exact hundredths-of-a-contract quantities, dynamic price grids,
canonical reciprocal books, freeze-scoped hourly market metrics, exact series tags,
IOC/FOK orders, immutable fee snapshots, and FINALIZED-only settlement with
`settlement_ts`.

The metric contract is `kalshi-market-metrics-v1`: `volume_24h_units` is sourced from
`volume_24h_fp`; volatility is the sample standard deviation of available consecutive
hourly close changes in the recent 24-hour window; volume trend compares two complete
24-hour windows and reports `insufficient_data` when either window is incomplete; and
competitive score combines reciprocal-book spread, balanced near-midpoint depth, and
24-hour activity. These values are discovery heuristics and never substitute for an
executable order book or a probability estimate.

## Boundary

Paper execution and a future real adapter share the venue-neutral semantic order and
result contract. Kalshi endpoint paths, cursor mechanics, bid-only translation, fee
source details, raw payloads, and reconciliation evidence remain below that boundary.
The future real adapter is disabled in this version: no authentication, signing,
WebSocket, order-submission, or real-money fallback is reachable.

There is no compatibility loader, alias, dual venue, dual write, legacy database
upgrade, historical conversion, or application rollback path. The deployment starts
from an empty database and uses the exact nine-migration chain. This v1 revision
refreshes the active artifact hashes and adds the metric and order-time execution
persistence migrations;
incompatible future changes still require a new experiment version. Missing external
resources and unresolved reviewed fixture evidence fail closed.

## Historical provenance

Historical evidence is read-only and lives outside active source, configuration,
specification, fixtures, migrations, tests, and image inputs. It is not imported or
loaded by runtime, tests, image builds, migrations, or fixture discovery and does not
establish venue, schema, execution, or performance equivalence.

Historical provenance (controlled): the agent methodology is informed by the
PredictionArena Polymarket experiment; this sentence records provenance only and is
not an active venue, schema, compatibility, or performance claim.
