from __future__ import annotations

import os
import unittest
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from vtrade.postgres_runtime import PostgresRuntimeRepository
from vtrade.runtime import CycleStage, MarketFreezeResult

RUN_POSTGRES = os.environ.get("VTRADE_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_POSTGRES,
    "set VTRADE_RUN_POSTGRES_INTEGRATION=1 for the rollback-only PostgreSQL test",
)
class PhaseFivePostgresIntegrationTests(unittest.TestCase):
    def test_schedule_lease_checkpoint_recovery_path_rolls_back(self) -> None:
        database_url = os.environ.get("VTRADE_DATABASE_URL")
        if not database_url:
            self.fail("VTRADE_DATABASE_URL is required when PostgreSQL integration is enabled")
        import psycopg

        marker = uuid.uuid4()
        ids = {name: uuid.uuid5(marker, name) for name in ("definition", "model", "run", "agent")}
        now = datetime(2200 + marker.int % 1000, 1, 15, 12, tzinfo=UTC)
        connection = psycopg.connect(database_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE system_controls SET globally_paused = false")
                cursor.execute(
                    "INSERT INTO experiment_definitions "
                    "(id, experiment_version, version_number, status, definition, config_sha256, "
                    "code_version) VALUES (%s, %s, 1, 'running', '{}'::jsonb, %s, 'test')",
                    (ids["definition"], f"phase5-{marker}", marker.hex.ljust(64, "0")[:64]),
                )
                cursor.execute(
                    "INSERT INTO model_configs "
                    "(id, definition_id, label, model_slug, provider_policy, parameters, "
                    "config_sha256) VALUES (%s, %s, 'integration', 'integration/model', "
                    "'{}'::jsonb, '{}'::jsonb, %s)",
                    (ids["model"], ids["definition"], ("1" + marker.hex).ljust(64, "0")[:64]),
                )
                cursor.execute(
                    "INSERT INTO experiment_runs "
                    "(id, definition_id, run_label, status, starts_at) "
                    "VALUES (%s, %s, 'integration', 'running', %s)",
                    (ids["run"], ids["definition"], now),
                )
                cursor.execute(
                    "INSERT INTO agents "
                    "(id, run_id, model_config_id, name, initial_cash_micros) "
                    "VALUES (%s, %s, %s, 'integration', 10000000000)",
                    (ids["agent"], ids["run"], ids["model"]),
                )
                cursor.execute(
                    "INSERT INTO agent_runtime_schedules (agent_id, next_scheduled_at) "
                    "VALUES (%s, %s)",
                    (ids["agent"], now),
                )

            def connect(_url: str) -> AbstractContextManager[Any]:
                return nullcontext(connection)

            repository = PostgresRuntimeRepository(database_url, connect=connect)
            claims = repository.claim_due_cycles(
                now=now,
                lease_owner=f"integration-{marker}",
                lease_duration=timedelta(minutes=10),
                missed_grace=timedelta(minutes=10),
                limit=1,
            )
            self.assertEqual(len(claims), 1)
            claim = claims[0]
            repository.begin_stage(claim, CycleStage.MARKET_FREEZE, "a" * 64, now=now)
            result = MarketFreezeResult({"snapshot": "immutable"}, (), now)
            repository.complete_stage(
                claim, CycleStage.MARKET_FREEZE, "a" * 64, result, now=now
            )
            self.assertEqual(
                repository.load_stage(claim.cycle_id, CycleStage.MARKET_FREEZE), result
            )
        finally:
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM agents WHERE id = %s", (ids["agent"],))
                self.assertEqual(cursor.fetchone(), (0,))
            connection.close()


if __name__ == "__main__":
    unittest.main()
