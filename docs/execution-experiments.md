# Execution experiments

`vtrade-kalshi-v1` is the only active experiment. It is paper-only and uses real
public Kalshi market data for ordinary binary YES/NO markets.

The simulator observes six captured levels per executable side, applies
`best-level-haircut-v1`, retains at least 50% of the captured raw contract depth,
and executes over at most five positive effective levels. It records raw, ignored,
effective, consumed, cancelled, and remaining contract units at every level and in
aggregate. Insufficient evidence fails closed.

Orders use exact CASH or CONTRACTS amounts, exact microdollar prices, IOC/FOK
semantics, agent-scoped idempotency, a frozen decision context, and a refreshed
execution context. A refresh failure can produce only a pending reconciliation state;
it creates no cash or position reservation.

Fees come from an immutable policy snapshot and are rounded exactly. Accounting is
fill-only, append-only, balanced, and atomic with portfolio updates. Settlement is
idempotent and pays only a validated FINALIZED binary result with `settlement_ts`.

Changing the provider, prompt, model, market policy, fee policy, or execution rule
requires a new experiment version. No historical result is silently compared with
the active Kalshi cohort.

