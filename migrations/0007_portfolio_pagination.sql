-- Immutable, per-agent-cycle portfolio snapshots and opaque server-side cursors.
-- A snapshot is materialized on the first get_portfolio call. Subsequent pages
-- never query mutable live positions, preventing gaps or overlap while trading.

CREATE TABLE portfolio_query_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  agent_id uuid NOT NULL REFERENCES agents(id),
  data_cutoff timestamptz NOT NULL,
  portfolio_version bigint NOT NULL CHECK (portfolio_version >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, agent_id, agent_cycle_id)
);

CREATE TABLE portfolio_snapshot_positions (
  snapshot_id uuid NOT NULL REFERENCES portfolio_query_snapshots(id) ON DELETE CASCADE,
  position_id uuid NOT NULL,
  item jsonb NOT NULL,
  PRIMARY KEY (snapshot_id, position_id)
);

COMMENT ON TABLE portfolio_snapshot_positions IS
  'Immutable get_portfolio projection ordered by position_id; never refreshed from live positions.';

CREATE TABLE portfolio_page_cursors (
  cursor_hash char(64) PRIMARY KEY CHECK (cursor_hash ~ '^[0-9a-f]{64}$'),
  snapshot_id uuid NOT NULL,
  agent_id uuid NOT NULL,
  agent_cycle_id uuid NOT NULL,
  after_position_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (snapshot_id, agent_id, agent_cycle_id)
    REFERENCES portfolio_query_snapshots(id, agent_id, agent_cycle_id)
    ON DELETE CASCADE
);

CREATE INDEX portfolio_page_cursors_scope_idx
  ON portfolio_page_cursors (agent_id, agent_cycle_id, snapshot_id);

CREATE FUNCTION reject_portfolio_snapshot_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END $$;

CREATE TRIGGER portfolio_query_snapshots_immutable
BEFORE UPDATE ON portfolio_query_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_portfolio_snapshot_mutation();

CREATE TRIGGER portfolio_snapshot_positions_immutable
BEFORE UPDATE ON portfolio_snapshot_positions
FOR EACH ROW EXECUTE FUNCTION reject_portfolio_snapshot_mutation();
