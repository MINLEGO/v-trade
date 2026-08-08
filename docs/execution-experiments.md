# Execution experiments

The historical `predictionarena-polymarket-v1` experiment uses the frozen
`predictionarena_unconditional` paper policy: a valid order fills at the best displayed
quote without requiring a counterparty. That policy and its recorded results are retained
for historical reproduction.

The active experiment is `predictionarena-polymarket-v1-liquidity-aware`. It uses
`liquidity_aware`, observes six raw bid/ask levels internally, applies the versioned
`best-level-haircut-v1` rule to ignore at most 50% of the best level, and executes across
at most five positive effective levels. This haircut is simulator-only: the agent-facing
order book remains the existing raw five-level tool response, and no haircut fields are
returned by `place_market_order`. The live context is refreshed at order time, rejects a
missing, stale, expired, or inconsistent context, and uses IOC semantics. The agent still
reasons over the cycle-frozen book, but execution uses the live quote, fee policy, metadata,
effective depth, cash sizing, limit, and VWAP. Available effective depth may therefore
produce a partial fill; the unfilled remainder is cancelled.

Live context construction is bounded by ten seconds, sources may differ by at most five
seconds, and a network/timeout failure gets one retry with the same intent. There is no
frozen fallback after a refresh failure and no financial mutation before a valid context.
Historical bids for existing positions remain eligible for valuation for up to 1,800
seconds; a missing or too-old bid rejects the order's financial controls.

For this treatment, displayed depth is consumed virtually in a private context identified
by the agent, cycle, immutable order-book snapshot, token, side, price level, and the
versioned haircut parameters. Ignored shares are never available, consumed, cancelled, or
shared with another agent. Sequential and concurrent orders from one agent therefore share
that context atomically, while different agents receive independent capacity. A newly
frozen book starts a new context; the prior context remains immutable audit history and is
not reconciled by mutating the market snapshot. Each execution records raw, ignored,
effective, available, consumed, cancelled, and remaining shares per level and in aggregate,
including the rule version and parameters. Replaying the same order id reuses that audit
record and does not consume capacity or recalculate the haircut.

The historical `predictionarena-polymarket-v1` definition and its results remain immutable
for reproduction. Its frozen execution semantics are not converted in place, and historical
results are never retroactively ranked against the active liquidity-aware experience. Its
legacy tool contract remains available at `spec/tool-schemas-v1-legacy.json`; the active
schema extensions are referenced only by `predictionarena-polymarket-v1-liquidity-aware`.
Operational reports must label the experiment version and execution context.
Backtesting is not a V1 feature or acceptance criterion; replay fixtures and ledger
reconstruction remain only for deterministic tests and audit verification.
