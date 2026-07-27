-- IOC is the public immediate-order contract. FAK remains readable for
-- immutable historical executions created before the contract upgrade.
ALTER TABLE orders
  DROP CONSTRAINT orders_liquidity_time_in_force_check;

ALTER TABLE orders
  ADD CONSTRAINT orders_liquidity_time_in_force_check
    CHECK (liquidity_time_in_force IN ('IOC', 'FOK', 'FAK'));

ALTER TABLE orders
  ALTER COLUMN liquidity_time_in_force SET DEFAULT 'IOC';
