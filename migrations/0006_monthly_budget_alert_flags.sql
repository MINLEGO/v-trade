-- vtrade-kalshi-persistence-v1 / durable monthly budget alert state

ALTER TABLE monthly_provider_budgets
  ADD COLUMN alerted_20 boolean NOT NULL DEFAULT false,
  ADD COLUMN alerted_32 boolean NOT NULL DEFAULT false,
  ADD COLUMN alerted_40 boolean NOT NULL DEFAULT false;
