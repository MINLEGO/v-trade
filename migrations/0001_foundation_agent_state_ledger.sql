-- vtrade-kalshi-persistence-v1 / foundation
--
-- This is the first block of a fresh-database release.  schema_migrations is
-- deliberately owned by vtrade.migrate: the runner creates it, verifies the
-- exact bytes, and is the only component allowed to advance it.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE vtrade_run_status AS ENUM (
  'ready', 'running', 'paused', 'completed', 'failed'
);

CREATE TYPE vtrade_cycle_status AS ENUM (
  'running', 'interrupted', 'completed', 'failed', 'skipped'
);

CREATE TABLE experiment_definitions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_version text NOT NULL,
  version_number integer NOT NULL CHECK (version_number > 0),
  status text NOT NULL CHECK (status IN ('ready', 'active', 'retired')),
  definition jsonb NOT NULL,
  config_sha256 char(64) NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
  code_version text NOT NULL CHECK (length(code_version) BETWEEN 1 AND 256),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (experiment_version, version_number),
  UNIQUE (experiment_version, config_sha256, code_version)
);

CREATE TABLE prompt_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  definition_id uuid NOT NULL REFERENCES experiment_definitions(id),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
  body text NOT NULL,
  body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
  classification jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (definition_id, name),
  UNIQUE (body_sha256)
);

CREATE TABLE model_configs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  definition_id uuid NOT NULL REFERENCES experiment_definitions(id),
  label text NOT NULL CHECK (length(label) BETWEEN 1 AND 256),
  model_slug text NOT NULL CHECK (length(model_slug) BETWEEN 1 AND 512),
  provider_policy jsonb NOT NULL,
  parameters jsonb NOT NULL,
  config_sha256 char(64) NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (definition_id, label),
  UNIQUE (definition_id, config_sha256)
);

CREATE TABLE experiment_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  definition_id uuid NOT NULL REFERENCES experiment_definitions(id),
  run_label text NOT NULL CHECK (length(run_label) BETWEEN 1 AND 256),
  status vtrade_run_status NOT NULL,
  starts_at timestamptz NOT NULL,
  ends_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  idempotency_key text NOT NULL UNIQUE,
  UNIQUE (definition_id, run_label),
  CHECK (ends_at IS NULL OR ends_at >= starts_at)
);

CREATE TABLE agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES experiment_runs(id),
  model_config_id uuid NOT NULL REFERENCES model_configs(id),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
  initial_cash_micros bigint NOT NULL CHECK (initial_cash_micros > 0),
  portfolio_version bigint NOT NULL DEFAULT 0 CHECK (portfolio_version >= 0),
  paused_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, name)
);

CREATE TABLE agent_runtime_schedules (
  agent_id uuid PRIMARY KEY REFERENCES agents(id),
  interval_seconds integer NOT NULL DEFAULT 3600 CHECK (interval_seconds = 3600),
  next_scheduled_at timestamptz NOT NULL,
  enabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_cycles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  scheduled_at timestamptz NOT NULL,
  data_cutoff timestamptz,
  status vtrade_cycle_status NOT NULL,
  started_at timestamptz,
  completed_at timestamptz,
  lease_owner text,
  lease_expires_at timestamptz,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  model_termination_status text,
  final_summary text,
  failure_reason text,
  idempotency_key text NOT NULL UNIQUE,
  UNIQUE (agent_id, scheduled_at),
  CHECK (data_cutoff IS NOT NULL OR status IN ('running', 'interrupted')),
  CHECK (data_cutoff IS NULL OR data_cutoff >= scheduled_at),
  CHECK (lease_expires_at IS NULL OR lease_owner IS NOT NULL),
  CHECK (completed_at IS NULL OR completed_at >= scheduled_at)
);

CREATE INDEX agent_cycles_recovery_idx
  ON agent_cycles (lease_expires_at, scheduled_at)
  WHERE status IN ('running', 'interrupted');

CREATE TABLE plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  plan_type text NOT NULL CHECK (plan_type IN ('long_term', 'next_cycle')),
  status text NOT NULL CHECK (status IN ('active', 'archived')),
  due_at timestamptz,
  idempotency_key text UNIQUE,
  memory_fingerprint char(64) CHECK (memory_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plan_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id uuid NOT NULL REFERENCES plans(id),
  revision integer NOT NULL CHECK (revision > 0),
  content text NOT NULL,
  created_by_cycle_id uuid REFERENCES agent_cycles(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (plan_id, revision)
);

CREATE TABLE beliefs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  active boolean NOT NULL DEFAULT true,
  idempotency_key text UNIQUE,
  memory_fingerprint char(64) CHECK (memory_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE belief_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  belief_id uuid NOT NULL REFERENCES beliefs(id),
  revision integer NOT NULL CHECK (revision > 0),
  probability numeric(12, 10) CHECK (probability BETWEEN 0 AND 1),
  confidence numeric(12, 10) CHECK (confidence BETWEEN 0 AND 1),
  content text NOT NULL,
  category text NOT NULL,
  evidence jsonb NOT NULL,
  created_by_cycle_id uuid REFERENCES agent_cycles(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (belief_id, revision)
);

CREATE TABLE raw_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  uri text NOT NULL,
  byte_length bigint NOT NULL CHECK (byte_length >= 0),
  source_endpoint text,
  request_identity text,
  source_timestamp timestamptz,
  effective_timestamp timestamptz,
  observed_at timestamptz NOT NULL,
  captured_cutoff timestamptz,
  schema_version text NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 256),
  audit_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sha256),
  UNIQUE (uri),
  CHECK (captured_cutoff IS NULL OR observed_at <= captured_cutoff),
  CHECK (effective_timestamp IS NULL OR source_timestamp IS NULL
    OR effective_timestamp >= source_timestamp)
);

CREATE INDEX raw_artifacts_observed_idx ON raw_artifacts (observed_at, sha256);

CREATE TABLE ledger_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  event_type text NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
  source_table text NOT NULL CHECK (length(source_table) BETWEEN 1 AND 128),
  source_id uuid NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  reversal_of uuid REFERENCES ledger_entries(id),
  occurred_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_table, source_id, event_type)
);

CREATE TABLE ledger_postings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ledger_entry_id uuid NOT NULL REFERENCES ledger_entries(id),
  account text NOT NULL CHECK (account IN (
    'cash', 'owner_equity', 'position_cost', 'fee_expense',
    'settlement_payout', 'realized_pnl'
  )),
  amount_micros bigint NOT NULL CHECK (amount_micros <> 0),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ledger_postings_entry_idx ON ledger_postings (ledger_entry_id, id);

CREATE OR REPLACE FUNCTION vtrade_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; use a new revision or compensating event', TG_TABLE_NAME;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_guard_cycle_cutoff() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.data_cutoff IS NOT NULL AND NEW.data_cutoff IS DISTINCT FROM OLD.data_cutoff THEN
    RAISE EXCEPTION 'agent cycle data_cutoff is immutable after freeze completion';
  END IF;
  IF NEW.data_cutoff IS NOT NULL AND NEW.data_cutoff < NEW.scheduled_at THEN
    RAISE EXCEPTION 'agent cycle data_cutoff precedes scheduled_at';
  END IF;
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_assert_balanced_ledger() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  entry_id uuid;
  posting_count integer;
  balance bigint;
BEGIN
  IF TG_TABLE_NAME = 'ledger_entries' THEN
    entry_id := NEW.id;
  ELSE
    entry_id := NEW.ledger_entry_id;
  END IF;
  SELECT count(*), COALESCE(sum(amount_micros), 0)
    INTO posting_count, balance
    FROM ledger_postings
   WHERE ledger_entry_id = entry_id;
  IF posting_count < 2 OR balance <> 0 THEN
    RAISE EXCEPTION
      'ledger entry % must have at least two balanced postings (count %, balance %)',
      entry_id, posting_count, balance;
  END IF;
  RETURN NULL;
END
$$;

CREATE TRIGGER experiment_definitions_append_only
BEFORE UPDATE OR DELETE ON experiment_definitions
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER prompt_versions_append_only
BEFORE UPDATE OR DELETE ON prompt_versions
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER model_configs_append_only
BEFORE UPDATE OR DELETE ON model_configs
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER plan_revisions_append_only
BEFORE UPDATE OR DELETE ON plan_revisions
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER belief_revisions_append_only
BEFORE UPDATE OR DELETE ON belief_revisions
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER raw_artifacts_append_only
BEFORE UPDATE OR DELETE ON raw_artifacts
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER ledger_entries_append_only
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER ledger_postings_append_only
BEFORE UPDATE OR DELETE ON ledger_postings
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

CREATE CONSTRAINT TRIGGER ledger_entries_balanced
AFTER INSERT ON ledger_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_balanced_ledger();

CREATE CONSTRAINT TRIGGER ledger_postings_balanced
AFTER INSERT ON ledger_postings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_balanced_ledger();

CREATE TRIGGER agent_cycles_cutoff_guard
BEFORE UPDATE ON agent_cycles
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_cycle_cutoff();

COMMENT ON TABLE agent_cycles IS
  'A cycle has no causal cutoff until its complete market freeze has committed. '
  'Once set, data_cutoff cannot be changed.';
COMMENT ON TABLE raw_artifacts IS
  'Content-addressed, credential-free source evidence. Source records reference this table '
  'instead of storing mutable provider payloads as domain state.';
COMMENT ON TABLE ledger_entries IS
  'Append-only financial events. The deferred constraint requires a balanced double-entry '
  'record with at least two postings before transaction commit.';
