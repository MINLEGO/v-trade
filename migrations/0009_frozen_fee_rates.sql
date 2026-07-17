-- Official public CLOB GET /fee-rate/{token_id} observations archived during market freeze.

CREATE TABLE fee_rate_snapshots (
  id uuid PRIMARY KEY,
  outcome_id uuid NOT NULL REFERENCES outcomes(id),
  token_id text NOT NULL,
  base_fee_bps integer NOT NULL CHECK (base_fee_bps BETWEEN 0 AND 10000),
  observed_at timestamptz NOT NULL,
  source_created_at timestamptz,
  raw_artifact_uri text NOT NULL,
  raw_sha256 char(64) NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (token_id, observed_at, raw_sha256),
  CHECK (source_created_at IS NULL OR source_created_at <= observed_at)
);

CREATE INDEX fee_rate_snapshots_token_cutoff_idx
  ON fee_rate_snapshots (token_id, observed_at DESC);

CREATE TRIGGER fee_rate_snapshots_append_only
BEFORE UPDATE OR DELETE ON fee_rate_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

COMMENT ON TABLE fee_rate_snapshots IS
  'Archive-first official CLOB fee-rate observations. Broker fee policy must be derived '
  'only from a row frozen at or before the immutable agent-cycle cutoff.';
