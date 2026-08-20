# V-Trade

V-Trade is an auditable agent experiment for researching and trading prediction markets. Its
current venue is Kalshi, while its cognitive and experimental method derives from
PredictionArena's Polymarket experiment.

## Language

**Venue**:
The external prediction market that supplies market data and may ultimately execute orders.
_Avoid_: Provider, platform

**Event**:
A user-facing occurrence that groups one or more related markets.
_Avoid_: Market, contract

**Series**:
A venue-defined recurring family of related markets and rules; its identity is retained as an
opaque venue reference and is never inferred by parsing a market reference.
_Avoid_: Event, market

**Market**:
One binary proposition whose mutually exclusive outcomes are YES and NO.
_Avoid_: Event, token

**Market reference**:
The exact opaque venue reference for one Market, stable across live and historical reads and not
derived from a label, slug, or ticker structure.
_Avoid_: Token ID, condition ID, slug

**Outcome**:
The YES or NO side of a market that determines a position's payoff.
_Avoid_: Token, market

**Outcome side**:
Exactly one of YES or NO for a Market; its identity is owned by the Market and side, not by a
venue-specific token or leg.
_Avoid_: Token, leg

**Contract**:
The quantity-bearing claim on one outcome of a market; contract quantities may be fractional.
_Avoid_: Share, token

**Canonical order book**:
A frozen two-sided view of executable bids and complementary asks for both outcome sides, with
exact prices and contract quantities observed at one cutoff.
_Avoid_: Raw venue book, quote

**Discovery universe**:
The bounded set of active, tradeable markets made available for an agent to investigate during a
cycle.
_Avoid_: Resolution universe, predefined market list

**Resolution universe**:
Every previously touched or held market that must remain tracked until its payout is final.
_Avoid_: Discovery universe, active-market shortlist

**Market freeze**:
The immutable venue state made available to an agent for one decision cycle.
_Avoid_: Live market state, cache

**Paper execution**:
An execution simulated against real venue data without submitting an order to the venue.
_Avoid_: Fake trade, mock execution

**Real execution**:
An execution submitted to and reconciled with the venue.
_Avoid_: Live context, paper execution

**Methodological comparability**:
Continuity with the agent process used by PredictionArena's Polymarket experiment, including
Plans, Beliefs, tools, and autonomous market selection; it creates no product or venue lineage
with Polymarket and does not imply financial-performance comparability across venues.
_Avoid_: Performance equivalence
