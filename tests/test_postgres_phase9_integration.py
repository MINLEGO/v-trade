from __future__ import annotations

import os
import unittest
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from vtrade.domain.types import (
    Event,
    Market,
    MarketDelta,
    MarketStatus,
    MicroDollars,
    OrderBookSnapshot,
    Outcome,
    PriceLevel,
    RawArtifact,
    Resolution,
)
from vtrade.market_data import FrozenFeePolicyUnavailable, PostgresMarketDataRepository
from vtrade.polymarket import FeeRateSnapshot

RUN_POSTGRES = os.environ.get("VTRADE_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_POSTGRES,
    "set VTRADE_RUN_POSTGRES_INTEGRATION=1 for the rollback-only PostgreSQL test",
)
class PhaseNinePostgresIntegrationTests(unittest.TestCase):
    def test_normalized_freeze_fee_lookup_and_rollback(self) -> None:
        database_url = os.environ.get("VTRADE_DATABASE_URL")
        if not database_url:
            self.fail("VTRADE_DATABASE_URL is required when PostgreSQL integration is enabled")
        import psycopg

        marker = uuid.uuid4().hex
        now = datetime(2400, 1, 1, 12, tzinfo=UTC)
        artifact = RawArtifact(marker.ljust(64, "0")[:64], 10, f"supabase://private/{marker}")
        event = Event(
            f"polymarket:event:{marker}",
            marker,
            marker,
            "Integration event",
            "",
            None,
            now - timedelta(days=1),
            now + timedelta(days=1),
            True,
            False,
            False,
            now,
            now,
        )
        market_id = f"polymarket:market:{marker}"
        token = f"token-{marker}"
        outcome = Outcome(
            f"polymarket:outcome:{token}",
            market_id,
            "YES",
            token,
            None,
            None,
            MicroDollars(10_000),
            MicroDollars(1_000_000),
            0,
            Decimal("0.5"),
            True,
        )
        market = Market(
            market_id,
            marker,
            event.id,
            "Integration question",
            "rules",
            event.opens_at,
            event.closes_at,
            MarketStatus.OPEN,
            None,
            MicroDollars(1_000_000),
            MicroDollars(1_000_000),
            {"condition_id": marker},
            marker,
            None,
            True,
            (outcome,),
            now,
            now,
        )
        page = MarketDelta("markets", None, None, now, (event,), (market,), artifact)
        book = OrderBookSnapshot(
            token,
            marker,
            now,
            now,
            (PriceLevel(Decimal("0.49"), Decimal("10")),),
            (PriceLevel(Decimal("0.51"), Decimal("10")),),
            Decimal("0.01"),
            Decimal("1"),
            False,
            artifact,
        )
        resolution = Resolution(
            market_id, outcome.id, "YES", now, now, now, artifact
        )
        fee = FeeRateSnapshot(token, 30, now, None, artifact)
        connection = psycopg.connect(database_url)
        try:
            def connect(_url: str) -> AbstractContextManager[Any]:
                return nullcontext(connection)

            repository = PostgresMarketDataRepository(database_url, connect=connect)
            persisted = repository.persist_freeze((page,), (book,), (resolution,), (fee,))
            self.assertEqual(len(persisted.market_snapshot_ids), 1)
            self.assertEqual(len(persisted.order_book_snapshot_ids), 1)
            self.assertEqual(len(persisted.resolution_ids), 1)
            self.assertEqual(len(persisted.fee_rate_snapshot_ids), 1)
            self.assertEqual(
                repository.frozen_fee_policy(
                    token,
                    cutoff=now,
                    fee_rate_snapshot_ids=persisted.fee_rate_snapshot_ids,
                ).rate,
                Decimal("0.003"),
            )
            with self.assertRaisesRegex(
                FrozenFeePolicyUnavailable, "no frozen fee rate exists"
            ):
                repository.frozen_fee_policy(
                    token,
                    cutoff=now,
                    fee_rate_snapshot_ids=(uuid.uuid4(),),
                )
        finally:
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM fee_rate_snapshots WHERE token_id = %s", (token,)
                )
                self.assertEqual(cursor.fetchone(), (0,))
            connection.close()


if __name__ == "__main__":
    unittest.main()
