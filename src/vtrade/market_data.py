from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

from vtrade.broker import FeePolicy, RejectionCode
from vtrade.domain.types import (
    BinaryMarket,
    CatalogueSnapshot,
    Event,
    Market,
    MarketContext,
    MarketDelta,
    MarketKey,
    MarketStatus,
    OrderBookSnapshot,
    RawArtifact,
    Resolution,
    ResolutionObservation,
)
from vtrade.kalshi import KalshiPublicRestAdapter
from vtrade.order_execution import (
    LiveContextError,
    LiveContextPersistence,
    LiveOrderContext,
    MarketOrderSubmission,
    ValidatedLiveOrderContextProvider,
)
from vtrade.polymarket import (
    FeePolicySnapshot,
    PolymarketError,
    PolymarketTransportError,
    PolymarketVenue,
    RetryPolicy,
)
from vtrade.runtime import (
    ArtifactRegistration,
    CycleClaim,
    MarketFreezeResult,
    six_month_retain_until,
)

_LIVE_CONTEXT_RETRY_POLICY = RetryPolicy(maximum_attempts=1)


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...

    def fetchone(self) -> Sequence[object] | None: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class FrozenPersistence:
    market_snapshot_ids: tuple[uuid.UUID, ...]
    order_book_snapshot_ids: tuple[uuid.UUID, ...]
    resolution_ids: tuple[uuid.UUID, ...]
    fee_rate_snapshot_ids: tuple[uuid.UUID, ...] = ()


class FrozenFeePolicyUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiveMarketReference:
    market_id: uuid.UUID
    market_venue_id: str
    outcome_id: uuid.UUID
    token_id: str
    condition_id: str


class PostgresMarketDataRepository:
    """Transactional normalized persistence for one pre-cutoff market freeze."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def historical_universe(
        self, agent_id: uuid.UUID, *, maximum_outcomes: int = 20
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if maximum_outcomes <= 0:
            raise ValueError("maximum_outcomes must be positive")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT o.venue_token_id, m.venue_market_id FROM outcomes o "
                "JOIN markets m ON m.id = o.market_id LEFT JOIN positions p "
                "ON p.outcome_id = o.id AND p.agent_id = %s LEFT JOIN order_intents oi "
                "ON oi.outcome_id = o.id AND oi.agent_cycle_id IN "
                "(SELECT id FROM agent_cycles WHERE agent_id = %s) "
                "WHERE (p.shares > 0 OR oi.id IS NOT NULL) "
                "ORDER BY o.venue_token_id LIMIT %s",
                (agent_id, agent_id, maximum_outcomes),
            )
            rows = cursor.fetchall()
        return (
            tuple(str(row[0]) for row in rows),
            tuple(dict.fromkeys(str(row[1]) for row in rows)),
        )

    def historical_discovery_universe(
        self, agent_id: uuid.UUID, *, maximum_outcomes: int = 20
    ) -> tuple[str, ...]:
        """Return only historical outcomes still eligible for discovery.

        ``historical_universe`` intentionally remains unfiltered because its market
        IDs are also used to request resolution observations. Closed historical
        markets must not consume the active discovery allowance or order-book/fee
        requests.
        """
        if maximum_outcomes <= 0:
            raise ValueError("maximum_outcomes must be positive")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT o.venue_token_id FROM outcomes o "
                "JOIN markets m ON m.id = o.market_id LEFT JOIN positions p "
                "ON p.outcome_id = o.id AND p.agent_id = %s LEFT JOIN order_intents oi "
                "ON oi.outcome_id = o.id AND oi.agent_cycle_id IN "
                "(SELECT id FROM agent_cycles WHERE agent_id = %s) "
                "WHERE (p.shares > 0 OR oi.id IS NOT NULL) "
                "AND m.status = 'open' AND m.tradeable AND o.tradeable "
                "ORDER BY o.venue_token_id LIMIT %s",
                (agent_id, agent_id, maximum_outcomes),
            )
            rows = cursor.fetchall()
        return tuple(str(row[0]) for row in rows)

    def held_universe(self, agent_id: uuid.UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return every currently held outcome; valuation may never truncate this set."""
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT o.venue_token_id, m.venue_market_id FROM positions p "
                "JOIN outcomes o ON o.id = p.outcome_id "
                "JOIN markets m ON m.id = o.market_id "
                "WHERE p.agent_id = %s AND p.shares > 0 "
                "ORDER BY o.venue_token_id",
                (agent_id,),
            )
            rows = cursor.fetchall()
        return (
            tuple(str(row[0]) for row in rows),
            tuple(dict.fromkeys(str(row[1]) for row in rows)),
        )

    def persist_freeze(
        self,
        pages: Sequence[MarketDelta],
        books: Sequence[OrderBookSnapshot],
        resolutions: Sequence[Resolution],
        fee_policies: Sequence[FeePolicySnapshot] = (),
    ) -> FrozenPersistence:
        snapshot_ids: list[uuid.UUID] = []
        book_ids: list[uuid.UUID] = []
        resolution_ids: list[uuid.UUID] = []
        fee_rate_ids: list[uuid.UUID] = []
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            for page in pages:
                cursor.execute(
                    "INSERT INTO venue_sync_pages "
                    "(id, venue, resource, requested_cursor, next_cursor, record_count, "
                    "observed_at, raw_artifact_uri, raw_sha256) VALUES "
                    "(%s, 'polymarket', %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (venue, resource, observed_at, raw_sha256) DO NOTHING",
                    (
                        _id(
                            "sync",
                            page.resource,
                            page.observed_at.isoformat(),
                            page.artifact.sha256,
                        ),
                        page.resource,
                        page.requested_cursor,
                        page.next_cursor,
                        len(page.markets),
                        page.observed_at,
                        page.artifact.uri,
                        page.artifact.sha256,
                    ),
                )
                for event in page.events:
                    self._upsert_event(cursor, event)
                for market in page.markets:
                    self._upsert_market(cursor, market)
                    snapshot_id = _id(
                        "market-snapshot",
                        market.id,
                        page.observed_at.isoformat(),
                        page.artifact.sha256,
                    )
                    snapshot_ids.append(snapshot_id)
                    cursor.execute(
                        "INSERT INTO market_snapshots "
                        "(id, market_id, cutoff, status, volume_micros, liquidity_micros, "
                        "payload, raw_artifact_uri, raw_sha256) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                        "ON CONFLICT (market_id, cutoff, raw_sha256) DO NOTHING",
                        (
                            snapshot_id,
                            _id("market", market.id),
                            page.observed_at,
                            market.status.value,
                            int(market.volume_micros),
                            int(market.liquidity_micros),
                            json.dumps(_market_snapshot_payload(market), default=str),
                            page.artifact.uri,
                            page.artifact.sha256,
                        ),
                    )
            for book in books:
                outcome_id = _id("outcome", f"polymarket:outcome:{book.token_id}")
                book_id = _id(
                    "book", book.token_id, book.observed_at.isoformat(), book.artifact.sha256
                )
                book_ids.append(book_id)
                cursor.execute(
                    "INSERT INTO order_book_snapshots "
                    "(id, outcome_id, cutoff, source_created_at, bids, asks, best_bid, "
                    "best_ask, raw_artifact_uri, raw_sha256) VALUES "
                    "(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s) "
                    "ON CONFLICT (outcome_id, cutoff, raw_sha256) DO NOTHING",
                    (
                        book_id,
                        outcome_id,
                        book.observed_at,
                        book.source_created_at,
                        json.dumps(
                            [{"price": str(x.price), "size": str(x.size)} for x in book.bids]
                        ),
                        json.dumps(
                            [{"price": str(x.price), "size": str(x.size)} for x in book.asks]
                        ),
                        book.best_bid,
                        book.best_ask,
                        book.artifact.uri,
                        book.artifact.sha256,
                    ),
                )
            for resolution in resolutions:
                resolution_id = _id(
                    "resolution",
                    resolution.market_id,
                    resolution.source_created_at.isoformat(),
                    resolution.artifact.sha256,
                )
                resolution_ids.append(resolution_id)
                cursor.execute(
                    "INSERT INTO resolutions "
                    "(id, market_id, winning_outcome_id, result, source_created_at, "
                    "observed_at, eligible_after, raw_artifact_uri, raw_sha256) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (market_id, source_created_at, raw_sha256) DO NOTHING",
                    (
                        resolution_id,
                        _id("market", resolution.market_id),
                        (
                            _id("outcome", resolution.winning_outcome_id)
                            if resolution.winning_outcome_id is not None
                            else None
                        ),
                        resolution.result,
                        resolution.source_created_at,
                        resolution.observed_at,
                        resolution.eligible_after,
                        resolution.artifact.uri,
                        resolution.artifact.sha256,
                    ),
                )
            books_by_condition: dict[str, tuple[OrderBookSnapshot, ...]] = {}
            for book in books:
                books_by_condition.setdefault(book.condition_id, ())
                books_by_condition[book.condition_id] += (book,)
            for fee_policy in fee_policies:
                for book in books_by_condition.get(fee_policy.condition_id, ()):
                    fee_rate_id = _id(
                        "fee-policy",
                        fee_policy.condition_id,
                        book.token_id,
                        fee_policy.observed_at.isoformat(),
                        fee_policy.artifact.sha256,
                    )
                    fee_rate_ids.append(fee_rate_id)
                    cursor.execute(
                        "INSERT INTO fee_rate_snapshots "
                        "(id, outcome_id, token_id, condition_id, base_fee_bps, fee_rate, "
                        "fee_exponent, fee_taker_only, observed_at, source_created_at, "
                        "raw_artifact_uri, raw_sha256) VALUES "
                        "(%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (token_id, observed_at, raw_sha256) DO NOTHING",
                        (
                            fee_rate_id,
                            _id("outcome", f"polymarket:outcome:{book.token_id}"),
                            book.token_id,
                            fee_policy.condition_id,
                            fee_policy.rate,
                            fee_policy.exponent,
                            fee_policy.taker_only,
                            fee_policy.observed_at,
                            fee_policy.source_created_at,
                            fee_policy.artifact.uri,
                            fee_policy.artifact.sha256,
                        ),
                    )
        return FrozenPersistence(
            tuple(snapshot_ids),
            tuple(book_ids),
            tuple(resolution_ids),
            tuple(fee_rate_ids),
        )

    def frozen_fee_policy(
        self,
        token_id: str,
        *,
        cutoff: datetime,
        fee_rate_snapshot_ids: Sequence[uuid.UUID],
    ) -> FeePolicy:
        cutoff = self._aware(cutoff)
        if not token_id or not fee_rate_snapshot_ids:
            raise FrozenFeePolicyUnavailable(
                "token and current-cycle fee snapshot membership are required"
            )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT fee_rate, fee_exponent, fee_taker_only, observed_at, source_created_at "
                "FROM fee_rate_snapshots WHERE token_id = %s AND id = ANY(%s::uuid[]) "
                "ORDER BY observed_at DESC, id DESC LIMIT 1",
                (token_id, list(fee_rate_snapshot_ids)),
            )
            rows = cursor.fetchall()
        if not rows:
            raise FrozenFeePolicyUnavailable(
                f"no frozen fee rate exists for token {token_id} at cycle cutoff"
            )
        row = rows[0]
        observed = self._aware(cast(datetime, row[3]))
        source = self._aware(cast(datetime, row[4])) if row[4] is not None else None
        if observed > cutoff or (source is not None and source > cutoff):
            raise FrozenFeePolicyUnavailable("fee rate timestamp is after cycle cutoff")
        rate = Decimal(str(row[0]))
        if not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1):
            raise FrozenFeePolicyUnavailable("persisted fee rate is outside 0..1")
        exponent = Decimal(str(row[1])) if row[1] is not None else None
        taker_only = row[2]
        if not isinstance(taker_only, bool):
            raise FrozenFeePolicyUnavailable("persisted fee taker_only is not boolean")
        return FeePolicy(
            rate=rate,
            enabled=rate > 0,
            exponent=exponent,
            taker_only=taker_only,
        )

    def live_market_reference(self, outcome_id: uuid.UUID) -> LiveMarketReference:
        """Resolve stable venue identifiers without consulting the frozen cycle."""
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT m.id, m.venue_market_id, m.condition_id, o.id, "
                "o.venue_token_id FROM outcomes o JOIN markets m ON m.id = o.market_id "
                "WHERE o.id = %s",
                (outcome_id,),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise FrozenFeePolicyUnavailable("live order outcome metadata is unavailable")
        row = rows[0]
        condition_id = str(row[2] or "")
        token_id = str(row[4] or "")
        if not condition_id or not token_id:
            raise FrozenFeePolicyUnavailable("live order metadata lacks token or condition")
        return LiveMarketReference(
            uuid.UUID(str(row[0])),
            str(row[1]),
            uuid.UUID(str(row[3])),
            token_id,
            condition_id,
        )

    def persist_live_order_context(
        self,
        market: Market,
        book: OrderBookSnapshot,
        fee_policy: FeePolicySnapshot,
        *,
        market_artifact: RawArtifact | None = None,
    ) -> FrozenPersistence:
        """Append one live market/book/fee observation without changing old snapshots."""
        market_artifact = market_artifact or book.artifact
        market_cutoff = market.observed_at or book.observed_at
        market_snapshot_id = _id(
            "market-snapshot",
            market.id,
            market_cutoff.isoformat(),
            market_artifact.sha256,
        )
        book_id = _id("book", book.token_id, book.observed_at.isoformat(), book.artifact.sha256)
        fee_id = _id(
            "fee-policy",
            fee_policy.condition_id,
            book.token_id,
            fee_policy.observed_at.isoformat(),
            fee_policy.artifact.sha256,
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            self._upsert_market(cursor, market)
            cursor.execute(
                "INSERT INTO market_snapshots "
                "(id, market_id, cutoff, status, volume_micros, liquidity_micros, "
                "payload, raw_artifact_uri, raw_sha256) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (market_id, cutoff, raw_sha256) DO NOTHING",
                (
                    market_snapshot_id,
                    _id("market", market.id),
                    market_cutoff,
                    market.status.value,
                    int(market.volume_micros),
                    int(market.liquidity_micros),
                    json.dumps(_market_snapshot_payload(market), default=str),
                    market_artifact.uri,
                    market_artifact.sha256,
                ),
            )
            cursor.execute(
                "SELECT id FROM market_snapshots WHERE market_id = %s AND cutoff = %s "
                "AND raw_sha256 = %s",
                (_id("market", market.id), market_cutoff, market_artifact.sha256),
            )
            market_row = cursor.fetchone()
            if market_row is None:
                raise RuntimeError("live market snapshot disappeared after persistence")
            market_snapshot_id = uuid.UUID(str(market_row[0]))
            cursor.execute(
                "INSERT INTO order_book_snapshots "
                "(id, outcome_id, cutoff, source_created_at, bids, asks, best_bid, "
                "best_ask, raw_artifact_uri, raw_sha256) VALUES "
                "(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s) "
                "ON CONFLICT (outcome_id, cutoff, raw_sha256) DO NOTHING",
                (
                    book_id,
                    _id("outcome", f"polymarket:outcome:{book.token_id}"),
                    book.observed_at,
                    book.source_created_at,
                    json.dumps([{"price": str(x.price), "size": str(x.size)} for x in book.bids]),
                    json.dumps([{"price": str(x.price), "size": str(x.size)} for x in book.asks]),
                    book.best_bid,
                    book.best_ask,
                    book.artifact.uri,
                    book.artifact.sha256,
                ),
            )
            cursor.execute(
                "SELECT id FROM order_book_snapshots WHERE outcome_id = %s AND cutoff = %s "
                "AND raw_sha256 = %s",
                (
                    _id("outcome", f"polymarket:outcome:{book.token_id}"),
                    book.observed_at,
                    book.artifact.sha256,
                ),
            )
            book_row = cursor.fetchone()
            if book_row is None:
                raise RuntimeError("live order-book snapshot disappeared after persistence")
            book_id = uuid.UUID(str(book_row[0]))
            cursor.execute(
                "INSERT INTO fee_rate_snapshots "
                "(id, outcome_id, token_id, condition_id, base_fee_bps, fee_rate, "
                "fee_exponent, fee_taker_only, observed_at, source_created_at, "
                "raw_artifact_uri, raw_sha256) VALUES "
                "(%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (token_id, observed_at, raw_sha256) DO NOTHING",
                (
                    fee_id,
                    _id("outcome", f"polymarket:outcome:{book.token_id}"),
                    book.token_id,
                    fee_policy.condition_id,
                    fee_policy.rate,
                    fee_policy.exponent,
                    fee_policy.taker_only,
                    fee_policy.observed_at,
                    fee_policy.source_created_at,
                    fee_policy.artifact.uri,
                    fee_policy.artifact.sha256,
                ),
            )
            cursor.execute(
                "SELECT id FROM fee_rate_snapshots WHERE token_id = %s AND observed_at = %s "
                "AND raw_sha256 = %s",
                (book.token_id, fee_policy.observed_at, fee_policy.artifact.sha256),
            )
            fee_row = cursor.fetchone()
            if fee_row is None:
                raise RuntimeError("live fee snapshot disappeared after persistence")
            fee_id = uuid.UUID(str(fee_row[0]))
        return FrozenPersistence((market_snapshot_id,), (book_id,), (), (fee_id,))

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fee timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _upsert_event(cursor: _Cursor, event: Event) -> None:
        cursor.execute(
            "INSERT INTO events (id, venue, venue_event_id, slug, title, metadata, "
            "source_created_at, observed_at) VALUES "
            "(%s, 'polymarket', %s, %s, %s, %s::jsonb, %s, %s) "
            "ON CONFLICT (venue, venue_event_id) DO UPDATE SET slug = EXCLUDED.slug, "
            "title = EXCLUDED.title, metadata = EXCLUDED.metadata, "
            "observed_at = EXCLUDED.observed_at",
            (
                _id("event", event.id),
                event.venue_id,
                event.slug,
                event.title,
                json.dumps(event.venue_metadata, default=str),
                None,
                event.observed_at,
            ),
        )

    @staticmethod
    def _upsert_market(cursor: _Cursor, market: Market) -> None:
        cursor.execute(
            "INSERT INTO markets "
            "(id, event_id, venue, venue_market_id, condition_id, slug, question, "
            "resolution_rules, status, category, opens_at, closes_at, source_updated_at, "
            "observed_at, metadata, tradeable, resolution_source) VALUES "
            "(%s, %s, 'polymarket', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s::jsonb, %s, %s) ON CONFLICT (venue, venue_market_id) DO UPDATE SET "
            "question = EXCLUDED.question, resolution_rules = EXCLUDED.resolution_rules, "
            "slug = EXCLUDED.slug, status = EXCLUDED.status, category = EXCLUDED.category, "
            "opens_at = EXCLUDED.opens_at, closes_at = EXCLUDED.closes_at, "
            "source_updated_at = EXCLUDED.source_updated_at, "
            "observed_at = EXCLUDED.observed_at, metadata = EXCLUDED.metadata, "
            "tradeable = EXCLUDED.tradeable, resolution_source = EXCLUDED.resolution_source",
            (
                _id("market", market.id),
                _id("event", market.event_id),
                market.venue_id,
                str(market.venue_metadata.get("condition_id") or "") or None,
                market.slug or market.venue_id,
                market.question,
                market.resolution_rules,
                market.status.value,
                market.category,
                market.opens_at,
                market.closes_at,
                market.source_updated_at,
                market.observed_at,
                json.dumps(market.venue_metadata, default=str),
                market.tradeable,
                market.resolution_source,
            ),
        )
        for outcome in market.outcomes:
            cursor.execute(
                "INSERT INTO outcomes "
                "(id, market_id, venue_token_id, name, outcome_index, tick_size, "
                "minimum_order_size, indicative_price, tradeable, metadata) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (venue_token_id) DO UPDATE SET name = EXCLUDED.name, "
                "outcome_index = EXCLUDED.outcome_index, tick_size = EXCLUDED.tick_size, "
                "minimum_order_size = EXCLUDED.minimum_order_size, "
                "indicative_price = EXCLUDED.indicative_price, tradeable = EXCLUDED.tradeable, "
                "metadata = EXCLUDED.metadata",
                (
                    _id("outcome", outcome.id),
                    _id("market", market.id),
                    outcome.venue_token_id,
                    outcome.name,
                    outcome.outcome_index,
                    str(outcome.tick_size_micros / 1_000_000),
                    str(outcome.minimum_order_micros / 1_000_000),
                    outcome.indicative_price,
                    outcome.tradeable,
                    json.dumps(outcome.venue_metadata, default=str),
                ),
            )


class PostgresLiveOrderContextProvider:
    """Refresh and archive only the market data required by one paper order."""

    def __init__(
        self,
        repository: PostgresMarketDataRepository,
        venue: PolymarketVenue,
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float] | None = None,
        maximum_book_age: timedelta = timedelta(minutes=5),
        maximum_source_skew: timedelta = timedelta(seconds=5),
        maximum_build_time: timedelta = timedelta(seconds=10),
    ) -> None:
        self._repository = repository
        self._venue = venue
        if monotonic is None:
            self._validator = ValidatedLiveOrderContextProvider(
                self._refresh,
                clock=clock,
                persist=self._persist_live_order_context,
                maximum_build_time=maximum_build_time,
                maximum_observation_age=maximum_book_age,
                maximum_source_skew=maximum_source_skew,
            )
        else:
            self._validator = ValidatedLiveOrderContextProvider(
                self._refresh,
                clock=clock,
                monotonic=monotonic,
                persist=self._persist_live_order_context,
                maximum_build_time=maximum_build_time,
                maximum_observation_age=maximum_book_age,
                maximum_source_skew=maximum_source_skew,
            )

    def build(
        self, submission: MarketOrderSubmission, *, requested_at: datetime
    ) -> LiveOrderContext:
        return self._validator.build(submission, requested_at=requested_at)

    def _refresh(
        self, submission: MarketOrderSubmission, requested_at: datetime
    ) -> LiveOrderContext:
        try:
            reference = self._repository.live_market_reference(submission.outcome_id)
            if (
                reference.market_id != submission.market_id
                or reference.outcome_id != submission.outcome_id
            ):
                raise LiveContextError(
                    "live market reference does not match the order intent",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            get_market_observation = getattr(self._venue, "get_market_observation", None)
            market_observation = (
                self._live_call(get_market_observation, reference.market_venue_id)
                if get_market_observation is not None
                else None
            )
            market = (
                market_observation.market
                if market_observation is not None
                else self._venue.get_market(reference.market_venue_id)
            )
            market_artifact = (
                market_observation.artifact if market_observation is not None else None
            )
            if (
                market.venue_id != reference.market_venue_id
                or str(market.venue_metadata.get("condition_id") or "")
                != reference.condition_id
            ):
                raise LiveContextError(
                    "live market metadata does not match the canonical market",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            raw_outcome = next(
                (
                    outcome
                    for outcome in market.outcomes
                    if outcome.venue_token_id == reference.token_id
                ),
                None,
            )
            if raw_outcome is None:
                raise LiveContextError(
                    "live market metadata does not contain the requested token",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            books = self._live_call(self._venue.get_order_book, [reference.token_id])
            if len(books) != 1:
                raise LiveContextError(
                    "live order-book refresh returned an unexpected number of books",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            book = books[0]
            if book.token_id != reference.token_id or book.condition_id != reference.condition_id:
                raise LiveContextError(
                    "live order-book condition does not match market metadata",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            policies = self._live_call(self._venue.get_fee_policies, [reference.condition_id])
            if len(policies) != 1:
                raise LiveContextError(
                    "live fee refresh returned an unexpected number of policies",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            raw_fee = policies[0]
            if raw_fee.condition_id != book.condition_id:
                raise LiveContextError(
                    "live fee policy condition does not match the order-book",
                    code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                )
            live_outcome = replace(
                raw_outcome,
                id=str(reference.outcome_id),
                market_id=str(reference.market_id),
            )
            live_market = replace(
                market,
                id=str(reference.market_id),
                outcomes=(live_outcome,),
            )
            return LiveOrderContext(
                market=live_market,
                outcome=live_outcome,
                book=book,
                fee_policy=FeePolicy(
                    raw_fee.rate,
                    enabled=raw_fee.rate > 0,
                    exponent=raw_fee.exponent,
                    taker_only=raw_fee.taker_only,
                ),
                market_snapshot_id=uuid.UUID(int=0),
                book_snapshot_id=uuid.UUID(int=0),
                fee_rate_snapshot_id=uuid.UUID(int=0),
                requested_at=requested_at,
                validated_at=requested_at,
                market_observed_at=market.observed_at or book.observed_at,
                book_observed_at=book.observed_at,
                fee_observed_at=raw_fee.observed_at,
                artifact_hashes=(
                    (
                        "market",
                        market_artifact.sha256
                        if market_artifact is not None
                        else book.artifact.sha256,
                    ),
                    ("order_book", book.artifact.sha256),
                    ("fee_policy", raw_fee.artifact.sha256),
                ),
                persistence_payload=LiveContextPersistence(
                    market=market,
                    book=book,
                    fee_policy=raw_fee,
                    market_artifact=market_artifact,
                ),
            )
        except LiveContextError:
            raise
        except (PolymarketTransportError, TimeoutError, ConnectionError, OSError) as exc:
            raise LiveContextError(
                "live market provider failed while refreshing the order context",
                code=RejectionCode.NETWORK_ERROR,
                retryable=True,
            ) from exc
        except PolymarketError as exc:
            raise LiveContextError(
                "live market provider returned invalid order metadata",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            ) from exc

    def _persist_live_order_context(self, context: LiveOrderContext) -> LiveOrderContext:
        if context.persistence_payload is None:
            raise LiveContextError(
                "live context lacks its immutable persistence payload",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            )
        payload = context.persistence_payload
        persisted = self._repository.persist_live_order_context(
            payload.market,
            payload.book,
            payload.fee_policy,
            market_artifact=payload.market_artifact,
        )
        return replace(
            context,
            market_snapshot_id=persisted.market_snapshot_ids[0],
            book_snapshot_id=persisted.order_book_snapshot_ids[0],
            fee_rate_snapshot_id=persisted.fee_rate_snapshot_ids[0],
            persistence_payload=None,
        )

    @staticmethod
    def _live_call(method: Callable[..., Any], *args: Any) -> Any:
        try:
            return method(*args, retry_policy=_LIVE_CONTEXT_RETRY_POLICY)
        except TypeError as exc:
            if "retry_policy" not in str(exc):
                raise
            return method(*args)


class PolymarketFreezeService:
    """The only cycle component allowed to fetch venue data before cutoff finalization."""

    def __init__(
        self,
        venue: PolymarketVenue,
        repository: PostgresMarketDataRepository,
        *,
        clock: Callable[[], datetime],
        maximum_historical_outcomes: int = 20,
        # May cause latency; decrease below 50 if needed, increase to 100+ otherwise.
        # This bounds how many markets the agent can choose from.
        maximum_additional_markets: int = 80,
        venue_batch_size: int = 20,
    ) -> None:
        if (
            maximum_historical_outcomes <= 0
            or maximum_additional_markets <= 0
            or not 1 <= venue_batch_size <= 20
        ):
            raise ValueError("freeze shortlist and venue batch bounds are invalid")
        self._venue = venue
        self._repository = repository
        self._clock = clock
        self._maximum_historical_outcomes = maximum_historical_outcomes
        self._maximum_additional_markets = maximum_additional_markets
        self._venue_batch_size = venue_batch_size

    def freeze(self, claim: CycleClaim) -> MarketFreezeResult:
        if claim.data_cutoff is not None:
            raise ValueError("market freeze cannot fetch after a cycle cutoff is finalized")
        pages = self._venue.sync_all_markets()
        if not pages:
            raise RuntimeError("bounded Polymarket market synchronization returned no pages")
        held_tokens, held_markets = self._repository.held_universe(claim.agent_id)
        historical_discovery_tokens = self._repository.historical_discovery_universe(
            claim.agent_id,
            maximum_outcomes=self._maximum_historical_outcomes,
        )
        _historical_resolution_tokens, historical_markets = self._repository.historical_universe(
            claim.agent_id,
            maximum_outcomes=self._maximum_historical_outcomes,
        )
        tokens = list(dict.fromkeys(held_tokens))
        for token in historical_discovery_tokens:
            if token not in tokens:
                tokens.append(token)
        candidates = sorted(
            (
                market
                for page in pages
                for market in page.markets
                if market.status is MarketStatus.OPEN and market.tradeable
            ),
            key=lambda market: (
                int(market.volume_micros),
                int(market.liquidity_micros),
                market.id,
            ),
            reverse=True,
        )
        selected_additional_markets = 0
        for market in candidates:
            new_tokens = [
                outcome.venue_token_id
                for outcome in market.outcomes
                if outcome.tradeable and outcome.venue_token_id not in tokens
            ]
            if not new_tokens:
                continue
            if selected_additional_markets >= self._maximum_additional_markets:
                break
            tokens.extend(new_tokens)
            selected_additional_markets += 1
        books = tuple(
            item
            for batch in _batches(tokens, self._venue_batch_size)
            for item in self._venue.get_order_book(batch)
        )
        condition_ids = tuple(
            dict.fromkeys(book.condition_id for book in books if book.condition_id)
        )
        fee_policies = tuple(
            item
            for batch in _batches(condition_ids, self._venue_batch_size)
            for item in self._venue.get_fee_policies(batch)
        )
        resolution_markets = tuple(dict.fromkeys((*held_markets, *historical_markets)))
        resolutions = tuple(
            item
            for batch in _batches(resolution_markets, 100)
            for item in self._venue.sync_resolutions(batch)
        )
        persisted = self._repository.persist_freeze(pages, books, resolutions, fee_policies)
        selected_tokens = set(tokens)
        persisted_markets = tuple(market for page in pages for market in page.markets)
        if len(persisted.market_snapshot_ids) != len(persisted_markets):
            raise RuntimeError("persisted market snapshot membership is incomplete")
        selected_market_snapshot_ids = tuple(
            snapshot_id
            for snapshot_id, market in zip(
                persisted.market_snapshot_ids, persisted_markets, strict=True
            )
            if any(outcome.venue_token_id in selected_tokens for outcome in market.outcomes)
        )
        completed = self._aware(self._clock())
        artifacts = tuple(
            ArtifactRegistration(
                item.uri, item.sha256, item.byte_length, six_month_retain_until(completed)
            )
            for item in (
                *(page.artifact for page in pages),
                *(book.artifact for book in books),
                *(fee.artifact for fee in fee_policies),
                *(resolution.artifact for resolution in resolutions),
            )
        )
        freshest = max(
            (
                *(page.observed_at for page in pages),
                *(book.observed_at for book in books),
                *(fee.observed_at for fee in fee_policies),
                *(resolution.observed_at for resolution in resolutions),
            ),
            default=completed,
        )
        if freshest > completed:
            raise ValueError("frozen market data is newer than freeze completion")
        return MarketFreezeResult(
            {
                "market_snapshot_ids": [str(value) for value in selected_market_snapshot_ids],
                "order_book_snapshot_ids": [
                    str(value) for value in persisted.order_book_snapshot_ids
                ],
                "resolution_ids": [str(value) for value in persisted.resolution_ids],
                "fee_rate_snapshot_ids": [str(value) for value in persisted.fee_rate_snapshot_ids],
                "order_book_token_ids": tokens,
            },
            artifacts,
            freshest,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("freeze clock must be timezone-aware")
        return value.astimezone(UTC)


def _id(kind: str, *parts: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, ":".join(("vtrade", kind, *parts)))


def _batches(values: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _market_snapshot_payload(market: Market) -> dict[str, object]:
    """Freeze every mutable normalized field consumed by tools or paper execution."""
    return {
        "venue_market_id": market.venue_id,
        "slug": market.slug or market.venue_id,
        "event_id": str(_id("event", market.event_id)),
        "question": market.question,
        "resolution_rules": market.resolution_rules,
        "opens_at": market.opens_at.isoformat() if market.opens_at else None,
        "closes_at": market.closes_at.isoformat() if market.closes_at else None,
        "category": market.category,
        "status": market.status.value,
        "tradeable": market.tradeable,
        "source_updated_at": (
            market.source_updated_at.isoformat() if market.source_updated_at else None
        ),
        "observed_at": market.observed_at.isoformat() if market.observed_at else None,
        "resolution_source": market.resolution_source,
        "metadata": market.venue_metadata,
        "outcomes": [
            {
                "id": outcome.id,
                "venue_token_id": outcome.venue_token_id,
                "name": outcome.name,
                "outcome_index": outcome.outcome_index,
                "tick_size": str(outcome.tick_size_micros / 1_000_000),
                "minimum_order_size": str(outcome.minimum_order_micros / 1_000_000),
                "indicative_price": (
                    str(outcome.indicative_price) if outcome.indicative_price is not None else None
                ),
                "tradeable": outcome.tradeable,
                "metadata": outcome.venue_metadata,
            }
            for outcome in market.outcomes
        ],
    }


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


# ---------------------------------------------------------------------------
# Kalshi v1 immutable freeze
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KalshiFreezeRequest:
    """Inputs owned by the cycle boundary, not by the public REST adapter."""

    held_markets: tuple[MarketKey, ...] = ()
    touched_markets: tuple[MarketKey, ...] = ()
    historical_markets: tuple[MarketKey, ...] = ()
    cutoff: datetime | None = None
    maximum_historical_markets: int = 20
    maximum_additional_markets: int = 80

    def __post_init__(self) -> None:
        if self.cutoff is not None and (
            self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None
        ):
            raise ValueError("freeze cutoff must be timezone-aware")
        if self.maximum_historical_markets < 0 or self.maximum_additional_markets < 0:
            raise ValueError("freeze retention limits cannot be negative")
        for name, values in (
            ("held_markets", self.held_markets),
            ("touched_markets", self.touched_markets),
            ("historical_markets", self.historical_markets),
        ):
            if any(not isinstance(value, MarketKey) for value in values):
                raise ValueError(f"{name} must contain MarketKey values")


@dataclass(frozen=True, slots=True)
class KalshiMarketFreeze:
    """The only publishable result of a complete catalogue/book cycle."""

    catalogue: CatalogueSnapshot
    discovery_market_keys: tuple[MarketKey, ...]
    resolution_market_keys: tuple[MarketKey, ...]
    contexts: tuple[MarketContext, ...]
    resolutions: tuple[ResolutionObservation, ...]
    data_cutoff: datetime
    artifacts: tuple[RawArtifact, ...]

    def __post_init__(self) -> None:
        if self.data_cutoff.tzinfo is None or self.data_cutoff.utcoffset() is None:
            raise ValueError("freeze data_cutoff must be timezone-aware")
        context_keys = tuple(context.market.key for context in self.contexts)
        if context_keys != self.discovery_market_keys:
            raise ValueError("freeze context order/membership is incomplete")
        if any(
            observation.observed_at > self.data_cutoff
            or (
                observation.audit.observed_at is not None
                and observation.audit.observed_at > self.data_cutoff
            )
            for observation in self.resolutions
        ):
            raise ValueError("freeze contains evidence newer than its data cutoff")

    @property
    def markets(self) -> tuple[BinaryMarket, ...]:
        return tuple(context.market for context in self.contexts)


class _KalshiFreezeVenue(Protocol):
    def sync_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot: ...

    def get_context(self, market_key: MarketKey, *, cutoff: datetime) -> MarketContext: ...

    def get_resolutions(
        self, market_keys: Sequence[MarketKey], *, cutoff: datetime
    ) -> tuple[ResolutionObservation, ...]: ...


class KalshiMarketFreezeService:
    """Apply deterministic retention and bounded book reads around a venue seam."""

    def __init__(
        self,
        venue: _KalshiFreezeVenue | KalshiPublicRestAdapter,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        maximum_parallel_book_requests: int = 8,
        freeze_deadline_seconds: float = 600.0,
    ) -> None:
        if maximum_parallel_book_requests < 1:
            raise ValueError("maximum_parallel_book_requests must be positive")
        if freeze_deadline_seconds <= 0:
            raise ValueError("freeze_deadline_seconds must be positive")
        self._venue = venue
        self._clock = clock
        self._maximum_parallel_book_requests = maximum_parallel_book_requests
        self._freeze_deadline_seconds = freeze_deadline_seconds

    def freeze(self, request: KalshiFreezeRequest | None = None) -> KalshiMarketFreeze:
        freeze_request = request or KalshiFreezeRequest()
        self._aware(self._clock(), "freeze start")
        deadline = time.monotonic() + self._freeze_deadline_seconds
        catalogue = self._venue.sync_catalogue(cutoff=freeze_request.cutoff)
        by_key = {market.key: market for market in catalogue.markets}
        operation_cutoff = self._aware(
            freeze_request.cutoff
            or self._clock() + timedelta(seconds=self._freeze_deadline_seconds),
            "freeze operation cutoff",
        )
        discovery_keys = self._select_discovery_keys(freeze_request, catalogue.markets)
        context_keys = tuple(key for key in discovery_keys if key in by_key)
        contexts = self._read_contexts(context_keys, operation_cutoff, deadline)
        resolution_keys = tuple(
            dict.fromkeys(
                (
                    *freeze_request.held_markets,
                    *freeze_request.touched_markets,
                    *freeze_request.historical_markets,
                )
            )
        )
        resolution_cutoff = self._aware(
            freeze_request.cutoff
            or self._clock() + timedelta(seconds=self._freeze_deadline_seconds),
            "resolution operation cutoff",
        )
        resolutions = self._venue.get_resolutions(resolution_keys, cutoff=resolution_cutoff)
        completed = self._aware(self._clock(), "freeze completion")
        if time.monotonic() > deadline:
            raise RuntimeError("Kalshi freeze deadline exceeded before publication")
        data_cutoff = self._aware(freeze_request.cutoff or completed, "freeze data cutoff")
        finalized_contexts = tuple(
            MarketContext(
                context.market,
                replace(context.order_book, cutoff=data_cutoff),
            )
            for context in contexts
        )
        for context in finalized_contexts:
            if (
                context.order_book.observed_at > data_cutoff
                or context.market.observed_at > data_cutoff
            ):
                raise RuntimeError("book observation is newer than the freeze cutoff")
        historical_cutoff = getattr(self._venue, "last_historical_cutoff", None)
        cutoff_artifact = (
            historical_cutoff.audit if historical_cutoff is not None else None
        )
        artifacts = self._collect_artifacts(
            catalogue,
            finalized_contexts,
            resolutions,
            extra=(cutoff_artifact,) if cutoff_artifact is not None else (),
        )
        # Construction is deliberately the publication point.  No service state
        # is mutated before every required read and evidence reference is valid.
        return KalshiMarketFreeze(
            catalogue,
            context_keys,
            resolution_keys,
            finalized_contexts,
            resolutions,
            data_cutoff,
            artifacts,
        )

    def freeze_markets(self, request: KalshiFreezeRequest | None = None) -> KalshiMarketFreeze:
        return self.freeze(request)

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _select_discovery_keys(
        request: KalshiFreezeRequest, markets: Sequence[BinaryMarket]
    ) -> tuple[MarketKey, ...]:
        by_key = {market.key: market for market in markets}
        retained: list[MarketKey] = []
        for key in (
            *request.historical_markets[: request.maximum_historical_markets],
            *request.held_markets,
            *request.touched_markets,
        ):
            market = by_key.get(key)
            if market is not None and market.tradeable and key not in retained:
                retained.append(key)
        candidates = sorted(
            (market for market in markets if market.tradeable and market.key not in retained),
            key=lambda market: (
                -int(market.volume),
                -int(market.liquidity_micros),
                market.key.market_ref,
            ),
        )
        retained.extend(
            market.key
            for market in candidates[: request.maximum_additional_markets]
            if market.key not in retained
        )
        return tuple(retained)

    def _read_contexts(
        self, keys: Sequence[MarketKey], cutoff: datetime, deadline: float
    ) -> tuple[MarketContext, ...]:
        if not keys:
            return ()

        def read(key: MarketKey) -> MarketContext:
            if time.monotonic() > deadline:
                raise RuntimeError("Kalshi freeze deadline exceeded while reading books")
            return self._venue.get_context(key, cutoff=cutoff)

        with ThreadPoolExecutor(
            max_workers=min(self._maximum_parallel_book_requests, len(keys))
        ) as executor:
            return tuple(executor.map(read, keys))

    @staticmethod
    def _collect_artifacts(
        catalogue: CatalogueSnapshot,
        contexts: Sequence[MarketContext],
        resolutions: Sequence[ResolutionObservation],
        *,
        extra: Sequence[RawArtifact] = (),
    ) -> tuple[RawArtifact, ...]:
        values = [*catalogue.artifacts, *extra]
        values.extend(context.market.audit for context in contexts)
        values.extend(context.order_book.artifact for context in contexts)
        values.extend(observation.audit for observation in resolutions)
        unique: dict[str, RawArtifact] = {}
        for artifact in values:
            # The same exact response bytes can be referenced by a page,
            # normalized observation, and a later idempotent read.  The hash
            # is the content identity; request metadata remains available on
            # each typed observation and must not manufacture a second object.
            unique.setdefault(artifact.sha256, artifact)
        return tuple(unique.values())
