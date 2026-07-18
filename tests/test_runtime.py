from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from vtrade.runtime import (
    AlertEvent,
    BrokerExecutionResult,
    CycleClaim,
    CycleOrchestrator,
    CycleStage,
    ExpiredArtifact,
    HarnessExecutionResult,
    HourlyRuntime,
    MarketFreezeResult,
    PreSettlementResult,
    ProjectionInputs,
    PromptResult,
    RetentionCleaner,
    RuntimeConfigurationError,
    RuntimeProjection,
    SettlementValuationResult,
    StageResult,
    restore_stage,
    six_month_retain_until,
    stage_checkpoint,
)

NOW = datetime(2026, 7, 16, 10, 5, tzinfo=UTC)


class MemoryRuntimeRepository:
    def __init__(self) -> None:
        self.stages: dict[tuple[uuid.UUID, CycleStage], StageResult] = {}
        self.alerts: list[AlertEvent] = []
        self.failed: list[str] = []
        self.completed = 0
        self.expired: tuple[ExpiredArtifact, ...] = ()
        self.purge_calls = 0
        self.deletion_results: list[tuple[uuid.UUID, str | None]] = []
        self.due: tuple[CycleClaim, ...] = ()

    def claim_due_cycles(self, **_kwargs: object) -> tuple[CycleClaim, ...]:
        return self.due

    def recover_expired_cycles(self, **_kwargs: object) -> tuple[CycleClaim, ...]:
        return ()

    def renew_lease(self, *_args: object, **_kwargs: object) -> None:
        return None

    def load_stage(self, cycle_id: uuid.UUID, stage: CycleStage) -> StageResult | None:
        stored = self.stages.get((cycle_id, stage))
        if stored is None:
            return None
        return restore_stage(stage, stage_checkpoint(stored))

    def begin_stage(self, *_args: object, **_kwargs: object) -> None:
        return None

    def complete_stage(
        self,
        claim: CycleClaim,
        stage: CycleStage,
        _fingerprint: str,
        result: StageResult,
        **_kwargs: object,
    ) -> None:
        self.stages[(claim.cycle_id, stage)] = result

    def complete_cycle(self, *_args: object, **_kwargs: object) -> None:
        self.completed += 1

    def fail_cycle(self, *_args: object, **kwargs: object) -> int:
        self.failed.append(cast(str, kwargs["reason"]))
        return len(self.failed)

    def open_alert(self, alert: AlertEvent) -> None:
        self.alerts.append(alert)

    def record_projection(self, _projection: RuntimeProjection) -> None:
        return None

    def projection_inputs(self, **_kwargs: object) -> ProjectionInputs:
        return ProjectionInputs(0, 0, 0, 0)

    def claim_expired_artifacts(self, **_kwargs: object) -> tuple[ExpiredArtifact, ...]:
        return self.expired

    def purge_expired_payloads(self, **_kwargs: object) -> int:
        self.purge_calls += 1
        return 0

    def complete_artifact_deletion(self, artifact: ExpiredArtifact, **kwargs: object) -> None:
        self.deletion_results.append((artifact.inventory_id, cast(str | None, kwargs["error"])))


class Ports:
    def __init__(self, *, fail_after_broker_side_effect: bool = False) -> None:
        self.calls: list[str] = []
        self.financial_events: set[uuid.UUID] = set()
        self.fail_after_broker_side_effect = fail_after_broker_side_effect

    def freeze(self, _claim: CycleClaim) -> MarketFreezeResult:
        self.calls.append("market_freeze")
        return MarketFreezeResult({"snapshot": "frozen"}, (), NOW - timedelta(minutes=1))

    def render(self, _claim: CycleClaim, _frozen: dict[str, Any]) -> PromptResult:
        self.calls.append("prompt")
        return PromptResult({"messages": []}, (), 10_000)

    def settle_before_prompt(
        self, _claim: CycleClaim, _frozen: dict[str, Any]
    ) -> PreSettlementResult:
        self.calls.append("pre_settlement")
        return PreSettlementResult({"settlement_ids": []}, (), 0)

    def run(
        self, _claim: CycleClaim, _frozen: dict[str, Any], _prompt: dict[str, Any]
    ) -> HarnessExecutionResult:
        self.calls.append("harness")
        return HarnessExecutionResult({"intents": []}, (), 13, 20)

    def execute(
        self, claim: CycleClaim, _frozen: dict[str, Any], _harness: dict[str, Any]
    ) -> BrokerExecutionResult:
        self.calls.append("broker")
        created = claim.cycle_id not in self.financial_events
        self.financial_events.add(claim.cycle_id)
        if created and self.fail_after_broker_side_effect:
            self.fail_after_broker_side_effect = False
            raise RuntimeError("Authorization: Bearer secret-token")
        return BrokerExecutionResult({"orders": [str(claim.cycle_id)]}, (), 1)

    def settle_and_value(
        self, _claim: CycleClaim, _frozen: dict[str, Any], _broker: dict[str, Any]
    ) -> SettlementValuationResult:
        self.calls.append("settlement_valuation")
        return SettlementValuationResult({"valued": True}, (), 8_000, 10_000, 0)


def claim() -> CycleClaim:
    return CycleClaim(uuid.uuid4(), uuid.uuid4(), NOW, NOW, "worker-1", NOW + timedelta(minutes=10))


class RuntimeTests(unittest.TestCase):
    def test_runtime_rejects_a_lease_shorter_than_the_cycle_wall_clock(self) -> None:
        repository = MemoryRuntimeRepository()
        ports = Ports()
        with self.assertRaisesRegex(ValueError, "3300-second"):
            CycleOrchestrator(
                repository=repository,
                market_freezer=ports,
                pre_settlement=ports,
                prompt=ports,
                harness=ports,
                broker=ports,
                settlement_valuation=ports,
                clock=lambda: NOW,
                lease_duration=timedelta(seconds=3_299),
            )
        orchestrator = CycleOrchestrator(
            repository=repository,
            market_freezer=ports,
            pre_settlement=ports,
            prompt=ports,
            harness=ports,
            broker=ports,
            settlement_valuation=ports,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "3300-second"):
            HourlyRuntime(
                repository=repository,
                orchestrator=orchestrator,
                lease_owner="worker-1",
                clock=lambda: NOW,
                lease_duration=timedelta(seconds=3_299),
            )

    def test_ordered_boundaries_and_restart_do_not_duplicate_financial_event(self) -> None:
        repository = MemoryRuntimeRepository()
        ports = Ports(fail_after_broker_side_effect=True)
        orchestrator = CycleOrchestrator(
            repository=repository,
            market_freezer=ports,
            pre_settlement=ports,
            prompt=ports,
            harness=ports,
            broker=ports,
            settlement_valuation=ports,
            clock=lambda: NOW,
        )
        cycle = claim()
        with self.assertRaises(RuntimeError):
            orchestrator.run(cycle)
        self.assertNotIn("secret-token", repository.failed[0])
        orchestrator.run(cycle)
        self.assertEqual(len(ports.financial_events), 1)
        self.assertEqual(
            ports.calls,
            [
                "market_freeze",
                "pre_settlement",
                "prompt",
                "harness",
                "broker",
                "broker",
                "settlement_valuation",
            ],
        )

    def test_rehydrated_checkpoints_still_raise_operational_alerts(self) -> None:
        repository = MemoryRuntimeRepository()
        ports = Ports()
        cycle = claim()
        repository.stages.update(
            {
                (cycle.cycle_id, CycleStage.MARKET_FREEZE): MarketFreezeResult(
                    {"snapshot": "old"}, (), NOW - timedelta(minutes=20)
                ),
                (cycle.cycle_id, CycleStage.PRE_SETTLEMENT): PreSettlementResult(
                    {"settlement_ids": []}, (), 0
                ),
                (cycle.cycle_id, CycleStage.PROMPT): PromptResult({"messages": []}, (), 9_012),
                (cycle.cycle_id, CycleStage.HARNESS): HarnessExecutionResult({}, (), 13, 15),
                (cycle.cycle_id, CycleStage.BROKER): BrokerExecutionResult({}, (), 0),
                (cycle.cycle_id, CycleStage.SETTLEMENT_VALUATION): SettlementValuationResult(
                    {}, (), 6_000, 10_000, 25
                ),
            }
        )
        CycleOrchestrator(
            repository=repository,
            market_freezer=ports,
            pre_settlement=ports,
            prompt=ports,
            harness=ports,
            broker=ports,
            settlement_valuation=ports,
            clock=lambda: NOW,
        ).run(cycle)
        self.assertEqual(ports.calls, [])
        self.assertEqual(
            {alert.code for alert in repository.alerts},
            {"stale_market_data", "ledger_mismatch", "abnormal_drawdown"},
        )

    def test_six_calendar_month_retention_handles_month_ends(self) -> None:
        created = datetime(2026, 8, 31, 12, tzinfo=UTC)
        self.assertEqual(six_month_retain_until(created), datetime(2027, 2, 28, 12, tzinfo=UTC))

    def test_retention_purges_database_payloads_before_deleting_storage(self) -> None:
        repository = MemoryRuntimeRepository()
        artifact = ExpiredArtifact(uuid.uuid4(), "supabase://private/aa/object", "a" * 64)
        repository.expired = (artifact,)

        class Deletion:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete(self, uri: str, _sha256: str) -> None:
                self.deleted.append(uri)

        deletion = Deletion()
        result = RetentionCleaner(
            repository=repository,
            deletion=deletion,
            lease_owner="retention-1",
            clock=lambda: NOW,
        ).run_once()
        self.assertEqual(repository.purge_calls, 1)
        self.assertEqual(result, (artifact.inventory_id,))
        self.assertEqual(deletion.deleted, [artifact.uri])
        self.assertEqual(repository.deletion_results, [(artifact.inventory_id, None)])

    def test_one_agent_failure_does_not_block_another_due_agent(self) -> None:
        repository = MemoryRuntimeRepository()
        first, second = claim(), claim()
        repository.due = (first, second)
        ports = Ports(fail_after_broker_side_effect=True)
        orchestrator = CycleOrchestrator(
            repository=repository,
            market_freezer=ports,
            pre_settlement=ports,
            prompt=ports,
            harness=ports,
            broker=ports,
            settlement_valuation=ports,
            clock=lambda: NOW,
        )
        result = HourlyRuntime(
            repository=repository,
            orchestrator=orchestrator,
            lease_owner="worker-1",
            clock=lambda: NOW,
        ).tick()
        self.assertEqual(result.processed_cycle_ids, (second.cycle_id,))
        self.assertEqual(result.failures[0][0], first.cycle_id)


if __name__ == "__main__":
    unittest.main()
