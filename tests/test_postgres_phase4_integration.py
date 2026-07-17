from __future__ import annotations

import os
import unittest
import uuid
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from vtrade.harness import BeliefRecord, HarnessResult, PlanRecord, PlanType, ToolCallRecord
from vtrade.harness_repository import PostgresBudgetGuard, PostgresHarnessRepository
from vtrade.providers import ProviderTelemetry

RUN_POSTGRES = os.environ.get("VTRADE_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_POSTGRES,
    "set VTRADE_RUN_POSTGRES_INTEGRATION=1 for the rollback-only PostgreSQL test",
)
class PhaseFourPostgresIntegrationTests(unittest.TestCase):
    def test_real_repositories_commit_nothing_after_rollback(self) -> None:
        database_url = os.environ.get("VTRADE_DATABASE_URL")
        if not database_url:
            self.fail("VTRADE_DATABASE_URL is required when PostgreSQL integration is enabled")

        import psycopg

        marker = uuid.uuid4()
        ids = {name: uuid.uuid5(marker, name) for name in (
            "definition", "model", "run", "agent", "cycle", "belief", "plan"
        )}
        year = 2100 + marker.int % 7000
        now = datetime(year, 1, 15, 12, tzinfo=UTC)
        retain_until = now + timedelta(days=183)
        connection = psycopg.connect(database_url)
        reservation_id: uuid.UUID | None = None
        try:
            self._insert_fk_fixture(connection, ids, now, marker)
            def connect(_url: str) -> AbstractContextManager[Any]:
                return nullcontext(connection)
            budget = PostgresBudgetGuard(
                database_url,
                connect=connect,
                clock=lambda: now,
            )
            repository = PostgresHarnessRepository(database_url, connect=connect)

            reservation = budget.reserve("openrouter", 1_000)
            reservation_id = uuid.UUID(reservation.id)
            budget.reconcile(
                reservation,
                billed_cost_micros=750,
                nominal_cost_micros=1_000,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT billed_cost_micros, nominal_cost_micros, halted "
                    "FROM monthly_provider_budgets WHERE month_start = %s",
                    (now.date().replace(day=1),),
                )
                self.assertEqual(cursor.fetchone(), (750, 1_000, False))

            belief = BeliefRecord(
                id=str(ids["belief"]),
                agent_id=str(ids["agent"]),
                probability=Decimal("0.65"),
                content="Integration belief",
                category="forecast",
                evidence=("integration-source",),
                created_at=now,
            )
            plan = PlanRecord(
                id=str(ids["plan"]),
                agent_id=str(ids["agent"]),
                plan_type=PlanType.NEXT_CYCLE,
                content="Integration plan",
                due_at=now + timedelta(hours=1),
                created_at=now,
            )
            repository.append_belief(
                belief, actor_id=ids["agent"], cycle_id=ids["cycle"]
            )
            repository.append_belief(
                belief, actor_id=ids["agent"], cycle_id=ids["cycle"]
            )
            repository.append_plan(plan, actor_id=ids["agent"], cycle_id=ids["cycle"])
            repository.append_plan(plan, actor_id=ids["agent"], cycle_id=ids["cycle"])
            beliefs = repository.read_beliefs(
                actor_id=ids["agent"], target_agent_id=ids["agent"]
            )
            plans = repository.read_plans(
                actor_id=ids["agent"], target_agent_id=ids["agent"]
            )
            self.assertEqual([row["content"] for row in beliefs], [belief.content])
            self.assertEqual([row["content"] for row in plans], [plan.content])
            with self.assertRaises(ValueError):
                repository.append_belief(
                    replace(belief, content="Conflicting payload"),
                    actor_id=ids["agent"],
                    cycle_id=ids["cycle"],
                )
            with self.assertRaises(PermissionError):
                repository.read_plans(
                    actor_id=ids["agent"], target_agent_id=uuid.uuid4()
                )

            result = self._harness_result()
            first_run_id = repository.persist_run(
                agent_cycle_id=ids["cycle"],
                result=result,
                transcript_uri=f"supabase://integration/{marker}/transcript.json.gz",
                transcript_sha256="b" * 64,
                completed_at=now,
                retain_until=retain_until,
            )
            second_run_id = repository.persist_run(
                agent_cycle_id=ids["cycle"],
                result=result,
                transcript_uri=f"supabase://integration/{marker}/transcript.json.gz",
                transcript_sha256="b" * 64,
                completed_at=now,
                retain_until=retain_until,
            )
            self.assertEqual(first_run_id, second_run_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT retain_until, transcript_sha256 FROM harness_runs "
                    "WHERE agent_cycle_id = %s",
                    (ids["cycle"],),
                )
                harness_row = cursor.fetchone()
                self.assertIsNotNone(harness_row)
                assert harness_row is not None
                self.assertEqual(harness_row[0], retain_until)
                self.assertEqual(harness_row[1], "b" * 64)
                cursor.execute(
                    "SELECT retain_until, raw_artifact_uri, raw_sha256 FROM provider_usage "
                    "WHERE agent_cycle_id = %s",
                    (ids["cycle"],),
                )
                usage_row = cursor.fetchone()
                self.assertIsNotNone(usage_row)
                assert usage_row is not None
                self.assertEqual(usage_row[0], retain_until)
                self.assertEqual(usage_row[2], "a" * 64)
                self.assertTrue(str(usage_row[1]).startswith("supabase://private/"))
        finally:
            connection.rollback()

        try:
            with connection.cursor() as cursor:
                for table, identifier in (
                    ("experiment_definitions", ids["definition"]),
                    ("agents", ids["agent"]),
                    ("agent_cycles", ids["cycle"]),
                    ("beliefs", ids["belief"]),
                    ("plans", ids["plan"]),
                ):
                    cursor.execute(f"SELECT count(*) FROM {table} WHERE id = %s", (identifier,))
                    self.assertEqual(cursor.fetchone(), (0,))
                if reservation_id is not None:
                    cursor.execute(
                        "SELECT count(*) FROM provider_budget_reservations WHERE id = %s",
                        (reservation_id,),
                    )
                    self.assertEqual(cursor.fetchone(), (0,))
                cursor.execute(
                    "SELECT count(*) FROM monthly_provider_budgets WHERE month_start = %s",
                    (now.date().replace(day=1),),
                )
                self.assertEqual(cursor.fetchone(), (0,))
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _insert_fk_fixture(
        connection: Any,
        ids: dict[str, uuid.UUID],
        now: datetime,
        marker: uuid.UUID,
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO experiment_definitions "
                "(id, experiment_version, version_number, status, definition, config_sha256, "
                "code_version) VALUES (%s, %s, 1, 'owner_pending', '{}'::jsonb, %s, 'test')",
                (ids["definition"], f"phase4-integration-{marker}", marker.hex[:64].ljust(64, "0")),
            )
            cursor.execute(
                "INSERT INTO model_configs "
                "(id, definition_id, label, model_slug, provider_policy, parameters, "
                "config_sha256) VALUES (%s, %s, 'integration', %s, '{}'::jsonb, "
                "'{}'::jsonb, %s)",
                (
                    ids["model"],
                    ids["definition"],
                    "deepseek/deepseek-v4-flash",
                    ("1" + marker.hex).ljust(64, "0")[:64],
                ),
            )
            cursor.execute(
                "INSERT INTO experiment_runs "
                "(id, definition_id, run_label, status, starts_at) "
                "VALUES (%s, %s, 'integration', 'owner_pending', %s)",
                (ids["run"], ids["definition"], now),
            )
            cursor.execute(
                "INSERT INTO agents "
                "(id, run_id, model_config_id, name, initial_cash_micros) "
                "VALUES (%s, %s, %s, 'integration', 10000000000)",
                (ids["agent"], ids["run"], ids["model"]),
            )
            cursor.execute(
                "INSERT INTO agent_cycles "
                "(id, agent_id, scheduled_at, data_cutoff, status, idempotency_key) "
                "VALUES (%s, %s, %s, %s, 'running', %s)",
                (ids["cycle"], ids["agent"], now, now, f"integration:{marker}"),
            )

    @staticmethod
    def _harness_result() -> HarnessResult:
        telemetry = ProviderTelemetry(
            provider="openrouter",
            usage_kind="model",
            route="deepseek/deepseek-v4-flash",
            request_count=1,
            credit_count=Decimal(0),
            prompt_tokens=100,
            completion_tokens=20,
            reasoning_tokens=5,
            cached_tokens=0,
            billed_cost_micros=100,
            nominal_cost_micros=120,
            latency_ms=15,
            artifact_uri="supabase://private/integration-response.json.gz",
            raw_sha256="a" * 64,
        )
        call = ToolCallRecord(
            id="integration-call",
            name="web_search",
            arguments={"query": "integration"},
            success=True,
            output={"results": []},
            category="research",
        )
        return HarnessResult(
            messages=(),
            tool_calls=(call,),
            telemetry=(telemetry,),
            termination_status="completed",
            total_completion_tokens=20,
        )


if __name__ == "__main__":
    unittest.main()
