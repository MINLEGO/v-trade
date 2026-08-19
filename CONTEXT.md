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

**Market**:
One binary proposition whose mutually exclusive outcomes are YES and NO.
_Avoid_: Event, token

**Outcome**:
The YES or NO side of a market that determines a position's payoff.
_Avoid_: Token, market

**Contract**:
The quantity-bearing claim on one outcome of a market; contract quantities may be fractional.
_Avoid_: Share, token

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
