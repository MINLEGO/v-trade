-- vtrade-kalshi-persistence-v1 / runtime audit, retention, and admin boundary

CREATE TABLE runtime_cycle_steps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
  UNIQUE (agent_cycle_id, stage),
  CHECK (status = 'running' OR completed_at IS NOT NULL)
);

CREATE TABLE cycle_contexts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  prompt_version_id uuid NOT NULL REFERENCES prompt_versions(id),
  rendered_cycle_prompt text NOT NULL,
  rendered_prompt_sha256 char(64) NOT NULL CHECK (rendered_prompt_sha256 ~ '^[0-9a-f]{64}$'),
  context jsonb NOT NULL,
  frozen_market_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  market_snapshot_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
  artifact_uri text,
  artifact_sha256 char(64) CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'),
  retain_until timestamptz NOT NULL,
  retention_purged_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (retain_until >= created_at + interval '6 months')
);

CREATE TABLE model_turns (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  turn_index integer NOT NULL CHECK (turn_index >= 0),
  request jsonb NOT NULL,
  response jsonb,
  provider_response_id text,
  termination_status text,
  started_at timestamptz NOT NULL,
  completed_at timestamptz,
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
  raw_artifact_uri text,
  raw_sha256 char(64) CHECK (raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'),
  retain_until timestamptz NOT NULL,
  retention_purged_at timestamptz,
  UNIQUE (agent_cycle_id, turn_index),
  CHECK (retain_until >= started_at + interval '6 months')
);

CREATE TABLE tool_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_turn_id uuid NOT NULL REFERENCES model_turns(id),
  call_index integer NOT NULL CHECK (call_index >= 0),
  provider_call_id text NOT NULL,
  category text NOT NULL,
  tool_name text NOT NULL,
  display_name text NOT NULL,
  arguments jsonb NOT NULL,
  output jsonb,
  success boolean,
  validation_status text NOT NULL,
  error text,
  called_at timestamptz NOT NULL,
  completed_at timestamptz,
  retain_until timestamptz NOT NULL,
  retention_purged_at timestamptz,
  UNIQUE (model_turn_id, call_index),
  UNIQUE (model_turn_id, provider_call_id),
  CHECK (retain_until >= called_at + interval '6 months')
);

CREATE TABLE provider_usage (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid REFERENCES agent_cycles(id),
  model_turn_id uuid REFERENCES model_turns(id),
  tool_call_id uuid REFERENCES tool_calls(id),
  provider text NOT NULL,
  route text,
  usage_kind text NOT NULL,
  prompt_tokens bigint CHECK (prompt_tokens >= 0),
  completion_tokens bigint CHECK (completion_tokens >= 0),
  reasoning_tokens bigint CHECK (reasoning_tokens >= 0),
  cached_tokens bigint CHECK (cached_tokens >= 0),
  request_count integer NOT NULL DEFAULT 1 CHECK (request_count >= 0),
  credit_count numeric(30, 6) NOT NULL DEFAULT 0 CHECK (credit_count >= 0),
  estimated_cost_micros bigint NOT NULL DEFAULT 0 CHECK (estimated_cost_micros >= 0),
  billed_cost_micros bigint NOT NULL DEFAULT 0 CHECK (billed_cost_micros >= 0),
  nominal_cost_micros bigint NOT NULL DEFAULT 0 CHECK (nominal_cost_micros >= 0),
  latency_ms bigint CHECK (latency_ms >= 0),
  cache_hit boolean NOT NULL DEFAULT false,
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
  raw_sha256 char(64) CHECK (raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'),
  raw_artifact_uri text,
  retain_until timestamptz,
  retention_purged_at timestamptz,
  created_at timestamptz NOT NULL,
  CHECK (retain_until IS NULL OR retain_until >= created_at + interval '6 months')
);

CREATE INDEX provider_usage_created_provider_idx ON provider_usage (created_at, provider);

CREATE TABLE research_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_url text NOT NULL,
  title text,
  source_published_at timestamptz,
  fetched_at timestamptz NOT NULL,
  content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  UNIQUE (canonical_url, content_sha256)
);

CREATE TABLE research_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tool_call_id uuid NOT NULL REFERENCES tool_calls(id),
  document_id uuid REFERENCES research_documents(id),
  provider text NOT NULL,
  query text,
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
  artifact_uri text,
  raw_sha256 char(64) CHECK (raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'),
  source_cutoff timestamptz,
  created_at timestamptz NOT NULL
);

CREATE TABLE harness_runs (
  id uuid PRIMARY KEY,
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  termination_status text NOT NULL,
  total_model_turns integer NOT NULL CHECK (total_model_turns >= 0),
  total_tool_calls integer NOT NULL CHECK (total_tool_calls >= 0),
  total_web_searches integer NOT NULL CHECK (total_web_searches >= 0),
  total_completion_tokens bigint NOT NULL CHECK (total_completion_tokens >= 0),
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
  transcript_artifact_uri text,
  transcript_sha256 char(64) NOT NULL CHECK (transcript_sha256 ~ '^[0-9a-f]{64}$'),
  idempotency_key text NOT NULL UNIQUE,
  retain_until timestamptz NOT NULL,
  retention_purged_at timestamptz,
  completed_at timestamptz NOT NULL,
  CHECK (retain_until >= completed_at + interval '6 months')
);

CREATE TABLE harness_tool_records (
  id uuid PRIMARY KEY,
  harness_run_id uuid NOT NULL REFERENCES harness_runs(id),
  call_index integer NOT NULL CHECK (call_index >= 0),
  provider_call_id text NOT NULL,
  tool_name text NOT NULL,
  category text NOT NULL,
  arguments jsonb,
  output jsonb NOT NULL,
  success boolean NOT NULL,
  retain_until timestamptz NOT NULL,
  retention_purged_at timestamptz,
  UNIQUE (harness_run_id, call_index),
  UNIQUE (harness_run_id, provider_call_id)
);

CREATE TABLE model_replay_records (
  id uuid PRIMARY KEY,
  model_turn_id uuid REFERENCES model_turns(id),
  model_slug text NOT NULL,
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
  response_artifact_uri text,
  response_sha256 char(64) NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
  provider_response_id text,
  retain_until timestamptz NOT NULL,
  retention_purged_at timestamptz,
  created_at timestamptz NOT NULL,
  UNIQUE (model_slug, response_sha256)
);

CREATE TABLE artifact_inventory (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  raw_artifact_id uuid REFERENCES raw_artifacts(id),
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
  CHECK (retain_until >= created_at + interval '6 months'),
  CHECK (status <> 'deleted' OR deleted_at IS NOT NULL)
);

CREATE INDEX artifact_inventory_retention_idx
  ON artifact_inventory (retain_until)
  WHERE status = 'active';

CREATE TABLE runtime_projections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  window_started_at timestamptz NOT NULL,
  window_ended_at timestamptz NOT NULL,
  projected_monthly_artifact_bytes bigint NOT NULL CHECK (projected_monthly_artifact_bytes >= 0),
  projected_monthly_billed_cost_micros bigint NOT NULL
    CHECK (projected_monthly_billed_cost_micros >= 0),
  projected_monthly_nominal_cost_micros bigint NOT NULL
    CHECK (projected_monthly_nominal_cost_micros >= 0),
  observed_cycles integer NOT NULL CHECK (observed_cycles >= 0),
  calculated_at timestamptz NOT NULL,
  CHECK (window_ended_at > window_started_at)
);

CREATE TABLE alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid REFERENCES experiment_runs(id),
  agent_id uuid REFERENCES agents(id),
  severity text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
  code text NOT NULL,
  details jsonb NOT NULL,
  opened_at timestamptz NOT NULL,
  acknowledged_at timestamptz,
  resolved_at timestamptz,
  dedupe_key text
);

CREATE UNIQUE INDEX alerts_open_dedupe_idx
  ON alerts (dedupe_key)
  WHERE resolved_at IS NULL AND dedupe_key IS NOT NULL;

CREATE TABLE system_controls (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton = true),
  globally_paused boolean NOT NULL DEFAULT false,
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  updated_at timestamptz NOT NULL,
  updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 128)
);

INSERT INTO system_controls (singleton, globally_paused, version, updated_at, updated_by)
VALUES (true, false, 1, now(), 'migration');

CREATE TABLE operator_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 256),
  action text NOT NULL CHECK (length(action) BETWEEN 1 AND 128),
  target_type text NOT NULL CHECK (length(target_type) BETWEEN 1 AND 128),
  target_id uuid,
  before_state jsonb,
  after_state jsonb,
  occurred_at timestamptz NOT NULL,
  idempotency_key text NOT NULL UNIQUE
);

CREATE TABLE monthly_provider_budgets (
  month_start date PRIMARY KEY,
  limit_micros bigint NOT NULL CHECK (limit_micros > 0),
  billed_cost_micros bigint NOT NULL DEFAULT 0 CHECK (billed_cost_micros >= 0),
  nominal_cost_micros bigint NOT NULL DEFAULT 0 CHECK (nominal_cost_micros >= 0),
  halted boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL
);

CREATE TABLE provider_budget_reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  month_start date NOT NULL REFERENCES monthly_provider_budgets(month_start),
  provider text NOT NULL,
  estimated_cost_micros bigint NOT NULL CHECK (estimated_cost_micros >= 0),
  billed_cost_micros bigint CHECK (billed_cost_micros >= 0),
  nominal_cost_micros bigint CHECK (nominal_cost_micros >= 0),
  status text NOT NULL CHECK (status IN ('reserved', 'reconciled')),
  reserved_at timestamptz NOT NULL,
  reconciled_at timestamptz,
  UNIQUE (month_start, provider, id)
);

CREATE TABLE exa_quota_reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  month_start date NOT NULL,
  request_key text NOT NULL UNIQUE,
  estimated_units integer NOT NULL CHECK (estimated_units >= 0),
  actual_units integer CHECK (actual_units >= 0),
  status text NOT NULL CHECK (status IN ('reserved', 'reconciled')),
  reserved_at timestamptz NOT NULL,
  reconciled_at timestamptz
);

CREATE TABLE monthly_exa_quotas (
  month_start date PRIMARY KEY,
  limit_units integer NOT NULL CHECK (limit_units > 0),
  reserved_units integer NOT NULL DEFAULT 0 CHECK (reserved_units >= 0),
  actual_units integer NOT NULL DEFAULT 0 CHECK (actual_units >= 0),
  halted boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL
);

CREATE TABLE performance_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  cash_micros bigint NOT NULL,
  position_liquidation_micros bigint NOT NULL CHECK (position_liquidation_micros >= 0),
  account_value_micros bigint NOT NULL,
  realized_pnl_micros bigint NOT NULL,
  unrealized_pnl_micros bigint NOT NULL,
  calculated_at timestamptz NOT NULL,
  calculation jsonb NOT NULL
);

CREATE VIEW vtrade_readiness AS
SELECT
  COALESCE((SELECT max(position) FROM schema_migrations), 0) AS latest_position,
  (SELECT version FROM schema_migrations ORDER BY position DESC LIMIT 1) AS latest_version,
  COALESCE((SELECT count(*) FROM schema_migrations), 0) = 4 AS migrations_complete,
  (SELECT globally_paused FROM system_controls WHERE singleton = true) AS globally_paused;

CREATE INDEX operator_actions_occurred_idx
  ON operator_actions (occurred_at DESC, id DESC);
CREATE INDEX agent_cycles_admin_status_idx
  ON agent_cycles (scheduled_at DESC, agent_id, status);
CREATE INDEX fills_admin_filled_idx ON fills (filled_at DESC, id DESC);
CREATE INDEX settlements_admin_settled_idx ON settlements (settled_at DESC, id DESC);
CREATE INDEX alerts_admin_open_idx ON alerts (opened_at DESC, id DESC)
  WHERE resolved_at IS NULL;

CREATE TRIGGER research_documents_append_only
BEFORE UPDATE OR DELETE ON research_documents
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER research_artifacts_append_only
BEFORE UPDATE OR DELETE ON research_artifacts
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER runtime_projections_append_only
BEFORE UPDATE OR DELETE ON runtime_projections
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER operator_actions_append_only
BEFORE UPDATE OR DELETE ON operator_actions
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

COMMENT ON VIEW vtrade_readiness IS
  'Provider-independent readiness projection. The migration runner verifies the full '
  'four-file prefix before the API or worker may use this view.';
COMMENT ON TABLE artifact_inventory IS
  'Leased object deletion state. raw_artifacts remains immutable after object bytes are '
  'cleaned, preserving the audit reference and digest.';
COMMENT ON TABLE operator_actions IS
  'Append-only authenticated operator evidence. Pause/resume state is a separate projection.';
