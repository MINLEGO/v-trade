# V-Trade

V-Trade is an auditable agent experiment for researching and paper-trading ordinary
binary Kalshi markets. The active first release is `vtrade-kalshi-v1` and is
paper-only.

## Domain language

**Venue**: The external prediction market that supplies read-only market data.

**Series**: A recurring venue family whose reference is opaque and never inferred by
parsing another reference.

**Event**: A user-facing occurrence grouping related markets.

**Market**: The durable identity of one binary proposition with exactly YES and NO
outcomes. Its changing venue state belongs to market observations.

**Market reference**: The exact opaque reference for one market, stable across live
and historical reads and not derived from a label or ticker structure.

**Outcome**: YES or NO for one market.

**Contract**: A quantity-bearing claim on one outcome, measured in exact hundredths
of a contract.

**Canonical order book**: A venue-normalized view of executable bids and
complementary asks for both outcomes, with exact integer microdollar prices and
contract units.

**Live catalogue**: The global collection of known ordinary binary markets currently
eligible for consideration.

**Catalogue generation**: One atomically published, internally coherent version of
the live catalogue.

**Cycle catalogue**: The catalogue generation selected as the starting market set for
one decision cycle. Its membership and discovery-ranking observations remain stable
for that cycle, while observations used for selected market reads may be refreshed.
_Avoid_: Discovery universe

**Tracked market set**: Every market kept accessible outside the current cycle
catalogue because an order, position, settlement, reconciliation, or financial
history requires it. Its membership is independent of cycle-catalogue membership.
_Avoid_: Resolution universe

**Market observation**: A capture of a market's venue state at one observation time;
only observations needed to support audit are retained, and retained observations
are immutable.

**Order-book observation**: A capture of one canonical order book at one observation
time; it is retained when needed to support audit and is immutable once retained.

**Fee-policy observation**: A capture of the fee policy applicable to one market at
one observation time; it is retained when needed to support audit and is immutable
once retained.

**Resolution observation**: A capture of a market's resolution state at one
observation time; it is retained when needed to support audit and is immutable once
retained.

**Settlement**: The financial record of a payable market outcome, created only from
a validated binary `FINALIZED` result with `settlement_ts`; a resolution observation
alone is not a payout.

**Decision cycle**: One scheduled agent run that becomes active only after selecting
a successfully loaded cycle catalogue. Once failed or abandoned, it is terminal and
is never resumed; another run receives a new identity.

**Abandoned decision cycle**: A decision cycle whose exclusive runtime ownership was
lost before a clean terminal result could be recorded.

**Initial prompt context**: The agent-visible information assembled at the beginning
of one decision cycle; later tool observations do not retroactively change it, and
`as_of` is normally its sufficient temporal marker. It briefly warns when the
previous started decision cycle failed or was abandoned.
_Avoid_: Cycle context, Market freeze

**Execution context**: The precise and sufficiently fresh market, order-book, fee,
and risk observations validated for one order attempt.

**Observation time (`observed_at`)**: The instant at which V-Trade captured one
observation from the venue.

**Source update time (`source_updated_at`)**: The venue-reported instant at which the
source record last changed, when the venue supplies it.

**Catalogue observation time (`catalog_observed_at`)**: The instant at which a
complete catalogue generation was published.

**As-of time (`as_of`)**: The instant at which a response is assembled from its
selected observations, without implying simultaneous capture or resetting their age.
_Avoid_: data_cutoff

**Observation age**: The elapsed time between an observation time and the instant at
which its freshness is evaluated.

**Stale observation**: An observation that is causally valid for its `as_of` time but
older than the freshness policy permits. It may remain visible with its age, but it
cannot authorize an execution context.

**Provider evidence**: Immutable raw venue material, or its verifiable fingerprint,
that supports an observation. It is retained only when the cycle trace is
insufficient or when reconciliation, ledger, or settlement requires it.

**Provider invocation**: One identified paid model or research request whose usage
remains accountable even if its decision cycle fails or is abandoned.

**Operational record**: A record of runtime activity such as a cycle, attempt, tool
call, failure, or resource usage.

**Cycle trace**: The primary audit record of one decision cycle, including its
context, activity, decisions, and outcomes.

**Financial record**: An authoritative durable record of an order, fill, ledger
entry, position, or settlement.

**Release evidence**: A redacted operator record proving the result and scope of a
validation or release gate.

**Paper execution**: A simulation against real venue data that never submits a venue
order. It preserves the same execution, accounting, and reconciliation details as
the real-order path, without promising determinism or replay equivalence for the
surrounding cycle.

**Methodological provenance**: The active experiment preserves the agreed agent
process—plans, beliefs, research, tools, and autonomous market selection—without
creating product, schema, venue, or performance equivalence with historical work.

## Safety vocabulary

Financial values are exact integer microdollars. Orders use `market_ref`, YES/NO,
BUY/SELL, CASH/CONTRACTS, optional limits, and IOC/FOK. A missing, stale, crossed,
malformed, or causally invalid market context fails closed. Only a FINALIZED binary
result with `settlement_ts` can pay a position.

