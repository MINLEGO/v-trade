# V-Trade

V-Trade is an auditable agent experiment for researching and paper-trading ordinary
binary Kalshi markets. The active first release is `vtrade-kalshi-v1` and is
paper-only.

## Domain language

**Venue**: The external prediction market that supplies read-only market data.

**Series**: A recurring venue family whose reference is opaque and never inferred by
parsing another reference.

**Event**: A user-facing occurrence grouping related markets.

**Market**: One binary proposition with exactly YES and NO outcomes.

**Market reference**: The exact opaque reference for one market, stable across live
and historical reads and not derived from a label or ticker structure.

**Outcome**: YES or NO for one market.

**Contract**: A quantity-bearing claim on one outcome, measured in exact hundredths
of a contract.

**Canonical order book**: A cutoff-bound view of executable bids and complementary
asks for both outcomes, with exact integer microdollar prices and contract units.

**Discovery universe**: The bounded active catalogue selected for one decision cycle.

**Resolution universe**: Every held or previously touched market tracked until its
validated payout is final.

**Market freeze**: The immutable market, book, fee, resolution, cutoff, and audit
evidence made available during one cycle.

**Paper execution**: A deterministic simulation against real venue data that never
submits a venue order.

**Methodological provenance**: The active experiment preserves the agreed agent
process—plans, beliefs, research, tools, and autonomous market selection—without
creating product, schema, venue, or performance equivalence with historical work.

## Safety vocabulary

Financial values are exact integer microdollars. Orders use `market_ref`, YES/NO,
BUY/SELL, CASH/CONTRACTS, optional limits, and IOC/FOK. A missing, stale, crossed,
malformed, or causally invalid market context fails closed. Only a FINALIZED binary
result with `settlement_ts` can pay a position.

