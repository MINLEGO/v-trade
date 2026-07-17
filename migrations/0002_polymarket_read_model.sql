ALTER TABLE markets
  ADD COLUMN IF NOT EXISTS tradeable boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS resolution_source text;

ALTER TABLE outcomes
  ADD COLUMN IF NOT EXISTS indicative_price numeric(30, 18)
    CHECK (indicative_price BETWEEN 0 AND 1),
  ADD COLUMN IF NOT EXISTS tradeable boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE venue_sync_pages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  venue text NOT NULL,
  resource text NOT NULL CHECK (resource IN ('events', 'markets', 'resolutions')),
  requested_cursor text,
  next_cursor text,
  record_count integer NOT NULL CHECK (record_count >= 0),
  observed_at timestamptz NOT NULL,
  raw_artifact_uri text NOT NULL,
  raw_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (venue, resource, observed_at, raw_sha256)
);

CREATE INDEX venue_sync_pages_cutoff_idx
  ON venue_sync_pages (venue, resource, observed_at DESC);
CREATE INDEX markets_tradeable_status_idx
  ON markets (tradeable, status, closes_at);

COMMENT ON TABLE venue_sync_pages IS
  'Append-only keyset page observations. observed_at is the earliest eligible agent cutoff.';
COMMENT ON COLUMN outcomes.indicative_price IS
  'Gamma display price only; never an executable bid/ask.';

CREATE TRIGGER venue_sync_pages_append_only
BEFORE UPDATE OR DELETE ON venue_sync_pages
FOR EACH ROW EXECUTE FUNCTION reject_mutation();
