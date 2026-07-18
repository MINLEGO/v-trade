-- Exa documents costDollars as an endpoint-dependent estimated cost. Billing is
-- computed separately from usage counters, and the owner-selected 18,000-request
-- ceiling remains below the 20,000-request free allowance. Undo only false billing
-- halts created by the former response interpretation; preserve request/credit usage.

UPDATE exa_quota_reservations reservations
SET billed_cost_micros = 0
FROM alerts false_alerts
WHERE false_alerts.code = 'exa_unexpected_billed_cost'
  AND false_alerts.details->>'reservation_id' = reservations.id::text
  AND reservations.billed_cost_micros > 0;

UPDATE monthly_exa_quotas quotas
SET unexpected_billed_cost_micros = 0,
    halted = (
      quotas.request_count > quotas.request_limit
      OR quotas.credit_count > quotas.credit_limit
      OR EXISTS (
        SELECT 1
        FROM exa_quota_reservations reservations
        WHERE reservations.month_start = quotas.month_start
          AND reservations.status = 'reconciled'
          AND reservations.actual_credit_count > reservations.reserved_credit_count
      )
    ),
    updated_at = now()
WHERE quotas.unexpected_billed_cost_micros > 0
  AND EXISTS (
    SELECT 1
    FROM alerts false_alerts
    WHERE false_alerts.code = 'exa_unexpected_billed_cost'
      AND false_alerts.details->>'month' = quotas.month_start::text
  );

UPDATE alerts
SET resolved_at = now(),
    details = details || jsonb_build_object(
      'resolution', 'costDollars is provider-estimated nominal cost, not actual billing'
    )
WHERE code = 'exa_unexpected_billed_cost'
  AND resolved_at IS NULL;
