CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  sha256 text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE experiment_status AS ENUM ('owner_pending', 'ready', 'running', 'paused', 'completed', 'failed');
CREATE TYPE cycle_status AS ENUM ('scheduled', 'running', 'completed', 'failed', 'skipped', 'interrupted');
CREATE TYPE order_side AS ENUM ('BUY', 'SELL');

CREATE TABLE experiment_definitions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_version text NOT NULL,
  version_number integer NOT NULL CHECK (version_number > 0),
  status experiment_status NOT NULL DEFAULT 'owner_pending',
  definition jsonb NOT NULL,
  config_sha256 char(64) NOT NULL,
  code_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  supersedes_id uuid REFERENCES experiment_definitions(id),
  UNIQUE (experiment_version, version_number),
  UNIQUE (config_sha256)
);
CREATE TABLE experiment_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  definition_id uuid NOT NULL REFERENCES experiment_definitions(id),
  run_label text NOT NULL,
  status experiment_status NOT NULL,
  starts_at timestamptz NOT NULL,
  ends_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (definition_id, run_label)
);
CREATE TABLE model_configs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  definition_id uuid NOT NULL REFERENCES experiment_definitions(id),
  label text NOT NULL,
  model_slug text,
  provider_policy jsonb NOT NULL,
  parameters jsonb NOT NULL,
  config_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (definition_id, label), UNIQUE (definition_id, config_sha256)
);
CREATE TABLE agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES experiment_runs(id),
  model_config_id uuid NOT NULL REFERENCES model_configs(id),
  name text NOT NULL,
  initial_cash_micros bigint NOT NULL CHECK (initial_cash_micros >= 0),
  paused_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, name), UNIQUE (run_id, model_config_id)
);
CREATE TABLE prompt_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  definition_id uuid NOT NULL REFERENCES experiment_definitions(id),
  name text NOT NULL,
  body text NOT NULL,
  body_sha256 char(64) NOT NULL,
  classification jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (definition_id, name), UNIQUE (body_sha256)
);

CREATE TABLE cohort_cycles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES experiment_runs(id),
  scheduled_at timestamptz NOT NULL,
  actual_started_at timestamptz,
  completed_at timestamptz,
  data_cutoff timestamptz NOT NULL,
  status cycle_status NOT NULL,
  failure_reason text,
  lease_owner text,
  idempotency_key text NOT NULL UNIQUE,
  UNIQUE (run_id, scheduled_at)
);
COMMENT ON TABLE cohort_cycles IS
  'Optional synchronization/comparison group; independent agent cycles need not belong to one.';
CREATE TABLE agent_cycles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cohort_cycle_id uuid REFERENCES cohort_cycles(id),
  agent_id uuid NOT NULL REFERENCES agents(id),
  scheduled_at timestamptz NOT NULL,
  data_cutoff timestamptz NOT NULL,
  status cycle_status NOT NULL,
  started_at timestamptz,
  completed_at timestamptz,
  model_termination_status text,
  final_summary text,
  failure_reason text,
  idempotency_key text NOT NULL UNIQUE,
  UNIQUE (agent_id, scheduled_at),
  UNIQUE (cohort_cycle_id, agent_id)
);
CREATE TABLE cycle_contexts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  prompt_version_id uuid NOT NULL REFERENCES prompt_versions(id),
  rendered_cycle_prompt text NOT NULL,
  rendered_prompt_sha256 char(64) NOT NULL,
  context jsonb NOT NULL,
  market_snapshot_ids uuid[] NOT NULL,
  artifact_uri text,
  artifact_sha256 char(64),
  retain_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
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
  raw_artifact_uri text,
  raw_sha256 char(64),
  retain_until timestamptz NOT NULL,
  UNIQUE (agent_cycle_id, turn_index)
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
  UNIQUE (model_turn_id, call_index), UNIQUE (model_turn_id, provider_call_id)
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
  credit_count numeric(30,6) NOT NULL DEFAULT 0 CHECK (credit_count >= 0),
  billed_cost_micros bigint NOT NULL DEFAULT 0 CHECK (billed_cost_micros >= 0),
  nominal_cost_micros bigint NOT NULL DEFAULT 0 CHECK (nominal_cost_micros >= 0),
  latency_ms bigint CHECK (latency_ms >= 0),
  cache_hit boolean NOT NULL DEFAULT false,
  raw_sha256 char(64),
  created_at timestamptz NOT NULL
);
CREATE INDEX provider_usage_created_provider_idx ON provider_usage (created_at, provider);
CREATE TABLE research_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_url text NOT NULL,
  title text,
  source_published_at timestamptz,
  fetched_at timestamptz NOT NULL,
  content_sha256 char(64) NOT NULL,
  UNIQUE (canonical_url, content_sha256)
);
CREATE TABLE research_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tool_call_id uuid NOT NULL REFERENCES tool_calls(id),
  document_id uuid REFERENCES research_documents(id),
  provider text NOT NULL,
  query text,
  artifact_uri text NOT NULL,
  raw_sha256 char(64) NOT NULL,
  source_cutoff timestamptz,
  created_at timestamptz NOT NULL
);

CREATE TABLE events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  venue text NOT NULL,
  venue_event_id text NOT NULL,
  slug text,
  title text NOT NULL,
  metadata jsonb NOT NULL,
  source_created_at timestamptz,
  observed_at timestamptz NOT NULL,
  UNIQUE (venue, venue_event_id)
);
CREATE TABLE markets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES events(id),
  venue text NOT NULL,
  venue_market_id text NOT NULL,
  condition_id text,
  slug text NOT NULL,
  question text NOT NULL,
  resolution_rules text NOT NULL,
  status text NOT NULL,
  category text,
  opens_at timestamptz,
  closes_at timestamptz,
  source_updated_at timestamptz,
  observed_at timestamptz NOT NULL,
  metadata jsonb NOT NULL,
  UNIQUE (venue, venue_market_id), UNIQUE (venue, slug)
);
CREATE TABLE outcomes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  venue_token_id text NOT NULL,
  name text NOT NULL,
  outcome_index integer,
  tick_size numeric(30,12) NOT NULL CHECK (tick_size > 0),
  minimum_order_size numeric(30,12) NOT NULL CHECK (minimum_order_size >= 0),
  UNIQUE (market_id, name), UNIQUE (venue_token_id)
);
CREATE TABLE market_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  cutoff timestamptz NOT NULL,
  status text NOT NULL,
  volume_micros bigint NOT NULL CHECK (volume_micros >= 0),
  liquidity_micros bigint NOT NULL CHECK (liquidity_micros >= 0),
  payload jsonb NOT NULL,
  raw_artifact_uri text NOT NULL,
  raw_sha256 char(64) NOT NULL,
  UNIQUE (market_id, cutoff, raw_sha256)
);
CREATE TABLE order_book_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  outcome_id uuid NOT NULL REFERENCES outcomes(id),
  cutoff timestamptz NOT NULL,
  source_created_at timestamptz,
  bids jsonb NOT NULL,
  asks jsonb NOT NULL,
  best_bid numeric(30,12),
  best_ask numeric(30,12),
  raw_artifact_uri text NOT NULL,
  raw_sha256 char(64) NOT NULL,
  UNIQUE (outcome_id, cutoff, raw_sha256)
);
CREATE TABLE resolutions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  winning_outcome_id uuid REFERENCES outcomes(id),
  result text NOT NULL,
  source_created_at timestamptz NOT NULL,
  observed_at timestamptz NOT NULL,
  eligible_after timestamptz NOT NULL,
  raw_artifact_uri text NOT NULL,
  raw_sha256 char(64) NOT NULL,
  UNIQUE (market_id, source_created_at, raw_sha256)
);

CREATE TABLE order_intents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  outcome_id uuid NOT NULL REFERENCES outcomes(id),
  side order_side NOT NULL,
  amount_micros bigint,
  shares numeric(30,12),
  strategy text NOT NULL,
  thesis text NOT NULL,
  estimated_probability numeric(10,9) NOT NULL CHECK (estimated_probability BETWEEN 0 AND 1),
  expected_value_micros bigint NOT NULL,
  validation_status text NOT NULL,
  rejection_code text,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL
);
CREATE TABLE orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  intent_id uuid NOT NULL REFERENCES order_intents(id),
  policy text NOT NULL,
  status text NOT NULL,
  requested_shares numeric(30,12) NOT NULL CHECK (requested_shares > 0),
  accepted_at timestamptz,
  rejected_at timestamptz,
  rejection_code text,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL
);
CREATE TABLE fills (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id uuid NOT NULL REFERENCES orders(id),
  fill_index integer NOT NULL CHECK (fill_index >= 0),
  shares numeric(30,12) NOT NULL CHECK (shares > 0),
  price numeric(30,12) NOT NULL CHECK (price BETWEEN 0 AND 1),
  gross_micros bigint NOT NULL CHECK (gross_micros >= 0),
  fee_micros bigint NOT NULL CHECK (fee_micros >= 0),
  snapshot_id uuid NOT NULL REFERENCES order_book_snapshots(id),
  idempotency_key text NOT NULL UNIQUE,
  filled_at timestamptz NOT NULL,
  UNIQUE (order_id, fill_index)
);
CREATE TABLE positions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  outcome_id uuid NOT NULL REFERENCES outcomes(id),
  shares numeric(30,12) NOT NULL CHECK (shares >= 0),
  average_cost numeric(30,12) NOT NULL CHECK (average_cost >= 0),
  cost_basis_micros bigint NOT NULL CHECK (cost_basis_micros >= 0),
  realized_pnl_micros bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL,
  UNIQUE (agent_id, outcome_id)
);
CREATE TABLE settlements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  position_id uuid NOT NULL REFERENCES positions(id),
  resolution_id uuid NOT NULL REFERENCES resolutions(id),
  shares numeric(30,12) NOT NULL CHECK (shares >= 0),
  payout_micros bigint NOT NULL CHECK (payout_micros >= 0),
  realized_pnl_micros bigint NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  settled_at timestamptz NOT NULL,
  UNIQUE (position_id, resolution_id)
);
CREATE TABLE ledger_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  event_type text NOT NULL,
  source_table text NOT NULL,
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
  account text NOT NULL,
  amount_micros bigint NOT NULL CHECK (amount_micros <> 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE performance_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  cash_micros bigint NOT NULL,
  position_liquidation_micros bigint NOT NULL,
  account_value_micros bigint NOT NULL,
  realized_pnl_micros bigint NOT NULL,
  unrealized_pnl_micros bigint NOT NULL,
  calculated_at timestamptz NOT NULL,
  calculation jsonb NOT NULL
);

CREATE TABLE beliefs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE belief_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  belief_id uuid NOT NULL REFERENCES beliefs(id),
  revision integer NOT NULL CHECK (revision > 0),
  probability numeric(10,9) CHECK (probability BETWEEN 0 AND 1),
  content text NOT NULL,
  category text NOT NULL,
  confidence numeric(10,9) CHECK (confidence BETWEEN 0 AND 1),
  evidence jsonb NOT NULL,
  created_by_cycle_id uuid REFERENCES agent_cycles(id),
  created_at timestamptz NOT NULL,
  UNIQUE (belief_id, revision)
);
CREATE TABLE plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id),
  plan_type text NOT NULL CHECK (plan_type IN ('long_term', 'next_cycle')),
  status text NOT NULL,
  due_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE plan_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id uuid NOT NULL REFERENCES plans(id),
  revision integer NOT NULL CHECK (revision > 0),
  content text NOT NULL,
  created_by_cycle_id uuid REFERENCES agent_cycles(id),
  created_at timestamptz NOT NULL,
  UNIQUE (plan_id, revision)
);
CREATE TABLE alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid REFERENCES experiment_runs(id),
  agent_id uuid REFERENCES agents(id),
  severity text NOT NULL,
  code text NOT NULL,
  details jsonb NOT NULL,
  opened_at timestamptz NOT NULL,
  acknowledged_at timestamptz,
  resolved_at timestamptz
);
CREATE TABLE operator_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id text NOT NULL,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id uuid,
  before_state jsonb,
  after_state jsonb,
  occurred_at timestamptz NOT NULL,
  idempotency_key text NOT NULL UNIQUE
);

CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; use a reversal or superseding version', TG_TABLE_NAME;
END $$;
CREATE TRIGGER experiment_definitions_append_only BEFORE UPDATE OR DELETE ON experiment_definitions
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER ledger_postings_append_only BEFORE UPDATE OR DELETE ON ledger_postings
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER order_intents_append_only BEFORE UPDATE OR DELETE ON order_intents
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER orders_append_only BEFORE UPDATE OR DELETE ON orders
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER fills_append_only BEFORE UPDATE OR DELETE ON fills
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER settlements_append_only BEFORE UPDATE OR DELETE ON settlements
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER resolutions_append_only BEFORE UPDATE OR DELETE ON resolutions
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER performance_snapshots_append_only BEFORE UPDATE OR DELETE ON performance_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER operator_actions_append_only BEFORE UPDATE OR DELETE ON operator_actions
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE FUNCTION enforce_balanced_ledger_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE total bigint;
BEGIN
  SELECT COALESCE(sum(amount_micros), 0) INTO total
  FROM ledger_postings WHERE ledger_entry_id = NEW.ledger_entry_id;
  IF total <> 0 THEN
    RAISE EXCEPTION 'ledger entry % is unbalanced by % micro-dollars', NEW.ledger_entry_id, total;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ledger_entry_balanced
AFTER INSERT ON ledger_postings DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_balanced_ledger_entry();

CREATE FUNCTION enforce_ledger_entry_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE posting_count integer;
DECLARE total bigint;
BEGIN
  SELECT count(*), COALESCE(sum(amount_micros), 0) INTO posting_count, total
  FROM ledger_postings WHERE ledger_entry_id = NEW.id;
  IF posting_count < 2 OR total <> 0 THEN
    RAISE EXCEPTION 'ledger entry % must have at least two balanced postings', NEW.id;
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER ledger_entry_complete
AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_ledger_entry_complete();
