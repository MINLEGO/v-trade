-- vtrade-kalshi-persistence-v1 / semantic execution, accounting, and settlement

CREATE TABLE fee_policy_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  policy_version text NOT NULL,
  formula_version text NOT NULL,
  schedule_identity text NOT NULL,
  participant_role text NOT NULL CHECK (participant_role IN ('maker', 'taker')),
  multiplier_numerator bigint NOT NULL CHECK (multiplier_numerator > 0),
  multiplier_denominator bigint NOT NULL CHECK (multiplier_denominator > 0),
  event_override_micros bigint,
  event_override_cleared boolean NOT NULL DEFAULT false,
  waiver_evidence jsonb,
  exact_inputs jsonb NOT NULL,
  effective_at timestamptz NOT NULL,
  as_of_at timestamptz NOT NULL,
  observed_at timestamptz NOT NULL,
  cutoff timestamptz NOT NULL,
  source_tier text NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  policy_fingerprint char(64) NOT NULL CHECK (policy_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (market_id, policy_fingerprint),
  CHECK (event_override_micros IS NULL OR event_override_micros >= 0),
  CHECK (NOT (event_override_cleared AND event_override_micros IS NOT NULL)),
  CHECK (observed_at <= cutoff),
  CHECK (as_of_at <= cutoff)
);

CREATE TABLE order_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  order_side text NOT NULL CHECK (order_side IN ('BUY', 'SELL')),
  amount_kind text NOT NULL CHECK (amount_kind IN ('CASH', 'CONTRACTS')),
  cash_amount_micros bigint,
  contract_units bigint,
  limit_price_micros bigint,
  time_in_force text NOT NULL CHECK (time_in_force IN ('IOC', 'FOK')),
  frozen_cutoff timestamptz NOT NULL,
  execution_cutoff timestamptz NOT NULL,
  idempotency_key text NOT NULL,
  request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (agent_id, idempotency_key),
  FOREIGN KEY (market_id, outcome_side) REFERENCES outcomes (market_id, outcome_side),
  CHECK (
    (amount_kind = 'CASH' AND cash_amount_micros IS NOT NULL AND cash_amount_micros > 0
      AND contract_units IS NULL)
    OR
    (amount_kind = 'CONTRACTS' AND contract_units IS NOT NULL AND contract_units > 0
      AND cash_amount_micros IS NULL)
  ),
  CHECK (limit_price_micros IS NULL OR limit_price_micros BETWEEN 0 AND 1000000),
  CHECK (frozen_cutoff <= execution_cutoff)
);

CREATE TABLE order_operation_current (
  operation_id uuid PRIMARY KEY REFERENCES order_operations(id),
  agent_id uuid NOT NULL REFERENCES agents(id),
  state text NOT NULL CHECK (state IN (
    'REJECTED', 'PENDING', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED'
  )),
  reconciliation_state text NOT NULL CHECK (reconciliation_state IN (
    'NOT_REQUIRED', 'REQUIRED', 'RESOLVED', 'CONFLICT'
  )),
  state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_lifecycle_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id uuid NOT NULL REFERENCES order_operations(id),
  sequence_number integer NOT NULL CHECK (sequence_number >= 0),
  state text NOT NULL CHECK (state IN (
    'REJECTED', 'PENDING', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED'
  )),
  reason text,
  observed_at timestamptz NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  UNIQUE (operation_id, sequence_number)
);

CREATE TABLE order_reconciliation_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id uuid NOT NULL REFERENCES order_operations(id),
  sequence_number integer NOT NULL CHECK (sequence_number >= 0),
  reconciliation_state text NOT NULL CHECK (reconciliation_state IN (
    'NOT_REQUIRED', 'REQUIRED', 'RESOLVED', 'CONFLICT'
  )),
  evidence jsonb NOT NULL,
  observed_at timestamptz NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  UNIQUE (operation_id, sequence_number)
);

CREATE TABLE fills (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id uuid NOT NULL REFERENCES order_operations(id),
  fill_id text NOT NULL CHECK (length(fill_id) BETWEEN 1 AND 512),
  fill_fingerprint char(64) NOT NULL CHECK (fill_fingerprint ~ '^[0-9a-f]{64}$'),
  contract_units bigint NOT NULL CHECK (contract_units > 0),
  price_micros bigint NOT NULL CHECK (price_micros BETWEEN 0 AND 1000000),
  gross_cash_micros bigint NOT NULL CHECK (gross_cash_micros >= 0),
  authoritative_fee_micros bigint NOT NULL CHECK (authoritative_fee_micros >= 0),
  net_cash_delta_micros bigint NOT NULL,
  frozen_context_id uuid,
  execution_context_id uuid,
  adapter_evidence jsonb,
  filled_at timestamptz NOT NULL,
  UNIQUE (operation_id, fill_id),
  UNIQUE (operation_id, fill_fingerprint)
);

CREATE TABLE positions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  outcome_id uuid NOT NULL REFERENCES outcomes(id),
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  contract_units bigint NOT NULL CHECK (contract_units >= 0),
  gross_cost_basis_micros bigint NOT NULL CHECK (gross_cost_basis_micros >= 0),
  entry_fees_micros bigint NOT NULL DEFAULT 0 CHECK (entry_fees_micros >= 0),
  realized_pnl_micros bigint NOT NULL DEFAULT 0,
  portfolio_version bigint NOT NULL CHECK (portfolio_version >= 0),
  updated_at timestamptz NOT NULL,
  UNIQUE (agent_id, outcome_id),
  UNIQUE (agent_id, market_id, outcome_side),
  FOREIGN KEY (market_id, outcome_side) REFERENCES outcomes (market_id, outcome_side)
);

CREATE TABLE position_fee_allocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id uuid NOT NULL REFERENCES positions(id),
  fill_id uuid REFERENCES fills(id),
  settlement_id uuid,
  contract_units bigint NOT NULL CHECK (contract_units > 0),
  fee_micros bigint NOT NULL CHECK (fee_micros >= 0),
  allocation_kind text NOT NULL CHECK (allocation_kind IN ('entry', 'sell_release', 'settlement')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((fill_id IS NOT NULL) <> (settlement_id IS NOT NULL))
);

CREATE TABLE portfolio_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  version bigint NOT NULL CHECK (version >= 0),
  reason text NOT NULL,
  created_at timestamptz NOT NULL,
  UNIQUE (agent_id, version)
);

CREATE TABLE portfolio_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  portfolio_version bigint NOT NULL CHECK (portfolio_version >= 0),
  cash_micros bigint NOT NULL,
  position_value_micros bigint NOT NULL CHECK (position_value_micros >= 0),
  account_value_micros bigint NOT NULL,
  realized_pnl_micros bigint NOT NULL,
  unrealized_pnl_micros bigint NOT NULL,
  captured_at timestamptz NOT NULL,
  UNIQUE (agent_cycle_id)
);

CREATE TABLE risk_policy_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  policy_version text NOT NULL,
  max_market_concentration_numerator bigint NOT NULL DEFAULT 15 CHECK (
    max_market_concentration_numerator > 0
  ),
  max_market_concentration_denominator bigint NOT NULL DEFAULT 100 CHECK (
    max_market_concentration_denominator > 0
  ),
  effective_at timestamptz NOT NULL,
  cutoff timestamptz NOT NULL,
  policy_inputs jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (max_market_concentration_numerator <= max_market_concentration_denominator),
  CHECK (effective_at <= cutoff)
);

CREATE TABLE risk_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  operation_id uuid NOT NULL REFERENCES order_operations(id),
  policy_snapshot_id uuid NOT NULL REFERENCES risk_policy_snapshots(id),
  account_value_micros bigint NOT NULL CHECK (account_value_micros >= 0),
  existing_market_basis_micros bigint NOT NULL CHECK (existing_market_basis_micros >= 0),
  proposed_market_basis_micros bigint NOT NULL CHECK (proposed_market_basis_micros >= 0),
  concentration_numerator bigint NOT NULL CHECK (concentration_numerator > 0),
  concentration_denominator bigint NOT NULL CHECK (concentration_denominator > 0),
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
  rejection_reason text,
  checked_at timestamptz NOT NULL,
  UNIQUE (operation_id),
  CHECK (
    decision <> 'approved'
    OR (existing_market_basis_micros::numeric + proposed_market_basis_micros::numeric)
      * concentration_denominator
      <= account_value_micros::numeric * concentration_numerator
  )
);

CREATE TABLE resolution_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  lifecycle_status text NOT NULL CHECK (lifecycle_status IN (
    'determined', 'disputed', 'amended', 'finalized', 'resolved', 'ambiguous'
  )),
  result text CHECK (result IN ('YES', 'NO')),
  observed_at timestamptz NOT NULL,
  source_timestamp timestamptz,
  settlement_ts timestamptz,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  blocked boolean NOT NULL DEFAULT false,
  UNIQUE (market_id, observed_at, raw_artifact_id),
  CHECK (lifecycle_status = 'finalized' OR settlement_ts IS NULL),
  CHECK (
    lifecycle_status <> 'finalized'
    OR blocked
    OR (result IS NOT NULL AND settlement_ts IS NOT NULL)
  ),
  CHECK (observed_at <= cutoff),
  CHECK (source_timestamp IS NULL OR source_timestamp <= cutoff)
);

CREATE TABLE settlements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  position_id uuid NOT NULL REFERENCES positions(id),
  resolution_id uuid NOT NULL REFERENCES resolution_observations(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  settlement_ts timestamptz NOT NULL,
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  contract_units bigint NOT NULL CHECK (contract_units > 0),
  gross_payout_micros bigint NOT NULL CHECK (gross_payout_micros >= 0),
  entry_fees_deducted_micros bigint NOT NULL CHECK (entry_fees_deducted_micros >= 0),
  realized_pnl_micros bigint NOT NULL,
  ledger_entry_id uuid REFERENCES ledger_entries(id),
  idempotency_key text NOT NULL UNIQUE,
  settlement_fingerprint char(64) NOT NULL CHECK (settlement_fingerprint ~ '^[0-9a-f]{64}$'),
  settled_at timestamptz NOT NULL,
  UNIQUE (position_id, resolution_id)
);

ALTER TABLE position_fee_allocations
  ADD CONSTRAINT position_fee_allocations_settlement_fk
  FOREIGN KEY (settlement_id) REFERENCES settlements(id);

ALTER TABLE ledger_postings
  ADD COLUMN market_id uuid REFERENCES markets(id),
  ADD COLUMN outcome_side text CHECK (outcome_side IN ('YES', 'NO')),
  ADD COLUMN contract_units_delta bigint,
  ADD CONSTRAINT ledger_posting_dimensions_atomic CHECK (
    (market_id IS NULL AND outcome_side IS NULL AND contract_units_delta IS NULL)
    OR (market_id IS NOT NULL AND outcome_side IS NOT NULL AND contract_units_delta IS NOT NULL
      AND contract_units_delta <> 0)
  );

CREATE OR REPLACE FUNCTION vtrade_guard_order_operation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  cycle_cutoff timestamptz;
  grid_match boolean;
BEGIN
  IF EXISTS (
    SELECT 1 FROM order_operation_current
     WHERE agent_id = NEW.agent_id AND reconciliation_state IN ('REQUIRED', 'CONFLICT')
  ) THEN
    RAISE EXCEPTION 'agent % has an unresolved pending reconciliation', NEW.agent_id;
  END IF;
  SELECT data_cutoff INTO cycle_cutoff FROM agent_cycles WHERE id = NEW.agent_cycle_id;
  IF cycle_cutoff IS NULL OR NEW.frozen_cutoff > cycle_cutoff
     OR NEW.execution_cutoff > cycle_cutoff THEN
    RAISE EXCEPTION 'order operation is newer than its agent-cycle cutoff';
  END IF;
  IF NEW.limit_price_micros IS NOT NULL THEN
    SELECT EXISTS (
      SELECT 1 FROM market_price_grid_ranges ranges
       WHERE ranges.market_id = NEW.market_id
         AND NEW.limit_price_micros BETWEEN ranges.start_price_micros
                                         AND ranges.end_price_micros
         AND (NEW.limit_price_micros - ranges.start_price_micros) % ranges.step_micros = 0
    ) INTO grid_match;
    IF NOT grid_match THEN
      RAISE EXCEPTION 'limit price is not on the market grid';
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_create_order_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO order_operation_current
    (operation_id, agent_id, state, reconciliation_state, state_version, updated_at)
  VALUES (NEW.id, NEW.agent_id, 'PENDING', 'NOT_REQUIRED', 0, NEW.created_at);
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_guard_fill_cash_delta() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  operation_side text;
BEGIN
  SELECT order_side INTO operation_side FROM order_operations WHERE id = NEW.operation_id;
  IF operation_side = 'BUY'
     AND NEW.net_cash_delta_micros <> -(NEW.gross_cash_micros + NEW.authoritative_fee_micros) THEN
    RAISE EXCEPTION 'BUY fill net cash delta does not include authoritative fee';
  END IF;
  IF operation_side = 'SELL'
     AND NEW.net_cash_delta_micros <> NEW.gross_cash_micros - NEW.authoritative_fee_micros THEN
    RAISE EXCEPTION 'SELL fill net cash delta does not include authoritative fee';
  END IF;
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_guard_resolution_conflict() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  prior resolution_observations%ROWTYPE;
BEGIN
  SELECT * INTO prior FROM resolution_observations
   WHERE market_id = NEW.market_id
     AND lifecycle_status = 'finalized'
     AND blocked = false
   ORDER BY observed_at DESC, id DESC
   LIMIT 1;
  IF prior.id IS NOT NULL
     AND (prior.result IS DISTINCT FROM NEW.result
       OR prior.settlement_ts IS DISTINCT FROM NEW.settlement_ts) THEN
    NEW.blocked := true;
  END IF;
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_guard_settlement() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  observation resolution_observations%ROWTYPE;
  position_row positions%ROWTYPE;
  prior_settlement settlements%ROWTYPE;
BEGIN
  SELECT * INTO observation FROM resolution_observations WHERE id = NEW.resolution_id;
  IF observation.id IS NULL OR observation.lifecycle_status <> 'finalized'
     OR observation.blocked OR observation.result IS NULL
     OR observation.settlement_ts IS NULL
     OR observation.settlement_ts <> NEW.settlement_ts
     OR observation.market_id <> NEW.market_id THEN
    RAISE EXCEPTION 'settlement requires one validated, unblocked FINALIZED observation';
  END IF;
  SELECT * INTO position_row FROM positions WHERE id = NEW.position_id;
  IF position_row.id IS NULL OR position_row.agent_id <> NEW.agent_id
     OR position_row.market_id <> NEW.market_id
     OR position_row.outcome_side <> NEW.outcome_side
     OR position_row.contract_units <> NEW.contract_units THEN
    RAISE EXCEPTION 'settlement position does not match the finalized market outcome';
  END IF;
  IF NEW.gross_payout_micros <> CASE
    WHEN observation.result = NEW.outcome_side THEN NEW.contract_units * 10000
    ELSE 0
  END THEN
    RAISE EXCEPTION 'binary settlement payout is inconsistent with the finalized result';
  END IF;
  SELECT * INTO prior_settlement FROM settlements
   WHERE idempotency_key = NEW.idempotency_key OR (position_id = NEW.position_id
     AND resolution_id = NEW.resolution_id)
   LIMIT 1;
  IF prior_settlement.id IS NOT NULL THEN
    IF prior_settlement.settlement_fingerprint = NEW.settlement_fingerprint
       AND prior_settlement.agent_id = NEW.agent_id
       AND prior_settlement.position_id = NEW.position_id
       AND prior_settlement.resolution_id = NEW.resolution_id
       AND prior_settlement.market_id = NEW.market_id
       AND prior_settlement.settlement_ts = NEW.settlement_ts
       AND prior_settlement.outcome_side = NEW.outcome_side
       AND prior_settlement.contract_units = NEW.contract_units
       AND prior_settlement.gross_payout_micros = NEW.gross_payout_micros
       AND prior_settlement.entry_fees_deducted_micros = NEW.entry_fees_deducted_micros THEN
      RETURN NULL;
    END IF;
    RAISE EXCEPTION 'settlement idempotency key or finalized pair conflicts with prior evidence';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER order_operations_guard
BEFORE INSERT ON order_operations
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_order_operation();
CREATE TRIGGER order_operations_projection
AFTER INSERT ON order_operations
FOR EACH ROW EXECUTE FUNCTION vtrade_create_order_projection();
CREATE TRIGGER fills_cash_delta_guard
BEFORE INSERT ON fills
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_fill_cash_delta();
CREATE TRIGGER resolution_conflict_guard
BEFORE INSERT ON resolution_observations
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_resolution_conflict();
CREATE TRIGGER settlement_finalized_guard
BEFORE INSERT ON settlements
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_settlement();

CREATE TRIGGER fee_policy_snapshots_append_only
BEFORE UPDATE OR DELETE ON fee_policy_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER order_operations_append_only
BEFORE UPDATE OR DELETE ON order_operations
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER order_lifecycle_append_only
BEFORE UPDATE OR DELETE ON order_lifecycle_events
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER order_reconciliation_append_only
BEFORE UPDATE OR DELETE ON order_reconciliation_events
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER fills_append_only
BEFORE UPDATE OR DELETE ON fills
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER fee_allocations_append_only
BEFORE UPDATE OR DELETE ON position_fee_allocations
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER portfolio_versions_append_only
BEFORE UPDATE OR DELETE ON portfolio_versions
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER portfolio_snapshots_append_only
BEFORE UPDATE OR DELETE ON portfolio_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER risk_policy_snapshots_append_only
BEFORE UPDATE OR DELETE ON risk_policy_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER risk_checks_append_only
BEFORE UPDATE OR DELETE ON risk_checks
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER resolution_observations_append_only
BEFORE UPDATE OR DELETE ON resolution_observations
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER settlements_append_only
BEFORE UPDATE OR DELETE ON settlements
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

COMMENT ON TABLE order_operations IS
  'Agent-facing semantic order request. Venue request IDs, signatures, raw payloads, and '
  'bid/ask translation are intentionally outside this table.';
COMMENT ON TABLE positions IS
  'Projection keyed by agent, opaque market, and YES/NO outcome. Quantities are exact '
  'hundredths of a contract; entry fees are kept separate from gross cost basis.';
COMMENT ON TABLE resolution_observations IS
  'Append-only evidence. Only an unblocked FINALIZED row with result and settlement_ts '
  'can be consumed by settlements.';
