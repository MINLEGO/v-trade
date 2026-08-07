# ADR 0001: Keep entry fees separate from gross position cost

- Status: accepted
- Date: 2026-08-07

## Context

Buy fees are debited from cash and recorded in the ledger, but closed-trade P&L
previously deducted only sell fees. Settlements also used the gross position cost
without the buy fees still attached to the open shares. Redefining `cost_basis` or
`average_cost` would break their existing gross-notional meaning and the ledger
reconciliation contract.

## Decision

Track remaining buy fees as `entry_fees_micros` on each open position. A BUY adds
its fees; a partial SELL removes the proportional share and includes it in the
realized P&L; a settlement deducts the remaining entry fees. Ledger replay follows
the same allocation using fee and position-share postings.

`cost_basis_micros` and `average_cost` remain gross trade-notional metrics. Agent
portfolio and order-result outputs expose `entry_fees_micros` separately.

`get_closed_trades` keeps its existing fields and computes realized P&L and
return-on-cost as:

```text
exit_proceeds - entry_cost - total_fees
```

The new database column defaults to zero. Historical positions are not backfilled
because no prior experiment has produced positions requiring migration accounting.

## Consequences

- Cash and fee ledger postings remain unchanged, avoiding fee double counting.
- Open-position unrealized P&L subtracts remaining entry fees while gross cost
  fields remain stable.
- Consumers that need net performance must use `entry_fees_micros` in addition to
  `cost_basis_micros`.
- Positions created before migration `0021` retain the zero default by design.
