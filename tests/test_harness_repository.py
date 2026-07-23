from __future__ import annotations

import unittest
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from vtrade.harness import BeliefRecord, HarnessResult, PlanRecord, PlanType, ToolCallRecord
from vtrade.harness_repository import PostgresHarnessRepository
from vtrade.providers import ProviderTelemetry
from vtrade.runtime import ArtifactRegistration

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
CYCLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class RecordingCursor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.harness_runs: dict[str, tuple[object, ...]] = {}
        self.beliefs: dict[str, tuple[object, ...]] = {}
        self.plans: dict[str, tuple[object, ...]] = {}
        self.active_belief_count = 0
        self.selected: tuple[object, ...] | None = None

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> RecordingCursor:
        values = tuple(params)
        self.queries.append((query, values))
        self.selected = None
        if query.startswith("SELECT id, transcript_sha256 FROM harness_runs"):
            self.selected = self.harness_runs.get(str(values[0]))
        elif query.startswith("INSERT INTO harness_runs"):
            self.harness_runs[str(values[9])] = (values[0], values[8])
        elif query.startswith("SELECT id, memory_fingerprint FROM beliefs"):
            self.selected = self.beliefs.get(str(values[0]))
        elif query.startswith("SELECT id FROM agents"):
            self.selected = (values[0],)
        elif query.startswith("SELECT count(*) FROM beliefs"):
            self.selected = (self.active_belief_count,)
        elif query.startswith("INSERT INTO beliefs"):
            self.beliefs[str(values[2])] = (values[0], values[3])
            self.active_belief_count += 1
        elif query.startswith("SELECT id, memory_fingerprint FROM plans"):
            self.selected = self.plans.get(str(values[0]))
        elif query.startswith("INSERT INTO plans"):
            self.plans[str(values[4])] = (values[0], values[5])
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        selected = self.selected
        self.selected = None
        return selected


class RecordingConnection:
    def __init__(self) -> None:
        self.cursor_instance = RecordingCursor()
        self.transaction_count = 0

    def __enter__(self) -> RecordingConnection:
        self.transaction_count += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


def harness_result() -> HarnessResult:
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
        artifact_uri="supabase://private/raw.json.gz",
        raw_sha256="a" * 64,
    )
    call = ToolCallRecord(
        id="call-1",
        name="web_search",
        arguments={"query": "forecast"},
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


class PostgresHarnessRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = RecordingConnection()
        self.repository = PostgresHarnessRepository(
            "postgresql://unused", connect=lambda _url: self.connection
        )

    def test_run_replay_is_idempotent_and_retains_raw_artifacts(self) -> None:
        retain_until = NOW + timedelta(days=183)
        artifact = ArtifactRegistration(
            "supabase://private/provider.json.gz", "c" * 64, 73, retain_until
        )
        first = self.repository.persist_run(
            agent_cycle_id=CYCLE_ID,
            result=harness_result(),
            transcript_uri="supabase://private/transcript.json.gz",
            transcript_sha256="b" * 64,
            completed_at=NOW,
            retain_until=retain_until,
            artifacts=(artifact,),
        )
        second = self.repository.persist_run(
            agent_cycle_id=CYCLE_ID,
            result=harness_result(),
            transcript_uri="supabase://private/transcript.json.gz",
            transcript_sha256="b" * 64,
            completed_at=NOW,
            retain_until=retain_until,
        )
        self.assertEqual(first, second)
        inserts = [
            (query, params)
            for query, params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO harness_runs")
        ]
        usages = [
            (query, params)
            for query, params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO provider_usage")
        ]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(len(usages), 1)
        self.assertEqual(inserts[0][1][10], retain_until)
        self.assertEqual(usages[0][1][-2], retain_until)
        inventory = next(
            params
            for query, params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO artifact_inventory")
        )
        self.assertEqual(inventory[4], 73)

    def test_total_model_turns_excludes_research_telemetry(self) -> None:
        result = harness_result()
        search = replace(
            result.telemetry[0],
            provider="exa",
            usage_kind="web_search",
            prompt_tokens=0,
            completion_tokens=0,
        )
        self.repository.persist_run(
            agent_cycle_id=CYCLE_ID,
            result=replace(result, telemetry=(*result.telemetry, search)),
            transcript_uri="supabase://private/transcript.json.gz",
            transcript_sha256="b" * 64,
            completed_at=NOW,
            retain_until=NOW + timedelta(days=183),
        )
        insert = next(
            params
            for query, params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO harness_runs")
        )
        self.assertEqual(insert[3], 1)

    def test_run_idempotency_conflict_and_invalid_retention_fail_closed(self) -> None:
        result = harness_result()
        transcript_uri = "supabase://private/transcript.json.gz"
        retain_until = NOW + timedelta(days=183)
        self.repository.persist_run(
            agent_cycle_id=CYCLE_ID,
            result=result,
            transcript_uri=transcript_uri,
            transcript_sha256="b" * 64,
            completed_at=NOW,
            retain_until=retain_until,
        )
        with self.assertRaises(ValueError):
            self.repository.persist_run(
                agent_cycle_id=CYCLE_ID,
                result=result,
                transcript_uri=transcript_uri,
                transcript_sha256="c" * 64,
                completed_at=NOW,
                retain_until=retain_until,
            )
        with self.assertRaises(ValueError):
            self.repository.persist_run(
                agent_cycle_id=uuid.uuid4(),
                result=harness_result(),
                transcript_uri="supabase://private/too-short.json.gz",
                transcript_sha256="d" * 64,
                completed_at=NOW,
                retain_until=NOW,
            )

    def test_memory_writes_are_private_idempotent_and_conflict_checked(self) -> None:
        belief = BeliefRecord(
            id=str(uuid.uuid4()),
            agent_id=str(AGENT_ID),
            confidence=Decimal("0.7"),
            content="Market likely resolves yes",
            category="event_analysis",
            evidence=("source-1",),
            created_at=NOW,
        )
        plan = PlanRecord(
            id=str(uuid.uuid4()),
            agent_id=str(AGENT_ID),
            plan_type=PlanType.NEXT_CYCLE,
            content="Recheck source",
            due_at=NOW + timedelta(hours=1),
            created_at=NOW,
        )
        self.repository.append_belief(belief, actor_id=AGENT_ID, cycle_id=CYCLE_ID)
        self.repository.append_belief(belief, actor_id=AGENT_ID, cycle_id=CYCLE_ID)
        self.repository.append_plan(plan, actor_id=AGENT_ID, cycle_id=CYCLE_ID)
        self.repository.append_plan(plan, actor_id=AGENT_ID, cycle_id=CYCLE_ID)
        inserts = [
            query
            for query, _params in self.connection.cursor_instance.queries
            if query.startswith(("INSERT INTO beliefs", "INSERT INTO plans"))
        ]
        self.assertEqual(len(inserts), 2)
        self.assertTrue(
            any(
                query.startswith("UPDATE plans SET status = 'superseded'")
                for query, _params in self.connection.cursor_instance.queries
            )
        )
        with self.assertRaises(ValueError):
            self.repository.append_belief(
                replace(belief, content="Different content"),
                actor_id=AGENT_ID,
                cycle_id=CYCLE_ID,
            )
        other = uuid.uuid4()
        with self.assertRaises(PermissionError):
            self.repository.append_plan(plan, actor_id=other, cycle_id=CYCLE_ID)
        with self.assertRaises(PermissionError):
            self.repository.read_beliefs(actor_id=AGENT_ID, target_agent_id=other)
        with self.assertRaises(PermissionError):
            self.repository.read_plans(actor_id=AGENT_ID, target_agent_id=other)

    def test_active_belief_quota_is_enforced(self) -> None:
        repository = PostgresHarnessRepository(
            "postgresql://unused",
            connect=lambda _url: self.connection,
            maximum_beliefs_per_agent=1,
        )
        first = BeliefRecord(
            id=str(uuid.uuid4()),
            agent_id=str(AGENT_ID),
            confidence=Decimal("0.7"),
            content="First belief",
            category="event_analysis",
            evidence=(),
            created_at=NOW,
        )
        second = replace(first, id=str(uuid.uuid4()), content="Second belief")
        repository.append_belief(first, actor_id=AGENT_ID, cycle_id=CYCLE_ID)
        with self.assertRaisesRegex(ValueError, "maximum active beliefs"):
            repository.append_belief(second, actor_id=AGENT_ID, cycle_id=CYCLE_ID)

    def test_belief_quota_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "maximum_beliefs_per_agent"):
            PostgresHarnessRepository("postgresql://unused", maximum_beliefs_per_agent=0)

    def test_phase_four_migration_contains_budget_replay_and_retention_tables(self) -> None:
        migration = Path("migrations/0004_model_research_harness.sql").read_text(encoding="utf-8")
        for required in (
            "monthly_provider_budgets",
            "provider_budget_reservations",
            "harness_runs",
            "harness_tool_records",
            "model_replay_records",
            "critical_learning_snapshots",
            "retain_until",
            "memory_fingerprint",
        ):
            self.assertIn(required, migration)


if __name__ == "__main__":
    unittest.main()
