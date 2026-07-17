-- Phase 5: independent hourly scheduling, restart-safe stage checkpoints,
-- artifact retention inventory, operational projections, and alert deduplication.

CREATE TABLE agent_runtime_schedules (
  agent_id uuid PRIMARY KEY REFERENCES agents(id),
  interval_seconds integer NOT NULL DEFAULT 3600 CHECK (interval_seconds = 3600),
  next_scheduled_at timestamptz NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE agent_runtime_schedules IS
  'Each agent owns an independent UTC hourly cursor. Missed cursors are marked skipped '
  'and advanced beyond now; the worker never backfills them.';

ALTER TABLE agent_cycles
  ADD COLUMN lease_owner text,
  ADD COLUMN lease_expires_at timestamptz,
  ADD COLUMN attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0);

CREATE INDEX agent_cycles_recovery_idx
  ON agent_cycles (lease_expires_at, scheduled_at)
  WHERE status IN ('running', 'interrupted');

CREATE TABLE runtime_cycle_steps (
  id uuid PRIMARY KEY,
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  stage text NOT NULL CHECK (stage IN (
    'market_freeze', 'prompt', 'harness', 'broker', 'settlement_valuation'
  )),
  status text NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  input_fingerprint char(64) NOT NULL CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
  output jsonb,
  attempt_count integer NOT NULL CHECK (attempt_count > 0),
  started_at timestamptz NOT NULL,
  completed_at timestamptz,
  error text,
  UNIQUE (agent_cycle_id, stage)
);

CREATE TABLE artifact_inventory (
  id uuid PRIMARY KEY,
  agent_cycle_id uuid REFERENCES agent_cycles(id),
  stage text CHECK (stage IS NULL OR stage IN (
    'market_freeze', 'prompt', 'harness', 'broker', 'settlement_valuation'
  )),
  uri text NOT NULL UNIQUE,
  sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  byte_length bigint NOT NULL CHECK (byte_length >= 0),
  retain_until timestamptz NOT NULL,
  status text NOT NULL CHECK (status IN ('active', 'deleting', 'deleted')),
  lease_owner text,
  lease_expires_at timestamptz,
  deletion_attempts integer NOT NULL DEFAULT 0 CHECK (deletion_attempts >= 0),
  deletion_error text,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL,
  CHECK (retain_until >= created_at + interval '6 months')
);

CREATE INDEX artifact_inventory_retention_idx
  ON artifact_inventory (retain_until)
  WHERE status = 'active';

CREATE TABLE runtime_projections (
  id uuid PRIMARY KEY,
  window_started_at timestamptz NOT NULL,
  window_ended_at timestamptz NOT NULL,
  projected_monthly_artifact_bytes bigint NOT NULL
    CHECK (projected_monthly_artifact_bytes >= 0),
  projected_monthly_billed_cost_micros bigint NOT NULL
    CHECK (projected_monthly_billed_cost_micros >= 0),
  projected_monthly_nominal_cost_micros bigint NOT NULL
    CHECK (projected_monthly_nominal_cost_micros >= 0),
  observed_cycles integer NOT NULL CHECK (observed_cycles >= 0),
  calculated_at timestamptz NOT NULL,
  CHECK (window_ended_at > window_started_at)
);

ALTER TABLE alerts ADD COLUMN dedupe_key text;

ALTER TABLE cycle_contexts ADD COLUMN retention_purged_at timestamptz;
ALTER TABLE model_turns ADD COLUMN retention_purged_at timestamptz;
ALTER TABLE tool_calls ADD COLUMN retention_purged_at timestamptz;
ALTER TABLE provider_usage ADD COLUMN retention_purged_at timestamptz;
ALTER TABLE harness_runs ADD COLUMN retention_purged_at timestamptz;
ALTER TABLE model_replay_records ADD COLUMN retention_purged_at timestamptz;
ALTER TABLE harness_tool_records
  ADD COLUMN retain_until timestamptz,
  ADD COLUMN retention_purged_at timestamptz;

UPDATE harness_tool_records records
SET retain_until = runs.retain_until
FROM harness_runs runs
WHERE records.harness_run_id = runs.id;

ALTER TABLE harness_tool_records ALTER COLUMN retain_until SET NOT NULL;

CREATE UNIQUE INDEX alerts_open_dedupe_idx
  ON alerts (dedupe_key)
  WHERE resolved_at IS NULL;

CREATE TRIGGER runtime_projections_append_only
BEFORE UPDATE OR DELETE ON runtime_projections
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE FUNCTION enforce_retention_purge_only() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_payload jsonb := to_jsonb(OLD);
DECLARE new_payload jsonb := to_jsonb(NEW);
DECLARE field_name text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
  END IF;
  IF OLD.retain_until > now() OR NEW.retention_purged_at IS NULL
     OR NEW.retention_purged_at < OLD.retain_until THEN
    RAISE EXCEPTION '% may change only for an expired retention purge', TG_TABLE_NAME;
  END IF;
  FOREACH field_name IN ARRAY TG_ARGV LOOP
    old_payload := old_payload - field_name;
    new_payload := new_payload - field_name;
  END LOOP;
  IF old_payload <> new_payload THEN
    RAISE EXCEPTION '% retention purge changed immutable fields', TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER harness_runs_append_only ON harness_runs;
CREATE TRIGGER harness_runs_retention_guard
BEFORE UPDATE OR DELETE ON harness_runs
FOR EACH ROW EXECUTE FUNCTION enforce_retention_purge_only(
  'transcript_artifact_uri', 'retention_purged_at'
);

DROP TRIGGER harness_tool_records_append_only ON harness_tool_records;
CREATE TRIGGER harness_tool_records_retention_guard
BEFORE UPDATE OR DELETE ON harness_tool_records
FOR EACH ROW EXECUTE FUNCTION enforce_retention_purge_only(
  'arguments', 'output', 'retention_purged_at'
);

DROP TRIGGER model_replay_records_append_only ON model_replay_records;
CREATE TRIGGER model_replay_records_retention_guard
BEFORE UPDATE OR DELETE ON model_replay_records
FOR EACH ROW EXECUTE FUNCTION enforce_retention_purge_only(
  'response_artifact_uri', 'retention_purged_at'
);

COMMENT ON TABLE artifact_inventory IS
  'Private raw artifacts remain scheduled for at least six calendar months. Cleanup '
  'deletes object bytes only after a leased inventory claim; audit metadata remains.';
