from __future__ import annotations

import os
import unittest
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from vtrade.portfolio import PortfolioPaginationError, PostgresPortfolioHandler

RUN_POSTGRES = os.environ.get("VTRADE_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_POSTGRES,
    "set VTRADE_RUN_POSTGRES_INTEGRATION=1 for the rollback-only PostgreSQL test",
)
class PhaseSevenPostgresIntegrationTests(unittest.TestCase):
    def test_snapshot_pagination_and_scope_rollback_cleanly(self) -> None:
        database_url = os.environ.get("VTRADE_DATABASE_URL")
        if not database_url:
            self.fail("VTRADE_DATABASE_URL is required when PostgreSQL integration is enabled")
        import psycopg

        marker = uuid.uuid4()
        names = (
            "definition", "model", "run", "agent", "other_agent", "cycle", "other_cycle",
            "event", "market", "snapshot",
        )
        ids = {name: uuid.uuid5(marker, name) for name in names}
        outcome_ids = [uuid.uuid5(marker, f"outcome-{index}") for index in range(3)]
        position_ids = [uuid.uuid5(marker, f"position-{index}") for index in range(3)]
        now = datetime(2037, 7, 16, 12, tzinfo=UTC)
        connection = psycopg.connect(database_url)

        def connect(_url: str) -> AbstractContextManager[Any]:
            return nullcontext(connection)

        try:
            self._insert_fixture(connection, ids, outcome_ids, position_ids, marker, now)
            tokens = iter(("phase7-first-cursor", "phase7-second-cursor"))
            handler = PostgresPortfolioHandler(
                database_url,
                agent_id=ids["agent"],
                agent_cycle_id=ids["cycle"],
                connect=connect,
                cursor_factory=lambda: next(tokens),
            )
            first = handler({"limit": 1})
            self.assertTrue(first["has_more"])
            first_id = first["items"][0]["position_id"]

            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE positions SET shares = 999, updated_at = %s WHERE agent_id = %s",
                    (now, ids["agent"]),
                )
            second = handler({"cursor": first["next_cursor"], "limit": 2})
            all_items = first["items"] + second["items"]
            self.assertEqual(len(all_items), 3)
            self.assertEqual(len({item["position_id"] for item in all_items}), 3)
            self.assertEqual(first_id, min(str(value) for value in position_ids))
            self.assertTrue(all(item["shares"] == "10.000000000000" for item in all_items))

            for foreign_agent, foreign_cycle in (
                (ids["other_agent"], ids["cycle"]),
                (ids["agent"], ids["other_cycle"]),
            ):
                foreign = PostgresPortfolioHandler(
                    database_url,
                    agent_id=foreign_agent,
                    agent_cycle_id=foreign_cycle,
                    connect=connect,
                )
                with self.assertRaisesRegex(PortfolioPaginationError, "invalid or foreign"):
                    foreign({"cursor": first["next_cursor"]})
        finally:
            connection.rollback()

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM portfolio_query_snapshots WHERE agent_cycle_id = %s",
                    (ids["cycle"],),
                )
                self.assertEqual(cursor.fetchone(), (0,))
                cursor.execute(
                    "SELECT count(*) FROM positions WHERE agent_id = %s", (ids["agent"],)
                )
                self.assertEqual(cursor.fetchone(), (0,))
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _insert_fixture(
        connection: Any,
        ids: dict[str, uuid.UUID],
        outcome_ids: list[uuid.UUID],
        position_ids: list[uuid.UUID],
        marker: uuid.UUID,
        now: datetime,
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO experiment_definitions "
                "(id, experiment_version, version_number, status, definition, config_sha256, "
                "code_version) VALUES (%s, %s, 1, 'ready', '{}'::jsonb, %s, 'test')",
                (ids["definition"], f"phase7-{marker}", marker.hex.ljust(64, "0")),
            )
            cursor.execute(
                "INSERT INTO model_configs "
                "(id, definition_id, label, provider_policy, parameters, config_sha256) "
                "VALUES (%s, %s, 'phase7', '{}'::jsonb, '{}'::jsonb, %s)",
                (ids["model"], ids["definition"], ("1" + marker.hex).ljust(64, "0")),
            )
            cursor.execute(
                "INSERT INTO experiment_runs (id, definition_id, run_label, status, starts_at) "
                "VALUES (%s, %s, 'phase7', 'ready', %s)",
                (ids["run"], ids["definition"], now),
            )
            cursor.execute(
                "INSERT INTO agents "
                "(id, run_id, model_config_id, name, initial_cash_micros) "
                "VALUES (%s, %s, %s, 'phase7', 10000000000)",
                (ids["agent"], ids["run"], ids["model"]),
            )
            for cycle_key, agent_key, suffix in (
                ("cycle", "agent", "main"), ("other_cycle", "agent", "other")
            ):
                scheduled_at = now if cycle_key == "cycle" else now + timedelta(hours=1)
                cursor.execute(
                    "INSERT INTO agent_cycles "
                    "(id, agent_id, scheduled_at, data_cutoff, status, idempotency_key) "
                    "VALUES (%s, %s, %s, %s, 'running', %s)",
                    (
                        ids[cycle_key], ids[agent_key], scheduled_at, scheduled_at,
                        f"phase7:{marker}:{suffix}",
                    ),
                )
            cursor.execute(
                "INSERT INTO events (id, venue, venue_event_id, title, metadata, observed_at) "
                "VALUES (%s, 'test', %s, 'Phase 7', '{}'::jsonb, %s)",
                (ids["event"], str(marker), now),
            )
            cursor.execute(
                "INSERT INTO markets "
                "(id, event_id, venue, venue_market_id, slug, question, resolution_rules, "
                "status, observed_at, metadata) VALUES "
                "(%s, %s, 'test', %s, %s, 'Question?', 'Rules', 'open', %s, '{}'::jsonb)",
                (ids["market"], ids["event"], str(marker), f"phase7-{marker}", now),
            )
            pairs = zip(outcome_ids, position_ids, strict=True)
            for index, (outcome_id, position_id) in enumerate(pairs):
                cursor.execute(
                    "INSERT INTO outcomes "
                    "(id, market_id, venue_token_id, name, outcome_index, tick_size, "
                    "minimum_order_size) VALUES (%s, %s, %s, %s, %s, 0.01, 1)",
                    (outcome_id, ids["market"], f"{marker}-{index}", f"O{index}", index),
                )
                cursor.execute(
                    "INSERT INTO positions "
                    "(id, agent_id, outcome_id, shares, average_cost, cost_basis_micros, "
                    "realized_pnl_micros, updated_at) VALUES "
                    "(%s, %s, %s, 10, 0.5, 5000000, 0, %s)",
                    (position_id, ids["agent"], outcome_id, now),
                )


if __name__ == "__main__":
    unittest.main()
