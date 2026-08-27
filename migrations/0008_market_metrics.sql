-- vtrade-kalshi-metrics-v1 / immutable discovery metrics and series metadata snapshots

CREATE TABLE series_metadata_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  freeze_id uuid NOT NULL REFERENCES market_freezes(id),
  series_id uuid NOT NULL REFERENCES series(id),
  tags jsonb NOT NULL DEFAULT '[]'::jsonb,
  observed_at timestamptz NOT NULL,
  source_timestamp timestamptz,
  cutoff timestamptz NOT NULL,
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  UNIQUE (freeze_id, series_id),
  CHECK (jsonb_typeof(tags) = 'array'),
  CHECK (observed_at <= cutoff),
  CHECK (source_timestamp IS NULL OR source_timestamp <= cutoff)
);

CREATE TABLE market_metric_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  freeze_id uuid NOT NULL REFERENCES market_freezes(id),
  market_id uuid NOT NULL REFERENCES markets(id),
  volume_24h_units bigint NOT NULL CHECK (volume_24h_units >= 0),
  volatility_micros bigint CHECK (volatility_micros IS NULL OR volatility_micros >= 0),
  volume_trend text NOT NULL CHECK (
    volume_trend IN ('increasing', 'decreasing', 'flat', 'insufficient_data')
  ),
  volume_trend_delta numeric,
  competitive_score numeric(12, 10) CHECK (
    competitive_score IS NULL OR competitive_score BETWEEN 0 AND 1
  ),
  indicative_yes_price_micros bigint CHECK (
    indicative_yes_price_micros IS NULL
    OR indicative_yes_price_micros BETWEEN 0 AND 1000000
  ),
  indicative_no_price_micros bigint CHECK (
    indicative_no_price_micros IS NULL
    OR indicative_no_price_micros BETWEEN 0 AND 1000000
  ),
  recent_volume_units bigint NOT NULL CHECK (recent_volume_units >= 0),
  baseline_volume_units bigint NOT NULL CHECK (baseline_volume_units >= 0),
  volatility_sample_count integer NOT NULL CHECK (volatility_sample_count >= 0),
  recent_bucket_count integer NOT NULL CHECK (recent_bucket_count >= 0),
  baseline_bucket_count integer NOT NULL CHECK (baseline_bucket_count >= 0),
  as_of_at timestamptz NOT NULL,
  formula_version text NOT NULL CHECK (formula_version = 'kalshi-market-metrics-v1'),
  cutoff timestamptz NOT NULL,
  UNIQUE (freeze_id, market_id),
  CHECK (
    indicative_yes_price_micros IS NULL
    OR indicative_no_price_micros IS NULL
    OR indicative_yes_price_micros + indicative_no_price_micros = 1000000
  ),
  CHECK (
    (volume_trend = 'insufficient_data' AND volume_trend_delta IS NULL)
    OR (
      volume_trend <> 'insufficient_data'
      AND baseline_volume_units = 0
      AND volume_trend_delta IS NULL
    )
    OR (
      volume_trend <> 'insufficient_data'
      AND baseline_volume_units > 0
      AND volume_trend_delta IS NOT NULL
      AND volume_trend_delta =
        round(
          (recent_volume_units - baseline_volume_units)::numeric / baseline_volume_units,
          10
        )
    )
  ),
  CHECK (as_of_at <= cutoff)
);

CREATE TABLE market_metric_snapshot_artifacts (
  metric_snapshot_id uuid NOT NULL REFERENCES market_metric_snapshots(id),
  raw_artifact_id uuid NOT NULL REFERENCES raw_artifacts(id),
  artifact_role text NOT NULL CHECK (length(artifact_role) BETWEEN 1 AND 128),
  PRIMARY KEY (metric_snapshot_id, raw_artifact_id, artifact_role)
);

CREATE OR REPLACE FUNCTION vtrade_assert_discovery_metrics() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM market_freeze_memberships membership
     WHERE membership.freeze_id = NEW.id
       AND membership.membership_type = 'discovery'
       AND NOT EXISTS (
         SELECT 1
           FROM market_metric_snapshots metric
          WHERE metric.freeze_id = membership.freeze_id
            AND metric.market_id = membership.market_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has discovery market without metrics', NEW.id;
  END IF;
  IF EXISTS (
    SELECT 1
      FROM market_freeze_memberships membership
      JOIN markets market ON market.id = membership.market_id
     WHERE membership.freeze_id = NEW.id
       AND membership.membership_type = 'discovery'
       AND NOT EXISTS (
         SELECT 1
           FROM series_metadata_snapshots metadata
          WHERE metadata.freeze_id = membership.freeze_id
            AND metadata.series_id = market.series_id
       )
  ) THEN
    RAISE EXCEPTION 'published freeze % has discovery market without series metadata', NEW.id;
  END IF;
  RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER discovery_metrics_present
AFTER INSERT ON market_freezes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION vtrade_assert_discovery_metrics();

CREATE TRIGGER series_metadata_snapshots_append_only
BEFORE UPDATE OR DELETE ON series_metadata_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER market_metric_snapshots_append_only
BEFORE UPDATE OR DELETE ON market_metric_snapshots
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();
CREATE TRIGGER market_metric_snapshot_artifacts_append_only
BEFORE UPDATE OR DELETE ON market_metric_snapshot_artifacts
FOR EACH ROW EXECUTE FUNCTION vtrade_reject_mutation();

COMMENT ON TABLE series_metadata_snapshots IS
  'Freeze-scoped Kalshi series tags preserved from /series/{series_ticker}.';
COMMENT ON TABLE market_metric_snapshots IS
  'Freeze-scoped, auditable derived discovery metrics; provider market rows remain immutable.';
COMMENT ON TABLE market_metric_snapshot_artifacts IS
  'Raw market, order-book, and candlestick evidence used by one metric snapshot.';
