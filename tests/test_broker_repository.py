from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from vtrade.broker import (
    ExecutionResult,
    ExecutionStatus,
    FeePolicy,
    LiquidityTimeInForce,
    PaperFill,
    PaperOrder,
    PaperPolicy,
    PortfolioState,
    PositionState,
    RejectionCode,
    SettlementEngine,
    SettlementObservation,
)
from vtrade.broker_repository import PostgresBrokerRepository
from vtrade.domain.types import MicroDollars, OrderBookSnapshot, RawArtifact, Side
from vtrade.ledger import LedgerAccount, LedgerEntry, Posting

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class RecordingCursor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.orders: dict[str, tuple[uuid.UUID, str]] = {}
        self.settlements: dict[str, tuple[uuid.UUID, str]] = {}
        self.selected: tuple[object, ...] | None = None
        self.portfolio_version = 0
        self.rowcount = 1
        self.execution_relation: tuple[object, ...] | None = None
        self.settlement_relation: tuple[object, ...] | None = None
        self.position_update_rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params=()):
        values = tuple(params)
        self.queries.append((query, values))
        self.selected = None
        self.rowcount = 1
        if query.startswith("SELECT ac.agent_id, oi.market_id"):
            self.selected = self.execution_relation
        elif query.startswith("SELECT p.agent_id, o.market_id"):
            self.selected = self.settlement_relation
        elif query.startswith("SELECT id, execution_fingerprint FROM orders"):
            self.selected = self.orders.get(str(values[0]))
        elif query.startswith("SELECT id, execution_fingerprint FROM settlements"):
            self.selected = self.settlements.get(str(values[0]))
        elif query.startswith("SELECT portfolio_version FROM agents"):
            self.selected = (self.portfolio_version,)
        elif query.startswith("INSERT INTO orders"):
            self.orders[str(values[8])] = (values[0], str(values[-1]))
        elif query.startswith("INSERT INTO settlements"):
            self.settlements[str(values[7])] = (values[0], str(values[-1]))
        elif query.startswith("UPDATE agents SET portfolio_version"):
            self.portfolio_version = int(values[0])
        elif query.startswith("UPDATE positions SET shares"):
            self.rowcount = self.position_update_rowcount
        return self

    def fetchone(self):
        return self.selected


class RecordingConnection:
    def __init__(self) -> None:
        self.cursor_instance = RecordingCursor()
        self.transaction_count = 0

    def __enter__(self):
        self.transaction_count += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self):
        return self.cursor_instance


def rejected_result() -> ExecutionResult:
    paper_order = PaperOrder(
        id="external-order-1",
        agent_id=str(AGENT_ID),
        market_id="polymarket:market:1",
        outcome_id="polymarket:outcome:yes",
        side=Side.BUY,
        shares=Decimal(10),
        created_at=NOW,
    )
    snapshot = OrderBookSnapshot(
        token_id="yes",
        condition_id="condition",
        observed_at=NOW - timedelta(seconds=1),
        source_created_at=NOW - timedelta(seconds=1),
        bids=(),
        asks=(),
        tick_size=Decimal("0.01"),
        minimum_order_size=Decimal(1),
        negative_risk=False,
        artifact=RawArtifact("a" * 64, 1, "memory://book"),
    )
    portfolio = PortfolioState(str(AGENT_ID), MicroDollars(10_000_000_000))
    return ExecutionResult(
        order=paper_order,
        policy=PaperPolicy.PREDICTIONARENA_UNCONDITIONAL,
        status=ExecutionStatus.REJECTED,
        fills=(),
        rejection_code=RejectionCode.REQUIRED_QUOTE_ABSENT,
        portfolio_before=portfolio,
        portfolio=portfolio,
        ledger_entries=(),
        snapshot=snapshot,
        fee_policy=FeePolicy(Decimal("0.05"), exponent=Decimal(2)),
    )


def accepted_result() -> ExecutionResult:
    rejected = rejected_result()
    before = replace(rejected.portfolio_before, version=7)
    position = PositionState(
        "polymarket:market:1",
        "polymarket:outcome:yes",
        Decimal(10),
        Decimal("0.4"),
        MicroDollars(4_000_000),
    )
    after = PortfolioState(
        str(AGENT_ID),
        MicroDollars(9_996_000_000),
        positions=(position,),
        version=8,
    )
    fill = PaperFill(
        id="external-fill-1",
        order_id=rejected.order.id,
        fill_index=0,
        shares=Decimal(10),
        price=Decimal("0.4"),
        gross_micros=MicroDollars(4_000_000),
        fee_micros=MicroDollars(0),
        filled_at=NOW,
    )
    ledger = LedgerEntry(
        id=str(uuid.uuid4()),
        agent_id=str(AGENT_ID),
        idempotency_key="trade:external-order-1",
        event_type="paper_trade",
        occurred_at=NOW,
        postings=(
            Posting(LedgerAccount.CASH, MicroDollars(-4_000_000)),
            Posting(
                LedgerAccount.POSITION_COST,
                MicroDollars(4_000_000),
                market_id=position.market_id,
                outcome_id=position.outcome_id,
                shares_delta=Decimal(10),
            ),
        ),
    )
    return replace(
        rejected,
        status=ExecutionStatus.FILLED,
        fills=(fill,),
        rejection_code=None,
        portfolio_before=before,
        portfolio=after,
        ledger_entries=(ledger,),
    )


def partial_liquidity_aware_result() -> ExecutionResult:
    base = accepted_result()
    order = replace(
        base.order,
        shares=Decimal(5),
        liquidity_time_in_force=LiquidityTimeInForce.FAK,
    )
    first = replace(
        base.fills[0],
        id="external-fill-1-partial",
        shares=Decimal(1),
        price=Decimal("0.4"),
        gross_micros=MicroDollars(400_000),
    )
    second = replace(
        base.fills[0],
        id="external-fill-2-partial",
        fill_index=1,
        shares=Decimal(2),
        price=Decimal("0.41"),
        gross_micros=MicroDollars(820_000),
    )
    position = replace(
        base.portfolio.positions[0],
        shares=Decimal(3),
        average_cost=Decimal("0.4066666666666666666666666667"),
        cost_basis_micros=MicroDollars(1_220_000),
    )
    after = replace(
        base.portfolio,
        cash_micros=MicroDollars(9_998_780_000),
        positions=(position,),
    )
    ledger = replace(
        base.ledger_entries[0],
        postings=(
            Posting(LedgerAccount.CASH, MicroDollars(-1_220_000)),
            Posting(
                LedgerAccount.POSITION_COST,
                MicroDollars(1_220_000),
                market_id=position.market_id,
                outcome_id=position.outcome_id,
                shares_delta=Decimal(3),
            ),
        ),
    )
    return replace(
        base,
        order=order,
        policy=PaperPolicy.LIQUIDITY_AWARE,
        status=ExecutionStatus.PARTIAL,
        fills=(first, second),
        portfolio=after,
        ledger_entries=(ledger,),
        fee_policy=FeePolicy(Decimal("0")),
    )


class PostgresBrokerRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = RecordingConnection()
        self.repository = PostgresBrokerRepository(
            "postgresql://unused",
            connect=lambda _url: self.connection,
        )
        self.ids = {
            "agent_id": AGENT_ID,
            "intent_id": uuid.uuid4(),
            "market_id": uuid.uuid4(),
            "outcome_id": uuid.uuid4(),
            "snapshot_id": uuid.uuid4(),
        }
        self.connection.cursor_instance.execution_relation = (
            AGENT_ID,
            self.ids["market_id"],
            self.ids["outcome_id"],
            Side.BUY.value,
            "yes",
            self.ids["outcome_id"],
            NOW - timedelta(seconds=1),
            "a" * 64,
        )

    def test_repeated_place_is_one_insert_and_returns_existing_record(self) -> None:
        result = rejected_result()
        first = self.repository.persist_execution(result, **self.ids)
        second = self.repository.persist_execution(result, **self.ids)
        inserts = [
            query
            for query, _params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO orders")
        ]
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(len(inserts), 1)
        self.assertEqual(self.connection.transaction_count, 2)

    def test_idempotency_key_payload_mismatch_fails_closed(self) -> None:
        result = rejected_result()
        self.repository.persist_execution(result, **self.ids)
        changed = replace(result, rejection_code=RejectionCode.INVALID_TICK)
        with self.assertRaises(ValueError):
            self.repository.persist_execution(changed, **self.ids)

    def test_phase_three_migration_records_replay_and_fee_parameters(self) -> None:
        migration = Path("migrations/0003_paper_broker.sql").read_text(encoding="utf-8")
        for required in (
            "execution_fingerprint",
            "liquidity_time_in_force",
            "shares_delta",
            "fee_exponent",
            "as_of_cutoff",
            "portfolio_version",
        ):
            self.assertIn(required, migration)

    def test_stale_accepted_projection_is_rejected_before_order_insert(self) -> None:
        self.connection.cursor_instance.portfolio_version = 6
        with self.assertRaises(ValueError):
            self.repository.persist_execution(accepted_result(), **self.ids)
        self.assertFalse(self.connection.cursor_instance.orders)

    def test_accepted_projection_advances_version_in_same_transaction(self) -> None:
        self.connection.cursor_instance.portfolio_version = 7
        persisted = self.repository.persist_execution(accepted_result(), **self.ids)
        self.assertTrue(persisted.created)
        self.assertEqual(self.connection.cursor_instance.portfolio_version, 8)

    def test_partial_liquidity_aware_execution_persists_policy_tif_and_all_fills(self) -> None:
        self.connection.cursor_instance.portfolio_version = 7
        result = partial_liquidity_aware_result()
        persisted = self.repository.persist_execution(result, **self.ids)

        order_insert = next(
            params
            for query, params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO orders")
        )
        fill_inserts = [
            params
            for query, params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO fills")
        ]
        self.assertTrue(persisted.created)
        self.assertEqual(order_insert[2], PaperPolicy.LIQUIDITY_AWARE.value)
        self.assertEqual(order_insert[3], ExecutionStatus.PARTIAL.value)
        self.assertEqual(order_insert[4], Decimal(5))
        self.assertEqual(order_insert[10], LiquidityTimeInForce.FAK.value)
        self.assertEqual(len(fill_inserts), 2)
        self.assertEqual(sum((params[3] for params in fill_inserts), Decimal(0)), Decimal(3))
        self.assertEqual(
            result.portfolio.position("polymarket:outcome:yes").shares,
            Decimal(3),
        )

    def test_execution_relation_mismatch_fails_before_order_insert(self) -> None:
        relation = self.connection.cursor_instance.execution_relation
        assert relation is not None
        self.connection.cursor_instance.execution_relation = (
            uuid.uuid4(),
            *relation[1:],
        )
        with self.assertRaisesRegex(ValueError, "ownership or dimensions"):
            self.repository.persist_execution(rejected_result(), **self.ids)
        self.assertFalse(self.connection.cursor_instance.orders)

    def test_settlement_relations_are_locked_and_position_update_is_owned(self) -> None:
        result, ids = self._settlement_result_and_ids()
        self.connection.cursor_instance.portfolio_version = 3
        self.connection.cursor_instance.settlement_relation = self._settlement_relation(
            result, ids
        )

        persisted = self.repository.persist_settlement(result, **ids)

        self.assertTrue(persisted.created)
        self.assertEqual(self.connection.cursor_instance.portfolio_version, 4)

    def test_settlement_relation_mismatch_fails_before_insert(self) -> None:
        result, ids = self._settlement_result_and_ids()
        relation = self._settlement_relation(result, ids)
        self.connection.cursor_instance.settlement_relation = (
            uuid.uuid4(),
            *relation[1:],
        )
        with self.assertRaisesRegex(ValueError, "ownership, resolution"):
            self.repository.persist_settlement(result, **ids)
        self.assertFalse(self.connection.cursor_instance.settlements)

    def test_settlement_position_update_requires_exact_owned_row(self) -> None:
        result, ids = self._settlement_result_and_ids()
        self.connection.cursor_instance.portfolio_version = 3
        self.connection.cursor_instance.settlement_relation = self._settlement_relation(
            result, ids
        )
        self.connection.cursor_instance.position_update_rowcount = 0
        with self.assertRaisesRegex(ValueError, "position update"):
            self.repository.persist_settlement(result, **ids)

    def test_fifty_fifty_settlement_accepts_null_database_winner(self) -> None:
        result, ids = self._settlement_result_and_ids(winner=None)
        self.connection.cursor_instance.portfolio_version = 3
        self.connection.cursor_instance.settlement_relation = self._settlement_relation(
            result, ids
        )

        persisted = self.repository.persist_settlement(result, **ids)

        self.assertTrue(persisted.created)
        self.assertEqual(result.payout_micros, 5_000_000)

    def _settlement_result_and_ids(
        self, *, winner: str | None = "polymarket:outcome:yes"
    ):
        position = PositionState(
            "polymarket:market:1",
            "polymarket:outcome:yes",
            Decimal(10),
            Decimal("0.4"),
            MicroDollars(4_000_000),
        )
        portfolio = PortfolioState(
            str(AGENT_ID),
            MicroDollars(10_000_000_000),
            positions=(position,),
            version=3,
        )
        resolution = SettlementObservation(
            id="resolution-1",
            market_id=position.market_id,
            winning_outcome_id=winner,
            source_created_at=NOW - timedelta(seconds=4),
            observed_at=NOW - timedelta(seconds=3),
            eligible_after=NOW - timedelta(seconds=2),
        )
        result = SettlementEngine().settle(
            resolution=resolution,
            position=position,
            portfolio=portfolio,
            as_of=NOW - timedelta(seconds=1),
            settled_at=NOW,
        )
        ids = {
            "agent_id": AGENT_ID,
            "position_id": uuid.uuid4(),
            "resolution_id": uuid.uuid4(),
            "market_id": self.ids["market_id"],
            "outcome_id": self.ids["outcome_id"],
        }
        return result, ids

    @staticmethod
    def _settlement_relation(result, ids):
        return (
            AGENT_ID,
            ids["market_id"],
            ids["outcome_id"],
            result.position.shares,
            result.position.average_cost,
            int(result.position.cost_basis_micros),
            int(result.position.realized_pnl_micros),
            ids["market_id"],
            (
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vtrade:outcome:{result.resolution.winning_outcome_id}",
                )
                if result.resolution.winning_outcome_id is not None
                else None
            ),
            result.resolution.source_created_at,
            result.resolution.observed_at,
            result.resolution.eligible_after,
        )


if __name__ == "__main__":
    unittest.main()
