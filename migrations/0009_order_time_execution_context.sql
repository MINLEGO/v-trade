-- vtrade-kalshi-persistence-v1 / order-time execution context and reconciliation

CREATE TABLE order_operation_intents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  outcome_id uuid NOT NULL REFERENCES outcomes(id),
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  order_side text NOT NULL CHECK (order_side IN ('BUY', 'SELL')),
  amount_kind text NOT NULL CHECK (amount_kind IN ('CASH', 'CONTRACTS')),
  cash_amount_micros bigint,
  contract_units bigint,
  limit_price_micros bigint,
  time_in_force text NOT NULL CHECK (time_in_force IN ('IOC', 'FOK')),
  frozen_context_id text,
  frozen_cutoff timestamptz NOT NULL,
  requested_at timestamptz NOT NULL,
  idempotency_key text NOT NULL,
  request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
  status text NOT NULL CHECK (status IN ('OPEN', 'FINALIZED')),
  operation_id uuid REFERENCES order_operations(id),
  created_at timestamptz NOT NULL,
  UNIQUE (agent_id, idempotency_key),
  UNIQUE (operation_id),
  FOREIGN KEY (market_id, outcome_side) REFERENCES outcomes (market_id, outcome_side),
  CHECK (
    (amount_kind = 'CASH' AND cash_amount_micros IS NOT NULL AND cash_amount_micros > 0
      AND contract_units IS NULL)
    OR
    (amount_kind = 'CONTRACTS' AND contract_units IS NOT NULL AND contract_units > 0
      AND cash_amount_micros IS NULL)
  ),
  CHECK (limit_price_micros IS NULL OR limit_price_micros BETWEEN 0 AND 1000000),
  CHECK (frozen_cutoff <= requested_at),
  CHECK (status = 'OPEN' OR operation_id IS NOT NULL)
);

CREATE INDEX order_operation_intents_open_idx
  ON order_operation_intents (agent_id, requested_at)
  WHERE status = 'OPEN';

CREATE TABLE execution_contexts (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL UNIQUE REFERENCES order_operations(id),
  agent_id uuid NOT NULL REFERENCES agents(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  frozen_context_id text,
  frozen_cutoff timestamptz NOT NULL,
  refreshed_at timestamptz NOT NULL,
  execution_cutoff timestamptz NOT NULL,
  fee_policy_snapshot_id uuid REFERENCES fee_policy_snapshots(id),
  created_at timestamptz NOT NULL,
  CHECK (frozen_cutoff <= refreshed_at),
  CHECK (refreshed_at <= execution_cutoff)
);

CREATE TABLE execution_market_snapshots (
  id uuid PRIMARY KEY,
  execution_context_id uuid NOT NULL UNIQUE REFERENCES execution_contexts(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  market_ref text NOT NULL CHECK (length(market_ref) BETWEEN 1 AND 512),
  lifecycle_status text NOT NULL CHECK (lifecycle_status IN (
    'initialized', 'active', 'inactive', 'open', 'closed', 'determined',
    'disputed', 'amended', 'finalized', 'resolved', 'ambiguous'
  )),
  eligible boolean NOT NULL,
  tradeable boolean NOT NULL,
  question text NOT NULL,
  resolution_rules text NOT NULL,
  resolution_source text,
  open_time timestamptz NOT NULL,
  close_time timestamptz,
  expected_expiration_time timestamptz,
  latest_expiration_time timestamptz,
  volume_units bigint NOT NULL CHECK (volume_units >= 0),
  liquidity_micros bigint NOT NULL CHECK (liquidity_micros >= 0),
  observed_at timestamptz NOT NULL,
  source_updated_at timestamptz,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  CHECK (observed_at <= cutoff),
  CHECK (source_updated_at IS NULL OR source_updated_at <= cutoff),
  CHECK (source_updated_at IS NULL OR source_updated_at <= observed_at),
  CHECK (close_time IS NULL OR close_time >= open_time),
  CHECK (expected_expiration_time IS NULL OR expected_expiration_time >= open_time),
  CHECK (latest_expiration_time IS NULL OR latest_expiration_time >= open_time)
);

ALTER TABLE order_book_snapshots
  ALTER COLUMN freeze_id DROP NOT NULL,
  ADD COLUMN execution_context_id uuid REFERENCES execution_contexts(id),
  ADD CONSTRAINT order_book_snapshot_owner CHECK (
    (freeze_id IS NULL) <> (execution_context_id IS NULL)
  ),
  ADD CONSTRAINT execution_context_book_unique UNIQUE (execution_context_id, market_id);

ALTER TABLE fills
  ADD CONSTRAINT fills_execution_context_fk
  FOREIGN KEY (execution_context_id) REFERENCES execution_contexts(id);

CREATE OR REPLACE FUNCTION vtrade_guard_execution_context() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  operation_row order_operations%ROWTYPE;
BEGIN
  SELECT * INTO operation_row FROM order_operations WHERE id = NEW.operation_id;
  IF operation_row.id IS NULL
     OR operation_row.agent_id <> NEW.agent_id
     OR operation_row.market_id <> NEW.market_id
     OR operation_row.frozen_cutoff <> NEW.frozen_cutoff
     OR operation_row.execution_cutoff <> NEW.execution_cutoff
     OR operation_row.created_at > NEW.created_at THEN
    RAISE EXCEPTION 'execution context does not match its order operation';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER execution_context_integrity
BEFORE INSERT ON execution_contexts
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_execution_context();

CREATE OR REPLACE FUNCTION vtrade_guard_execution_market_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  context_market_id uuid;
  context_cutoff timestamptz;
  expected_market_ref text;
BEGIN
  SELECT market_id, execution_cutoff
    INTO context_market_id, context_cutoff
    FROM execution_contexts WHERE id = NEW.execution_context_id;
  SELECT market_ref INTO expected_market_ref FROM markets WHERE id = NEW.market_id;
  IF context_market_id IS NULL
     OR context_market_id <> NEW.market_id
     OR expected_market_ref IS NULL
     OR expected_market_ref <> NEW.market_ref
     OR context_cutoff <> NEW.cutoff THEN
    RAISE EXCEPTION 'execution market snapshot does not match its execution context';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER execution_market_snapshot_integrity
BEFORE INSERT ON execution_market_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_execution_market_snapshot();

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
     OR NEW.frozen_cutoff > NEW.execution_cutoff
     OR NEW.created_at < NEW.frozen_cutoff
     OR NEW.created_at > NEW.execution_cutoff THEN
    RAISE EXCEPTION 'order operation has invalid temporal evidence';
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

DROP TRIGGER IF EXISTS order_book_snapshot_cutoff_guard ON order_book_snapshots;

CREATE OR REPLACE FUNCTION vtrade_guard_order_book_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  expected_cutoff timestamptz;
  artifact_observed_at timestamptz;
  context_market_id uuid;
BEGIN
  IF NEW.freeze_id IS NOT NULL THEN
    SELECT data_cutoff INTO expected_cutoff FROM market_freezes WHERE id = NEW.freeze_id;
    IF expected_cutoff IS NULL OR NEW.cutoff <> expected_cutoff THEN
      RAISE EXCEPTION 'freeze order book does not use the immutable freeze cutoff';
    END IF;
  ELSIF NEW.execution_context_id IS NOT NULL THEN
    SELECT execution_cutoff, market_id
      INTO expected_cutoff, context_market_id
      FROM execution_contexts WHERE id = NEW.execution_context_id;
    IF expected_cutoff IS NULL OR context_market_id <> NEW.market_id
       OR NEW.cutoff <> expected_cutoff THEN
      RAISE EXCEPTION 'execution order book does not match its execution context';
    END IF;
  ELSE
    RAISE EXCEPTION 'order book snapshot requires a freeze or execution context';
  END IF;
  SELECT observed_at INTO artifact_observed_at FROM raw_artifacts WHERE id = NEW.raw_artifact_id;
  IF artifact_observed_at IS NULL OR artifact_observed_at > NEW.cutoff THEN
    RAISE EXCEPTION 'order book snapshot references evidence newer than its cutoff';
  END IF;
  IF NEW.source_timestamp IS NOT NULL AND NEW.source_timestamp > NEW.cutoff THEN
    RAISE EXCEPTION 'order book snapshot source evidence is newer than its cutoff';
  END IF;
  IF NEW.execution_context_id IS NOT NULL
     AND NEW.source_timestamp IS NOT NULL
     AND NEW.source_timestamp > NEW.observed_at THEN
    RAISE EXCEPTION 'execution order book source evidence postdates its observation';
  END IF;
  IF NEW.observed_at > NEW.cutoff THEN
    RAISE EXCEPTION 'order book snapshot observation is newer than its cutoff';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER order_book_snapshot_cutoff_guard
BEFORE INSERT ON order_book_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_order_book_snapshot();

CREATE TRIGGER execution_contexts_append_only
BEFORE UPDATE OR DELETE ON execution_contexts
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

CREATE TRIGGER execution_market_snapshots_append_only
BEFORE UPDATE OR DELETE ON execution_market_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

COMMENT ON TABLE order_operation_intents IS
  'The request timestamp and idempotency reservation are persisted before order-time '
  'context refresh; no venue transport fields are stored here. Pending '
  'reconciliation evidence uses the exact NOT_SUBMITTED submission state.';
COMMENT ON TABLE execution_contexts IS
  'Immutable order-time evidence linking one semantic operation to its fresh market, '
  'book, retained fee policy, and frozen decision cutoff.';
COMMENT ON TABLE execution_market_snapshots IS
  'Normalized market metadata captured with an order-time execution context.';
