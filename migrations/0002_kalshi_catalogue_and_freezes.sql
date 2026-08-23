-- vtrade-kalshi-persistence-v1 / catalogue, books, and immutable freezes

CREATE TABLE series (
  id uuid PRIMARY KEY,
  venue text NOT NULL CHECK (venue = 'kalshi'),
  kind text NOT NULL CHECK (kind = 'series'),
  series_ref text NOT NULL CHECK (length(series_ref) BETWEEN 1 AND 512),
  title text NOT NULL,
  rules text,
  observed_at timestamptz NOT NULL,
  source_updated_at timestamptz,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (venue, kind, series_ref)
);

CREATE TABLE events (
  id uuid PRIMARY KEY,
  venue text NOT NULL CHECK (venue = 'kalshi'),
  kind text NOT NULL CHECK (kind = 'event'),
  event_ref text NOT NULL CHECK (length(event_ref) BETWEEN 1 AND 512),
  series_id uuid NOT NULL REFERENCES series(id),
  title text NOT NULL,
  category text,
  observed_at timestamptz NOT NULL,
  source_updated_at timestamptz,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (venue, kind, event_ref)
);

CREATE TABLE markets (
  id uuid PRIMARY KEY,
  venue text NOT NULL CHECK (venue = 'kalshi'),
  kind text NOT NULL CHECK (kind = 'binary'),
  market_ref text NOT NULL CHECK (length(market_ref) BETWEEN 1 AND 512),
  series_id uuid NOT NULL REFERENCES series(id),
  event_id uuid NOT NULL REFERENCES events(id),
  question text NOT NULL,
  resolution_rules text NOT NULL,
  resolution_source text,
  open_time timestamptz NOT NULL,
  close_time timestamptz,
  expected_expiration_time timestamptz,
  latest_expiration_time timestamptz,
  lifecycle_status text NOT NULL CHECK (lifecycle_status IN (
    'initialized', 'active', 'inactive', 'open', 'closed', 'determined',
    'disputed', 'amended', 'finalized', 'resolved', 'ambiguous'
  )),
  eligible boolean NOT NULL,
  tradeable boolean NOT NULL,
  volume_units bigint NOT NULL DEFAULT 0 CHECK (volume_units >= 0),
  liquidity_micros bigint NOT NULL DEFAULT 0 CHECK (liquidity_micros >= 0),
  observed_at timestamptz NOT NULL,
  source_updated_at timestamptz,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (venue, kind, market_ref),
  CHECK (close_time IS NULL OR close_time >= open_time),
  CHECK (expected_expiration_time IS NULL OR expected_expiration_time >= open_time),
  CHECK (latest_expiration_time IS NULL OR latest_expiration_time >= open_time)
);

CREATE TABLE outcomes (
  id uuid PRIMARY KEY,
  market_id uuid NOT NULL REFERENCES markets(id),
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  label text NOT NULL,
  eligible boolean NOT NULL,
  UNIQUE (market_id, outcome_side)
);

CREATE TABLE market_price_grid_ranges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  start_price_micros bigint NOT NULL CHECK (start_price_micros >= 0),
  end_price_micros bigint NOT NULL CHECK (end_price_micros <= 1000000),
  step_micros bigint NOT NULL CHECK (step_micros > 0),
  UNIQUE (market_id, ordinal),
  CHECK (start_price_micros < end_price_micros),
  CHECK ((end_price_micros - start_price_micros) % step_micros = 0)
);

CREATE TABLE catalogue_page_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  resource text NOT NULL CHECK (resource IN (
    'series', 'events', 'markets', 'historical_cutoff', 'orderbook'
  )),
  requested_cursor text,
  next_cursor text,
  record_count integer NOT NULL CHECK (record_count >= 0),
  observed_at timestamptz NOT NULL,
  source_timestamp timestamptz,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (resource, requested_cursor, observed_at, raw_artifact_id),
  CHECK (next_cursor IS NULL OR length(next_cursor) > 0),
  CHECK (observed_at <= cutoff),
  CHECK (source_timestamp IS NULL OR source_timestamp <= cutoff)
);

CREATE TABLE catalogue_market_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  market_id uuid NOT NULL REFERENCES markets(id),
  lifecycle_status text NOT NULL CHECK (lifecycle_status IN (
    'initialized', 'active', 'inactive', 'open', 'closed', 'determined',
    'disputed', 'amended', 'finalized', 'resolved', 'ambiguous'
  )),
  eligible boolean NOT NULL,
  tradeable boolean NOT NULL,
  observed_at timestamptz NOT NULL,
  source_updated_at timestamptz,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (market_id, observed_at, raw_artifact_id),
  CHECK (observed_at <= cutoff),
  CHECK (source_updated_at IS NULL OR source_updated_at <= cutoff)
);

CREATE TABLE market_freezes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_cycle_id uuid NOT NULL UNIQUE REFERENCES agent_cycles(id),
  data_cutoff timestamptz NOT NULL,
  historical_cutoff timestamptz NOT NULL,
  catalogue_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  publication_status text NOT NULL CHECK (publication_status = 'published'),
  complete boolean NOT NULL CHECK (complete),
  published_at timestamptz NOT NULL,
  UNIQUE (agent_cycle_id, data_cutoff)
);

CREATE TABLE market_freeze_memberships (
  freeze_id uuid NOT NULL REFERENCES market_freezes(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  membership_type text NOT NULL CHECK (membership_type IN ('discovery', 'resolution')),
  inclusion_reason text NOT NULL,
  PRIMARY KEY (freeze_id, market_id, membership_type)
);

CREATE TABLE frozen_market_states (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  freeze_id uuid NOT NULL REFERENCES market_freezes(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  lifecycle_status text NOT NULL CHECK (lifecycle_status IN (
    'initialized', 'active', 'inactive', 'open', 'closed', 'determined',
    'disputed', 'amended', 'finalized', 'resolved', 'ambiguous'
  )),
  eligible boolean NOT NULL,
  tradeable boolean NOT NULL,
  observed_at timestamptz NOT NULL,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (freeze_id, market_id),
  CHECK (observed_at <= cutoff)
);

CREATE TABLE order_book_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  freeze_id uuid NOT NULL REFERENCES market_freezes(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  observed_at timestamptz NOT NULL,
  source_timestamp timestamptz,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (freeze_id, market_id, raw_artifact_id),
  CHECK (observed_at <= cutoff),
  CHECK (source_timestamp IS NULL OR source_timestamp <= cutoff)
);

CREATE TABLE order_book_levels (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_id uuid NOT NULL REFERENCES order_book_snapshots(id),
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  book_side text NOT NULL CHECK (book_side IN ('bid', 'ask')),
  level_index integer NOT NULL CHECK (level_index >= 0),
  price_micros bigint NOT NULL CHECK (price_micros BETWEEN 0 AND 1000000),
  contract_units bigint NOT NULL CHECK (contract_units > 0),
  UNIQUE (snapshot_id, outcome_side, book_side, level_index),
  UNIQUE (snapshot_id, outcome_side, book_side, price_micros)
);

CREATE TABLE liquidity_haircut_audits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_id uuid NOT NULL REFERENCES order_book_snapshots(id),
  outcome_side text NOT NULL CHECK (outcome_side IN ('YES', 'NO')),
  rule_version text NOT NULL CHECK (rule_version = 'best-level-haircut-v1'),
  captured_raw_levels integer NOT NULL CHECK (captured_raw_levels = 6),
  effective_levels integer NOT NULL CHECK (effective_levels = 5),
  raw_depth_units bigint NOT NULL CHECK (raw_depth_units >= 0),
  ignored_quantity_units bigint NOT NULL CHECK (ignored_quantity_units >= 0),
  effective_depth_units bigint NOT NULL CHECK (effective_depth_units >= 0),
  consumed_quantity_units bigint NOT NULL DEFAULT 0 CHECK (consumed_quantity_units >= 0),
  cancelled_quantity_units bigint NOT NULL DEFAULT 0 CHECK (cancelled_quantity_units >= 0),
  remaining_quantity_units bigint NOT NULL DEFAULT 0 CHECK (remaining_quantity_units >= 0),
  executable boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (snapshot_id, outcome_side),
  CHECK (raw_depth_units = ignored_quantity_units + effective_depth_units),
  CHECK (ignored_quantity_units::numeric * 100 <= raw_depth_units::numeric * 50),
  CHECK (consumed_quantity_units + remaining_quantity_units <= effective_depth_units)
);

CREATE OR REPLACE FUNCTION vtrade_assert_binary_outcomes() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  market_id_value uuid;
  outcome_count integer;
  side_count integer;
BEGIN
  IF TG_TABLE_NAME = 'markets' THEN
    market_id_value := NEW.id;
  ELSE
    market_id_value := NEW.market_id;
  END IF;
  SELECT count(*), count(*) FILTER (WHERE outcome_side IN ('YES', 'NO'))
    INTO outcome_count, side_count
    FROM outcomes
   WHERE market_id = market_id_value;
  IF outcome_count <> 2 OR side_count <> 2 THEN
    RAISE EXCEPTION 'market % must have exactly YES and NO outcomes', market_id_value;
  END IF;
  RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_assert_grid_is_ordered() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM market_price_grid_ranges first_range
      JOIN market_price_grid_ranges second_range
        ON second_range.market_id = first_range.market_id
       AND second_range.ordinal > first_range.ordinal
     WHERE first_range.market_id = NEW.market_id
       AND first_range.end_price_micros > second_range.start_price_micros
  ) THEN
    RAISE EXCEPTION 'market % has overlapping price-grid ranges', NEW.market_id;
  END IF;
  RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_assert_market_grid() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  market_id_value uuid;
BEGIN
  IF TG_TABLE_NAME = 'markets' THEN
    market_id_value := NEW.id;
  ELSE
    market_id_value := NEW.market_id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM market_price_grid_ranges WHERE market_id = market_id_value
  ) THEN
    RAISE EXCEPTION 'market % must have a dynamic price grid', market_id_value;
  END IF;
  RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_assert_published_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  cycle_cutoff timestamptz;
  artifact_observed_at timestamptz;
BEGIN
  SELECT data_cutoff INTO cycle_cutoff FROM agent_cycles
   WHERE id = NEW.agent_cycle_id;
  IF cycle_cutoff IS NULL THEN
    UPDATE agent_cycles
       SET data_cutoff = NEW.data_cutoff
     WHERE id = NEW.agent_cycle_id AND data_cutoff IS NULL;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'published freeze % could not finalize its agent-cycle cutoff', NEW.id;
    END IF;
  ELSIF cycle_cutoff <> NEW.data_cutoff THEN
    RAISE EXCEPTION 'published freeze % must match its finalized agent-cycle cutoff', NEW.id;
  END IF;
  SELECT observed_at INTO artifact_observed_at FROM raw_artifacts
   WHERE id = NEW.catalogue_artifact_id;
  IF artifact_observed_at IS NULL OR artifact_observed_at > NEW.data_cutoff THEN
    RAISE EXCEPTION 'published freeze % references evidence newer than its cutoff', NEW.id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM market_freeze_memberships WHERE freeze_id = NEW.id
  ) THEN
    RAISE EXCEPTION 'published freeze % has no discovery or resolution membership', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM market_freeze_memberships membership
     WHERE membership.freeze_id = NEW.id
       AND NOT EXISTS (
         SELECT 1 FROM frozen_market_states state
          WHERE state.freeze_id = membership.freeze_id
            AND state.market_id = membership.market_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has membership without frozen market state', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM market_freeze_memberships membership
     WHERE membership.freeze_id = NEW.id AND membership.membership_type = 'discovery'
       AND NOT EXISTS (
         SELECT 1 FROM order_book_snapshots book
          WHERE book.freeze_id = membership.freeze_id
            AND book.market_id = membership.market_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has discovery market without a book read', NEW.id;
  END IF;
  RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION vtrade_guard_freeze_child_cutoff() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  freeze_cutoff timestamptz;
  artifact_id uuid;
  artifact_observed_at timestamptz;
BEGIN
  SELECT data_cutoff INTO freeze_cutoff FROM market_freezes WHERE id = NEW.freeze_id;
  IF freeze_cutoff IS NULL OR NEW.cutoff <> freeze_cutoff THEN
    RAISE EXCEPTION 'freeze child record does not use the immutable freeze cutoff';
  END IF;
  artifact_id := NEW.raw_artifact_id;
  SELECT observed_at INTO artifact_observed_at FROM raw_artifacts WHERE id = artifact_id;
  IF artifact_observed_at IS NULL OR artifact_observed_at > NEW.cutoff THEN
    RAISE EXCEPTION 'freeze child record references evidence newer than its cutoff';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER series_append_only
BEFORE UPDATE OR DELETE ON series
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER events_append_only
BEFORE UPDATE OR DELETE ON events
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER markets_append_only
BEFORE UPDATE OR DELETE ON markets
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER outcomes_append_only
BEFORE UPDATE OR DELETE ON outcomes
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER catalogue_pages_append_only
BEFORE UPDATE OR DELETE ON catalogue_page_observations
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER catalogue_markets_append_only
BEFORE UPDATE OR DELETE ON catalogue_market_observations
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER freezes_append_only
BEFORE UPDATE OR DELETE ON market_freezes
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER freeze_memberships_append_only
BEFORE UPDATE OR DELETE ON market_freeze_memberships
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER frozen_states_append_only
BEFORE UPDATE OR DELETE ON frozen_market_states
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER book_snapshots_append_only
BEFORE UPDATE OR DELETE ON order_book_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER book_levels_append_only
BEFORE UPDATE OR DELETE ON order_book_levels
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER haircut_audits_append_only
BEFORE UPDATE OR DELETE ON liquidity_haircut_audits
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

CREATE CONSTRAINT TRIGGER market_has_binary_outcomes
AFTER INSERT ON markets
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_binary_outcomes();

CREATE CONSTRAINT TRIGGER outcomes_complete_binary_market
AFTER INSERT ON outcomes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_binary_outcomes();

CREATE CONSTRAINT TRIGGER price_grid_is_ordered
AFTER INSERT ON market_price_grid_ranges
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_grid_is_ordered();

CREATE CONSTRAINT TRIGGER market_has_price_grid
AFTER INSERT ON markets
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_market_grid();

CREATE CONSTRAINT TRIGGER price_grid_is_present
AFTER INSERT ON market_price_grid_ranges
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_market_grid();

CREATE CONSTRAINT TRIGGER published_freeze_is_causal
AFTER INSERT ON market_freezes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_published_freeze();

CREATE TRIGGER frozen_market_state_cutoff_guard
BEFORE INSERT ON frozen_market_states
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_freeze_child_cutoff();

CREATE TRIGGER order_book_snapshot_cutoff_guard
BEFORE INSERT ON order_book_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_guard_freeze_child_cutoff();

COMMENT ON TABLE markets IS
  'Opaque Kalshi MarketKey storage. The reference is never parsed and the market owns '
  'exactly two outcomes: YES and NO.';
COMMENT ON TABLE market_freezes IS
  'An immutable, complete publication. Partial catalogue, book, cursor, or raw evidence '
  'cannot be represented as a published freeze.';
COMMENT ON TABLE liquidity_haircut_audits IS
  'Audit of the simulator-only six-level capture, best-level exclusion, and five-level '
  'effective depth rule. Raw book levels remain unchanged.';
