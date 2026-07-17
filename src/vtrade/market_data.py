from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

from vtrade.broker import FeePolicy
from vtrade.domain.types import Event, Market, MarketDelta, OrderBookSnapshot, Resolution
from vtrade.polymarket import FeeRateSnapshot, PolymarketVenue
from vtrade.runtime import (
    ArtifactRegistration,
    CycleClaim,
    MarketFreezeResult,
    six_month_retain_until,
)


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


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

    def persist_freeze(
        self,
        pages: Sequence[MarketDelta],
        books: Sequence[OrderBookSnapshot],
        resolutions: Sequence[Resolution],
        fee_rates: Sequence[FeeRateSnapshot] = (),
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
                            json.dumps({"tradeable": market.tradeable}),
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
                        _id("outcome", resolution.winning_outcome_id),
                        resolution.result,
                        resolution.source_created_at,
                        resolution.observed_at,
                        resolution.eligible_after,
                        resolution.artifact.uri,
                        resolution.artifact.sha256,
                    ),
                )
            for fee_rate in fee_rates:
                fee_rate_id = _id(
                    "fee-rate",
                    fee_rate.token_id,
                    fee_rate.observed_at.isoformat(),
                    fee_rate.artifact.sha256,
                )
                fee_rate_ids.append(fee_rate_id)
                cursor.execute(
                    "INSERT INTO fee_rate_snapshots "
                    "(id, outcome_id, token_id, base_fee_bps, observed_at, "
                    "source_created_at, raw_artifact_uri, raw_sha256) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (token_id, observed_at, raw_sha256) DO NOTHING",
                    (
                        fee_rate_id,
                        _id("outcome", f"polymarket:outcome:{fee_rate.token_id}"),
                        fee_rate.token_id,
                        fee_rate.base_fee_bps,
                        fee_rate.observed_at,
                        fee_rate.source_created_at,
                        fee_rate.artifact.uri,
                        fee_rate.artifact.sha256,
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
                "SELECT base_fee_bps, observed_at, source_created_at "
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
        observed = self._aware(cast(datetime, row[1]))
        source = self._aware(cast(datetime, row[2])) if row[2] is not None else None
        if observed > cutoff or (source is not None and source > cutoff):
            raise FrozenFeePolicyUnavailable("fee rate timestamp is after cycle cutoff")
        bps = int(str(row[0]))
        if not 0 <= bps <= 10_000:
            raise FrozenFeePolicyUnavailable("persisted fee rate is outside 0..10000 bps")
        return FeePolicy(Decimal(bps) / Decimal(10_000))

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
            "status = EXCLUDED.status, observed_at = EXCLUDED.observed_at, "
            "metadata = EXCLUDED.metadata, tradeable = EXCLUDED.tradeable",
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
                "indicative_price = EXCLUDED.indicative_price, "
                "tradeable = EXCLUDED.tradeable, metadata = EXCLUDED.metadata",
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


class PolymarketFreezeService:
    """The only cycle component allowed to fetch venue data before cutoff finalization."""

    def __init__(
        self,
        venue: PolymarketVenue,
        repository: PostgresMarketDataRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._venue = venue
        self._repository = repository
        self._clock = clock

    def freeze(self, claim: CycleClaim) -> MarketFreezeResult:
        if claim.data_cutoff is not None:
            raise ValueError("market freeze cannot fetch after a cycle cutoff is finalized")
        pages = self._venue.sync_all_markets()
        if not pages or pages[-1].next_cursor is not None:
            raise RuntimeError("bounded Polymarket market synchronization is incomplete")
        historical_tokens, historical_markets = self._repository.historical_universe(
            claim.agent_id
        )
        tokens = list(historical_tokens)
        candidates = sorted(
            (market for page in pages for market in page.markets if market.tradeable),
            key=lambda market: (
                int(market.volume_micros),
                int(market.liquidity_micros),
                market.id,
            ),
            reverse=True,
        )
        for market in candidates:
            for outcome in market.outcomes:
                if outcome.venue_token_id not in tokens and len(tokens) < 20:
                    tokens.append(outcome.venue_token_id)
        books = self._venue.get_order_book(tokens) if tokens else ()
        fee_rates = self._venue.get_fee_rates(tokens) if tokens else ()
        resolutions = (
            self._venue.sync_resolutions(historical_markets[:100])
            if historical_markets
            else ()
        )
        persisted = self._repository.persist_freeze(pages, books, resolutions, fee_rates)
        completed = self._aware(self._clock())
        artifacts = tuple(
            ArtifactRegistration(
                item.uri, item.sha256, item.byte_length, six_month_retain_until(completed)
            )
            for item in (
                *(page.artifact for page in pages),
                *(book.artifact for book in books),
                *(fee.artifact for fee in fee_rates),
                *(resolution.artifact for resolution in resolutions),
            )
        )
        freshest = max(
            (
                *(page.observed_at for page in pages),
                *(book.observed_at for book in books),
                *(fee.observed_at for fee in fee_rates),
                *(resolution.observed_at for resolution in resolutions),
            ),
            default=completed,
        )
        if freshest > completed:
            raise ValueError("frozen market data is newer than freeze completion")
        return MarketFreezeResult(
            {
                "market_snapshot_ids": [str(value) for value in persisted.market_snapshot_ids],
                "order_book_snapshot_ids": [
                    str(value) for value in persisted.order_book_snapshot_ids
                ],
                "resolution_ids": [str(value) for value in persisted.resolution_ids],
                "fee_rate_snapshot_ids": [
                    str(value) for value in persisted.fee_rate_snapshot_ids
                ],
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


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))
