from __future__ import annotations

import json
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


def _market(
    index: int,
    volume: int,
    *,
    status: MarketStatus = MarketStatus.OPEN,
    tradeable: bool = True,
) -> Market:
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
            tradeable,
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
        status,
        None,
        MicroDollars(volume),
        MicroDollars(volume),
        {"condition_id": f"condition-{index}"},
        f"market-{index}",
        None,
        tradeable,
        outcomes,
        NOW,
        NOW,
    )


class _Venue:
    def __init__(self, page: MarketDelta) -> None:
        self.page = page
        self.requested_tokens: tuple[str, ...] = ()
        self.book_batches: list[tuple[str, ...]] = []
        self.fee_batches: list[tuple[str, ...]] = []
        self.resolution_batches: list[tuple[str, ...]] = []

    def sync_all_markets(self):
        return (self.page,)

    def get_order_book(self, tokens):
        self.requested_tokens = tuple(tokens)
        self.book_batches.append(tuple(tokens))
        return ()

    def get_fee_rates(self, tokens):
        self.fee_batches.append(tuple(tokens))
        return ()

    def sync_resolutions(self, markets):
        self.resolution_batches.append(tuple(markets))
        return ()


class _Repository:
    def __init__(
        self,
        *,
        held_tokens: tuple[str, ...] = (),
        held_markets: tuple[str, ...] = (),
        historical_discovery_tokens: tuple[str, ...] = (),
        historical_tokens: tuple[str, ...] = (),
        historical_markets: tuple[str, ...] = (),
    ) -> None:
        self.persisted = False
        self.held_tokens = held_tokens
        self.held_markets = held_markets
        self.historical_discovery_tokens = historical_discovery_tokens
        self.historical_tokens = historical_tokens
        self.historical_markets = historical_markets
        self.persisted_market_ids: dict[str, uuid.UUID] = {}

    def held_universe(self, _agent_id):
        return self.held_tokens, self.held_markets

    def historical_universe(self, _agent_id, *, maximum_outcomes=20):
        del maximum_outcomes
        return self.historical_tokens, self.historical_markets

    def historical_discovery_universe(self, _agent_id, *, maximum_outcomes=20):
        del maximum_outcomes
        return self.historical_discovery_tokens

    def persist_freeze(self, pages, books, resolutions, fee_rates=()):
        self.persisted = bool(pages) and not books and not resolutions and not fee_rates
        market_ids = tuple(
            uuid.uuid5(uuid.NAMESPACE_URL, f"test-market-snapshot:{market.id}")
            for page in pages
            for market in page.markets
        )
        self.persisted_market_ids = {
            market.id: snapshot_id
            for page in pages
            for market, snapshot_id in zip(page.markets, market_ids, strict=False)
        }
        return FrozenPersistence(market_ids, (), ())


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
        markets = tuple(_market(index, index + 1) for index in range(25))
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
            for index in range(25)
        )
        page = MarketDelta("markets", None, None, NOW, events, markets, ARTIFACT)
        venue = _Venue(page)
        repository = _Repository()
        claim = CycleClaim(
            uuid.uuid4(), uuid.uuid4(), NOW, None, "worker", NOW + timedelta(minutes=10)
        )
        result = PolymarketFreezeService(venue, repository, clock=lambda: NOW).freeze(claim)
        self.assertEqual([len(batch) for batch in venue.book_batches], [20, 20])
        self.assertEqual(venue.book_batches[0][:2], ("t-24-YES", "t-24-NO"))
        self.assertTrue(repository.persisted)
        self.assertEqual(result.freshest_observed_at, NOW)
        selected_ids = set(result.payload["market_snapshot_ids"])
        self.assertEqual(len(selected_ids), 20)
        self.assertNotIn(str(repository.persisted_market_ids[markets[0].id]), selected_ids)
        self.assertNotIn(str(repository.persisted_market_ids[markets[4].id]), selected_ids)

    def test_all_held_outcomes_are_frozen_in_batches_beyond_shortlist_limit(self) -> None:
        page = MarketDelta("markets", None, None, NOW, (), (), ARTIFACT)
        held_tokens = tuple(f"held-{index:02d}" for index in range(45))
        held_markets = tuple(f"market-{index:02d}" for index in range(45))
        venue = _Venue(page)
        repository = _Repository(
            held_tokens=held_tokens,
            held_markets=held_markets,
        )
        claim = CycleClaim(
            uuid.uuid4(), uuid.uuid4(), NOW, None, "worker", NOW + timedelta(minutes=10)
        )
        result = PolymarketFreezeService(venue, repository, clock=lambda: NOW).freeze(claim)
        self.assertEqual([len(batch) for batch in venue.book_batches], [20, 20, 5])
        self.assertEqual([len(batch) for batch in venue.fee_batches], [20, 20, 5])
        self.assertEqual(len(venue.resolution_batches[0]), 45)
        self.assertEqual(result.payload["order_book_token_ids"], list(held_tokens))

    def test_closed_historical_markets_remain_for_resolution_but_not_discovery(self) -> None:
        closed = _market(99, 100, status=MarketStatus.CLOSED, tradeable=False)
        active = _market(1, 10)
        page = MarketDelta("markets", None, None, NOW, (), (closed, active), ARTIFACT)
        venue = _Venue(page)
        repository = _Repository(
            historical_discovery_tokens=(),
            historical_tokens=(closed.outcomes[0].venue_token_id,),
            historical_markets=(closed.venue_id,),
        )
        claim = CycleClaim(
            uuid.uuid4(), uuid.uuid4(), NOW, None, "worker", NOW + timedelta(minutes=10)
        )

        result = PolymarketFreezeService(venue, repository, clock=lambda: NOW).freeze(claim)

        self.assertEqual(venue.requested_tokens, ("t-1-YES", "t-1-NO"))
        self.assertEqual(venue.resolution_batches, [(closed.venue_id,)])
        selected_ids = set(result.payload["market_snapshot_ids"])
        self.assertIn(repository.persisted_market_ids[active.id].__str__(), selected_ids)
        self.assertNotIn(repository.persisted_market_ids[closed.id].__str__(), selected_ids)

    def test_bounded_market_universe_accepts_a_remaining_source_cursor(self) -> None:
        market = _market(1, 10)
        page = MarketDelta("markets", None, "next-page", NOW, (), (market,), ARTIFACT)
        venue = _Venue(page)
        repository = _Repository()
        claim = CycleClaim(
            uuid.uuid4(), uuid.uuid4(), NOW, None, "worker", NOW + timedelta(minutes=10)
        )

        result = PolymarketFreezeService(venue, repository, clock=lambda: NOW).freeze(claim)

        self.assertTrue(repository.persisted)
        self.assertEqual(len(result.payload["market_snapshot_ids"]), 1)

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
            values for query, values in cursor.queries if query.startswith("INSERT INTO events")
        )
        self.assertIsNone(params[5])
        self.assertNotEqual(params[5], event.opens_at)

        snapshot_params = next(
            values
            for query, values in cursor.queries
            if query.startswith("INSERT INTO market_snapshots")
        )
        frozen = json.loads(str(snapshot_params[6]))
        self.assertEqual(frozen["question"], market.question)
        self.assertEqual(frozen["status"], "open")
        self.assertTrue(frozen["tradeable"])
        self.assertEqual(frozen["metadata"], market.venue_metadata)
        self.assertEqual(frozen["outcomes"][0]["venue_token_id"], "t-1-YES")
        self.assertEqual(frozen["outcomes"][0]["tick_size"], "0.01")

    def test_fee_policy_uses_only_fresh_frozen_persisted_basis_points(self) -> None:
        snapshot_id = uuid.uuid4()
        cursor = _Cursor(((30, NOW - timedelta(seconds=10), None),))
        repository = PostgresMarketDataRepository(
            "postgresql://unused", connect=lambda _url: _Connection(cursor)
        )
        policy = repository.frozen_fee_policy(
            "token", cutoff=NOW, fee_rate_snapshot_ids=(snapshot_id,)
        )
        self.assertEqual(policy.rate, Decimal("0.003"))
        query, params = cursor.queries[-1]
        self.assertIn("id = ANY", query)
        self.assertEqual(params, ("token", [snapshot_id]))

    def test_missing_foreign_or_after_cutoff_fee_rate_fails_closed(self) -> None:
        missing = PostgresMarketDataRepository(
            "postgresql://unused", connect=lambda _url: _Connection(_Cursor())
        )
        with self.assertRaises(FrozenFeePolicyUnavailable):
            missing.frozen_fee_policy("token", cutoff=NOW, fee_rate_snapshot_ids=(uuid.uuid4(),))
        after_cutoff = PostgresMarketDataRepository(
            "postgresql://unused",
            connect=lambda _url: _Connection(_Cursor(((30, NOW + timedelta(seconds=1), None),))),
        )
        with self.assertRaisesRegex(FrozenFeePolicyUnavailable, "after cycle cutoff"):
            after_cutoff.frozen_fee_policy(
                "token", cutoff=NOW, fee_rate_snapshot_ids=(uuid.uuid4(),)
            )


if __name__ == "__main__":
    unittest.main()
