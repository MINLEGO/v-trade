-- Phase 22: conservative simulator-only haircut of the best displayed level.
-- The raw order-book snapshots remain immutable.  These columns version and
-- audit the private effective view derived from each snapshot.

ALTER TABLE virtual_liquidity_levels
  ADD COLUMN ignored_shares numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN effective_shares numeric(30, 12),
  ADD COLUMN executable boolean NOT NULL DEFAULT true;

UPDATE virtual_liquidity_levels
SET effective_shares = displayed_shares - ignored_shares
WHERE effective_shares IS NULL;

ALTER TABLE virtual_liquidity_levels
  ALTER COLUMN effective_shares SET NOT NULL,
  ADD CONSTRAINT virtual_liquidity_levels_ignored_shares_check
    CHECK (ignored_shares BETWEEN 0 AND displayed_shares),
  ADD CONSTRAINT virtual_liquidity_levels_effective_shares_check
    CHECK (effective_shares = displayed_shares - ignored_shares),
  ADD CONSTRAINT virtual_liquidity_levels_consumed_effective_check
    CHECK (consumed_shares <= effective_shares);

ALTER TABLE virtual_liquidity_execution_levels
  ADD COLUMN ignored_shares numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN effective_shares numeric(30, 12),
  ADD COLUMN executable boolean NOT NULL DEFAULT true;

UPDATE virtual_liquidity_execution_levels
SET effective_shares = displayed_shares - ignored_shares
WHERE effective_shares IS NULL;

ALTER TABLE virtual_liquidity_execution_levels
  ALTER COLUMN effective_shares SET NOT NULL,
  ADD CONSTRAINT virtual_liquidity_execution_levels_ignored_shares_check
    CHECK (ignored_shares BETWEEN 0 AND displayed_shares),
  ADD CONSTRAINT virtual_liquidity_execution_levels_effective_shares_check
    CHECK (effective_shares = displayed_shares - ignored_shares),
  ADD CONSTRAINT virtual_liquidity_execution_levels_available_effective_check
    CHECK (available_shares <= effective_shares);

ALTER TABLE virtual_liquidity_executions
  ADD COLUMN rule_version text NOT NULL DEFAULT 'best-level-haircut-v0',
  ADD COLUMN ignored_best_levels integer NOT NULL DEFAULT 0,
  ADD COLUMN maximum_ignored_depth_fraction numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN raw_depth_shares numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN best_level_fraction numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN ignored_depth_shares numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN ignored_fraction numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN effective_depth_shares numeric(30, 12) NOT NULL DEFAULT 0,
  ADD COLUMN best_level_price numeric(30, 12);

WITH aggregates AS (
  SELECT
    execution_id,
    sum(displayed_shares) AS raw_depth_shares,
    sum(ignored_shares) AS ignored_depth_shares,
    sum(effective_shares) FILTER (WHERE executable) AS effective_depth_shares
  FROM virtual_liquidity_execution_levels
  GROUP BY execution_id
)
UPDATE virtual_liquidity_executions AS executions
SET raw_depth_shares = aggregates.raw_depth_shares,
    ignored_depth_shares = aggregates.ignored_depth_shares,
    effective_depth_shares = aggregates.effective_depth_shares
FROM aggregates
WHERE executions.id = aggregates.execution_id;

UPDATE virtual_liquidity_executions AS executions
SET best_level_price = (
      SELECT price
      FROM virtual_liquidity_execution_levels
      WHERE execution_id = executions.id
      ORDER BY level_index
      LIMIT 1
    ),
    best_level_fraction = COALESCE((
      SELECT displayed_shares / NULLIF(executions.raw_depth_shares, 0)
      FROM virtual_liquidity_execution_levels
      WHERE execution_id = executions.id
      ORDER BY level_index
      LIMIT 1
    ), 0),
    ignored_fraction = COALESCE(
      executions.ignored_depth_shares / NULLIF(executions.raw_depth_shares, 0),
      0
    )
WHERE EXISTS (
  SELECT 1
  FROM virtual_liquidity_execution_levels
  WHERE execution_id = executions.id
);

ALTER TABLE virtual_liquidity_executions
  ADD CONSTRAINT virtual_liquidity_executions_rule_fraction_check
    CHECK (maximum_ignored_depth_fraction BETWEEN 0 AND 1),
  ADD CONSTRAINT virtual_liquidity_executions_best_fraction_check
    CHECK (best_level_fraction BETWEEN 0 AND 1),
  ADD CONSTRAINT virtual_liquidity_executions_ignored_fraction_check
    CHECK (ignored_fraction BETWEEN 0 AND 1),
  ADD CONSTRAINT virtual_liquidity_executions_depth_totals_check
    CHECK (
      ignored_depth_shares BETWEEN 0 AND raw_depth_shares
      AND effective_depth_shares >= 0
      AND available_shares <= effective_depth_shares
    );

COMMENT ON COLUMN virtual_liquidity_levels.ignored_shares IS
  'Simulator-only haircut; never available to or consumed by an agent.';
COMMENT ON COLUMN virtual_liquidity_levels.effective_shares IS
  'Displayed shares remaining after the deterministic simulator haircut.';
COMMENT ON COLUMN virtual_liquidity_executions.rule_version IS
  'Version of the immutable simulator haircut rule used for this order.';
COMMENT ON COLUMN virtual_liquidity_executions.raw_depth_shares IS
  'Displayed shares observed in the raw depth selected for this private context.';
