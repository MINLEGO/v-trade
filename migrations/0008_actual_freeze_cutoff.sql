-- The immutable data cutoff is the successful completion instant of market freeze,
-- not the independent schedule anchor. It is NULL only while market freeze is running.

ALTER TABLE agent_cycles ALTER COLUMN data_cutoff DROP NOT NULL;

ALTER TABLE agent_cycles
  ADD CONSTRAINT agent_cycles_cutoff_after_schedule CHECK (
    data_cutoff IS NULL OR data_cutoff >= scheduled_at
  );

COMMENT ON COLUMN agent_cycles.data_cutoff IS
  'Atomically finalized with the completed market_freeze checkpoint. No provider or '
  'venue fetch is allowed for this agent cycle after this instant.';
