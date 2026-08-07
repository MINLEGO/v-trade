-- Keep buy fees attached to open shares without changing gross cost metrics.
ALTER TABLE positions
  ADD COLUMN entry_fees_micros bigint NOT NULL DEFAULT 0
    CHECK (entry_fees_micros >= 0);
