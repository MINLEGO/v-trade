-- Owner-frozen Exa free-plan controls: Exa is outside the dollar breaker but has
-- independent, atomic monthly request and credit caps.

CREATE TABLE monthly_exa_quotas (
  month_start date PRIMARY KEY,
  request_limit integer NOT NULL DEFAULT 18000 CHECK (request_limit = 18000),
  credit_limit numeric(20, 6) NOT NULL DEFAULT 18000 CHECK (credit_limit = 18000),
  request_count integer NOT NULL DEFAULT 0 CHECK (request_count >= 0),
  credit_count numeric(20, 6) NOT NULL DEFAULT 0 CHECK (credit_count >= 0),
  nominal_cost_micros bigint NOT NULL DEFAULT 0 CHECK (nominal_cost_micros >= 0),
  unexpected_billed_cost_micros bigint NOT NULL DEFAULT 0
    CHECK (unexpected_billed_cost_micros >= 0),
  halted boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL
);

CREATE TABLE exa_quota_reservations (
  id uuid PRIMARY KEY,
  month_start date NOT NULL REFERENCES monthly_exa_quotas(month_start),
  reserved_request_count integer NOT NULL CHECK (reserved_request_count = 1),
  reserved_credit_count numeric(20, 6) NOT NULL CHECK (reserved_credit_count = 1),
  actual_request_count integer CHECK (actual_request_count = 1),
  actual_credit_count numeric(20, 6) CHECK (actual_credit_count >= 0),
  nominal_cost_micros bigint NOT NULL CHECK (nominal_cost_micros >= 0),
  billed_cost_micros bigint CHECK (billed_cost_micros >= 0),
  status text NOT NULL CHECK (status IN ('reserved', 'reconciled')),
  reserved_at timestamptz NOT NULL,
  reconciled_at timestamptz,
  CHECK (
    (status = 'reserved' AND actual_request_count IS NULL
      AND actual_credit_count IS NULL AND billed_cost_micros IS NULL
      AND reconciled_at IS NULL)
    OR
    (status = 'reconciled' AND actual_request_count IS NOT NULL
      AND actual_credit_count IS NOT NULL AND billed_cost_micros IS NOT NULL
      AND reconciled_at IS NOT NULL)
  )
);

CREATE INDEX exa_quota_active_idx
  ON exa_quota_reservations (month_start, status);

CREATE TRIGGER exa_quota_reservations_no_delete
BEFORE DELETE ON exa_quota_reservations
FOR EACH ROW EXECUTE FUNCTION reject_mutation();

COMMENT ON TABLE monthly_exa_quotas IS
  'Strict 18,000 request and 18,000 credit monthly Exa caps. Exa free-plan usage is '
  'excluded from the all-provider billed-dollar breaker; nominal value is retained.';
