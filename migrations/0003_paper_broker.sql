-- Phase 3: deterministic paper execution, replayable position dimensions, and
-- transaction-safe idempotency fingerprints. This migration is intentionally not
-- auto-applied by the application.

ALTER TABLE agents
  ADD COLUMN portfolio_version bigint NOT NULL DEFAULT 0
    CHECK (portfolio_version >= 0);

ALTER TABLE orders
  ADD COLUMN liquidity_time_in_force text NOT NULL DEFAULT 'FAK'
    CHECK (liquidity_time_in_force IN ('FAK', 'FOK')),
  ADD COLUMN execution_fingerprint char(64) NOT NULL DEFAULT repeat('0', 64)
    CHECK (execution_fingerprint ~ '^[0-9a-f]{64}$');

ALTER TABLE orders ALTER COLUMN execution_fingerprint DROP DEFAULT;

ALTER TABLE fills
  ADD COLUMN fee_rate numeric(30, 18) NOT NULL DEFAULT 0
    CHECK (fee_rate BETWEEN 0 AND 1),
  ADD COLUMN fee_exponent numeric(30, 18)
    CHECK (fee_exponent >= 0),
  ADD COLUMN fee_taker_only boolean NOT NULL DEFAULT true,
  ADD COLUMN fee_formula_version text NOT NULL
    DEFAULT 'polymarket-v2-p-one-minus-p';

COMMENT ON COLUMN fills.fee_exponent IS
  'Raw Polymarket fd.e parameter retained for audit. The documented v2 fee formula '
  'is C * rate * p * (1-p) and does not currently describe use of this exponent.';

ALTER TABLE ledger_postings
  ADD COLUMN market_id uuid REFERENCES markets(id),
  ADD COLUMN outcome_id uuid REFERENCES outcomes(id),
  ADD COLUMN shares_delta numeric(30, 12);

ALTER TABLE ledger_postings
  ADD CONSTRAINT ledger_posting_dimensions_atomic CHECK (
    (market_id IS NULL AND outcome_id IS NULL)
    OR (market_id IS NOT NULL AND outcome_id IS NOT NULL)
  ),
  ADD CONSTRAINT ledger_position_share_dimensions CHECK (
    shares_delta IS NULL
    OR (account = 'position_cost' AND market_id IS NOT NULL AND shares_delta <> 0)
  );

CREATE INDEX ledger_postings_agent_replay_idx
  ON ledger_postings (outcome_id, ledger_entry_id)
  WHERE outcome_id IS NOT NULL;

ALTER TABLE settlements
  ADD COLUMN as_of_cutoff timestamptz,
  ADD COLUMN execution_fingerprint char(64) NOT NULL DEFAULT repeat('0', 64)
    CHECK (execution_fingerprint ~ '^[0-9a-f]{64}$');

ALTER TABLE settlements ALTER COLUMN execution_fingerprint DROP DEFAULT;

COMMENT ON COLUMN settlements.as_of_cutoff IS
  'Immutable cycle cutoff used to prove source_created_at, observed_at, and '
  'eligible_after were all known before settlement.';

COMMENT ON COLUMN orders.liquidity_time_in_force IS
  'Explicit remainder semantics for liquidity_aware. FAK permits a partial fill; '
  'FOK rejects the complete order when displayed depth is insufficient.';

COMMENT ON COLUMN orders.execution_fingerprint IS
  'Canonical SHA-256 of the domain execution result. Idempotency-key reuse must match it.';
