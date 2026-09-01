-- vtrade-kalshi-persistence-v1 / auditable fee-policy evidence
--
-- This migration is additive.  Migrations 0001 through 0009 are checksum
-- verified and must not be edited.  Existing fee snapshots remain immutable;
-- the nullable additions preserve their legacy shape while all new snapshots
-- use the exact rational and multi-artifact fields below.

ALTER TABLE series
  ADD COLUMN fee_type text,
  ADD COLUMN fee_multiplier_numerator bigint,
  ADD COLUMN fee_multiplier_denominator bigint,
  ADD CONSTRAINT series_fee_multiplier_shape CHECK (
    (fee_multiplier_numerator IS NULL AND fee_multiplier_denominator IS NULL)
    OR
    (fee_multiplier_numerator >= 0 AND fee_multiplier_denominator > 0)
  ),
  ADD CONSTRAINT series_fee_type_shape CHECK (
    fee_type IS NULL OR length(btrim(fee_type)) > 0
  ),
  ADD CONSTRAINT series_source_updated_causality CHECK (
    source_updated_at IS NULL OR source_updated_at <= observed_at
  );

ALTER TABLE markets
  ADD COLUMN fee_waiver_expiration_time timestamptz,
  ADD CONSTRAINT markets_source_updated_causality CHECK (
    source_updated_at IS NULL OR source_updated_at <= observed_at
  );

ALTER TABLE execution_market_snapshots
  ADD COLUMN fee_waiver_expiration_time timestamptz;

ALTER TABLE fee_policy_snapshots
  DROP CONSTRAINT IF EXISTS fee_policy_snapshots_multiplier_numerator_check,
  ADD CONSTRAINT fee_policy_snapshots_multiplier_numerator_check
    CHECK (multiplier_numerator >= 0);

ALTER TABLE fee_policy_snapshots
  ADD COLUMN fee_type text NOT NULL DEFAULT 'quadratic',
  ADD COLUMN series_multiplier_numerator bigint,
  ADD COLUMN series_multiplier_denominator bigint,
  ADD COLUMN event_override_numerator bigint,
  ADD COLUMN event_override_denominator bigint,
  ADD COLUMN event_override_fee_type text,
  ADD COLUMN rate_numerator bigint,
  ADD COLUMN rate_denominator bigint,
  ADD COLUMN scheduled_ts timestamptz,
  ADD COLUMN waiver boolean NOT NULL DEFAULT false,
  ADD COLUMN schedule_sha256 char(64),
  ADD COLUMN settlement_fee_micros bigint NOT NULL DEFAULT 0;

ALTER TABLE frozen_market_states
  ADD COLUMN fee_policy_status text,
  ADD COLUMN fee_policy_reason text;

ALTER TABLE fills
  ADD COLUMN trade_fee_micros bigint NOT NULL DEFAULT 0,
  ADD COLUMN rounding_fee_micros bigint NOT NULL DEFAULT 0,
  ADD COLUMN rebate_micros bigint NOT NULL DEFAULT 0,
  ADD COLUMN fee_policy_snapshot_id uuid REFERENCES fee_policy_snapshots(id);

CREATE TABLE fee_policy_snapshot_artifacts (
  fee_policy_snapshot_id uuid NOT NULL REFERENCES fee_policy_snapshots(id),
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  evidence_role text NOT NULL CHECK (evidence_role IN (
    'official_schedule', 'series_metadata', 'series_fee_change',
    'event_fee_change', 'market_waiver'
  )),
  PRIMARY KEY (fee_policy_snapshot_id, raw_artifact_id, evidence_role)
);

CREATE TABLE freeze_market_fee_policies (
  freeze_id uuid NOT NULL REFERENCES market_freezes(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  fee_policy_snapshot_id uuid REFERENCES fee_policy_snapshots(id),
  status text NOT NULL CHECK (status IN ('AVAILABLE', 'UNSUPPORTED', 'INVALID', 'UNAVAILABLE')),
  closed_reason text,
  PRIMARY KEY (freeze_id, market_id),
  CHECK (
    (status = 'AVAILABLE' AND fee_policy_snapshot_id IS NOT NULL AND closed_reason IS NULL)
    OR
    (status <> 'AVAILABLE' AND fee_policy_snapshot_id IS NULL AND closed_reason IS NOT NULL)
  ),
  CHECK (closed_reason IS NULL OR length(btrim(closed_reason)) > 0)
);

CREATE INDEX fee_policy_snapshots_causal_idx
  ON fee_policy_snapshots (market_id, cutoff, as_of_at, observed_at);

CREATE OR REPLACE FUNCTION vtrade_guard_fee_policy_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  artifact_observed_at timestamptz;
BEGIN
  IF NEW.fee_type IS NULL OR length(btrim(NEW.fee_type)) = 0
     OR NEW.fee_type <> 'quadratic'
     OR NEW.participant_role <> 'taker'
     OR NEW.series_multiplier_numerator IS NULL
     OR NEW.series_multiplier_denominator IS NULL
     OR NEW.series_multiplier_numerator < 0
     OR NEW.series_multiplier_denominator <= 0
     OR (NEW.event_override_numerator IS NULL) <> (NEW.event_override_denominator IS NULL)
     OR (NEW.event_override_denominator IS NOT NULL AND NEW.event_override_denominator <= 0)
     OR (NEW.event_override_numerator IS NOT NULL AND NEW.event_override_numerator < 0)
     OR (NEW.event_override_cleared AND (
       NEW.event_override_numerator IS NOT NULL
       OR NEW.event_override_denominator IS NOT NULL
       OR NEW.event_override_fee_type IS NOT NULL))
     OR (NOT NEW.event_override_cleared
       AND NEW.event_override_fee_type IS NOT NULL
       AND NEW.event_override_numerator IS NULL)
     OR (NEW.rate_numerator IS NULL) <> (NEW.rate_denominator IS NULL)
     OR NEW.rate_numerator IS NULL
     OR (NEW.rate_numerator IS NOT NULL AND NEW.rate_numerator <= 0)
     OR (NEW.rate_denominator IS NOT NULL AND NEW.rate_denominator <= 0)
     OR (NEW.event_override_fee_type IS NOT NULL
       AND NEW.event_override_fee_type <> 'quadratic')
     OR (NEW.waiver AND NEW.waiver_evidence IS NULL)
     OR (NEW.waiver_evidence IS NOT NULL AND (
       jsonb_typeof(NEW.waiver_evidence) <> 'object'
       OR NEW.waiver_evidence->>'expiration_time' IS NULL
       OR NEW.waiver_evidence->>'as_of' IS NULL
       OR NEW.waiver_evidence->>'waived' NOT IN ('true', 'false')
       OR (NEW.waiver_evidence->>'waived')::boolean <> NEW.waiver
       OR (NEW.waiver_evidence->>'as_of')::timestamptz <> NEW.as_of_at
       OR ((NEW.waiver_evidence->>'expiration_time')::timestamptz > NEW.as_of_at)
         <> NEW.waiver
     ))
     OR NEW.settlement_fee_micros < 0
     OR NEW.schedule_sha256 IS NULL
     OR (NEW.schedule_sha256 IS NOT NULL
       AND NEW.schedule_sha256 !~ '^[0-9a-f]{64}$')
     OR (NEW.scheduled_ts IS NOT NULL AND NEW.scheduled_ts > NEW.cutoff)
     OR NEW.effective_at > NEW.cutoff
     OR NEW.as_of_at > NEW.cutoff
     OR NEW.observed_at > NEW.cutoff THEN
    RAISE EXCEPTION 'fee policy snapshot has invalid exact or temporal evidence';
  END IF;
  SELECT observed_at INTO artifact_observed_at FROM raw_artifacts
   WHERE id = NEW.raw_artifact_id;
  IF artifact_observed_at IS NULL OR artifact_observed_at > NEW.cutoff THEN
    RAISE EXCEPTION 'fee policy snapshot references evidence newer than its cutoff';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER fee_policy_snapshot_integrity
BEFORE INSERT ON fee_policy_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_fee_policy_snapshot();

CREATE OR REPLACE FUNCTION vtrade_guard_fee_policy_artifact() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  snapshot_cutoff timestamptz;
  artifact_observed_at timestamptz;
BEGIN
  SELECT cutoff INTO snapshot_cutoff FROM fee_policy_snapshots
   WHERE id = NEW.fee_policy_snapshot_id;
  SELECT observed_at INTO artifact_observed_at FROM raw_artifacts
   WHERE id = NEW.raw_artifact_id;
  IF snapshot_cutoff IS NULL OR artifact_observed_at IS NULL
     OR artifact_observed_at > snapshot_cutoff THEN
    RAISE EXCEPTION 'fee policy evidence artifact is newer than its snapshot cutoff';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER fee_policy_artifact_integrity
BEFORE INSERT ON fee_policy_snapshot_artifacts
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_fee_policy_artifact();

CREATE OR REPLACE FUNCTION vtrade_guard_freeze_fee_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  freeze_cutoff timestamptz;
  snapshot_market_id uuid;
  snapshot_cutoff timestamptz;
BEGIN
  SELECT data_cutoff INTO freeze_cutoff FROM market_freezes WHERE id = NEW.freeze_id;
  IF freeze_cutoff IS NULL THEN
    RAISE EXCEPTION 'fee policy result references an unknown freeze';
  END IF;
  IF NEW.fee_policy_snapshot_id IS NOT NULL THEN
    SELECT market_id, cutoff INTO snapshot_market_id, snapshot_cutoff
      FROM fee_policy_snapshots WHERE id = NEW.fee_policy_snapshot_id;
    IF snapshot_market_id IS NULL OR snapshot_market_id <> NEW.market_id
       OR snapshot_cutoff <> freeze_cutoff OR NEW.status <> 'AVAILABLE' THEN
      RAISE EXCEPTION 'freeze fee policy result does not match its immutable snapshot';
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER freeze_fee_policy_integrity
BEFORE INSERT ON freeze_market_fee_policies
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_freeze_fee_policy();

CREATE OR REPLACE FUNCTION vtrade_guard_execution_fee_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  policy_market_id uuid;
  policy_cutoff timestamptz;
BEGIN
  IF NEW.fee_policy_snapshot_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT market_id, cutoff INTO policy_market_id, policy_cutoff
    FROM fee_policy_snapshots WHERE id = NEW.fee_policy_snapshot_id;
  IF policy_market_id IS NULL
     OR policy_market_id <> NEW.market_id
     OR policy_cutoff > NEW.execution_cutoff THEN
    RAISE EXCEPTION 'execution context fee policy does not match its order-time context';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER execution_context_fee_policy_integrity
BEFORE INSERT ON execution_contexts
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_execution_fee_policy();

CREATE OR REPLACE FUNCTION vtrade_guard_fill_fee_policy() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  operation_market_id uuid;
  policy_market_id uuid;
  execution_policy_id uuid;
BEGIN
  IF NEW.fee_policy_snapshot_id IS NULL THEN
    RAISE EXCEPTION 'financial fill requires an immutable fee policy snapshot';
  END IF;
  SELECT market_id INTO operation_market_id FROM order_operations
   WHERE id = NEW.operation_id;
  SELECT market_id INTO policy_market_id FROM fee_policy_snapshots
   WHERE id = NEW.fee_policy_snapshot_id;
  IF NEW.execution_context_id IS NOT NULL THEN
    SELECT fee_policy_snapshot_id INTO execution_policy_id FROM execution_contexts
     WHERE id = NEW.execution_context_id;
  END IF;
  IF operation_market_id IS NULL OR policy_market_id IS NULL
     OR operation_market_id <> policy_market_id
     OR (NEW.execution_context_id IS NOT NULL
       AND (execution_policy_id IS NULL OR execution_policy_id <> NEW.fee_policy_snapshot_id))
     OR NEW.rebate_micros > NEW.trade_fee_micros + NEW.rounding_fee_micros
     OR NEW.authoritative_fee_micros <> NEW.trade_fee_micros
       + NEW.rounding_fee_micros - NEW.rebate_micros THEN
    RAISE EXCEPTION 'fill fee accounting does not match its immutable policy';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER fill_fee_policy_integrity
BEFORE INSERT ON fills
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_fill_fee_policy();

CREATE TRIGGER fee_policy_snapshot_artifacts_append_only
BEFORE UPDATE OR DELETE ON fee_policy_snapshot_artifacts
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER freeze_market_fee_policies_append_only
BEFORE UPDATE OR DELETE ON freeze_market_fee_policies
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

CREATE OR REPLACE FUNCTION vtrade_assert_published_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  cycle_cutoff timestamptz;
  artifact_observed_at timestamptz;
BEGIN
  SELECT data_cutoff INTO cycle_cutoff FROM agent_cycles
   WHERE id = NEW.agent_cycle_id;
  IF cycle_cutoff IS NULL THEN
    UPDATE agent_cycles
       SET data_cutoff = NEW.data_cutoff
     WHERE id = NEW.agent_cycle_id AND data_cutoff IS NULL;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'published freeze % could not finalize its agent-cycle cutoff', NEW.id;
    END IF;
  ELSIF cycle_cutoff <> NEW.data_cutoff THEN
    RAISE EXCEPTION 'published freeze % must match its finalized agent-cycle cutoff', NEW.id;
  END IF;
  SELECT observed_at INTO artifact_observed_at FROM raw_artifacts
   WHERE id = NEW.catalogue_artifact_id;
  IF artifact_observed_at IS NULL OR artifact_observed_at > NEW.data_cutoff THEN
    RAISE EXCEPTION 'published freeze % references evidence newer than its cutoff', NEW.id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM market_freeze_memberships WHERE freeze_id = NEW.id
  ) THEN
    RAISE EXCEPTION 'published freeze % has no discovery or resolution membership', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM market_freeze_memberships membership
     WHERE membership.freeze_id = NEW.id
       AND NOT EXISTS (
         SELECT 1 FROM frozen_market_states state
          WHERE state.freeze_id = membership.freeze_id
            AND state.market_id = membership.market_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has membership without frozen market state', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM market_freeze_memberships membership
     WHERE membership.freeze_id = NEW.id AND membership.membership_type = 'discovery'
       AND NOT EXISTS (
         SELECT 1 FROM order_book_snapshots book
          WHERE book.freeze_id = membership.freeze_id
            AND book.market_id = membership.market_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has discovery market without a book read', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM market_freeze_memberships membership
     JOIN frozen_market_states state
       ON state.freeze_id = membership.freeze_id AND state.market_id = membership.market_id
     WHERE membership.freeze_id = NEW.id AND membership.membership_type = 'discovery'
       AND NOT EXISTS (
         SELECT 1 FROM freeze_market_fee_policies policy
          WHERE policy.freeze_id = membership.freeze_id
            AND policy.market_id = membership.market_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has discovery market without fee policy result', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM market_freeze_memberships membership
     JOIN frozen_market_states state
       ON state.freeze_id = membership.freeze_id AND state.market_id = membership.market_id
     LEFT JOIN freeze_market_fee_policies policy
       ON policy.freeze_id = membership.freeze_id AND policy.market_id = membership.market_id
     WHERE membership.freeze_id = NEW.id AND membership.membership_type = 'discovery'
       AND state.tradeable
       AND (policy.status IS DISTINCT FROM 'AVAILABLE'
         OR policy.fee_policy_snapshot_id IS NULL)
  ) THEN
    RAISE EXCEPTION 'published freeze % marks a market tradeable without an available fee policy', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM freeze_market_fee_policies policy
     WHERE policy.freeze_id = NEW.id AND policy.status = 'AVAILABLE'
       AND policy.fee_policy_snapshot_id IS NULL
  ) THEN
    RAISE EXCEPTION 'published freeze % has available fee policy without snapshot', NEW.id;
  END IF;
  RETURN NULL;
END
$$;

ALTER TABLE fills
  ADD CONSTRAINT fills_fee_components_nonnegative CHECK (
    trade_fee_micros >= 0 AND rounding_fee_micros >= 0 AND rebate_micros >= 0
  ),
  ADD CONSTRAINT fills_fee_policy_requires_execution_context CHECK (
    fee_policy_snapshot_id IS NULL OR execution_context_id IS NOT NULL
  );

COMMENT ON TABLE fee_policy_snapshot_artifacts IS
  'Normalized immutable source-role links for the schedule, metadata, changes, and waiver evidence.';
COMMENT ON TABLE freeze_market_fee_policies IS
  'One closed fee-policy outcome per discovery market; AVAILABLE always points to an exact snapshot.';
