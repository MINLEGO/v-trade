-- Phase 19: per-agent virtual order-book consumption for liquidity-aware paper fills.
-- The execution context is versioned by (agent_cycle_id, snapshot_id).  A new
-- immutable order-book snapshot starts a fresh private context; no market-wide
-- depth or historical snapshot is ever updated.

CREATE TABLE virtual_liquidity_levels (
  agent_id uuid NOT NULL REFERENCES agents(id),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  snapshot_id uuid NOT NULL REFERENCES order_book_snapshots(id),
  token_id text NOT NULL,
  side order_side NOT NULL,
  level_index integer NOT NULL CHECK (level_index >= 0),
  price numeric(30, 12) NOT NULL CHECK (price BETWEEN 0 AND 1),
  displayed_shares numeric(30, 12) NOT NULL CHECK (displayed_shares > 0),
  consumed_shares numeric(30, 12) NOT NULL DEFAULT 0 CHECK (consumed_shares >= 0),
  cancelled_shares numeric(30, 12) NOT NULL DEFAULT 0 CHECK (cancelled_shares >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, agent_cycle_id, snapshot_id, token_id, side, level_index, price),
  CHECK (consumed_shares <= displayed_shares)
);

COMMENT ON TABLE virtual_liquidity_levels IS
  'Agent-private virtual depth. displayed_shares comes from an immutable order-book '
  'snapshot; consumed_shares is never shared with another agent or written back to it.';

CREATE INDEX virtual_liquidity_levels_agent_context_idx
  ON virtual_liquidity_levels (agent_id, agent_cycle_id, snapshot_id, token_id, side);

CREATE TABLE virtual_liquidity_executions (
  id uuid PRIMARY KEY,
  order_id uuid NOT NULL UNIQUE REFERENCES orders(id) DEFERRABLE INITIALLY DEFERRED,
  agent_id uuid NOT NULL REFERENCES agents(id),
  agent_cycle_id uuid NOT NULL REFERENCES agent_cycles(id),
  snapshot_id uuid NOT NULL REFERENCES order_book_snapshots(id),
  token_id text NOT NULL,
  side order_side NOT NULL,
  context_version text NOT NULL,
  requested_shares numeric(30, 12) NOT NULL CHECK (requested_shares > 0),
  available_shares numeric(30, 12) NOT NULL CHECK (available_shares >= 0),
  consumed_shares numeric(30, 12) NOT NULL CHECK (consumed_shares >= 0),
  cancelled_shares numeric(30, 12) NOT NULL CHECK (cancelled_shares >= 0),
  remaining_shares numeric(30, 12) NOT NULL CHECK (remaining_shares >= 0),
  portfolio_before jsonb NOT NULL,
  execution_at timestamptz NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (consumed_shares <= available_shares),
  CHECK (cancelled_shares = requested_shares - consumed_shares),
  CHECK (remaining_shares = available_shares - consumed_shares)
);

COMMENT ON TABLE virtual_liquidity_executions IS
  'One immutable audit record per order. The deferred order foreign key permits the '
  'record to be inserted in the same transaction immediately before the order row.';

CREATE INDEX virtual_liquidity_executions_agent_context_idx
  ON virtual_liquidity_executions (agent_id, agent_cycle_id, snapshot_id, token_id, side);

CREATE TABLE virtual_liquidity_execution_levels (
  execution_id uuid NOT NULL REFERENCES virtual_liquidity_executions(id) ON DELETE CASCADE,
  level_index integer NOT NULL CHECK (level_index >= 0),
  price numeric(30, 12) NOT NULL CHECK (price BETWEEN 0 AND 1),
  displayed_shares numeric(30, 12) NOT NULL CHECK (displayed_shares > 0),
  available_shares numeric(30, 12) NOT NULL CHECK (available_shares >= 0),
  consumed_shares numeric(30, 12) NOT NULL CHECK (consumed_shares >= 0),
  cancelled_shares numeric(30, 12) NOT NULL DEFAULT 0 CHECK (cancelled_shares >= 0),
  remaining_shares numeric(30, 12) NOT NULL CHECK (remaining_shares >= 0),
  PRIMARY KEY (execution_id, level_index),
  CHECK (available_shares <= displayed_shares),
  CHECK (consumed_shares <= available_shares),
  CHECK (remaining_shares = available_shares - consumed_shares)
);

COMMENT ON TABLE virtual_liquidity_execution_levels IS
  'Per-level before/consumed/remaining metrics used to make each private decrement auditable.';
