from __future__ import annotations

import unittest
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vtrade.postgres_runtime import PostgresRuntimeRepository
from vtrade.runtime import (
    ArtifactRegistration,
    CycleClaim,
    CycleStage,
    MarketFreezeResult,
    StageResult,
)

NOW = datetime(2026, 7, 16, 10, 5, tzinfo=UTC)
ANCHOR = datetime(2026, 7, 16, 7, 0, tzinfo=UTC)


class SchedulingCursor:
    def __init__(self) -> None:
        self.rowcount = 1
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> SchedulingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> SchedulingCursor:
        values = tuple(params)
        self.queries.append((query, values))
        if query.startswith("SELECT pg_try_advisory"):
            self.rows = [(True,)]
        elif query.startswith("SELECT 1 FROM agent_cycles"):
            self.rows = [(1,)]
        elif query.startswith("SELECT schedules.agent_id"):
            self.rows = [(AGENT_ID, ANCHOR)]
        elif "RETURNING id" in query:
            self.rows = [(CYCLE_ID,)]
        else:
            self.rows = []
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self.rows = self.rows, []
        return rows


class SchedulingConnection:
    def __init__(self) -> None:
        self.cursor_instance = SchedulingCursor()

    def __enter__(self) -> SchedulingConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> SchedulingCursor:
        return self.cursor_instance


AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000051")
CYCLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000052")


class PostgresSchedulingTests(unittest.TestCase):
    def test_overdue_slots_are_atomically_skipped_and_only_current_slot_is_claimed(self) -> None:
        connection = SchedulingConnection()
        repository = PostgresRuntimeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        claims = repository.claim_due_cycles(
            now=NOW,
            lease_owner="worker-1",
            lease_duration=timedelta(minutes=10),
            missed_grace=timedelta(minutes=10),
            limit=10,
        )
        self.assertEqual([item.scheduled_at for item in claims], [NOW.replace(minute=0)])
        skipped = next(
            params
            for query, params in connection.cursor_instance.queries
            if "generate_series" in query
        )
        self.assertEqual(skipped[-2:], (ANCHOR, NOW.replace(hour=9, minute=0)))
        advances = [
            params
            for query, params in connection.cursor_instance.queries
            if query.startswith("UPDATE agent_runtime_schedules")
        ]
        self.assertEqual(advances, [(NOW.replace(hour=11, minute=0), AGENT_ID)])
        self.assertEqual(len(claims), 1)
        self.assertIsNone(claims[0].data_cutoff)
        schedule_query = next(
            query
            for query, _params in connection.cursor_instance.queries
            if query.startswith("SELECT schedules.agent_id")
        )
        self.assertIn("controls.globally_paused = false", schedule_query)
        self.assertIn("NOT EXISTS (SELECT 1 FROM agent_cycles active", schedule_query)

    def test_phase_five_migration_contains_runtime_guards(self) -> None:
        migration = Path("migrations/0005_runtime_operations.sql").read_text(encoding="utf-8")
        for required in (
            "agent_runtime_schedules",
            "runtime_cycle_steps",
            "artifact_inventory",
            "runtime_projections",
            "lease_expires_at",
            "alerts_open_dedupe_idx",
        ):
            self.assertIn(required, migration)

    def test_actual_freeze_cutoff_migration_allows_only_temporary_null(self) -> None:
        migration = Path("migrations/0008_actual_freeze_cutoff.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("ALTER COLUMN data_cutoff DROP NOT NULL", migration)
        self.assertIn("data_cutoff >= scheduled_at", migration)
        self.assertIn("market_freeze", migration)

    def test_market_checkpoint_atomically_finalizes_actual_cutoff(self) -> None:
        connection = SchedulingConnection()
        repository = PostgresRuntimeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        claim = CycleClaim(
            CYCLE_ID,
            AGENT_ID,
            NOW.replace(minute=0),
            None,
            "worker-1",
            NOW + timedelta(minutes=10),
        )
        repository.complete_stage(
            claim,
            CycleStage.MARKET_FREEZE,
            "a" * 64,
            MarketFreezeResult({}, (), NOW),
            now=NOW,
        )
        writes = [query for query, _params in connection.cursor_instance.queries]
        checkpoint = next(
            index
            for index, query in enumerate(writes)
            if query.startswith("UPDATE runtime_cycle_steps SET status = 'completed'")
        )
        cutoff = next(
            index
            for index, query in enumerate(writes)
            if query.startswith("UPDATE agent_cycles SET data_cutoff")
        )
        self.assertLess(checkpoint, cutoff)
        cutoff_params = connection.cursor_instance.queries[cutoff][1]
        self.assertEqual(cutoff_params, (NOW, CYCLE_ID, "worker-1"))

    def test_registering_an_existing_deleted_artifact_reactivates_it(self) -> None:
        connection = SchedulingConnection()
        repository = PostgresRuntimeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        claim = CycleClaim(
            CYCLE_ID,
            AGENT_ID,
            NOW.replace(minute=0),
            NOW,
            "worker-1",
            NOW + timedelta(minutes=70),
        )
        artifact = ArtifactRegistration(
            "memory://transcript",
            "b" * 64,
            42,
            datetime(2027, 2, 1, tzinfo=UTC),
        )

        repository.complete_stage(
            claim,
            CycleStage.PROMPT,
            "c" * 64,
            StageResult({}, (artifact,)),
            now=NOW,
        )

        upsert = next(
            query
            for query, _params in connection.cursor_instance.queries
            if query.startswith("INSERT INTO artifact_inventory")
        )
        for reactivation in (
            "status = 'active'",
            "lease_owner = NULL",
            "lease_expires_at = NULL",
            "deletion_error = NULL",
            "deleted_at = NULL",
        ):
            self.assertIn(reactivation, upsert)

    def test_artifact_retention_uses_deterministic_cycle_schedule_lower_bound(self) -> None:
        connection = SchedulingConnection()
        repository = PostgresRuntimeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        scheduled = NOW.replace(minute=0)
        claim = CycleClaim(
            CYCLE_ID,
            AGENT_ID,
            scheduled,
            NOW,
            "worker-1",
            NOW + timedelta(minutes=70),
        )
        artifact = ArtifactRegistration(
            "memory://freeze",
            "d" * 64,
            42,
            scheduled.replace(year=2027, month=1),
        )

        repository.complete_stage(
            claim,
            CycleStage.MARKET_FREEZE,
            "e" * 64,
            StageResult({}, (artifact,)),
            now=NOW,
        )

        inventory = next(
            params
            for query, params in connection.cursor_instance.queries
            if query.startswith("INSERT INTO artifact_inventory")
        )
        self.assertEqual(inventory[6], datetime(2027, 1, 16, 10, 5, tzinfo=UTC))

    def test_artifact_registration_extends_retention_across_month_boundary(self) -> None:
        connection = SchedulingConnection()
        repository = PostgresRuntimeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        scheduled = datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)
        registered = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
        claim = CycleClaim(
            CYCLE_ID,
            AGENT_ID,
            scheduled,
            registered,
            "worker-1",
            registered + timedelta(minutes=70),
        )
        artifact = ArtifactRegistration(
            "memory://month-boundary",
            "f" * 64,
            42,
            datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC),
        )

        repository.complete_stage(
            claim,
            CycleStage.PROMPT,
            "a" * 64,
            StageResult({}, (artifact,)),
            now=registered,
        )

        inventory = next(
            params
            for query, params in connection.cursor_instance.queries
            if query.startswith("INSERT INTO artifact_inventory")
        )
        self.assertEqual(inventory[6], datetime(2026, 8, 1, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
