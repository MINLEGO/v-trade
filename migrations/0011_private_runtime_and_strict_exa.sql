-- Final private-runtime hardening: add the pre-prompt settlement checkpoint, reserve
-- the maximum Exa credits before each request, and make V-Trade's public-schema
-- objects inaccessible to Supabase PostgREST roles. The database owner and the
-- Supabase service_role retain operational access; no browser-facing policy exists.

ALTER TABLE runtime_cycle_steps
  DROP CONSTRAINT IF EXISTS runtime_cycle_steps_stage_check;
ALTER TABLE runtime_cycle_steps
  ADD CONSTRAINT runtime_cycle_steps_stage_check CHECK (stage IN (
    'market_freeze', 'pre_settlement', 'prompt', 'harness', 'broker',
    'settlement_valuation'
  ));

ALTER TABLE artifact_inventory
  DROP CONSTRAINT IF EXISTS artifact_inventory_stage_check;
ALTER TABLE artifact_inventory
  ADD CONSTRAINT artifact_inventory_stage_check CHECK (stage IS NULL OR stage IN (
    'market_freeze', 'pre_settlement', 'prompt', 'harness', 'broker',
    'settlement_valuation'
  ));

ALTER TABLE exa_quota_reservations
  DROP CONSTRAINT IF EXISTS exa_quota_reservations_reserved_credit_count_check;
ALTER TABLE exa_quota_reservations
  ADD CONSTRAINT exa_quota_reservations_reserved_credit_count_check
  CHECK (reserved_credit_count BETWEEN 1 AND 10);

COMMENT ON COLUMN exa_quota_reservations.reserved_credit_count IS
  'New searches reserve 10 credits (the strict 10-result request maximum). Reconcile '
  'records actual provider credits and releases the unused pending capacity.';

DO $security$
DECLARE
  object_name text;
  vtrade_tables constant text[] := ARRAY[
    'schema_migrations', 'experiment_definitions', 'experiment_runs', 'model_configs',
    'agents', 'prompt_versions', 'cohort_cycles', 'agent_cycles', 'cycle_contexts',
    'model_turns', 'tool_calls', 'provider_usage', 'research_documents',
    'research_artifacts', 'events', 'markets', 'outcomes', 'market_snapshots',
    'order_book_snapshots', 'resolutions', 'order_intents', 'orders', 'fills',
    'positions', 'settlements', 'ledger_entries', 'ledger_postings',
    'performance_snapshots', 'beliefs', 'belief_revisions', 'plans', 'plan_revisions',
    'alerts', 'operator_actions', 'venue_sync_pages', 'monthly_provider_budgets',
    'provider_budget_reservations', 'harness_runs', 'harness_tool_records',
    'model_replay_records', 'critical_learning_snapshots', 'agent_runtime_schedules',
    'runtime_cycle_steps', 'artifact_inventory', 'runtime_projections',
    'system_controls', 'portfolio_query_snapshots', 'portfolio_snapshot_positions',
    'portfolio_page_cursors', 'fee_rate_snapshots', 'monthly_exa_quotas',
    'exa_quota_reservations'
  ];
BEGIN
  FOREACH object_name IN ARRAY vtrade_tables LOOP
    IF to_regclass(format('public.%I', object_name)) IS NULL THEN
      RAISE EXCEPTION 'required V-Trade table public.% is missing', object_name;
    END IF;
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', object_name);
    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM PUBLIC', object_name);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
      EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM anon', object_name);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON TABLE public.%I FROM authenticated', object_name
      );
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
      EXECUTE format('GRANT ALL PRIVILEGES ON TABLE public.%I TO service_role', object_name);
    END IF;
  END LOOP;

END
$security$;

-- V-Trade currently owns no sequences (all keys are UUIDs). Do not alter schema-level
-- or default privileges: this Supabase public schema may be shared with unrelated apps.

DO $functions$
DECLARE
  function_name text;
  vtrade_functions constant text[] := ARRAY[
    'public.reject_mutation()',
    'public.enforce_balanced_ledger_entry()',
    'public.enforce_ledger_entry_complete()',
    'public.enforce_retention_purge_only()',
    'public.reject_portfolio_snapshot_mutation()'
  ];
BEGIN
  FOREACH function_name IN ARRAY vtrade_functions LOOP
    IF to_regprocedure(function_name) IS NULL THEN
      RAISE EXCEPTION 'required V-Trade function % is missing', function_name;
    END IF;
    EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM PUBLIC', function_name);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
      EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM anon', function_name);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM authenticated', function_name
      );
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', function_name);
    END IF;
  END LOOP;
END
$functions$;
