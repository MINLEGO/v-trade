from __future__ import annotations

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
from vtrade.harness import RecentActivityEvent
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
    ProductionPromptPort,
    ProductionWorker,
    _harness_artifact_registrations,
    _ignored_best_levels,
    _liquidity_time_in_force,
    _maximum_ignored_depth_fraction,
    _maximum_order_book_age,
    _order_book_depth,
    _paper_policy,
    _positive_integer,
    _PostgresTradingState,
    run_worker,
)

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)


def _write_config(directory: str, *, pending: bool) -> Path:
    raw = json.loads(Path("config/experiments/vtrade-kalshi-v1.json").read_text(encoding="utf-8"))
    raw["owner_decisions"]["pagination"] = {
        "status": "owner_pending" if pending else "resolved",
        "required": True,
    }
    path = Path(directory) / "experiment.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class _PromptCursor:
    def __init__(
        self,
        *,
        account_row: tuple[object, ...],
        position_rows: list[tuple[object, ...]],
        settlement_rows: list[tuple[object, ...]] | None = None,
        rejection_rows: list[tuple[object, ...]] | None = None,
        previous_cutoff: datetime | None = None,
        settlement_summary: tuple[object, ...] | None = None,
        rejection_summary_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.account_row = account_row
        self.position_rows = position_rows
        self.settlement_rows = settlement_rows or []
        self.rejection_rows = rejection_rows or []
        self.previous_cutoff = previous_cutoff
        self.settlement_summary = settlement_summary or (
            len(self.settlement_rows),
            sum(int(str(row[3])) for row in self.settlement_rows),
        )
        if rejection_summary_rows is None:
            counts: dict[str, int] = {}
            for row in self.rejection_rows:
                code = "" if row[5] is None else str(row[5]).strip()
                code = code or "unknown"
                counts[code] = counts.get(code, 0) + 1
            rejection_summary_rows = [(code, count) for code, count in counts.items()]
        self.rejection_summary_rows = rejection_summary_rows
        self.rows: list[tuple[object, ...]] = []
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params=()):
        self.queries.append((query, tuple(params)))
        if query.startswith("SELECT COALESCE(sum(lp.amount_micros)"):
            self.rows = [self.account_row]
        elif query.startswith("SELECT m.market_ref"):
            self.rows = self.position_rows
        elif query.startswith("SELECT previous.data_cutoff"):
            self.rows = [(self.previous_cutoff,)] if self.previous_cutoff is not None else []
        elif query.startswith("SELECT 'settlement'"):
            self.rows = self.settlement_rows
        elif query.startswith("SELECT 'rejection'"):
            self.rows = self.rejection_rows
        elif query.startswith("SELECT count(*), COALESCE(sum(s.realized_pnl_micros)"):
            self.rows = [self.settlement_summary]
        elif query.startswith("SELECT COALESCE(NULLIF(BTRIM(lifecycle.reason)"):
            self.rows = self.rejection_summary_rows
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class _PromptConnection:
    def __init__(self, cursor: _PromptCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


class PromptContextTests(unittest.TestCase):
    def _port(self, cursor: _PromptCursor) -> Any:
        port = cast(Any, ProductionPromptPort.__new__(ProductionPromptPort))
        port._database_url = "test"
        port._connect = lambda _url: _PromptConnection(cursor)
        port._maximum_market_cost_basis_fraction = Decimal("0.15")
        port._maximum_valuation_bid_age = timedelta(minutes=5)
        return port

    def _position_row(self, *, bid: str | None) -> tuple[object, ...]:
        return (
            "market-1",
            "outcome-1",
            "Question",
            NOW + timedelta(days=2),
            1_000,
            5_000_000,
            10_000,
            100_000,
            NOW,
            int(Decimal(bid) * 1_000_000) if bid is not None else None,
            NOW,
        )

    def test_prompt_account_summary_contains_complete_valuation_and_capacity(self) -> None:
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 100_000),
            position_rows=[self._position_row(bid="0.60")],
        )
        summary = self._port(cursor)._account_context(
            uuid.uuid4(),
            cutoff=NOW,
            frozen={"order_book_snapshot_ids": [str(uuid.uuid4())]},
        )

        self.assertTrue(summary["nav_complete"])
        self.assertEqual(summary["valuation_status"], "complete")
        self.assertEqual(summary["nav_micros"], 106_000_000)
        self.assertEqual(summary["unrealized_pnl_micros"], 990_000)
        position = summary["attention_positions"][0]
        self.assertEqual(position["liquidation_value_micros"], 6_000_000)
        self.assertEqual(position["remaining_capacity_micros"], 10_900_000)

    def test_prompt_capacity_includes_buy_reservations_and_pending_only_markets(self) -> None:
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 100_000),
            position_rows=[self._position_row(bid="0.60")],
        )
        pending_orders = (
            SimpleNamespace(
                market_ref="market-1", side=Side.BUY, reserved_cost_basis_micros=1_000_000
            ),
            SimpleNamespace(
                market_ref="market-1", side=Side.BUY, reserved_cost_basis_micros=2_000_000
            ),
            SimpleNamespace(
                market_ref="market-2", side=Side.BUY, reserved_cost_basis_micros=4_000_000
            ),
            SimpleNamespace(
                market_ref="market-1", side=Side.SELL, reserved_cost_basis_micros=99_000_000
            ),
        )

        summary = self._port(cursor)._account_context(
            uuid.uuid4(),
            cutoff=NOW,
            frozen={"order_book_snapshot_ids": [str(uuid.uuid4())]},
            pending_orders=pending_orders,
        )

        self.assertEqual(
            summary["market_capacities"],
            [
                {
                    "market_ref": "market-1",
                    "held_cost_basis_micros": 5_000_000,
                    "pending_buy_reserved_cost_basis_micros": 3_000_000,
                    "market_limit_micros": 15_900_000,
                    "remaining_capacity_micros": 7_900_000,
                },
                {
                    "market_ref": "market-2",
                    "held_cost_basis_micros": 0,
                    "pending_buy_reserved_cost_basis_micros": 4_000_000,
                    "market_limit_micros": 15_900_000,
                    "remaining_capacity_micros": 11_900_000,
                },
            ],
        )
        self.assertEqual(
            summary["attention_positions"][0]["remaining_capacity_micros"],
            7_900_000,
        )

    def test_prompt_account_summary_nulls_derived_values_without_a_bid(self) -> None:
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 100_000),
            position_rows=[self._position_row(bid=None)],
        )
        summary = self._port(cursor)._account_context(
            uuid.uuid4(),
            cutoff=NOW,
            frozen={"order_book_snapshot_ids": [str(uuid.uuid4())]},
        )

        self.assertFalse(summary["nav_complete"])
        self.assertEqual(summary["valuation_status"], "incomplete")
        self.assertIsNone(summary["nav_micros"])
        self.assertIsNone(summary["unrealized_pnl_micros"])
        self.assertIsNone(summary["attention_positions"][0]["position_weight"])

    def test_recent_activity_has_delta_and_complete_24_hour_summary(self) -> None:
        settlements = [
            (
                "settlement",
                f"market-{index}",
                f"outcome-{index}",
                index,
                NOW - timedelta(minutes=index),
                "",
                f"settlement-{index:02d}",
            )
            for index in range(13)
        ]
        rejections = [
            (
                "rejection",
                f"market-r{index}",
                f"outcome-r{index}",
                0,
                NOW - timedelta(minutes=index + 1),
                "stale_book",
                f"rejection-{index:02d}",
            )
            for index in range(13)
        ]
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 0),
            position_rows=[],
            settlement_rows=settlements,
            rejection_rows=rejections,
            settlement_summary=(30, -1_250),
            rejection_summary_rows=[("stale_book", 26)],
        )

        activity = self._port(cursor)._recent_activity(uuid.uuid4(), NOW)

        self.assertEqual(len(activity["since_last_cycle"]), 25)
        self.assertTrue(activity["since_last_cycle_truncated"])
        self.assertEqual(activity["since_last_cycle"][0]["occurred_at"], NOW.isoformat())
        self.assertEqual(activity["since_last_cycle"][0]["type"], "settlement")
        self.assertNotIn("events", activity)
        self.assertNotIn("truncated", activity)
        self.assertEqual(
            activity["summary_24h"],
            {
                "settlements": 30,
                "settlement_pnl_micros": -1_250,
                "rejections": {"stale_book": 26},
            },
        )

    def test_recent_activity_bootstraps_at_24_hours_without_previous_context(self) -> None:
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 0),
            position_rows=[],
        )

        activity = self._port(cursor)._recent_activity(uuid.uuid4(), NOW)

        delta_query = next(query for query in cursor.queries if "SELECT 'settlement'" in query[0])
        self.assertEqual(delta_query[1][1], NOW - timedelta(hours=24))
        self.assertEqual(delta_query[1][2], NOW)
        self.assertFalse(activity["since_last_cycle_truncated"])

    def test_recent_activity_uses_last_exposed_cutoff_and_separate_24_hour_bounds(self) -> None:
        previous_cutoff = NOW - timedelta(hours=48)
        cycle_id = uuid.uuid4()
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 0),
            position_rows=[],
            previous_cutoff=previous_cutoff,
            settlement_summary=(3, -10),
            rejection_summary_rows=[("", 2), (None, 1), ("stale_book", 4)],
        )

        activity = self._port(cursor)._recent_activity(
            uuid.uuid4(),
            NOW,
            current_cycle_id=cycle_id,
        )

        settlement_query = next(
            query for query in cursor.queries if "SELECT 'settlement'" in query[0]
        )
        rejection_query = next(
            query for query in cursor.queries if "SELECT 'rejection'" in query[0]
        )
        settlement_summary_query = next(
            query for query in cursor.queries if query[0].startswith("SELECT count(*)")
        )
        rejection_summary_query = next(
            query
            for query in cursor.queries
            if query[0].startswith("SELECT COALESCE(NULLIF(BTRIM(lifecycle.reason)")
        )
        self.assertEqual(settlement_query[1][1:3], (previous_cutoff, NOW))
        self.assertEqual(rejection_query[1][1:3], (previous_cutoff, NOW))
        self.assertEqual(
            settlement_summary_query[1][1:3],
            (NOW - timedelta(hours=24), NOW),
        )
        self.assertEqual(
            rejection_summary_query[1][1:3],
            (NOW - timedelta(hours=24), NOW),
        )
        self.assertIn("settled_at > %s", settlement_query[0])
        self.assertIn("settled_at <= %s", settlement_query[0])
        self.assertIn("oo.created_at > %s", rejection_query[0])
        self.assertIn("oo.created_at <= %s", rejection_query[0])
        self.assertEqual(activity["summary_24h"]["rejections"], {"stale_book": 4, "unknown": 3})

    def test_recent_activity_uses_rejection_fallback_and_stable_id_tie_breaking(self) -> None:
        settlements = [
            (
                "settlement",
                f"market-{index}",
                f"outcome-{index}",
                -index,
                NOW,
                "",
                f"settlement-{index:02d}",
            )
            for index in range(26)
        ]
        rejections = [
            (
                "rejection",
                "market-rejection",
                "outcome-rejection",
                0,
                NOW - timedelta(minutes=1),
                None,
                "order-01",
            ),
            (
                "rejection",
                "market-rejection-2",
                "outcome-rejection-2",
                0,
                NOW - timedelta(minutes=2),
                "",
                "order-02",
            ),
        ]
        cursor = _PromptCursor(
            account_row=(100_000_000, 3, 100_000_000, 0),
            position_rows=[],
            settlement_rows=settlements,
            rejection_rows=rejections,
        )

        activity = self._port(cursor)._recent_activity(uuid.uuid4(), NOW)
        delta = activity["since_last_cycle"]

        self.assertEqual(len(delta), 25)
        self.assertTrue(activity["since_last_cycle_truncated"])
        self.assertEqual(delta[0]["market_ref"], "market-25")
        self.assertEqual(delta[-1]["market_ref"], "market-1")
        self.assertNotIn("created_at", delta[0])
        self.assertEqual(
            ProductionPromptPort._activity_event_payload(
                RecentActivityEvent(
                    "rejection",
                    "market",
                    NOW,
                    "outcome",
                    None,
                    "",
                    "order",
                )
            ),
            {
                "type": "rejection",
                "market_ref": "market",
                "outcome": "outcome",
                "occurred_at": NOW.isoformat(),
                "rejection_code": "unknown",
            },
        )


class PortfolioLoadingTests(unittest.TestCase):
    def test_current_postgres_portfolio_projection_keeps_pending_orders_empty(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.rows: list[tuple[object, ...]] = []
                self.queries: list[str] = []

            def execute(self, query: str, _params=()) -> None:
                self.queries.append(query)
                if query.startswith("SELECT COALESCE(sum(lp.amount_micros)"):
                    self.rows = [(10_000_000, 7)]
                elif query.startswith("SELECT m.id, p.outcome_id"):
                    self.rows = []
                else:
                    raise AssertionError(query)

            def fetchone(self) -> tuple[object, ...] | None:
                return self.rows[0] if self.rows else None

            def fetchall(self) -> tuple[tuple[object, ...], ...]:
                rows, self.rows = self.rows, []
                return tuple(rows)

        cursor = Cursor()
        portfolio = _PostgresTradingState("postgresql://unused").portfolio(
            uuid.uuid4(), cursor=cast(Any, cursor)
        )

        self.assertEqual(portfolio.pending_orders, ())
        self.assertFalse(any("order_intents" in query for query in cursor.queries))


class WorkerFailClosedTests(unittest.TestCase):
    def test_positive_integer_configuration_rejects_non_positive_values(self) -> None:
        self.assertEqual(_positive_integer({"beliefs": 100}, "beliefs"), 100)
        with self.assertRaisesRegex(ProductionCompositionUnavailable, "must be positive"):
            _positive_integer({"beliefs": 0}, "beliefs")

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
            _liquidity_time_in_force({"execution": {}}), LiquidityTimeInForce.IOC
        )
        self.assertEqual(
            _liquidity_time_in_force({"execution": {"liquidity_time_in_force": "IOC"}}),
            LiquidityTimeInForce.IOC,
        )
        self.assertEqual(
            _maximum_order_book_age(
                {
                    "execution": {"maximum_order_book_age_seconds": 300},
                    "limits": {"maximum_archived_bid_age_seconds": 120},
                }
            ),
            timedelta(seconds=300),
        )
        self.assertEqual(_order_book_depth({"execution": {"order_book_depth": 5}}), 5)
        self.assertEqual(
            _ignored_best_levels({"execution": {"ignored_best_levels": 1}}), 1
        )
        self.assertEqual(
            _maximum_ignored_depth_fraction(
                {"execution": {"maximum_ignored_depth_fraction": 0.5}}
            ),
            Decimal("0.5"),
        )
        with self.assertRaisesRegex(ProductionCompositionUnavailable, "between zero and one"):
            _maximum_ignored_depth_fraction(
                {"execution": {"maximum_ignored_depth_fraction": 1.1}}
            )

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
        run_id, operation_id = uuid.uuid4(), uuid.uuid4()

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
                elif "FROM order_operations" in query:
                    self.rows = [(operation_id,)]
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
        self.assertEqual(result.payload["operation_ids"], [str(operation_id)])
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
        self.assertEqual(result.accepted_operations, 1)
        self.assertEqual(state.seen_books, (book_id,))
        self.assertEqual(market_repository.seen_fees, (fee_id,))
        self.assertEqual(port._broker.seen_order.liquidity_time_in_force, LiquidityTimeInForce.FAK)

    def test_production_broker_port_rejects_liquidity_aware_frozen_fallback(self) -> None:
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

        with self.assertRaises(ProductionCompositionUnavailable):
            port.execute(
                claim,
                {
                    "order_book_snapshot_ids": [str(book_id)],
                    "fee_rate_snapshot_ids": [str(fee_id)],
                },
                {"harness_run_id": str(uuid.uuid4()), "intent_ids": [str(intent_id)]},
            )

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
                return HarnessExecutionResult(
                    {"operation_ids": [str(intent_id)]}, (artifact,), 1, 2
                )

        class Broker:
            def execute(self, claim, frozen, harness):
                calls["broker"] += 1
                self.assert_operation = harness["operation_ids"]
                return BrokerExecutionResult({"operation_ids": [str(uuid.uuid4())]}, (), 1)

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
        self.assertEqual(summary[CycleStage.HARNESS.value]["operation_ids"], [str(intent_id)])
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
                ConfigurationError,
                "reviewed Kalshi fixture capture",
            ):
                run_worker(path)


if __name__ == "__main__":
    unittest.main()
