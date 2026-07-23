-- Beliefs expose confidence rather than probability. The old implementation
-- incorrectly stored the confidence input in probability, so preserve that
-- value as confidence before removing the redundant field.
UPDATE belief_revisions
SET confidence = probability
WHERE confidence IS NULL AND probability IS NOT NULL;

ALTER TABLE belief_revisions
  ALTER COLUMN confidence SET NOT NULL,
  DROP COLUMN probability;

ALTER TABLE belief_revisions
  ADD CONSTRAINT belief_category_allowed
  CHECK (category IN (
    'event_analysis',
    'trading_strategy',
    'market_sentiment',
    'market_structure',
    'risk_assessment'
  ));

-- Only the current plan of each type is active. Superseded rows remain for
-- audit/history and are excluded by repository reads.
UPDATE plans first_plan
SET status = 'superseded'
WHERE status = 'active'
  AND EXISTS (
    SELECT 1
    FROM plans newer_plan
    WHERE newer_plan.agent_id = first_plan.agent_id
      AND newer_plan.plan_type = first_plan.plan_type
      AND newer_plan.status = 'active'
      AND (newer_plan.created_at, newer_plan.id)
          > (first_plan.created_at, first_plan.id)
  );

CREATE UNIQUE INDEX plans_one_active_per_type_idx
  ON plans (agent_id, plan_type)
  WHERE status = 'active';
