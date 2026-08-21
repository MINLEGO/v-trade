# vtrade-kalshi-v1 compatibility statement

## Methodology

The process remains comparable to the PredictionArena-derived methodology at the
level of isolated accounts, Plans, Beliefs, autonomous discovery, research-before-
trade, cycle cutoffs, risk controls, and append-only audit. That provenance is
historical methodology only; it is not a product, schema, runtime, venue, or
performance-compatibility claim.

## Active domain contract

The active release supports only paper execution over ordinary binary Kalshi markets.
Each market has opaque `market_ref`, `series_ref`, and `event_ref` identities and
exactly two outcomes: `YES` and `NO`. Prices and money are integer microdollars;
contract quantities are integer hundredths. The canonical book contains reciprocal
YES/NO bids and asks, with raw evidence and the immutable `data_cutoff` retained for
audit.

Paper execution and a future real adapter share the same venue-neutral semantic order
and result contract: `market_ref`, `outcome`, `action`, `amount`, `amount_type`,
optional exact limit price, `IOC` or `FOK`, agent-scoped idempotency, lifecycle state,
reconciliation state, fills, fees, cash deltas, and audit references. Provider URLs,
transport identifiers, credentials, signing, and raw payloads remain below that
boundary. The future real adapter is disabled in this release.

## Compatibility boundaries

- Methodology compatibility is intentional.
- Domain compatibility is limited to the semantic binary market/order/result contract.
- There is no compatibility loader, alias, dual venue, dual write, legacy database
  upgrade, historical data conversion, or real-execution fallback.
- Historical evidence may remain in a clearly marked read-only archive, but active
  configuration, prompt, schema, fixture discovery, migrations, and composition do
  not import or load it.
- Results from the historical methodology are not assumed to transfer to Kalshi and
  do not establish equivalent financial performance.

## Deployment gate

The first deployment starts from an empty database using the four clean migrations.
The public read-only catalogue and book capture, complete cursor traversal, raw-byte
hashes, and French-host reachability evidence must be present in the Kalshi fixture
manifest before a production tool context is composed. Missing external evidence
fails closed; it is never replaced by a fabricated response or a local provider.
