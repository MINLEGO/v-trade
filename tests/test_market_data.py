from __future__ import annotations

import unittest
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from vtrade.domain.types import (
    Event,
    Market,
    MarketDelta,
    MarketStatus,
    MicroDollars,
    Outcome,
    RawArtifact,
)
from vtrade.market_data import (
    FrozenFeePolicyUnavailable,
    FrozenPersistence,
    PolymarketFreezeService,
    PostgresMarketDataRepository,
)
from vtrade.runtime import CycleClaim

NOW = datetime(2026, 7, 16, 10, 5, tzinfo=UTC)
ARTIFACT = RawArtifact("a" * 64, 10, "supabase://private/aa/a.json.gz")


def _market(index: int, volume: int) -> Market:
    event = f"polymarket:event:e-{index}"
    market_id = f"polymarket:market:m-{index}"
    outcomes = tuple(
        Outcome(
            f"polymarket:outcome:t-{index}-{side}",
            market_id,
            side,
            f"t-{index}-{side}",
            None,
            None,
            MicroDollars(10_000),
            MicroDollars(1_000_000),
            offset,
            Decimal("0.5"),
            True,
        )
        for offset, side in enumerate(("YES", "NO"))
    )
    return Market(
        market_id,
        f"m-{index}",
        event,
        f"Question {index}",
        "rules",
        NOW - timedelta(days=1),
        NOW + timedelta(days=1),
        MarketStatus.OPEN,
        None,
        MicroDollars(volume),
        MicroDollars(volume),
        {"condition_id": f"condition-{index}"},
        f"market-{index}",
        None,
        True,
        outcomes,
        NOW,
        NOW,
    )


class _Venue:
    def __init__(self, page: MarketDelta) -> None:
        self.page = page
        self.requested_tokens: tuple[str, ...] = ()

    def sync_all_markets(self):
        return (self.page,)

    def get_order_book(self, tokens):
        self.requested_tokens = tuple(tokens)
        return ()

    def get_fee_rates(self, _tokens):
        return ()

    def sync_resolutions(self, _markets):
        return ()


class _Repository:
    def __init__(self) -> None:
        self.persisted = False

    def historical_universe(self, _agent_id):
        return (), ()

    def persist_freeze(self, pages, books, resolutions, fee_rates=()):
        self.persisted = bool(pages) and not books and not resolutions and not fee_rates
        return FrozenPersistence((uuid.uuid4(),), (), ())


class _Cursor:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query: str, params: Sequence[object] = ()):
        self.queries.append((query, tuple(params)))
        return self

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_instance = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self) -> AbstractContextManager[_Cursor]:
        return self.cursor_instance


class MarketFreezeTests(unittest.TestCase):
    def test_freeze_prefetches_bounded_deterministic_shortlist_before_cutoff(self) -> None:
        markets = tuple(_market(index, index + 1) for index in range(12))
        events = tuple(
            Event(
                f"polymarket:event:e-{index}",
                f"e-{index}",
                f"event-{index}",
                f"Event {index}",
                "",
                None,
                None,
                None,
                True,
                False,
                False,
                NOW,
                NOW,
            )
            for index in range(12)
        )
        page = MarketDelta("markets", None, None, NOW, events, markets, ARTIFACT)
        venue = _Venue(page)
        repository = _Repository()
        claim = CycleClaim(
            uuid.uuid4(), uuid.uuid4(), NOW, None, "worker", NOW + timedelta(minutes=10)
        )
        result = PolymarketFreezeService(venue, repository, clock=lambda: NOW).freeze(claim)
        self.assertEqual(len(venue.requested_tokens), 20)
        self.assertEqual(venue.requested_tokens[:2], ("t-11-YES", "t-11-NO"))
        self.assertTrue(repository.persisted)
        self.assertEqual(result.freshest_observed_at, NOW)

    def test_finalized_cutoff_forbids_any_venue_fetch(self) -> None:
        page = MarketDelta("markets", None, None, NOW, (), (), ARTIFACT)
        venue = _Venue(page)
        repository = _Repository()
        claim = CycleClaim(
            uuid.uuid4(), uuid.uuid4(), NOW, NOW, "worker", NOW + timedelta(minutes=10)
        )
        with self.assertRaisesRegex(ValueError, "cannot fetch after"):
            PolymarketFreezeService(venue, repository, clock=lambda: NOW).freeze(claim)
        self.assertEqual(venue.requested_tokens, ())

    def test_event_open_time_is_never_persisted_as_source_creation_time(self) -> None:
        market = _market(1, 10)
        event = Event(
            market.event_id,
            "e-1",
            "event-1",
            "Event 1",
            "",
            None,
            NOW - timedelta(days=7),
            NOW + timedelta(days=7),
            True,
            False,
            False,
            NOW,
            NOW,
        )
        page = MarketDelta("markets", None, None, NOW, (event,), (market,), ARTIFACT)
        cursor = _Cursor()
        repository = PostgresMarketDataRepository(
            "postgresql://unused", connect=lambda _url: _Connection(cursor)
        )
        repository.persist_freeze((page,), (), ())
        params = next(
            values
            for query, values in cursor.queries
            if query.startswith("INSERT INTO events")
        )
        self.assertIsNone(params[5])
        self.assertNotEqual(params[5], event.opens_at)

    def test_fee_policy_uses_only_fresh_frozen_persisted_basis_points(self) -> None:
        cursor = _Cursor(((30, NOW - timedelta(seconds=10), None),))
        repository = PostgresMarketDataRepository(
            "postgresql://unused", connect=lambda _url: _Connection(cursor)
        )
        policy = repository.frozen_fee_policy(
            "token", cutoff=NOW, maximum_age=timedelta(minutes=5)
        )
        self.assertEqual(policy.rate, Decimal("0.003"))

    def test_missing_or_stale_frozen_fee_rate_fails_closed(self) -> None:
        missing = PostgresMarketDataRepository(
            "postgresql://unused", connect=lambda _url: _Connection(_Cursor())
        )
        with self.assertRaises(FrozenFeePolicyUnavailable):
            missing.frozen_fee_policy(
                "token", cutoff=NOW, maximum_age=timedelta(minutes=5)
            )
        stale = PostgresMarketDataRepository(
            "postgresql://unused",
            connect=lambda _url: _Connection(
                _Cursor(((30, NOW - timedelta(minutes=6), None),))
            ),
        )
        with self.assertRaisesRegex(FrozenFeePolicyUnavailable, "stale"):
            stale.frozen_fee_policy(
                "token", cutoff=NOW, maximum_age=timedelta(minutes=5)
            )


if __name__ == "__main__":
    unittest.main()
