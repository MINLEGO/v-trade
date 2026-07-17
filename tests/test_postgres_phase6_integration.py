from __future__ import annotations

import os
import unittest
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any

from vtrade.admin import Page, PostgresAdminRepository

RUN_POSTGRES = os.environ.get("VTRADE_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_POSTGRES,
    "set VTRADE_RUN_POSTGRES_INTEGRATION=1 for the rollback-only PostgreSQL test",
)
class PhaseSixPostgresIntegrationTests(unittest.TestCase):
    def test_every_admin_query_and_control_executes_against_postgresql(self) -> None:
        database_url = os.environ.get("VTRADE_DATABASE_URL")
        if not database_url:
            self.fail("VTRADE_DATABASE_URL is required when PostgreSQL integration is enabled")

        import psycopg

        marker = uuid.uuid4()
        now = datetime(2099, 1, 1, 12, tzinfo=UTC)
        connection = psycopg.connect(database_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version = %s",
                    ("0006_private_admin.sql",),
                )
                self.assertEqual(cursor.fetchone(), (1,))
            ids = self._insert_agent(connection, marker, now)

            def connect(_url: str) -> AbstractContextManager[Any]:
                return nullcontext(connection)

            repository = PostgresAdminRepository(database_url, connect=connect)
            probe = repository.probe()
            self.assertEqual(probe["latest_migration"], "0006_private_admin.sql")
            repository.overview()
            for view in (
                "leaderboard",
                "positions",
                "trades",
                "settlements",
                "rejections",
                "cycles",
                "usage",
                "freshness",
                "config_versions",
                "alerts",
                "operator_actions",
            ):
                repository.view(view, page=Page(10, 0), agent_id=ids["agent"])

            global_key = f"phase6-integration-global-{marker}"
            first = repository.set_global_pause(
                paused=True,
                actor_id="phase6-integration",
                idempotency_key=global_key,
                occurred_at=now,
            )
            second = repository.set_global_pause(
                paused=True,
                actor_id="phase6-integration",
                idempotency_key=global_key,
                occurred_at=now,
            )
            self.assertEqual(first, second)
            repository.set_agent_pause(
                ids["agent"],
                paused=True,
                actor_id="phase6-integration",
                idempotency_key=f"phase6-integration-agent-pause-{marker}",
                occurred_at=now,
            )
            repository.set_agent_pause(
                ids["agent"],
                paused=False,
                actor_id="phase6-integration",
                idempotency_key=f"phase6-integration-agent-resume-{marker}",
                occurred_at=now,
            )
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _insert_agent(
        connection: Any, marker: uuid.UUID, now: datetime
    ) -> dict[str, uuid.UUID]:
        ids = {
            name: uuid.uuid5(marker, name)
            for name in ("definition", "model", "run", "agent")
        }
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO experiment_definitions "
                "(id, experiment_version, version_number, status, definition, config_sha256, "
                "code_version) VALUES (%s, %s, 1, 'owner_pending', '{}'::jsonb, %s, 'test')",
                (
                    ids["definition"],
                    f"phase6-integration-{marker}",
                    marker.hex.ljust(64, "0"),
                ),
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
        return ids


if __name__ == "__main__":
    unittest.main()
