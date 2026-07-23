-- Replace token-level base-fee normalization with the per-market CLOB fee curve.
-- Existing base_fee_bps values remain as legacy audit data; new observations use
-- fee_rate, fee_exponent and fee_taker_only from clob-markets/{condition_id}.fd.

ALTER TABLE fee_rate_snapshots
  ADD COLUMN condition_id text,
  ADD COLUMN fee_rate numeric(30, 18),
  ADD COLUMN fee_exponent numeric(30, 18),
  ADD COLUMN fee_taker_only boolean;

UPDATE fee_rate_snapshots f
SET condition_id = m.condition_id,
    fee_rate = f.base_fee_bps::numeric / 10000,
    fee_taker_only = true
FROM outcomes o
JOIN markets m ON m.id = o.market_id
WHERE o.id = f.outcome_id;

ALTER TABLE fee_rate_snapshots
  ALTER COLUMN base_fee_bps DROP NOT NULL,
  ALTER COLUMN condition_id SET NOT NULL,
  ALTER COLUMN fee_rate SET NOT NULL,
  ALTER COLUMN fee_taker_only SET DEFAULT true,
  ALTER COLUMN fee_taker_only SET NOT NULL,
  ADD CONSTRAINT fee_rate_snapshots_fee_rate_check
    CHECK (fee_rate BETWEEN 0 AND 1),
  ADD CONSTRAINT fee_rate_snapshots_fee_exponent_check
    CHECK (fee_exponent IS NULL OR fee_exponent >= 0);

COMMENT ON COLUMN fee_rate_snapshots.base_fee_bps IS
  'Legacy raw /fee-rate value retained for audit; not used for current fee calculation.';
COMMENT ON COLUMN fee_rate_snapshots.condition_id IS
  'CLOB market condition whose fd fee parameters were observed.';
COMMENT ON COLUMN fee_rate_snapshots.fee_rate IS
  'Decimal fd.r parameter from GET /clob-markets/{condition_id}.';
COMMENT ON COLUMN fee_rate_snapshots.fee_exponent IS
  'Raw fd.e parameter retained for audit; the current formula does not apply it.';
COMMENT ON COLUMN fee_rate_snapshots.fee_taker_only IS
  'Raw fd.to parameter; current paper executions are explicitly taker executions.';
