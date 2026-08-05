# Execution experiments

The historical `predictionarena-polymarket-v1` experiment uses the frozen
`predictionarena_unconditional` paper policy: a valid order fills at the best displayed
quote without requiring a counterparty. That policy and its recorded results are retained
for historical reproduction.

The conservative comparison is a separate immutable experiment:
`predictionarena-polymarket-v1-liquidity-aware`. It uses `liquidity_aware`, walks up to five
displayed bid/ask levels from the cycle-frozen order book, rejects a missing or stale book
older than 300 seconds, and uses IOC semantics. Available depth may therefore produce a
partial fill; the unfilled remainder is cancelled.

For this treatment, displayed depth is consumed virtually in a private context identified
by the agent cycle and the immutable order-book snapshot. Sequential and concurrent orders
from one agent therefore share that context atomically, while different agents receive
independent capacity. A newly frozen book starts a new `agent-cycle-v1` context; the prior
context remains immutable audit history and is not reconciled by mutating the market
snapshot. Each execution records displayed, available, consumed, cancelled, and remaining
shares per level and in aggregate. Replaying the same order id reuses that audit record and
does not consume capacity again.

These execution treatments measure different things. Returns, fills, turnover, and other
performance measures from the conservative experiment must not be combined with or ranked
directly against the historical baseline. Any comparison must label the experiment version
and qualify the execution-policy difference; the conservative results are not a retroactive
correction to the baseline.
