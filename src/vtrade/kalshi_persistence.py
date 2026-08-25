from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from vtrade.domain.types import (
    BinaryEvent,
    BinaryMarket,
    CanonicalLevel,
    CanonicalOrderBook,
    EventKey,
    MarketKey,
    RawArtifact,
    Series,
    SeriesKey,
)
from vtrade.kalshi_freeze import KalshiMarketFreeze


class _Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _stable_id(kind: str, value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:{kind}:{value}")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Kalshi persistence timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class KalshiFreezePersistence:
    freeze_id: uuid.UUID
    market_snapshot_ids: tuple[uuid.UUID, ...]
    order_book_snapshot_ids: tuple[uuid.UUID, ...]
    resolution_ids: tuple[uuid.UUID, ...]


class PostgresKalshiFreezeRepository:
    """Persist one complete Kalshi freeze in the clean four-migration schema."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def market_refs_for_agent(self, agent_id: uuid.UUID) -> tuple[MarketKey, ...]:
        """Return held and previously touched market identities for resolution reads."""

        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT m.market_ref FROM positions p "
                "JOIN markets m ON m.id = p.market_id "
                "WHERE p.agent_id = %s AND p.contract_units > 0 "
                "UNION "
                "SELECT DISTINCT m.market_ref FROM order_operations operation "
                "JOIN markets m ON m.id = operation.market_id "
                "WHERE operation.agent_id = %s "
                "ORDER BY market_ref",
                (agent_id, agent_id),
            )
            return tuple(MarketKey(str(row[0])) for row in cursor.fetchall())

    def persist(
        self,
        freeze: KalshiMarketFreeze,
        *,
        agent_cycle_id: uuid.UUID,
        raw_artifact_ids: Mapping[str, uuid.UUID],
        published_at: datetime,
    ) -> KalshiFreezePersistence:
        published_at = _aware(published_at)
        pages = freeze.catalogue.pages
        if not pages:
            raise ValueError("Kalshi freeze requires at least one catalogue page")
        if published_at < _aware(freeze.data_cutoff):
            raise ValueError("Kalshi freeze cannot be published before its data cutoff")

        markets: dict[MarketKey, BinaryMarket] = {}
        series: dict[SeriesKey, Series] = {}
        events: dict[EventKey, BinaryEvent] = {}
        for page in pages:
            for series_item in page.series:
                series[series_item.key] = series_item
            for event_item in page.events:
                events[event_item.key] = event_item
            for market_item in page.markets:
                previous = markets.get(market_item.key)
                if previous is not None and previous != market_item:
                    raise ValueError("one market identity has conflicting freeze observations")
                markets[market_item.key] = market_item

        freeze_id = _stable_id("market-freeze", str(agent_cycle_id))
        catalogue_artifact_id = self._artifact_id(raw_artifact_ids, pages[0].audit)
        market_snapshot_ids: list[uuid.UUID] = []
        order_book_snapshot_ids: list[uuid.UUID] = []
        resolution_ids: list[uuid.UUID] = []

        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            for series_item in series.values():
                artifact = series_item.audit
                cursor.execute(
                    "INSERT INTO series "
                    "(id, venue, kind, series_ref, title, rules, observed_at, "
                    "source_updated_at, raw_artifact_id) VALUES "
                    "(%s, 'kalshi', 'series', %s, %s, %s, %s, NULL, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        series_item.key.stable_id,
                        series_item.series_ref,
                        series_item.title,
                        series_item.rules,
                        _aware(series_item.observed_at),
                        self._artifact_id(raw_artifact_ids, artifact),
                    ),
                )
            for event_item in events.values():
                artifact = event_item.audit
                cursor.execute(
                    "INSERT INTO events "
                    "(id, venue, kind, event_ref, series_id, title, category, observed_at, "
                    "source_updated_at, raw_artifact_id) VALUES "
                    "(%s, 'kalshi', 'event', %s, %s, %s, %s, %s, NULL, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        event_item.key.stable_id,
                        event_item.event_ref,
                        event_item.series_key.stable_id,
                        event_item.title,
                        event_item.category,
                        _aware(event_item.observed_at),
                        self._artifact_id(raw_artifact_ids, artifact),
                    ),
                )
            for market in markets.values():
                self._persist_market(cursor, market, raw_artifact_ids)

            market_ids: dict[MarketKey, uuid.UUID] = {
                key: key.stable_id for key in markets
            }
            existing_market_states: dict[MarketKey, tuple[object, ...]] = {}
            resolution_keys = set(freeze.resolution_market_keys)
            resolution_keys.update(item.market_key for item in freeze.resolutions)
            for market_key in sorted(
                resolution_keys - set(markets), key=lambda value: value.canonical
            ):
                cursor.execute(
                    "SELECT id, lifecycle_status, eligible, tradeable, observed_at, "
                    "raw_artifact_id FROM markets WHERE venue = 'kalshi' "
                    "AND market_ref = %s",
                    (market_key.market_ref,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise ValueError(
                        "resolution market is neither in the catalogue nor persisted: "
                        f"{market_key.market_ref}"
                    )
                market_ids[market_key] = uuid.UUID(str(existing[0]))
                existing_market_states[market_key] = tuple(existing)

            for page_index, page in enumerate(pages):
                page_id = _stable_id(
                    "catalogue-page",
                    f"{agent_cycle_id}:{page_index}:{page.audit.sha256}",
                )
                cursor.execute(
                    "INSERT INTO catalogue_page_observations "
                    "(id, resource, requested_cursor, next_cursor, record_count, "
                    "observed_at, source_timestamp, cutoff, raw_artifact_id) VALUES "
                    "(%s, 'markets', %s, %s, %s, %s, NULL, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        page_id,
                        page.requested_cursor,
                        page.next_cursor,
                        page.record_count,
                        _aware(page.observed_at),
                        _aware(freeze.data_cutoff),
                        self._artifact_id(raw_artifact_ids, page.audit),
                    ),
                )
                for market in page.markets:
                    cursor.execute(
                        "INSERT INTO catalogue_market_observations "
                        "(id, market_id, lifecycle_status, eligible, tradeable, observed_at, "
                        "source_updated_at, cutoff, raw_artifact_id) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (
                            _stable_id(
                                "catalogue-market",
                                f"{agent_cycle_id}:{page_index}:{market.market_ref}",
                            ),
                            market.key.stable_id,
                            market.status.value,
                            market.eligible,
                            market.tradeable,
                            _aware(market.observed_at),
                            market.source_updated_at,
                            _aware(freeze.data_cutoff),
                            self._artifact_id(raw_artifact_ids, page.audit),
                        ),
                    )

            cursor.execute(
                "INSERT INTO market_freezes "
                "(id, agent_cycle_id, data_cutoff, historical_cutoff, catalogue_artifact_id, "
                "publication_status, complete, published_at) VALUES "
                "(%s, %s, %s, %s, %s, 'published', true, %s) "
                "ON CONFLICT (agent_cycle_id) DO NOTHING",
                (
                    freeze_id,
                    agent_cycle_id,
                    _aware(freeze.data_cutoff),
                    _aware(freeze.catalogue.historical_cutoff),
                    catalogue_artifact_id,
                    published_at,
                ),
            )
            cursor.execute(
                "SELECT id, data_cutoff, historical_cutoff, catalogue_artifact_id, "
                "publication_status, complete FROM market_freezes "
                "WHERE agent_cycle_id = %s FOR UPDATE",
                (agent_cycle_id,),
            )
            stored_freeze = cursor.fetchone()
            if stored_freeze is None:
                raise RuntimeError("published Kalshi freeze disappeared after insert")
            if (
                uuid.UUID(str(stored_freeze[0])) != freeze_id
                or _aware(cast(datetime, stored_freeze[1])) != _aware(freeze.data_cutoff)
                or _aware(cast(datetime, stored_freeze[2]))
                != _aware(freeze.catalogue.historical_cutoff)
                or uuid.UUID(str(stored_freeze[3])) != catalogue_artifact_id
                or str(stored_freeze[4]) != "published"
                or not bool(stored_freeze[5])
            ):
                raise ValueError("Kalshi freeze idempotency evidence conflicts")

            discovery_keys = {context.market.key for context in freeze.contexts}
            memberships = (
                *((key, "discovery") for key in sorted(
                    discovery_keys, key=lambda value: value.canonical
                )),
                *((key, "resolution") for key in sorted(
                    resolution_keys, key=lambda value: value.canonical
                )),
            )
            for market_key, membership_type in memberships:
                if market_key not in market_ids:
                    raise ValueError(
                        "freeze membership market is absent from catalogue: "
                        f"{market_key.market_ref}"
                    )
                cursor.execute(
                    "INSERT INTO market_freeze_memberships "
                    "(freeze_id, market_id, membership_type, inclusion_reason) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        freeze_id,
                        market_ids[market_key],
                        membership_type,
                        (
                            "selected_discovery"
                            if membership_type == "discovery"
                            else "held_or_touched"
                        ),
                    ),
                )

            membership_markets = discovery_keys | resolution_keys
            context_by_key = {context.market.key: context for context in freeze.contexts}
            for market_key in sorted(membership_markets, key=lambda value: value.canonical):
                frozen_market = markets.get(market_key)
                existing_state = existing_market_states.get(market_key)
                if frozen_market is None and existing_state is None:
                    raise ValueError("freeze state market identity disappeared")
                state_id = _stable_id("frozen-market-state", f"{freeze_id}:{market_key.canonical}")
                market_snapshot_ids.append(state_id)
                if frozen_market is None:
                    assert existing_state is not None
                    state_status = str(existing_state[1])
                    state_eligible = bool(existing_state[2])
                    state_tradeable = bool(existing_state[3])
                    state_observed_at = _aware(cast(datetime, existing_state[4]))
                    state_artifact_id = existing_state[5]
                else:
                    state_status = frozen_market.status.value
                    state_eligible = frozen_market.eligible
                    state_tradeable = frozen_market.tradeable
                    state_observed_at = _aware(frozen_market.observed_at)
                    state_artifact_id = self._artifact_id(
                        raw_artifact_ids, frozen_market.audit
                    )
                cursor.execute(
                    "INSERT INTO frozen_market_states "
                    "(id, freeze_id, market_id, lifecycle_status, eligible, tradeable, "
                    "observed_at, cutoff, raw_artifact_id) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        state_id,
                        freeze_id,
                        market_ids[market_key],
                        state_status,
                        state_eligible,
                        state_tradeable,
                        state_observed_at,
                        _aware(freeze.data_cutoff),
                        state_artifact_id,
                    ),
                )
                context = context_by_key.get(market_key)
                if context is None:
                    continue
                book_id = _stable_id(
                    "order-book-snapshot",
                    f"{freeze_id}:{market_key.canonical}:{context.order_book.artifact.sha256}",
                )
                order_book_snapshot_ids.append(book_id)
                cursor.execute(
                    "INSERT INTO order_book_snapshots "
                    "(id, freeze_id, market_id, observed_at, source_timestamp, cutoff, "
                    "raw_artifact_id) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        book_id,
                        freeze_id,
                        market_key.stable_id,
                        _aware(context.order_book.observed_at),
                        context.order_book.source_timestamp,
                        _aware(context.order_book.cutoff),
                        self._artifact_id(raw_artifact_ids, context.order_book.artifact),
                    ),
                )
                for outcome_side, book_side, levels in self._book_levels(context.order_book):
                    for level_index, level in enumerate(levels):
                        cursor.execute(
                            "INSERT INTO order_book_levels "
                            "(snapshot_id, outcome_side, book_side, level_index, "
                            "price_micros, contract_units) VALUES (%s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING",
                            (
                                book_id,
                                outcome_side,
                                book_side,
                                level_index,
                                int(level.price),
                                int(level.quantity),
                            ),
                        )

            for observation in freeze.resolutions:
                if observation.market_key not in market_ids:
                    raise ValueError(
                        "resolution observation market is absent from the catalogue: "
                        f"{observation.market_key.market_ref}"
                    )
                resolution_id = _stable_id("resolution", str(observation.snapshot_id))
                resolution_ids.append(resolution_id)
                cursor.execute(
                    "INSERT INTO resolution_observations "
                    "(id, market_id, lifecycle_status, result, observed_at, source_timestamp, "
                    "settlement_ts, cutoff, raw_artifact_id, blocked) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        resolution_id,
                        market_ids[observation.market_key],
                        observation.status.value,
                        observation.result.value if observation.result is not None else None,
                        _aware(observation.observed_at),
                        observation.source_timestamp,
                        observation.settlement_ts,
                        _aware(freeze.data_cutoff),
                        self._artifact_id(raw_artifact_ids, observation.audit),
                        observation.blocked,
                    ),
                )

        return KalshiFreezePersistence(
            freeze_id,
            tuple(market_snapshot_ids),
            tuple(order_book_snapshot_ids),
            tuple(resolution_ids),
        )

    @staticmethod
    def _artifact_id(raw_artifact_ids: Mapping[str, uuid.UUID], artifact: RawArtifact) -> uuid.UUID:
        try:
            return raw_artifact_ids[artifact.sha256]
        except KeyError as exc:
            raise ValueError(f"raw artifact was not persisted: {artifact.sha256}") from exc

    @staticmethod
    def _persist_market(
        cursor: _Cursor,
        market: BinaryMarket,
        raw_artifact_ids: Mapping[str, uuid.UUID],
    ) -> None:
        cursor.execute(
            "INSERT INTO markets "
            "(id, venue, kind, market_ref, series_id, event_id, question, resolution_rules, "
            "resolution_source, open_time, close_time, expected_expiration_time, "
            "latest_expiration_time, lifecycle_status, eligible, tradeable, volume_units, "
            "liquidity_micros, observed_at, source_updated_at, raw_artifact_id) VALUES "
            "(%s, 'kalshi', 'binary', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                market.key.stable_id,
                market.market_ref,
                market.series_key.stable_id,
                market.event_key.stable_id,
                market.question,
                market.resolution_rules,
                market.resolution_source,
                _aware(market.open_time),
                market.close_time,
                market.expected_expiration_time,
                market.latest_expiration_time,
                market.status.value,
                market.eligible,
                market.tradeable,
                int(market.volume),
                int(market.liquidity_micros),
                _aware(market.observed_at),
                market.source_updated_at,
                PostgresKalshiFreezeRepository._artifact_id(raw_artifact_ids, market.audit),
            ),
        )
        for outcome in market.outcomes:
            cursor.execute(
                "INSERT INTO outcomes "
                "(id, market_id, outcome_side, label, eligible) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    outcome.key.stable_id,
                    market.key.stable_id,
                    outcome.side.value,
                    outcome.label,
                    outcome.eligible,
                ),
            )
        for ordinal, price_range in enumerate(market.price_grid.ranges):
            cursor.execute(
                "INSERT INTO market_price_grid_ranges "
                "(market_id, ordinal, start_price_micros, end_price_micros, step_micros) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    market.key.stable_id,
                    ordinal,
                    int(price_range.start),
                    int(price_range.end),
                    int(price_range.step),
                ),
            )

    @staticmethod
    def _book_levels(
        book: CanonicalOrderBook,
    ) -> tuple[tuple[str, str, tuple[CanonicalLevel, ...]], ...]:
        return (
            ("YES", "bid", book.yes_bids),
            ("YES", "ask", book.yes_asks),
            ("NO", "bid", book.no_bids),
            ("NO", "ask", book.no_asks),
        )


__all__ = ["KalshiFreezePersistence", "PostgresKalshiFreezeRepository"]
