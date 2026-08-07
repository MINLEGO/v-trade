-- Phase 20: live paper-order contexts.  The cycle cutoff remains the agent's
-- decision boundary; these rows record the fresh market data used at execution.

ALTER TABLE order_intents ADD COLUMN requested_at timestamptz;
ALTER TABLE order_intents DISABLE TRIGGER order_intents_append_only;
UPDATE order_intents SET requested_at = created_at WHERE requested_at IS NULL;
ALTER TABLE order_intents ENABLE TRIGGER order_intents_append_only;
ALTER TABLE order_intents ALTER COLUMN requested_at SET NOT NULL;

ALTER TABLE orders ADD COLUMN executed_at timestamptz;
ALTER TABLE orders DISABLE TRIGGER orders_append_only;
UPDATE orders
SET executed_at = COALESCE(accepted_at, rejected_at, created_at)
WHERE executed_at IS NULL;
ALTER TABLE orders ENABLE TRIGGER orders_append_only;
ALTER TABLE orders ALTER COLUMN executed_at SET NOT NULL;

CREATE TABLE order_execution_attempts (
  id uuid PRIMARY KEY,
  intent_id uuid NOT NULL REFERENCES order_intents(id),
  attempt integer NOT NULL CHECK (attempt >= 1),
  requested_at timestamptz NOT NULL,
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL,
  status text NOT NULL CHECK (status IN ('failed', 'validated')),
  error_code text,
  UNIQUE (intent_id, attempt),
  CHECK (completed_at >= started_at)
);

COMMENT ON TABLE order_execution_attempts IS
  'Append-only live refresh attempts. A retry keeps the same order intent and idempotency key.';

CREATE INDEX order_execution_attempts_intent_idx
  ON order_execution_attempts (intent_id, attempt);

CREATE TABLE live_order_contexts (
  id uuid PRIMARY KEY,
  intent_id uuid NOT NULL UNIQUE REFERENCES order_intents(id),
  market_snapshot_id uuid NOT NULL REFERENCES market_snapshots(id),
  order_book_snapshot_id uuid NOT NULL REFERENCES order_book_snapshots(id),
  fee_rate_snapshot_id uuid NOT NULL REFERENCES fee_rate_snapshots(id),
  requested_at timestamptz NOT NULL,
  validated_at timestamptz NOT NULL,
  executed_at timestamptz NOT NULL,
  market_observed_at timestamptz NOT NULL,
  order_book_observed_at timestamptz NOT NULL,
  fee_observed_at timestamptz NOT NULL,
  artifact_hashes jsonb NOT NULL,
  CHECK (validated_at >= requested_at),
  CHECK (executed_at >= validated_at)
);

COMMENT ON TABLE live_order_contexts IS
  'Immutable live market, order-book, and fee context used for one paper execution.';

CREATE INDEX live_order_contexts_book_idx
  ON live_order_contexts (order_book_snapshot_id, validated_at);

CREATE TRIGGER order_execution_attempts_append_only
BEFORE UPDATE OR DELETE ON order_execution_attempts
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE TRIGGER live_order_contexts_append_only
BEFORE UPDATE OR DELETE ON live_order_contexts
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
