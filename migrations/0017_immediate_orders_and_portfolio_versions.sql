-- Immediate execution can advance a portfolio during a harness cycle.  Keep
-- prior pages immutable while allowing a fresh no-cursor portfolio read.
ALTER TABLE portfolio_query_snapshots
  DROP CONSTRAINT portfolio_query_snapshots_agent_cycle_id_key;

ALTER TABLE portfolio_query_snapshots
  ADD CONSTRAINT portfolio_query_snapshots_cycle_version_key
    UNIQUE (agent_cycle_id, portfolio_version);

COMMENT ON TABLE portfolio_query_snapshots IS
  'Immutable get_portfolio snapshots, one per agent-cycle portfolio version.';
