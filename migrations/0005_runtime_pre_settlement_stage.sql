-- vtrade-kalshi-persistence-v1 / durable pre-settlement stage contract

ALTER TABLE runtime_cycle_steps
  DROP CONSTRAINT runtime_cycle_steps_stage_check,
  ADD CONSTRAINT runtime_cycle_steps_stage_check CHECK (stage IN (
    'market_freeze', 'pre_settlement', 'prompt', 'harness', 'broker', 'settlement_valuation'
  ));

ALTER TABLE artifact_inventory
  DROP CONSTRAINT artifact_inventory_stage_check,
  ADD CONSTRAINT artifact_inventory_stage_check CHECK (stage IS NULL OR stage IN (
    'market_freeze', 'pre_settlement', 'prompt', 'harness', 'broker', 'settlement_valuation'
  ));

COMMENT ON VIEW vtrade_readiness IS
  'Provider-independent readiness projection. The migration runner verifies the full '
  'clean migration prefix before the API or worker may use this view.';
