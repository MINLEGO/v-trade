-- Phase 4: bounded model/research harness, transactional budget reservations,
-- replay records, and private agent memory idempotency.

ALTER TABLE provider_usage
  ADD COLUMN estimated_cost_micros bigint NOT NULL DEFAULT 0
    CHECK (estimated_cost_micros >= 0),
  ADD COLUMN raw_artifact_uri text,
  ADD COLUMN retain_until timestamptz;

ALTER TABLE beliefs
  ADD COLUMN idempotency_key text UNIQUE,
  ADD COLUMN memory_fingerprint char(64)
    CHECK (memory_fingerprint ~ '^[0-9a-f]{64}$');

ALTER TABLE plans
  ADD COLUMN idempotency_key text UNIQUE,
  ADD COLUMN memory_fingerprint char(64)
    CHECK (memory_fingerprint ~ '^[0-9a-f]{64}$');

CREATE TABLE monthly_provider_budgets (
  month_start date PRIMARY KEY,
  limit_micros bigint NOT NULL CHECK (limit_micros > 0),
  billed_cost_micros bigint NOT NULL DEFAULT 0 CHECK (billed_cost_micros >= 0),
  nominal_cost_micros bigint NOT NULL DEFAULT 0 CHECK (nominal_cost_micros >= 0),
  halted boolean NOT NULL DEFAULT false,
  alerted_20 boolean NOT NULL DEFAULT false,
  alerted_32 boolean NOT NULL DEFAULT false,
  alerted_40 boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL
);

CREATE TABLE provider_budget_reservations (
  id uuid PRIMARY KEY,
  month_start date NOT NULL REFERENCES monthly_provider_budgets(month_start),
  provider text NOT NULL,
  estimated_cost_micros bigint NOT NULL CHECK (estimated_cost_micros >= 0),
  billed_cost_micros bigint CHECK (billed_cost_micros >= 0),
  nominal_cost_micros bigint CHECK (nominal_cost_micros >= 0),
  status text NOT NULL CHECK (status IN ('reserved', 'reconciled')),
  reserved_at timestamptz NOT NULL,
  reconciled_at timestamptz
);

CREATE INDEX provider_budget_active_idx
  ON provider_budget_reservations (month_start, status);

CREATE TABLE harness_runs (
  id uuid PRIMARY KEY,
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  termination_status text NOT NULL,
  total_model_turns integer NOT NULL CHECK (total_model_turns >= 0),
  total_tool_calls integer NOT NULL CHECK (total_tool_calls >= 0),
  total_web_searches integer NOT NULL CHECK (total_web_searches >= 0),
  total_completion_tokens bigint NOT NULL CHECK (total_completion_tokens >= 0),
  transcript_artifact_uri text NOT NULL,
  transcript_sha256 char(64) NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  retain_until timestamptz NOT NULL,
  completed_at timestamptz NOT NULL
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
  UNIQUE (harness_run_id, call_index),
  UNIQUE (harness_run_id, provider_call_id)
);

CREATE TABLE model_replay_records (
  id uuid PRIMARY KEY,
  model_turn_id uuid REFERENCES model_turns(id),
  model_slug text NOT NULL,
  response_artifact_uri text NOT NULL,
  response_sha256 char(64) NOT NULL,
  provider_response_id text,
  retain_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL,
  UNIQUE (model_slug, response_sha256)
);

CREATE TABLE critical_learning_snapshots (
  id uuid PRIMARY KEY,
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  agent_id uuid NOT NULL REFERENCES agents(id),
  summary text NOT NULL,
  input_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL
);

CREATE TRIGGER provider_budget_reservations_append_only
BEFORE DELETE ON provider_budget_reservations
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE TRIGGER harness_runs_append_only
BEFORE UPDATE OR DELETE ON harness_runs
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE TRIGGER harness_tool_records_append_only
BEFORE UPDATE OR DELETE ON harness_tool_records
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE TRIGGER model_replay_records_append_only
BEFORE UPDATE OR DELETE ON model_replay_records
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE TRIGGER critical_learning_snapshots_append_only
BEFORE UPDATE OR DELETE ON critical_learning_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

COMMENT ON TABLE monthly_provider_budgets IS
  'The $40 breaker reserves an explicit worst-case estimate before every request. '
  'Actual billed and nominal costs are reconciled after provider response.';
