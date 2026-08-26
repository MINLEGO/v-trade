-- vtrade-kalshi-persistence-v1 / durable Exa request-credit quota contract

ALTER TABLE monthly_exa_quotas
  ALTER COLUMN limit_units SET DEFAULT 18000,
  ADD COLUMN request_limit integer NOT NULL DEFAULT 18000
    CHECK (request_limit > 0),
  ADD COLUMN credit_limit numeric(18, 2) NOT NULL DEFAULT 18000
    CHECK (credit_limit > 0),
  ADD COLUMN request_count integer NOT NULL DEFAULT 0
    CHECK (request_count >= 0),
  ADD COLUMN credit_count numeric(18, 2) NOT NULL DEFAULT 0
    CHECK (credit_count >= 0),
  ADD COLUMN nominal_cost_micros bigint NOT NULL DEFAULT 0
    CHECK (nominal_cost_micros >= 0),
  ADD COLUMN unexpected_billed_cost_micros bigint NOT NULL DEFAULT 0
    CHECK (unexpected_billed_cost_micros >= 0);

ALTER TABLE exa_quota_reservations
  ADD COLUMN reserved_request_count integer NOT NULL DEFAULT 0
    CHECK (reserved_request_count >= 0),
  ADD COLUMN reserved_credit_count numeric(18, 2) NOT NULL DEFAULT 0
    CHECK (reserved_credit_count >= 0),
  ADD COLUMN nominal_cost_micros bigint NOT NULL DEFAULT 0
    CHECK (nominal_cost_micros >= 0),
  ADD COLUMN actual_request_count integer
    CHECK (actual_request_count IS NULL OR actual_request_count >= 0),
  ADD COLUMN actual_credit_count numeric(18, 2)
    CHECK (actual_credit_count IS NULL OR actual_credit_count >= 0),
  ADD COLUMN billed_cost_micros bigint
    CHECK (billed_cost_micros IS NULL OR billed_cost_micros >= 0);

CREATE OR REPLACE VIEW vtrade_readiness AS
SELECT
  COALESCE((SELECT max(position) FROM schema_migrations), 0) AS latest_position,
  (SELECT version FROM schema_migrations ORDER BY position DESC LIMIT 1) AS latest_version,
  COALESCE((SELECT count(*) FROM schema_migrations), 0) = 7 AS migrations_complete,
  (SELECT globally_paused FROM system_controls WHERE singleton = true) AS globally_paused;
