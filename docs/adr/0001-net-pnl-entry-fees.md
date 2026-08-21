# ADR 0001: net P&L includes allocated entry fees

## Status

Accepted.

## Decision

Buy fees remain attached to the open contract position as `entry_fees_micros`.
Partial sales release the proportional share of that allocation. A completed sale
and a FINALIZED settlement include the released entry fees in net realized P&L.

## Rationale

Gross cost basis remains useful for exposure and concentration checks, while net P&L
must include every authoritative cost. Keeping the allocation on the position makes
partial exits, settlement, replay, and dashboard projections deterministic.

## Consequences

The ledger, position projection, settlement record, and dashboard must agree on the
same allocation. A replay with divergent fee evidence fails closed; it is never
silently rounded to zero.

