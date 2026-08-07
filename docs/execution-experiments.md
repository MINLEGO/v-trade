# Execution experiments

The historical `predictionarena-polymarket-v1` experiment uses the frozen
`predictionarena_unconditional` paper policy: a valid order fills at the best displayed
quote without requiring a counterparty. That policy and its recorded results are retained
for historical reproduction.

The active experiment is `predictionarena-polymarket-v1-liquidity-aware`. It uses
`liquidity_aware`, walks up to five displayed bid/ask levels from a target-market context
refreshed at order time, rejects a missing, stale, expired, or inconsistent live context,
and uses IOC semantics. The agent still reasons over the cycle-frozen book, but execution
uses the live quote, fee policy, metadata, depth, cash sizing, limit, and VWAP. Available
depth may therefore produce a partial fill; the unfilled remainder is cancelled.

Live context construction is bounded by ten seconds, sources may differ by at most five
seconds, and a network/timeout failure gets one retry with the same intent. There is no
frozen fallback after a refresh failure and no financial mutation before a valid context.
Historical bids for existing positions remain eligible for valuation for up to 1,800
seconds; a missing or too-old bid rejects the order's financial controls.

For this treatment, displayed depth is consumed virtually in a private context identified
by the agent cycle and the immutable order-book snapshot. Sequential and concurrent orders
from one agent therefore share that context atomically, while different agents receive
independent capacity. A newly frozen book starts a new `agent-cycle-v1` context; the prior
context remains immutable audit history and is not reconciled by mutating the market
snapshot. Each execution records displayed, available, consumed, cancelled, and remaining
shares per level and in aggregate. Replaying the same order id reuses that audit record and
does not consume capacity again.

The historical `predictionarena-polymarket-v1` definition and its results remain immutable
for reproduction. Its frozen execution semantics are not converted in place, and historical
results are never retroactively ranked against the active liquidity-aware experience. Its
legacy tool contract remains available at `spec/tool-schemas-v1-legacy.json`; the active
schema extensions are referenced only by `predictionarena-polymarket-v1-liquidity-aware`.
Operational reports must label the experiment version and execution context.
Backtesting is not a V1 feature or acceptance criterion; replay fixtures and ledger
reconstruction remain only for deterministic tests and audit verification.
