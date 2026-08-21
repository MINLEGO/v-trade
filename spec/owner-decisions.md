# Active owner decisions

These decisions are frozen for `vtrade-kalshi-v1`. A contract change requires a new
experiment version and new artifact hashes.

- Venue: public unauthenticated Kalshi REST for read-only ingestion.
- Instrument scope: ordinary binary markets with exactly YES and NO outcomes.
- Deployment: a fresh empty PostgreSQL database using exactly four migrations; no
  legacy upgrade, conversion, dual write, or dual venue.
- Execution: paper-only IOC/FOK using real market data, exact microdollars, exact
  hundredths-of-a-contract quantities, and `best-level-haircut-v1`.
- Haircut: capture six levels, ignore the best level, retain at least 50% of raw
  depth, and fail closed when the evidence cannot satisfy the floor.
- Order boundary: `market_ref`, YES/NO, BUY/SELL, CASH/CONTRACTS, optional limit,
  agent-scoped idempotency, frozen decision context, refreshed execution context,
  lifecycle state, reconciliation state, and audit references.
- Accounting: fill-only, atomic, append-only balanced ledger postings with exact fee
  snapshots and entry-fee allocation. The concentration limit is exactly 15% of
  account value per market.
- Settlement: only an unblocked FINALIZED binary result with `settlement_ts` pays;
  duplicate finalization is a no-op and conflicting terminal evidence blocks the
  agent.
- Research: Exa is the configured provider; failures do not silently select another
  provider. Provider and storage limits remain versioned and auditable.
- Operations: `/health/live` is cheap and provider-independent; authenticated
  `/health/ready` checks PostgreSQL, the latest migration, private storage, and the
  active contract without provider calls.
- Real execution: authentication, signing, WebSockets, order submission, and
  real-money execution are disabled and unreachable from the active composition.
- Rollback: infrastructure-only. Before initialization, restore the prior snapshot
  and image. After active data exists, stop/pause and restore a matching snapshot or
  rebuild an empty target; never downgrade or mix schemas.

Historical methodology is retained only as explicitly marked archive evidence. It is
not loaded by runtime, tests, image builds, migrations, or fixture discovery and does
not establish venue or performance equivalence.

