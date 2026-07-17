-- Phase 6: private operator controls and query indexes. The API exposes only
-- fixed read views and four audited pause/resume mutations.

CREATE TABLE system_controls (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton = true),
  globally_paused boolean NOT NULL DEFAULT false,
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  updated_at timestamptz NOT NULL,
  updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 128)
);

INSERT INTO system_controls
  (singleton, globally_paused, version, updated_at, updated_by)
VALUES (true, false, 1, now(), 'migration')
ON CONFLICT (singleton) DO NOTHING;

CREATE TRIGGER system_controls_no_delete
BEFORE DELETE ON system_controls
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE INDEX operator_actions_occurred_idx
  ON operator_actions (occurred_at DESC, id DESC);
CREATE INDEX agent_cycles_admin_status_idx
  ON agent_cycles (scheduled_at DESC, agent_id, status);
CREATE INDEX fills_admin_filled_idx
  ON fills (filled_at DESC, id DESC);
CREATE INDEX settlements_admin_settled_idx
  ON settlements (settled_at DESC, id DESC);
CREATE INDEX alerts_admin_open_idx
  ON alerts (opened_at DESC, id DESC)
  WHERE resolved_at IS NULL;

COMMENT ON TABLE system_controls IS
  'Singleton global scheduler pause state. Every API mutation is recorded in operator_actions.';
