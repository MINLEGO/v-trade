from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from vtrade.broker import (
    ExecutionStatus,
    FeePolicy,
    LiquidityTimeInForce,
    PaperOrder,
    PaperPolicy,
    PortfolioState,
    PredictionArenaPaperBroker,
)
from vtrade.config import ConfigurationError
from vtrade.domain.types import MarketStatus, MicroDollars, Side
from vtrade.providers import ProviderTelemetry
from vtrade.runtime import (
    ArtifactRegistration,
    BrokerExecutionResult,
    CycleClaim,
    CycleOrchestrator,
    CycleStage,
    HarnessExecutionResult,
    MarketFreezeResult,
    PreSettlementResult,
    PromptResult,
    RuntimeTickResult,
    SettlementValuationResult,
)
from vtrade.worker import (
    ProductionBrokerPort,
    ProductionCompositionUnavailable,
    ProductionHarnessPort,
    ProductionWorker,
    _harness_artifact_registrations,
    _liquidity_time_in_force,
    _paper_policy,
    _PostgresTradingState,
    run_worker,
)


def _write_config(directory: str, *, pending: bool) -> Path:
    tool_path = Path("spec/tool-schemas-v1.json")
    path = Path(directory) / "experiment.json"
    path.write_text(
        json.dumps(
            {
                "experiment_version": "worker-test-v1",
                "classifications": {},
                "limits": {},
                "artifacts": {
                    "tool_schemas": {
                        "path": str(tool_path),
                        "sha256": hashlib.sha256(tool_path.read_bytes()).hexdigest(),
                    }
                },
                "owner_decisions": {
                    "pagination": {
                        "status": "owner_pending" if pending else "resolved",
                        "required": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class WorkerFailClosedTests(unittest.TestCase):
    def test_execution_policy_parsers_accept_supported_values_and_reject_unknown_values(
        self,
    ) -> None:
        self.assertEqual(
            _paper_policy({"execution": {"paper_policy": "liquidity_aware"}}),
            PaperPolicy.LIQUIDITY_AWARE,
        )
        self.assertEqual(
            _paper_policy({"execution": {"paper_policy": "predictionarena_unconditional"}}),
            PaperPolicy.PREDICTIONARENA_UNCONDITIONAL,
        )
        with self.assertRaisesRegex(
            ProductionCompositionUnavailable, "unsupported paper policy"
        ):
            _paper_policy({"execution": {"paper_policy": "unknown"}})

        self.assertEqual(
            _liquidity_time_in_force({"execution": {"liquidity_time_in_force": "FAK"}}),
            LiquidityTimeInForce.FAK,
        )
        self.assertEqual(
            _liquidity_time_in_force({"execution": {"liquidity_time_in_force": "FOK"}}),
            LiquidityTimeInForce.FOK,
        )
        self.assertEqual(
            _liquidity_time_in_force({"execution": {}}), LiquidityTimeInForce.FAK
        )
        with self.assertRaisesRegex(
            ProductionCompositionUnavailable, "unsupported liquidity time in force"
        ):
            _liquidity_time_in_force({"execution": {"liquidity_time_in_force": "IOC"}})

    def test_production_broker_port_uses_configured_policy_and_tif(self) -> None:
        port = ProductionBrokerPort(
            "postgresql://unused",
            cast(Any, object()),
            clock=lambda: datetime(2026, 7, 18, 10, tzinfo=UTC),
            maximum_market_fraction=Decimal("0.15"),
            maximum_bid_age=timedelta(minutes=5),
            paper_policy=PaperPolicy.LIQUIDITY_AWARE,
            liquidity_time_in_force=LiquidityTimeInForce.FOK,
        )
        self.assertEqual(port._broker.policy, PaperPolicy.LIQUIDITY_AWARE)
        self.assertEqual(port._liquidity_time_in_force, LiquidityTimeInForce.FOK)

    def test_owner_decisions_fail_before_composition_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, pending=True)
            with self.assertRaisesRegex(ConfigurationError, "pagination"):
                run_worker(path)

    def test_injected_worker_runs_one_tick_without_constructing_external_clients(self) -> None:
        expected = RuntimeTickResult((), ())

        class Runtime:
            def tick(self):
                return expected

        class Retention:
            def __init__(self):
                self.called = False

            def run_once(self):
                self.called = True
                return ()

        class Projection:
            def calculate(self):
                raise AssertionError("one tick must not run the hourly projection")

        retention = Retention()
        worker = ProductionWorker(
            cast(Any, Runtime()),
            cast(Any, retention),
            cast(Any, Projection()),
            lambda: datetime(2026, 7, 18, tzinfo=UTC),
            lambda: 0.0,
            lambda _seconds: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, pending=False)
            self.assertEqual(run_worker(path, worker=worker), expected)
        self.assertTrue(retention.called)

    def test_harness_recovery_reuses_only_a_completed_persisted_run(self) -> None:
        now = datetime(2026, 7, 18, 10, tzinfo=UTC)
        run_id, intent_id = uuid.uuid4(), uuid.uuid4()

        class Cursor:
            rowcount = 0

            def __init__(self):
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, query, _params=()):
                if "FROM harness_runs" in query:
                    self.rows = [(run_id, "completed", 7, 3, "transcript", "a" * 64, now)]
                elif "FROM artifact_inventory" in query:
                    self.rows = [
                        ("transcript", "a" * 64, 41, now),
                        ("provider", "b" * 64, 73, now),
                    ]
                elif "FROM order_intents" in query:
                    self.rows = [(intent_id,)]
                else:
                    raise AssertionError(query)

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return tuple(self.rows)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

        port = cast(Any, ProductionHarnessPort.__new__(ProductionHarnessPort))
        port._database_url = "postgresql://unused"
        port._connect = lambda _url: Connection()
        claim = CycleClaim(
            uuid.uuid4(),
            uuid.uuid4(),
            now,
            now,
            "recovery",
            now + timedelta(minutes=10),
            recovery=True,
        )
        result = port.run(claim, {}, {})
        self.assertEqual(result.payload["harness_run_id"], str(run_id))
        self.assertEqual(result.payload["intent_ids"], [str(intent_id)])
        self.assertEqual((result.tool_calls, result.exa_searches), (7, 3))
        self.assertEqual({item.uri for item in result.artifacts}, {"transcript", "provider"})
        self.assertEqual(
            {item.uri: item.byte_length for item in result.artifacts},
            {"transcript": 41, "provider": 73},
        )

    def test_harness_recovery_without_completed_run_fails_before_provider_access(self) -> None:
        now = datetime(2026, 7, 18, 10, tzinfo=UTC)

        class Cursor:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, _query, _params=()):
                return None

            def fetchone(self):
                return None

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

        port = cast(Any, ProductionHarnessPort.__new__(ProductionHarnessPort))
        port._database_url = "postgresql://unused"
        port._connect = lambda _url: Connection()
        claim = CycleClaim(
            uuid.uuid4(),
            uuid.uuid4(),
            now,
            now,
            "recovery",
            now + timedelta(minutes=10),
            recovery=True,
        )
        with self.assertRaisesRegex(
            ProductionCompositionUnavailable, "provider replay is forbidden"
        ):
            port.run(claim, {}, {})

    def test_successful_harness_inventory_uses_real_provider_artifact_lengths(self) -> None:
        now = datetime(2026, 7, 18, 10, tzinfo=UTC)
        transcript = SimpleNamespace(uri="transcript", sha256="a" * 64, byte_length=41)
        telemetry = ProviderTelemetry(
            "openrouter",
            "model",
            None,
            1,
            Decimal(0),
            10,
            5,
            2,
            0,
            1,
            1,
            20,
            "provider",
            "b" * 64,
            73,
        )
        registrations = _harness_artifact_registrations(transcript, (telemetry,), now)
        self.assertEqual(
            {item.uri: item.byte_length for item in registrations},
            {"transcript": 41, "provider": 73},
        )

    def test_archived_valuation_bid_ignores_current_membership_but_is_causal_and_fresh(
        self,
    ) -> None:
        now = datetime(2026, 7, 18, 10, tzinfo=UTC)
        outcome_id, snapshot_id = uuid.uuid4(), uuid.uuid4()

        class Cursor:
            rowcount = 0

            def __init__(self):
                self.query = ""
                self.params = ()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, query, params=()):
                self.query = query
                self.params = tuple(params)

            def fetchall(self):
                return ((outcome_id, "0.41", now - timedelta(seconds=20), snapshot_id),)

        cursor = Cursor()

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return cursor

        state = _PostgresTradingState("postgresql://unused", connect=lambda _url: Connection())
        portfolio = SimpleNamespace(positions=(SimpleNamespace(outcome_id=str(outcome_id)),))
        bids, ids = state.archived_executable_bids(
            cast(Any, portfolio), cutoff=now, maximum_bid_age=timedelta(seconds=300)
        )
        self.assertEqual(bids[str(outcome_id)].price, Decimal("0.41"))
        self.assertEqual(ids, (snapshot_id,))
        self.assertIn("best_bid IS NOT NULL", cursor.query)
        self.assertNotIn("obs.id = ANY", cursor.query)
        self.assertEqual(cursor.params[1:], (now, now - timedelta(seconds=300), now))

    def test_broker_propagates_only_same_cycle_book_and_fee_memberships(self) -> None:
        cycle_id, agent_id = uuid.uuid4(), uuid.uuid4()
        cutoff = datetime(2026, 7, 18, 10, tzinfo=UTC)
        claim = CycleClaim(
            cycle_id,
            agent_id,
            cutoff,
            cutoff,
            "worker",
            cutoff.replace(minute=10),
        )
        intent_id, market_id, outcome_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        book_id, fee_id, market_snapshot_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        item = SimpleNamespace(
            intent_id=intent_id,
            market_id=market_id,
            outcome_id=outcome_id,
            outcome=SimpleNamespace(venue_token_id="token"),
            order=PaperOrder(
                str(intent_id),
                str(agent_id),
                str(market_id),
                str(outcome_id),
                Side.BUY,
                Decimal("1"),
                cutoff,
            ),
            market=object(),
            book=object(),
            book_snapshot_id=book_id,
        )

        class State:
            seen_books = None

            def persisted_harness_intents(self, _claim, harness):
                self.harness = harness
                return {intent_id}

            def pending_intents(self, _claim, frozen):
                self.frozen = frozen
                return (item,)

            def portfolio(self, _agent_id):
                return object()

            def executable_bids(self, _portfolio, *, cutoff, order_book_snapshot_ids):
                self.seen_books = tuple(order_book_snapshot_ids)
                return {}

        class MarketRepository:
            seen_fees = None

            def frozen_fee_policy(self, token, *, cutoff, fee_rate_snapshot_ids):
                self.seen_fees = tuple(fee_rate_snapshot_ids)
                return FeePolicy(Decimal(0))

        class Broker:
            seen_order = None

            def place(self, *_args, **_kwargs):
                self.seen_order = _args[0]
                return SimpleNamespace(
                    status=ExecutionStatus.FILLED,
                    rejection_code=None,
                )

        class Repository:
            def persist_execution(self, *_args, **_kwargs):
                return SimpleNamespace(record_id=uuid.uuid4())

        state, market_repository = State(), MarketRepository()
        port = cast(Any, ProductionBrokerPort.__new__(ProductionBrokerPort))
        port._state = state
        port._market_repository = market_repository
        port._broker = Broker()
        port._repository = Repository()
        port._clock = lambda: cutoff
        port._liquidity_time_in_force = LiquidityTimeInForce.FAK
        frozen = {
            "market_snapshot_ids": [str(market_snapshot_id)],
            "order_book_snapshot_ids": [str(book_id)],
            "fee_rate_snapshot_ids": [str(fee_id)],
        }
        result = port.execute(
            claim,
            frozen,
            {"harness_run_id": str(uuid.uuid4()), "intent_ids": [str(intent_id)]},
        )
        self.assertEqual(result.accepted_trades, 1)
        self.assertEqual(state.seen_books, (book_id,))
        self.assertEqual(market_repository.seen_fees, (fee_id,))
        self.assertEqual(port._broker.seen_order.liquidity_time_in_force, LiquidityTimeInForce.FAK)

    def test_production_broker_port_returns_liquidity_aware_partial_fill(self) -> None:
        cycle_id, agent_id = uuid.uuid4(), uuid.uuid4()
        cutoff = datetime(2026, 7, 18, 10, tzinfo=UTC)
        intent_id, market_id, outcome_id, book_id, fee_id = (
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
        )
        claim = CycleClaim(
            cycle_id,
            agent_id,
            cutoff,
            cutoff,
            "worker",
            cutoff + timedelta(minutes=10),
        )
        order = PaperOrder(
            str(intent_id),
            str(agent_id),
            str(market_id),
            str(outcome_id),
            Side.BUY,
            Decimal("5"),
            cutoff,
        )
        item = SimpleNamespace(
            intent_id=intent_id,
            market_id=market_id,
            outcome_id=outcome_id,
            order=order,
            market=SimpleNamespace(
                id=str(market_id),
                status=MarketStatus.OPEN,
                opens_at=cutoff - timedelta(days=1),
                closes_at=cutoff + timedelta(days=1),
                tradeable=True,
                observed_at=cutoff - timedelta(seconds=2),
            ),
            outcome=SimpleNamespace(
                id=str(outcome_id),
                market_id=str(market_id),
                venue_token_id="token",
                tradeable=True,
            ),
            book=SimpleNamespace(
                token_id="token",
                observed_at=cutoff - timedelta(seconds=1),
                source_created_at=cutoff - timedelta(seconds=1),
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.40"),
                bids=(SimpleNamespace(price=Decimal("0.39"), size=Decimal("10")),),
                asks=(
                    SimpleNamespace(price=Decimal("0.40"), size=Decimal("1")),
                    SimpleNamespace(price=Decimal("0.41"), size=Decimal("2")),
                ),
                tick_size=Decimal("0.01"),
                minimum_order_size=Decimal("1"),
            ),
            book_snapshot_id=book_id,
        )

        class State:
            def persisted_harness_intents(self, _claim, _harness):
                return {intent_id}

            def pending_intents(self, _claim, _frozen):
                return (item,)

            def portfolio(self, _agent_id):
                return PortfolioState(str(agent_id), MicroDollars(10_000_000_000))

            def executable_bids(self, _portfolio, *, cutoff, order_book_snapshot_ids):
                return {}

        class MarketRepository:
            def frozen_fee_policy(self, _token, *, cutoff, fee_rate_snapshot_ids):
                return FeePolicy(Decimal("0"))

        class Repository:
            result = None

            def persist_execution(self, result, **_kwargs):
                self.result = result
                return SimpleNamespace(record_id=uuid.uuid4())

        repository = Repository()
        port = cast(Any, ProductionBrokerPort.__new__(ProductionBrokerPort))
        port._state = State()
        port._market_repository = MarketRepository()
        port._repository = repository
        port._broker = PredictionArenaPaperBroker(policy=PaperPolicy.LIQUIDITY_AWARE)
        port._liquidity_time_in_force = LiquidityTimeInForce.FAK
        port._clock = lambda: cutoff

        result = port.execute(
            claim,
            {
                "order_book_snapshot_ids": [str(book_id)],
                "fee_rate_snapshot_ids": [str(fee_id)],
            },
            {"harness_run_id": str(uuid.uuid4()), "intent_ids": [str(intent_id)]},
        )

        self.assertEqual(result.accepted_trades, 1)
        self.assertEqual(repository.result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(repository.result.policy, PaperPolicy.LIQUIDITY_AWARE)
        self.assertEqual(
            repository.result.order.liquidity_time_in_force,
            LiquidityTimeInForce.FAK,
        )
        self.assertEqual(
            [(fill.shares, fill.price) for fill in repository.result.fills],
            [(Decimal("1"), Decimal("0.40")), (Decimal("2"), Decimal("0.41"))],
        )
        self.assertEqual(repository.result.portfolio.position(str(outcome_id)).shares, Decimal("3"))

    def test_offline_cycle_graph_replays_completed_checkpoints_without_side_effects(self) -> None:
        now = datetime(2026, 7, 18, 10, tzinfo=UTC)
        agent_id, cycle_id, intent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        artifact = ArtifactRegistration(
            "supabase://private/aa/" + "a" * 64 + ".json.gz",
            "a" * 64,
            10,
            datetime(2027, 1, 18, 10, tzinfo=UTC),
        )

        class Repository:
            def __init__(self):
                self.stages = {}
                self.completed = 0

            def load_stage(self, cycle_id, stage):
                return self.stages.get(stage)

            def renew_lease(self, *_args, **_kwargs):
                return None

            def begin_stage(self, *_args, **_kwargs):
                return None

            def complete_stage(self, _claim, stage, _fingerprint, result, **_kwargs):
                self.stages[stage] = result

            def complete_cycle(self, *_args, **_kwargs):
                self.completed += 1

            def fail_cycle(self, *_args, **_kwargs):
                raise AssertionError("offline graph must not fail")

            def open_alert(self, _alert):
                return None

        calls = {
            "freeze": 0,
            "pre_settle": 0,
            "prompt": 0,
            "harness": 0,
            "broker": 0,
            "settle": 0,
        }
        portfolio = {"resolved_position_settled": False}

        class Freezer:
            def freeze(self, claim):
                calls["freeze"] += 1
                self.assert_claim = claim
                return MarketFreezeResult(
                    {
                        "market_snapshot_ids": [str(uuid.uuid4())],
                        "order_book_snapshot_ids": [str(uuid.uuid4())],
                        "fee_rate_snapshot_ids": [str(uuid.uuid4())],
                        "resolution_ids": [],
                    },
                    (artifact,),
                    now,
                )

        class Prompt:
            def render(self, claim, frozen):
                calls["prompt"] += 1
                if not portfolio["resolved_position_settled"]:
                    raise AssertionError("prompt observed the pre-settlement portfolio too early")
                self.assert_cutoff = claim.data_cutoff
                self.assert_frozen = frozen
                return PromptResult({"prompt_sha256": "b" * 64}, (artifact,), 100)

        class Harness:
            def run(self, claim, frozen, prompt):
                calls["harness"] += 1
                self.inputs = (claim, frozen, prompt)
                return HarnessExecutionResult({"intent_ids": [str(intent_id)]}, (artifact,), 1, 2)

        class Broker:
            def execute(self, claim, frozen, harness):
                calls["broker"] += 1
                self.assert_intent = harness["intent_ids"]
                return BrokerExecutionResult({"order_ids": [str(uuid.uuid4())]}, (), 1)

        class Settlement:
            def settle_before_prompt(self, claim, frozen):
                calls["pre_settle"] += 1
                portfolio["resolved_position_settled"] = True
                self.pre_inputs = (claim, frozen)
                return PreSettlementResult(
                    {"settlement_ids": [], "settlement_cutoff": claim.data_cutoff.isoformat()},
                    (),
                    0,
                )

            def settle_and_value(self, claim, frozen, broker):
                calls["settle"] += 1
                self.inputs = (claim, frozen, broker)
                return SettlementValuationResult(
                    {"valuation_cutoff": claim.data_cutoff.isoformat()},
                    (),
                    10_100_000_000,
                    10_100_000_000,
                    0,
                )

        repository = Repository()
        settlement = Settlement()
        orchestrator = CycleOrchestrator(
            repository=cast(Any, repository),
            market_freezer=cast(Any, Freezer()),
            pre_settlement=cast(Any, settlement),
            prompt=cast(Any, Prompt()),
            harness=cast(Any, Harness()),
            broker=cast(Any, Broker()),
            settlement_valuation=cast(Any, settlement),
            clock=lambda: now,
        )
        initial = CycleClaim(
            cycle_id,
            agent_id,
            now,
            None,
            "worker",
            now.replace(minute=10),
        )
        summary = orchestrator.run(initial)
        self.assertEqual(summary[CycleStage.HARNESS.value]["intent_ids"], [str(intent_id)])
        self.assertEqual(
            calls,
            {
                "freeze": 1,
                "pre_settle": 1,
                "prompt": 1,
                "harness": 1,
                "broker": 1,
                "settle": 1,
            },
        )

        recovered = CycleClaim(
            cycle_id,
            agent_id,
            now,
            now,
            "worker-recovery",
            now.replace(minute=10),
            recovery=True,
        )
        replayed = orchestrator.run(recovered)
        self.assertEqual(replayed, summary)
        self.assertEqual(
            calls,
            {
                "freeze": 1,
                "pre_settle": 1,
                "prompt": 1,
                "harness": 1,
                "broker": 1,
                "settle": 1,
            },
        )
        self.assertEqual(repository.completed, 2)

    def test_runnable_config_refuses_missing_production_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, pending=False)
            with self.assertRaisesRegex(
                ProductionCompositionUnavailable,
                "missing REQUIRED environment resources",
            ):
                run_worker(path)


if __name__ == "__main__":
    unittest.main()
