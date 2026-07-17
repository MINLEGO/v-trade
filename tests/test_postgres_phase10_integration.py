from __future__ import annotations

import os
import unittest
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from vtrade.harness_repository import PostgresBudgetGuard
from vtrade.providers import BudgetExceeded

RUN_POSTGRES = os.environ.get("VTRADE_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_POSTGRES,
    "set VTRADE_RUN_POSTGRES_INTEGRATION=1 for the rollback-only PostgreSQL test",
)
class PhaseTenPostgresIntegrationTests(unittest.TestCase):
    def test_exa_quota_is_atomic_separate_from_dollars_and_fail_closed(self) -> None:
        database_url = os.environ.get("VTRADE_DATABASE_URL")
        if not database_url:
            self.fail("VTRADE_DATABASE_URL is required when PostgreSQL integration is enabled")

        import psycopg

        marker = uuid.uuid4()
        year = 2100 + marker.int % 7000
        current = [datetime(year, 1, 15, 12, tzinfo=UTC)]
        first_month = current[0].date().replace(day=1)
        second_month = first_month.replace(month=2)
        connection = psycopg.connect(database_url)
        reservation_ids: list[uuid.UUID] = []
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO monthly_exa_quotas "
                    "(month_start, request_count, credit_count, updated_at) "
                    "VALUES (%s, 17999, 17999, %s)",
                    (first_month, current[0]),
                )

            def connect(_url: str) -> AbstractContextManager[Any]:
                return nullcontext(connection)

            guard = PostgresBudgetGuard(
                database_url,
                connect=connect,
                clock=lambda: current[0],
            )
            final_slot = guard.reserve(
                "exa", 20_000, request_count=1, credit_count=Decimal(1)
            )
            reservation_ids.append(uuid.UUID(final_slot.id))
            with self.assertRaisesRegex(BudgetExceeded, "cap reached"):
                guard.reserve("exa", 20_000, request_count=1, credit_count=Decimal(1))
            guard.reconcile(
                final_slot,
                billed_cost_micros=0,
                nominal_cost_micros=20_000,
                request_count=1,
                credit_count=Decimal(1),
            )
            with self.assertRaisesRegex(BudgetExceeded, "cap reached"):
                guard.reserve("exa", 20_000, request_count=1, credit_count=Decimal(1))

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT request_count, credit_count, nominal_cost_micros, halted "
                    "FROM monthly_exa_quotas WHERE month_start = %s",
                    (first_month,),
                )
                self.assertEqual(
                    cursor.fetchone(),
                    (18_000, Decimal("18000.000000"), 20_000, False),
                )
                cursor.execute(
                    "SELECT count(*) FROM provider_budget_reservations "
                    "WHERE provider = 'exa' AND month_start = %s",
                    (first_month,),
                )
                self.assertEqual(cursor.fetchone(), (0,))
                cursor.execute(
                    "SELECT count(*) FROM monthly_provider_budgets WHERE month_start = %s",
                    (first_month,),
                )
                self.assertEqual(cursor.fetchone(), (0,))

            current[0] = current[0].replace(month=2)
            charged = guard.reserve(
                "exa", 20_000, request_count=1, credit_count=Decimal(1)
            )
            reservation_ids.append(uuid.UUID(charged.id))
            with self.assertRaisesRegex(BudgetExceeded, "Exa usage recorded"):
                guard.reconcile(
                    charged,
                    billed_cost_micros=1,
                    nominal_cost_micros=20_000,
                    request_count=1,
                    credit_count=Decimal(1),
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT halted, unexpected_billed_cost_micros, nominal_cost_micros "
                    "FROM monthly_exa_quotas WHERE month_start = %s",
                    (second_month,),
                )
                self.assertEqual(cursor.fetchone(), (True, 1, 20_000))
                cursor.execute(
                    "SELECT count(*) FROM alerts WHERE code = 'exa_unexpected_billed_cost' "
                    "AND details->>'reservation_id' = %s",
                    (charged.id,),
                )
                self.assertEqual(cursor.fetchone(), (1,))
                cursor.execute(
                    "SELECT count(*) FROM monthly_provider_budgets WHERE month_start = %s",
                    (second_month,),
                )
                self.assertEqual(cursor.fetchone(), (0,))
        finally:
            connection.rollback()

        try:
            with connection.cursor() as cursor:
                for month in (first_month, second_month):
                    cursor.execute(
                        "SELECT count(*) FROM monthly_exa_quotas WHERE month_start = %s",
                        (month,),
                    )
                    self.assertEqual(cursor.fetchone(), (0,))
                for reservation_id in reservation_ids:
                    cursor.execute(
                        "SELECT count(*) FROM exa_quota_reservations WHERE id = %s",
                        (reservation_id,),
                    )
                    self.assertEqual(cursor.fetchone(), (0,))
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
