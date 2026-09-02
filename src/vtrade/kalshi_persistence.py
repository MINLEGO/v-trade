from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from vtrade.deadline import check_deadline
from vtrade.domain.execution import FeeParticipantRole, FeePolicySnapshot
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
from vtrade.fee_policy import FeeEvidenceRole
from vtrade.kalshi_freeze import KalshiMarketFreeze
from vtrade.runtime import LeaseLost


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

    return cast(
        AbstractContextManager[_Connection], psycopg.connect(database_url, connect_timeout=5)
    )


def _stable_id(kind: str, value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:{kind}:{value}")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Kalshi persistence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _check_deadline(deadline: float | None, label: str) -> None:
    if deadline is not None:
        check_deadline(deadline, f"Kalshi {label}")


def _set_statement_timeout(cursor: _Cursor, deadline: float) -> None:
    remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
    cursor.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{remaining_ms}ms",),
    )


def _assert_lease(
    cursor: _Cursor,
    cycle_id: uuid.UUID,
    lease_owner: str,
    now: datetime,
    *,
    lock: bool,
) -> None:
    query = (
        "SELECT 1 FROM agent_cycles WHERE id = %s AND status = 'running' "
        "AND lease_owner = %s AND lease_expires_at > %s"
    )
    if lock:
        query += " FOR UPDATE"
    cursor.execute(query, (cycle_id, lease_owner, now))
    if cursor.fetchone() is None:
        raise LeaseLost(f"cycle lease lost: {cycle_id}")


@dataclass(frozen=True, slots=True)
class KalshiFreezePersistence:
    freeze_id: uuid.UUID
    market_snapshot_ids: tuple[uuid.UUID, ...]
    order_book_snapshot_ids: tuple[uuid.UUID, ...]
    resolution_ids: tuple[uuid.UUID, ...]
    fee_policy_snapshot_ids: tuple[uuid.UUID, ...] = ()


class PostgresKalshiFreezeRepository:
    """Persist one complete Kalshi freeze in the clean migration schema."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def market_refs_for_agent(
        self, agent_id: uuid.UUID, *, deadline: float | None = None
    ) -> tuple[MarketKey, ...]:
        """Return held and previously touched market identities for resolution reads."""

        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            if deadline is not None:
                _set_statement_timeout(cursor, deadline)
                check_deadline(deadline, "held-market lookup")
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
        lease_owner: str | None = None,
        deadline: float | None = None,
    ) -> KalshiFreezePersistence:
        published_at = _aware(published_at)
        if deadline is not None:
            check_deadline(deadline, "Kalshi freeze persistence")
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
        for context in freeze.contexts:
            markets[context.market.key] = context.market

        freeze_id = _stable_id("market-freeze", str(agent_cycle_id))
        catalogue_artifact_id = self._artifact_id(raw_artifact_ids, pages[0].audit)
        market_snapshot_ids: list[uuid.UUID] = []
        order_book_snapshot_ids: list[uuid.UUID] = []
        resolution_ids: list[uuid.UUID] = []
        fee_policy_snapshot_ids: list[uuid.UUID] = []
        market_metrics = tuple(getattr(freeze, "market_metrics", ()))
        if tuple(metric.market_key for metric in market_metrics) != tuple(
            freeze.discovery_market_keys
        ):
            raise ValueError("freeze metrics do not cover the discovery market universe")

        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            if deadline is not None:
                _set_statement_timeout(cursor, deadline)
                check_deadline(deadline, "Kalshi freeze persistence")
            if lease_owner is not None:
                _assert_lease(cursor, agent_cycle_id, lease_owner, published_at, lock=False)
            for series_item in series.values():
                _check_deadline(deadline, "series persistence")
                artifact = series_item.audit
                cursor.execute(
                    "INSERT INTO series "
                    "(id, venue, kind, series_ref, title, rules, observed_at, "
                    "source_updated_at, fee_type, fee_multiplier_numerator, "
                    "fee_multiplier_denominator, raw_artifact_id) VALUES "
                    "(%s, 'kalshi', 'series', %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        series_item.key.stable_id,
                        series_item.series_ref,
                        series_item.title,
                        series_item.rules,
                        _aware(series_item.observed_at),
                        series_item.source_updated_at,
                        series_item.fee_type,
                        series_item.fee_multiplier_numerator,
                        series_item.fee_multiplier_denominator,
                        self._artifact_id(raw_artifact_ids, artifact),
                    ),
                )
            for event_item in events.values():
                _check_deadline(deadline, "event persistence")
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
                _check_deadline(deadline, "market persistence")
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
                _check_deadline(deadline, "resolution market lookup")
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
                _check_deadline(deadline, "catalogue persistence")
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
                    _check_deadline(deadline, "catalogue market persistence")
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

            if lease_owner is not None:
                _assert_lease(
                    cursor,
                    agent_cycle_id,
                    lease_owner,
                    _aware(datetime.now(UTC)),
                    lock=False,
                )
            _check_deadline(deadline, "before freeze publication")
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
            _check_deadline(deadline, "after freeze publication")

            policy_by_market = {
                MarketKey(item.market_ref): item for item in freeze.fee_policies
            }
            policy_status_by_market = {
                market_key: resolution.status.value
                for market_key, resolution in policy_by_market.items()
            }
            policy_reason_by_market = {
                market_key: resolution.reason.value if resolution.reason is not None else None
                for market_key, resolution in policy_by_market.items()
            }
            for market_key in freeze.discovery_market_keys:
                resolution = policy_by_market.get(market_key)
                if resolution is None:
                    continue
                _check_deadline(deadline, "fee policy persistence")
                snapshot_id: uuid.UUID | None = None
                if resolution.policy is not None:
                    policy_artifact = resolution.policy.raw_artifact
                    if policy_artifact is None:
                        policy_artifact = markets[market_key].audit
                    snapshot_id = self._persist_fee_policy_cursor(
                        cursor,
                        resolution.policy,
                        market_id=market_ids[market_key],
                        raw_artifact_id=self._artifact_id(raw_artifact_ids, policy_artifact),
                    )
                    fee_policy_snapshot_ids.append(snapshot_id)
                    for evidence in resolution.evidence:
                        cursor.execute(
                            "INSERT INTO fee_policy_snapshot_artifacts "
                            "(fee_policy_snapshot_id, raw_artifact_id, evidence_role) "
                            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (
                                snapshot_id,
                                self._artifact_id(raw_artifact_ids, evidence.artifact),
                                FeeEvidenceRole(evidence.role).value,
                            ),
                        )
                cursor.execute(
                    "INSERT INTO freeze_market_fee_policies "
                    "(freeze_id, market_id, fee_policy_snapshot_id, status, closed_reason) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (freeze_id, market_id) "
                    "DO NOTHING",
                    (
                        freeze_id,
                        market_ids[market_key],
                        snapshot_id,
                        resolution.status.value,
                        resolution.reason.value if resolution.reason is not None else None,
                    ),
                )

            for series_item in series.values():
                _check_deadline(deadline, "series metadata snapshot persistence")
                series_snapshot_id = _stable_id(
                    "series-metadata-snapshot",
                    f"{freeze_id}:{series_item.key.canonical}",
                )
                cursor.execute(
                    "INSERT INTO series_metadata_snapshots "
                    "(id, freeze_id, series_id, tags, observed_at, source_timestamp, "
                    "cutoff, raw_artifact_id) VALUES "
                    "(%s, %s, %s, %s::jsonb, %s, NULL, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        series_snapshot_id,
                        freeze_id,
                        series_item.key.stable_id,
                        json.dumps(list(series_item.tags), separators=(",", ":")),
                        _aware(series_item.observed_at),
                        _aware(freeze.data_cutoff),
                        self._artifact_id(raw_artifact_ids, series_item.audit),
                    ),
                )

            metric_by_key = {metric.market_key: metric for metric in market_metrics}
            for market_key in sorted(metric_by_key, key=lambda value: value.canonical):
                _check_deadline(deadline, "market metric persistence")
                metric = metric_by_key[market_key]
                if market_key not in market_ids:
                    raise ValueError(
                        "market metric is absent from the persisted freeze: "
                        f"{market_key.market_ref}"
                    )
                metric_id = _stable_id(
                    "market-metric-snapshot",
                    f"{freeze_id}:{market_key.canonical}",
                )
                cursor.execute(
                    "INSERT INTO market_metric_snapshots "
                    "(id, freeze_id, market_id, volume_24h_units, volatility_micros, "
                    "volume_trend, volume_trend_delta, competitive_score, "
                    "indicative_yes_price_micros, indicative_no_price_micros, "
                    "recent_volume_units, baseline_volume_units, volatility_sample_count, "
                    "recent_bucket_count, baseline_bucket_count, as_of_at, formula_version, "
                    "cutoff) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        metric_id,
                        freeze_id,
                        market_ids[market_key],
                        metric.volume_24h_units,
                        metric.volatility_micros,
                        metric.volume_trend,
                        metric.volume_trend_delta,
                        metric.competitive_score,
                        metric.indicative_yes_price_micros,
                        metric.indicative_no_price_micros,
                        metric.recent_volume_units,
                        metric.baseline_volume_units,
                        metric.volatility_sample_count,
                        metric.recent_bucket_count,
                        metric.baseline_bucket_count,
                        _aware(metric.as_of_at),
                        metric.formula_version,
                        _aware(freeze.data_cutoff),
                    ),
                )
                for artifact_index, artifact in enumerate(metric.source_artifacts):
                    _check_deadline(deadline, "market metric artifact persistence")
                    cursor.execute(
                        "INSERT INTO market_metric_snapshot_artifacts "
                        "(metric_snapshot_id, raw_artifact_id, artifact_role) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (
                            metric_id,
                            self._artifact_id(raw_artifact_ids, artifact),
                            f"source_{artifact_index}",
                        ),
                    )

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
                _check_deadline(deadline, "freeze membership persistence")
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
                _check_deadline(deadline, "frozen market persistence")
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
                    "fee_policy_status, fee_policy_reason, observed_at, cutoff, raw_artifact_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        state_id,
                        freeze_id,
                        market_ids[market_key],
                        state_status,
                        state_eligible,
                        state_tradeable,
                        (
                            policy_status_by_market[market_key]
                            if market_key in policy_by_market
                            else None
                        ),
                        (
                            policy_reason_by_market.get(market_key)
                        ),
                        state_observed_at,
                        _aware(freeze.data_cutoff),
                        state_artifact_id,
                    ),
                )
                persisted_context = context_by_key.get(market_key)
                if persisted_context is None:
                    continue
                _check_deadline(deadline, "order book persistence")
                book_id = _stable_id(
                    "order-book-snapshot",
                    f"{freeze_id}:{market_key.canonical}:"
                    f"{persisted_context.order_book.artifact.sha256}",
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
                        _aware(persisted_context.order_book.observed_at),
                        persisted_context.order_book.source_timestamp,
                        _aware(persisted_context.order_book.cutoff),
                        self._artifact_id(
                            raw_artifact_ids, persisted_context.order_book.artifact
                        ),
                    ),
                )
                for outcome_side, book_side, levels in self._book_levels(
                    persisted_context.order_book
                ):
                    for level_index, level in enumerate(levels):
                        _check_deadline(deadline, "order book level persistence")
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
                _check_deadline(deadline, "resolution persistence")
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

            if lease_owner is not None:
                _assert_lease(
                    cursor,
                    agent_cycle_id,
                    lease_owner,
                    _aware(datetime.now(UTC)),
                    lock=True,
                )
            _check_deadline(deadline, "after Kalshi freeze persistence")

        return KalshiFreezePersistence(
            freeze_id,
            tuple(market_snapshot_ids),
            tuple(order_book_snapshot_ids),
            tuple(resolution_ids),
            tuple(fee_policy_snapshot_ids),
        )

    @staticmethod
    def _artifact_id(raw_artifact_ids: Mapping[str, uuid.UUID], artifact: RawArtifact) -> uuid.UUID:
        try:
            return raw_artifact_ids[artifact.sha256]
        except KeyError as exc:
            raise ValueError(f"raw artifact was not persisted: {artifact.sha256}") from exc

    @staticmethod
    def _persist_fee_policy_cursor(
        cursor: _Cursor,
        snapshot: FeePolicySnapshot,
        *,
        market_id: uuid.UUID,
        raw_artifact_id: uuid.UUID,
    ) -> uuid.UUID:
        policy_id = _stable_id("fee-policy", f"{market_id}:{snapshot.fingerprint}")
        cursor.execute(
            "SELECT id, policy_fingerprint FROM fee_policy_snapshots "
            "WHERE market_id = %s AND policy_fingerprint = %s FOR UPDATE",
            (market_id, snapshot.fingerprint),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if str(existing[1]) != snapshot.fingerprint:
                raise ValueError("fee policy fingerprint conflict")
            return uuid.UUID(str(existing[0]))
        resolved_num, resolved_den = snapshot.resolved_multiplier
        cursor.execute(
            "INSERT INTO fee_policy_snapshots "
            "(id, market_id, policy_version, formula_version, schedule_identity, fee_type, "
            "participant_role, multiplier_numerator, multiplier_denominator, "
            "series_multiplier_numerator, series_multiplier_denominator, "
            "event_override_micros, event_override_numerator, event_override_denominator, "
            "event_override_fee_type, event_override_cleared, rate_numerator, "
            "rate_denominator, waiver, waiver_evidence, exact_inputs, effective_at, as_of_at, "
            "scheduled_ts, observed_at, cutoff, source_tier, raw_artifact_id, schedule_sha256, "
            "settlement_fee_micros, policy_fingerprint) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                policy_id,
                market_id,
                snapshot.contract_version,
                snapshot.formula_version,
                snapshot.schedule_version,
                snapshot.fee_type,
                FeeParticipantRole(snapshot.participant_role).value.lower(),
                resolved_num,
                resolved_den,
                snapshot.series_multiplier_numerator,
                snapshot.series_multiplier_denominator,
                snapshot.event_override_numerator
                if snapshot.event_override_denominator == 1_000_000
                else None,
                snapshot.event_override_numerator,
                snapshot.event_override_denominator,
                snapshot.event_override_fee_type,
                snapshot.event_override_cleared,
                snapshot.rate_numerator,
                snapshot.rate_denominator,
                snapshot.waiver,
                (
                    None
                    if snapshot.waiver_evidence is None
                    else json.dumps(dict(snapshot.waiver_evidence))
                ),
                json.dumps(dict(snapshot.exact_inputs), sort_keys=True),
                snapshot.effective_from,
                snapshot.as_of,
                snapshot.scheduled_ts,
                snapshot.source_observed_at,
                snapshot.cutoff,
                snapshot.source_tier,
                raw_artifact_id,
                snapshot.schedule_sha256,
                snapshot.settlement_fee_micros,
                snapshot.fingerprint,
            ),
        )
        return policy_id

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
        "liquidity_micros, observed_at, source_updated_at, fee_waiver_expiration_time, "
        "raw_artifact_id) VALUES "
        "(%s, 'kalshi', 'binary', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
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
                market.fee_waiver_expiration_time,
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
