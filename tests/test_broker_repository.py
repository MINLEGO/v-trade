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
    PaperFill,
    PaperOrder,
    PaperPolicy,
    PortfolioState,
    PositionState,
    RejectionCode,
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
        self.selected: tuple[object, ...] | None = None
        self.portfolio_version = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params=()):
        values = tuple(params)
        self.queries.append((query, values))
        if query.startswith("SELECT id, execution_fingerprint FROM orders"):
            self.selected = self.orders.get(str(values[0]))
        elif query.startswith("SELECT portfolio_version FROM agents"):
            self.selected = (self.portfolio_version,)
        elif query.startswith("INSERT INTO orders"):
            self.orders[str(values[8])] = (values[0], str(values[-1]))
        elif query.startswith("UPDATE agents SET portfolio_version"):
            self.portfolio_version = int(values[0])
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


if __name__ == "__main__":
    unittest.main()
