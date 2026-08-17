from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast

from vtrade.artifacts import SupabaseArtifactStore
from vtrade.broker import (
    ArchivedBid,
    ExecutionStatus,
    LiquidityTimeInForce,
    PaperOrder,
    PaperPolicy,
    PendingOrder,
    PortfolioState,
    PositionState,
    PredictionArenaPaperBroker,
    SettlementEngine,
    SettlementObservation,
)
from vtrade.broker_repository import PostgresBrokerRepository
from vtrade.config import (
    ConfigurationError,
    ExperimentConfig,
    load_experiment_config,
    required_environment,
)
from vtrade.domain.ports import ArtifactStore, JsonObject
from vtrade.domain.types import (
    Market,
    MarketStatus,
    MicroDollars,
    OrderBookSnapshot,
    Outcome,
    PriceLevel,
    RawArtifact,
    Side,
)
from vtrade.harness import (
    BoundedToolHarness,
    HarnessLimits,
    HarnessResult,
    PlanRecord,
    PlanType,
    PromptBuilder,
    RecentActivityEvent,
)
from vtrade.harness_repository import PostgresBudgetGuard, PostgresHarnessRepository
from vtrade.liquidity import (
    LIQUIDITY_HAIRCUT_RULE_VERSION,
    VirtualLiquidityLevel,
    VirtualLiquidityLevelMetrics,
    VirtualLiquidityMetrics,
    VirtualLiquidityReservation,
    consumed_by_level,
    effective_liquidity_book,
    metrics_for_fills,
    private_snapshot,
)
from vtrade.market_data import (
    PolymarketFreezeService,
    PostgresLiveOrderContextProvider,
    PostgresMarketDataRepository,
)
from vtrade.order_execution import LiveOrderContextProvider, MarketOrderExecutor
from vtrade.polymarket import PolymarketVenue
from vtrade.postgres_runtime import PostgresRuntimeRepository
from vtrade.production_tools import ProductionToolRegistry, production_tool_context
from vtrade.providers import (
    EXA_RESEARCH_TOOL_NAMES,
    ExaResearchProvider,
    OpenRouterModelGateway,
    ProviderTelemetry,
    canonical_redacted_json,
)
from vtrade.risk import MarketCapacity, calculate_market_capacity
from vtrade.runtime import (
    ArtifactRegistration,
    BrokerExecutionResult,
    CycleClaim,
    CycleOrchestrator,
    HarnessExecutionResult,
    HourlyRuntime,
    PreSettlementResult,
    ProjectionService,
    PromptResult,
    RetentionCleaner,
    RuntimeAlertPolicy,
    RuntimeTickResult,
    SettlementValuationResult,
    six_month_retain_until,
)


class ProductionCompositionUnavailable(RuntimeError):
    """The frozen production graph cannot be built from the supplied resources."""


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


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _PromptSource:
    prompt_version_id: uuid.UUID
    system_prompt: str
    model_config: JsonObject


@dataclass(frozen=True, slots=True)
class _PromptPosition:
    market_id: str
    outcome_id: str
    venue_token_id: str
    question: str
    outcome: str
    closes_at: datetime | None
    shares: Decimal
    average_cost: Decimal
    cost_basis_micros: int
    entry_fees_micros: int
    realized_pnl_micros: int
    updated_at: datetime
    bid: Decimal | None
    bid_observed_at: datetime | None


class ProductionPromptPort:
    """Materialize and persist the private, immutable per-cycle prompt context."""

    _RECENT_ACTIVITY_LIMIT = 25

    def __init__(
        self,
        database_url: str,
        artifact_store: ArtifactStore,
        *,
        clock: Callable[[], datetime],
        connect: _Connect | None = None,
        maximum_market_cost_basis_fraction: Decimal = Decimal("0.15"),
        maximum_valuation_bid_age: timedelta = timedelta(minutes=5),
    ) -> None:
        self._database_url = database_url
        self._store = artifact_store
        self._clock = clock
        self._connect = connect or _default_connect
        self._memory = PostgresHarnessRepository(database_url, connect=connect)
        if (
            not maximum_market_cost_basis_fraction.is_finite()
            or not Decimal(0) < maximum_market_cost_basis_fraction <= Decimal(1)
        ):
            raise ValueError("maximum market cost-basis fraction must be between zero and one")
        if maximum_valuation_bid_age < timedelta(0):
            raise ValueError("maximum valuation bid age cannot be negative")
        self._maximum_market_cost_basis_fraction = maximum_market_cost_basis_fraction
        self._maximum_valuation_bid_age = maximum_valuation_bid_age

    def render(self, claim: CycleClaim, frozen: JsonObject) -> PromptResult:
        cutoff = _cutoff(claim)
        source = self._prompt_source(claim.agent_id)
        plans = tuple(
            _plan(row, claim.agent_id)
            for row in self._memory.read_plans(
                actor_id=claim.agent_id, target_agent_id=claim.agent_id
            )
        )
        recent_activity = self._recent_activity(
            claim.agent_id,
            cutoff,
            current_cycle_id=claim.cycle_id,
        )
        cycle_context: JsonObject = {
            "scheduled_at": claim.scheduled_at.isoformat(),
            "data_cutoff": cutoff.isoformat(),
            # The current PostgreSQL graph has no pending-order projection. Keep that
            # absence explicit until a real reservation lifecycle is introduced.
            "account": self._account_context(
                claim.agent_id,
                cutoff=cutoff,
                frozen=frozen,
                pending_orders=(),
            ),
        }
        messages = PromptBuilder(source.system_prompt).build(
            agent_id=str(claim.agent_id),
            cycle_context=cycle_context,
            plans=plans,
            recent_activity=recent_activity,
        )
        rendered = json.dumps(
            messages,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        artifact = self._store.put(canonical_redacted_json({"messages": messages}))
        now = _aware(self._clock())
        retained = six_month_retain_until(now)
        context: JsonObject = {
            "messages": list(messages),
            "model_config": source.model_config,
            "recent_activity": recent_activity,
        }
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        market_ids = _uuids(frozen, "market_snapshot_ids")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT rendered_prompt_sha256 FROM cycle_contexts WHERE agent_cycle_id = %s",
                (claim.cycle_id,),
            )
            existing = cursor.fetchone()
            if existing is not None and str(existing[0]) != digest:
                raise ValueError("cycle prompt idempotency fingerprint conflict")
            if existing is None:
                cursor.execute(
                    "INSERT INTO cycle_contexts "
                    "(id, agent_cycle_id, prompt_version_id, rendered_cycle_prompt, "
                    "rendered_prompt_sha256, context, market_snapshot_ids, artifact_uri, "
                    "artifact_sha256, retain_until, created_at) VALUES "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s::uuid[], %s, %s, %s, %s)",
                    (
                        uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:cycle-context:{claim.cycle_id}"),
                        claim.cycle_id,
                        source.prompt_version_id,
                        rendered,
                        digest,
                        json.dumps(context, sort_keys=True, default=str),
                        list(market_ids),
                        artifact.uri,
                        artifact.sha256,
                        retained,
                        now,
                    ),
                )
        return PromptResult(
            {
                "cycle_context_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:cycle-context:{claim.cycle_id}")
                ),
                "prompt_sha256": digest,
            },
            (_registration(artifact, retained),),
            len(rendered),
        )

    def _prompt_source(self, agent_id: uuid.UUID) -> _PromptSource:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pv.id, pv.body, mc.model_slug, mc.provider_policy, mc.parameters "
                "FROM agents a JOIN experiment_runs r ON r.id = a.run_id "
                "JOIN prompt_versions pv ON pv.definition_id = r.definition_id "
                "JOIN model_configs mc ON mc.id = a.model_config_id "
                "WHERE a.id = %s ORDER BY pv.created_at DESC, pv.id DESC LIMIT 1",
                (agent_id,),
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[3], Mapping) or not isinstance(row[4], Mapping):
            raise ProductionCompositionUnavailable("agent prompt/model registration is missing")
        config: JsonObject = {str(key): value for key, value in row[4].items()}
        config.update({str(key): value for key, value in row[3].items()})
        config["slug"] = str(row[2])
        return _PromptSource(uuid.UUID(str(row[0])), str(row[1]), config)

    def _recent_activity(
        self,
        agent_id: uuid.UUID,
        cutoff: datetime,
        *,
        current_cycle_id: uuid.UUID | None = None,
    ) -> JsonObject:
        cutoff = _aware(cutoff)
        summary_oldest = cutoff - timedelta(hours=24)
        delta_oldest = summary_oldest
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            if current_cycle_id is not None:
                cursor.execute(
                    "SELECT previous.data_cutoff "
                    "FROM agent_cycles current_cycle "
                    "JOIN agent_cycles previous "
                    "ON previous.agent_id = current_cycle.agent_id "
                    "AND previous.scheduled_at < current_cycle.scheduled_at "
                    "JOIN cycle_contexts previous_context "
                    "ON previous_context.agent_cycle_id = previous.id "
                    "WHERE current_cycle.id = %s AND current_cycle.agent_id = %s "
                    "AND previous.data_cutoff IS NOT NULL "
                    "ORDER BY previous.scheduled_at DESC, previous.id DESC LIMIT 1",
                    (current_cycle_id, agent_id),
                )
                previous = cursor.fetchone()
                if previous is not None and previous[0] is not None:
                    delta_oldest = _aware(cast(datetime, previous[0]))

            cursor.execute(
                "SELECT 'settlement', m.id::text, p.outcome_id::text, "
                "s.realized_pnl_micros, s.settled_at, '', s.id::text "
                "FROM settlements s JOIN positions p ON p.id = s.position_id "
                "JOIN outcomes o ON o.id = p.outcome_id "
                "JOIN markets m ON m.id = o.market_id "
                "WHERE s.agent_id = %s AND s.settled_at > %s AND s.settled_at <= %s "
                "ORDER BY s.settled_at DESC, s.id DESC LIMIT %s",
                (agent_id, delta_oldest, cutoff, self._RECENT_ACTIVITY_LIMIT + 1),
            )
            rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT 'rejection', oi.market_id::text, oi.outcome_id::text, "
                "0, COALESCE(o.rejected_at, o.created_at), "
                "COALESCE(NULLIF(BTRIM(o.rejection_code), ''), 'unknown'), o.id::text "
                "FROM orders o JOIN order_intents oi ON oi.id = o.intent_id "
                "JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id "
                "WHERE ac.agent_id = %s AND o.status = 'rejected' "
                "AND COALESCE(o.rejected_at, o.created_at) > %s "
                "AND COALESCE(o.rejected_at, o.created_at) <= %s "
                "ORDER BY COALESCE(o.rejected_at, o.created_at) DESC, o.id DESC LIMIT %s",
                (agent_id, delta_oldest, cutoff, self._RECENT_ACTIVITY_LIMIT + 1),
            )
            rows.extend(cursor.fetchall())
            cursor.execute(
                "SELECT count(*), COALESCE(sum(s.realized_pnl_micros), 0) "
                "FROM settlements s "
                "WHERE s.agent_id = %s AND s.settled_at > %s AND s.settled_at <= %s",
                (agent_id, summary_oldest, cutoff),
            )
            settlement_summary = cursor.fetchone()
            cursor.execute(
                "SELECT COALESCE(NULLIF(BTRIM(o.rejection_code), ''), 'unknown'), count(*) "
                "FROM orders o JOIN order_intents oi ON oi.id = o.intent_id "
                "JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id "
                "WHERE ac.agent_id = %s AND o.status = 'rejected' "
                "AND COALESCE(o.rejected_at, o.created_at) > %s "
                "AND COALESCE(o.rejected_at, o.created_at) <= %s "
                "GROUP BY COALESCE(NULLIF(BTRIM(o.rejection_code), ''), 'unknown') "
                "ORDER BY COALESCE(NULLIF(BTRIM(o.rejection_code), ''), 'unknown')",
                (agent_id, summary_oldest, cutoff),
            )
            rejection_summary_rows = cursor.fetchall()
        events = tuple(
            RecentActivityEvent(
                kind=str(row[0]),
                market_id=str(row[1]),
                outcome_id=str(row[2]) if row[2] is not None else None,
                pnl_micros=int(str(row[3])) if row[0] == "settlement" else None,
                occurred_at=_aware(cast(datetime, row[4])),
                detail=(
                    _canonical_rejection_code(row[5])
                    if row[0] == "rejection"
                    else ""
                ),
                stable_id=str(row[6]) if len(row) > 6 else "",
            )
            for row in rows
        )
        ordered = tuple(
            sorted(
                events,
                key=lambda item: (
                    item.occurred_at,
                    item.stable_id,
                    item.kind,
                    item.market_id,
                    item.outcome_id or "",
                ),
                reverse=True,
            )
        )
        settlement_count = 0
        settlement_pnl_micros = 0
        if settlement_summary is not None:
            settlement_count = int(str(settlement_summary[0]))
            settlement_pnl_micros = int(str(settlement_summary[1]))
        rejection_counts: dict[str, int] = {}
        for row in rejection_summary_rows:
            code = _canonical_rejection_code(row[0])
            rejection_counts[code] = rejection_counts.get(code, 0) + int(str(row[1]))
        return {
            "since_last_cycle": [
                self._activity_event_payload(event)
                for event in ordered[: self._RECENT_ACTIVITY_LIMIT]
            ],
            "since_last_cycle_truncated": len(ordered) > self._RECENT_ACTIVITY_LIMIT,
            "summary_24h": {
                "settlements": settlement_count,
                "settlement_pnl_micros": settlement_pnl_micros,
                "rejections": dict(sorted(rejection_counts.items())),
            },
        }

    @staticmethod
    def _activity_event_payload(event: RecentActivityEvent) -> JsonObject:
        payload: JsonObject = {
            "type": event.kind,
            "market_id": event.market_id,
            "outcome_id": event.outcome_id,
            "occurred_at": event.occurred_at.isoformat(),
        }
        if event.kind == "settlement":
            payload["realized_pnl_micros"] = event.pnl_micros
        else:
            payload["rejection_code"] = _canonical_rejection_code(event.detail)
        return payload

    def _account_context(
        self,
        agent_id: uuid.UUID,
        *,
        cutoff: datetime,
        frozen: Mapping[str, object],
        pending_orders: Sequence[PendingOrder] = (),
    ) -> JsonObject:
        snapshot_ids = _uuids(frozen, "order_book_snapshot_ids")
        oldest_bid = cutoff - self._maximum_valuation_bid_age
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(sum(lp.amount_micros) FILTER "
                "(WHERE lp.account = 'cash'), 0), a.portfolio_version, "
                "a.initial_cash_micros, "
                "COALESCE((SELECT sum(p2.realized_pnl_micros) FROM positions p2 "
                "WHERE p2.agent_id = a.id), 0) "
                "FROM agents a LEFT JOIN ledger_entries le ON le.agent_id = a.id "
                "LEFT JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
                "WHERE a.id = %s GROUP BY a.id",
                (agent_id,),
            )
            account = cursor.fetchone()
            cursor.execute(
                "SELECT m.id::text, p.outcome_id::text, o.venue_token_id, m.question, "
                "o.name, m.closes_at, p.shares, p.average_cost, p.cost_basis_micros, "
                "p.entry_fees_micros, p.realized_pnl_micros, p.updated_at, "
                "book.best_bid, book.cutoff "
                "FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
                "JOIN markets m ON m.id = o.market_id "
                "LEFT JOIN LATERAL ("
                "SELECT obs.best_bid, obs.cutoff FROM order_book_snapshots obs "
                "WHERE obs.outcome_id = p.outcome_id "
                "AND obs.id = ANY(%s::uuid[]) AND obs.best_bid IS NOT NULL "
                "AND obs.cutoff <= %s AND obs.cutoff >= %s "
                "AND (obs.source_created_at IS NULL OR obs.source_created_at <= %s) "
                "ORDER BY obs.cutoff DESC, obs.id DESC LIMIT 1"
                ") book ON TRUE "
                "WHERE p.agent_id = %s AND p.shares > 0 ORDER BY p.outcome_id",
                (list(snapshot_ids), cutoff, oldest_bid, cutoff, agent_id),
            )
            rows = cursor.fetchall()
        if account is None:
            raise ProductionCompositionUnavailable("agent account is missing")
        positions = tuple(self._prompt_position(row) for row in rows)
        cash_micros = int(str(account[0]))
        portfolio_version = int(str(account[1]))
        initial_cash_micros = int(str(account[2]))
        realized_pnl_micros = int(str(account[3]))
        valid_positions = tuple(position for position in positions if self._valid_bid(position))
        nav_complete = len(valid_positions) == len(positions)
        liquidation_by_market: dict[str, int | None] = {}
        held_basis_by_market: dict[str, int] = {}
        for position in positions:
            held_basis_by_market[position.market_id] = (
                held_basis_by_market.get(position.market_id, 0) + position.cost_basis_micros
            )
            value = self._liquidation_value(position) if self._valid_bid(position) else None
            previous = liquidation_by_market.get(position.market_id, 0)
            liquidation_by_market[position.market_id] = (
                None
                if value is None or previous is None
                else previous + value
            )
        total_liquidation = (
            sum(self._liquidation_value(position) for position in positions)
            if nav_complete
            else None
        )
        nav_micros = cash_micros + total_liquidation if total_liquidation is not None else None
        entry_fees_micros = sum(position.entry_fees_micros for position in positions)
        unrealized_pnl_micros = (
            total_liquidation
            - sum(position.cost_basis_micros for position in positions)
            - entry_fees_micros
            if total_liquidation is not None
            else None
        )
        pending_basis_by_market: dict[str, int] = {}
        for pending in pending_orders:
            if pending.side is Side.BUY:
                pending_basis_by_market[pending.market_id] = (
                    pending_basis_by_market.get(pending.market_id, 0)
                    + int(pending.reserved_cost_basis_micros)
                )
        market_ids = sorted(set(held_basis_by_market) | set(pending_basis_by_market))
        capacity_by_market: dict[str, MarketCapacity | None] = {}
        for market_id in market_ids:
            if nav_micros is None:
                capacity_by_market[market_id] = None
            else:
                capacity_by_market[market_id] = calculate_market_capacity(
                    nav_micros,
                    self._maximum_market_cost_basis_fraction,
                    held_cost_basis_micros=held_basis_by_market.get(market_id, 0),
                    pending_buy_reserved_cost_basis_micros=pending_basis_by_market.get(
                        market_id, 0
                    ),
                )
        market_order = sorted(
            liquidation_by_market.items(),
            key=lambda item: (
                item[1] is not None,
                item[1] if item[1] is not None else -1,
                item[0],
            ),
            reverse=True,
        )
        concentration = self._concentration(market_order, nav_micros)
        position_info = [
            self._position_payload(
                position,
                cutoff=cutoff,
                nav_micros=nav_micros,
                market_basis_micros=(
                    held_basis_by_market[position.market_id]
                    + pending_basis_by_market.get(position.market_id, 0)
                ),
                market_capacity=capacity_by_market[position.market_id],
            )
            for position in positions
        ]
        position_info.sort(key=lambda item: self._attention_key(item, cutoff=cutoff))
        attention = position_info[:15]
        omitted = position_info[15:]
        market_capacities: list[JsonObject] = []
        for market_id in market_ids:
            capacity = capacity_by_market[market_id]
            market_capacities.append(
                {
                    "market_id": market_id,
                    "held_cost_basis_micros": held_basis_by_market.get(market_id, 0),
                    "pending_buy_reserved_cost_basis_micros": pending_basis_by_market.get(
                        market_id, 0
                    ),
                    "market_limit_micros": (
                        int(capacity.market_limit_micros) if capacity is not None else None
                    ),
                    "remaining_capacity_micros": (
                        int(capacity.remaining_capacity_micros) if capacity is not None else None
                    ),
                }
            )
        return {
            "cash_micros": cash_micros,
            "portfolio_version": portfolio_version,
            "initial_cash_micros": initial_cash_micros,
            "nav_micros": nav_micros,
            "nav_complete": nav_complete,
            "valuation_status": "complete" if nav_complete else "incomplete",
            "realized_pnl_micros": realized_pnl_micros,
            "unrealized_pnl_micros": unrealized_pnl_micros,
            "total_pnl_micros": (
                nav_micros - initial_cash_micros if nav_micros is not None else None
            ),
            "entry_fees_micros": entry_fees_micros,
            "concentration": concentration,
            "market_capacities": market_capacities,
            "attention_positions": attention,
            "other_positions": self._aggregate_positions(omitted),
        }

    @staticmethod
    def _prompt_position(row: Sequence[object]) -> _PromptPosition:
        closes_at = _aware(cast(datetime, row[5])) if row[5] is not None else None
        bid = Decimal(str(row[12])) if row[12] is not None else None
        bid_observed_at = _aware(cast(datetime, row[13])) if row[13] is not None else None
        return _PromptPosition(
            market_id=str(row[0]),
            outcome_id=str(row[1]),
            venue_token_id=str(row[2]),
            question=str(row[3]),
            outcome=str(row[4]),
            closes_at=closes_at,
            shares=Decimal(str(row[6])),
            average_cost=Decimal(str(row[7])),
            cost_basis_micros=int(str(row[8])),
            entry_fees_micros=int(str(row[9])),
            realized_pnl_micros=int(str(row[10])),
            updated_at=_aware(cast(datetime, row[11])),
            bid=bid,
            bid_observed_at=bid_observed_at,
        )

    def _valid_bid(self, position: _PromptPosition) -> bool:
        return (
            position.bid is not None
            and position.bid.is_finite()
            and Decimal(0) <= position.bid <= Decimal(1)
            and position.bid_observed_at is not None
        )

    @staticmethod
    def _liquidation_value(position: _PromptPosition) -> int:
        if position.bid is None:
            raise ValueError("liquidation value requires a bid")
        return int(
            (position.shares * position.bid * Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

    @staticmethod
    def _ratio(numerator: int, denominator: int | None) -> str | None:
        if denominator is None or denominator <= 0:
            return None
        return str(
            (Decimal(numerator) / Decimal(denominator)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
        )

    def _concentration(
        self, market_order: Sequence[tuple[str, int | None]], nav_micros: int | None
    ) -> JsonObject:
        values = [value for _market_id, value in market_order]
        if nav_micros is None or any(value is None for value in values):
            empty = None
            return {
                "top_1_fraction": empty,
                "top_5_fraction": empty,
                "top_10_fraction": empty,
                "other_fraction": empty,
            }
        numeric = [cast(int, value) for value in values]
        return {
            "top_1_fraction": self._ratio(sum(numeric[:1]), nav_micros),
            "top_5_fraction": self._ratio(sum(numeric[:5]), nav_micros),
            "top_10_fraction": self._ratio(sum(numeric[:10]), nav_micros),
            "other_fraction": self._ratio(sum(numeric[10:]), nav_micros),
        }

    def _position_payload(
        self,
        position: _PromptPosition,
        *,
        cutoff: datetime,
        nav_micros: int | None,
        market_basis_micros: int,
        market_capacity: MarketCapacity | None,
    ) -> JsonObject:
        valid = self._valid_bid(position)
        liquidation = self._liquidation_value(position) if valid else None
        unrealized = (
            liquidation - position.cost_basis_micros - position.entry_fees_micros
            if liquidation is not None
            else None
        )
        hours_to_close = (
            round((position.closes_at - cutoff).total_seconds() / 3600, 2)
            if position.closes_at is not None
            else None
        )
        return {
            "market_id": position.market_id,
            "outcome_id": position.outcome_id,
            "venue_token_id": position.venue_token_id,
            "question": position.question,
            "outcome": position.outcome,
            "shares": str(position.shares),
            "average_cost": str(position.average_cost),
            "cost_basis_micros": position.cost_basis_micros,
            "entry_fees_micros": position.entry_fees_micros,
            "bid": str(position.bid) if valid and position.bid is not None else None,
            "liquidation_value_micros": liquidation,
            "unrealized_pnl_micros": unrealized,
            "position_weight": self._ratio(liquidation or 0, nav_micros)
            if liquidation is not None
            else None,
            "market_cost_basis_micros": market_basis_micros,
            "market_exposure": self._ratio(market_basis_micros, nav_micros),
            "remaining_capacity_micros": (
                int(market_capacity.remaining_capacity_micros)
                if market_capacity is not None
                else None
            ),
            "closes_at": position.closes_at.isoformat() if position.closes_at else None,
            "hours_to_close": hours_to_close,
        }

    @staticmethod
    def _attention_key(item: JsonObject, *, cutoff: datetime) -> tuple[object, ...]:
        valuation_missing = 0 if item["liquidation_value_micros"] is None else 1
        exposure = Decimal(str(item["market_exposure"] or "0"))
        closes_at = item["closes_at"]
        close_soon = 0
        if isinstance(closes_at, str):
            close_soon = (
                0
                if _aware(datetime.fromisoformat(closes_at)) <= cutoff + timedelta(hours=48)
                else 1
            )
        pnl = item["unrealized_pnl_micros"]
        adverse = 0 if isinstance(pnl, int) and pnl < 0 else 1
        loss = pnl if isinstance(pnl, int) and pnl < 0 else 0
        return (
            valuation_missing,
            -exposure,
            close_soon,
            adverse,
            loss,
            str(item["market_id"]),
            str(item["outcome_id"]),
        )

    @staticmethod
    def _aggregate_positions(positions: Sequence[JsonObject]) -> JsonObject:
        liquidation_values = [item["liquidation_value_micros"] for item in positions]
        unrealized_values = [item["unrealized_pnl_micros"] for item in positions]
        return {
            "count": len(positions),
            "market_count": len({str(item["market_id"]) for item in positions}),
            "cost_basis_micros": sum(int(item["cost_basis_micros"]) for item in positions),
            "entry_fees_micros": sum(int(item["entry_fees_micros"]) for item in positions),
            "liquidation_value_micros": (
                sum(int(value) for value in liquidation_values)
                if all(isinstance(value, int) for value in liquidation_values)
                else None
            ),
            "unrealized_pnl_micros": (
                sum(int(value) for value in unrealized_values)
                if all(isinstance(value, int) for value in unrealized_values)
                else None
            ),
        }


class ProductionHarnessPort:
    """Execute all 27 tools through the bounded real model/provider harness."""

    def __init__(
        self,
        database_url: str,
        artifact_store: ArtifactStore,
        gateway: OpenRouterModelGateway,
        exa: ExaResearchProvider,
        limits: HarnessLimits,
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        schema_path: str | Path,
        connect: _Connect | None = None,
        maximum_beliefs_per_agent: int = 100,
        maximum_book_age: timedelta = timedelta(minutes=5),
        maximum_order_book_depth: int = 5,
        immediate_order_executor: MarketOrderExecutor | None = None,
        require_live_order_execution: bool = False,
    ) -> None:
        self._database_url = database_url
        self._store = artifact_store
        self._gateway = gateway
        self._exa = exa
        self._limits = limits
        self._clock = clock
        self._monotonic = monotonic
        self._schema_path = schema_path
        self._connect = connect or _default_connect
        if (
            not isinstance(maximum_beliefs_per_agent, int)
            or isinstance(maximum_beliefs_per_agent, bool)
            or maximum_beliefs_per_agent <= 0
        ):
            raise ValueError("maximum_beliefs_per_agent must be a positive integer")
        self._maximum_beliefs_per_agent = maximum_beliefs_per_agent
        self._maximum_book_age = maximum_book_age
        self._maximum_order_book_depth = maximum_order_book_depth
        self._repository = PostgresHarnessRepository(database_url, connect=connect)
        self._immediate_order_executor = immediate_order_executor
        self._require_live_order_execution = require_live_order_execution

    def run(
        self, claim: CycleClaim, frozen: JsonObject, prompt: JsonObject
    ) -> HarnessExecutionResult:
        del prompt
        if claim.recovery:
            return self._recover_completed_run(claim)
        messages, model_config = self._load_context(claim.cycle_id)
        immediate_executor = self._immediate_order_executor
        context = production_tool_context(
            self._database_url,
            claim,
            self._exa,
            frozen=frozen,
            clock=self._clock,
            maximum_beliefs_per_agent=self._maximum_beliefs_per_agent,
            maximum_book_age=self._maximum_book_age,
            maximum_order_book_depth=self._maximum_order_book_depth,
            immediate_order_executor=(
                (
                    lambda submission: immediate_executor.submit_and_execute(
                        claim, frozen, submission
                    )
                )
                if immediate_executor is not None
                else None
            ),
            live_order_execution=(
                immediate_executor is not None and immediate_executor.uses_live_context
            ),
            live_order_required=(
                self._require_live_order_execution
            ),
        )
        registry = ProductionToolRegistry(context, schema_path=self._schema_path)
        result = BoundedToolHarness(
            self._gateway,
            registry.tool_specs(),
            self._limits,
            monotonic=self._monotonic,
        ).run(messages, model_config=model_config)
        transcript = canonical_redacted_json(
            {
                "messages": result.messages,
                "tool_calls": [asdict(item) for item in result.tool_calls],
                "termination_status": result.termination_status,
            }
        )
        artifact = self._store.put(transcript)
        completed = _aware(self._clock())
        retained = six_month_retain_until(completed)
        registrations = _harness_artifact_registrations(artifact, result.telemetry, retained)
        run_id = self._repository.persist_run(
            agent_cycle_id=claim.cycle_id,
            result=result,
            transcript_uri=artifact.uri,
            transcript_sha256=artifact.sha256,
            completed_at=completed,
            retain_until=retained,
            artifacts=registrations,
        )
        self._persist_detailed_audit(claim, result, retained, completed)
        searches = sum(1 for item in result.tool_calls if item.name in EXA_RESEARCH_TOOL_NAMES)
        intent_ids = self._cycle_intent_ids(claim.cycle_id)
        return HarnessExecutionResult(
            {
                "harness_run_id": str(run_id),
                "termination_status": result.termination_status,
                "intent_ids": [str(value) for value in intent_ids],
                "transcript_sha256": artifact.sha256,
            },
            registrations,
            searches,
            len(result.tool_calls),
        )

    def _recover_completed_run(self, claim: CycleClaim) -> HarnessExecutionResult:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, termination_status, total_tool_calls, total_web_searches, "
                "transcript_artifact_uri, transcript_sha256, retain_until "
                "FROM harness_runs WHERE agent_cycle_id = %s",
                (claim.cycle_id,),
            )
            run = cursor.fetchone()
            if run is None:
                raise ProductionCompositionUnavailable(
                    "recovery found no completed persisted harness run; "
                    "provider replay is forbidden"
                )
            cursor.execute(
                "SELECT uri, sha256, byte_length, retain_until FROM artifact_inventory "
                "WHERE status = 'active' AND (uri = %s OR uri IN "
                "(SELECT raw_artifact_uri FROM provider_usage WHERE agent_cycle_id = %s "
                "AND raw_artifact_uri IS NOT NULL)) "
                "ORDER BY created_at, id",
                (str(run[4]), claim.cycle_id),
            )
            artifact_rows = tuple(cursor.fetchall())
        registrations = tuple(
            ArtifactRegistration(
                str(row[0]),
                str(row[1]),
                int(str(row[2])),
                cast(datetime, row[3]),
            )
            for row in artifact_rows
        )
        if not registrations or not any(
            item.uri == str(run[4]) and item.sha256 == str(run[5]) for item in registrations
        ):
            raise ProductionCompositionUnavailable(
                "completed harness run lacks its atomic artifact inventory"
            )
        intent_ids = self._cycle_intent_ids(claim.cycle_id)
        return HarnessExecutionResult(
            {
                "harness_run_id": str(run[0]),
                "termination_status": str(run[1]),
                "intent_ids": [str(value) for value in intent_ids],
                "transcript_sha256": str(run[5]),
                "recovered_from_persisted_run": True,
            },
            registrations,
            int(str(run[3])),
            int(str(run[2])),
        )

    def _load_context(self, cycle_id: uuid.UUID) -> tuple[list[JsonObject], JsonObject]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT context FROM cycle_contexts WHERE agent_cycle_id = %s",
                (cycle_id,),
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[0], Mapping):
            raise ProductionCompositionUnavailable("persisted cycle context is missing")
        raw_messages = row[0].get("messages")
        raw_model = row[0].get("model_config")
        if not isinstance(raw_messages, list) or not isinstance(raw_model, Mapping):
            raise ProductionCompositionUnavailable("persisted prompt context is malformed")
        messages: list[JsonObject] = []
        for item in raw_messages:
            if not isinstance(item, Mapping):
                raise ProductionCompositionUnavailable("persisted prompt message is malformed")
            messages.append({str(key): value for key, value in item.items()})
        return messages, {str(key): value for key, value in raw_model.items()}

    def _persist_detailed_audit(
        self,
        claim: CycleClaim,
        result: HarnessResult,
        retained: datetime,
        completed: datetime,
    ) -> None:
        model_telemetry = [row for row in result.telemetry if row.usage_kind == "model"]
        search_telemetry = iter(row for row in result.telemetry if row.usage_kind == "web_search")
        records = {row.id: row for row in result.tool_calls}
        prefix: list[JsonObject] = []
        turn = 0
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            for message in result.messages:
                if message.get("role") != "assistant":
                    prefix.append(message)
                    continue
                telemetry = model_telemetry[turn] if turn < len(model_telemetry) else None
                turn_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"vtrade:model-turn:{claim.cycle_id}:{turn}"
                )
                cursor.execute(
                    "INSERT INTO model_turns "
                    "(id, agent_cycle_id, turn_index, request, response, provider_response_id, "
                    "termination_status, started_at, completed_at, raw_artifact_uri, "
                    "raw_sha256, retain_until) VALUES "
                    "(%s, %s, %s, %s::jsonb, %s::jsonb, NULL, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (agent_cycle_id, turn_index) DO NOTHING",
                    (
                        turn_id,
                        claim.cycle_id,
                        turn,
                        json.dumps({"messages": prefix}, sort_keys=True, default=str),
                        json.dumps(message, sort_keys=True, default=str),
                        "stop" if not message.get("tool_calls") else "tool_calls",
                        completed,
                        completed,
                        telemetry.artifact_uri if telemetry else None,
                        telemetry.raw_sha256 if telemetry else None,
                        retained,
                    ),
                )
                calls = message.get("tool_calls", [])
                if isinstance(calls, list):
                    for call_index, call in enumerate(calls):
                        if not isinstance(call, Mapping):
                            continue
                        call_id = str(call.get("id") or "")
                        record = records.get(call_id)
                        if record is None:
                            continue
                        tool_record_id = uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vtrade:tool-call:{turn_id}:{call_index}",
                        )
                        cursor.execute(
                            "INSERT INTO tool_calls "
                            "(id, model_turn_id, call_index, provider_call_id, category, "
                            "tool_name, display_name, arguments, output, success, "
                            "validation_status, error, called_at, completed_at, retain_until) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
                            "%s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (model_turn_id, call_index) DO NOTHING",
                            (
                                tool_record_id,
                                turn_id,
                                call_index,
                                record.id,
                                record.category,
                                record.name,
                                record.name,
                                json.dumps(record.arguments or {}, sort_keys=True),
                                json.dumps(record.output, sort_keys=True, default=str),
                                record.success,
                                "valid" if record.success else "rejected",
                                None if record.success else str(record.output.get("message", "")),
                                completed,
                                completed,
                                retained,
                            ),
                        )
                        if record.name in EXA_RESEARCH_TOOL_NAMES and record.success:
                            telemetry = next(search_telemetry, None)
                            if telemetry is None:
                                raise ProductionCompositionUnavailable(
                                    "successful Exa research call lacks provider telemetry"
                                )
                            self._persist_research(
                                cursor,
                                claim,
                                tool_record_id,
                                record.arguments or {},
                                record.output,
                                telemetry,
                                completed,
                            )
                prefix.append(message)
                turn += 1

    @staticmethod
    def _persist_research(
        cursor: _Cursor,
        claim: CycleClaim,
        tool_call_id: uuid.UUID,
        arguments: Mapping[str, object],
        output: Mapping[str, object],
        telemetry: ProviderTelemetry,
        completed: datetime,
    ) -> None:
        if "results" in output:
            raw_results = output.get("results")
        elif "url" in output:
            raw_results = [output]
        else:
            raise ProductionCompositionUnavailable("Exa research result is malformed")
        if not isinstance(raw_results, list):
            raise ProductionCompositionUnavailable("Exa research result list is malformed")
        for row in raw_results:
            if not isinstance(row, Mapping):
                raise ProductionCompositionUnavailable("Exa research result is malformed")
            url = row.get("url")
            if not isinstance(url, str) or not url:
                raise ProductionCompositionUnavailable("Exa research result URL is missing")
            raw_content = row.get("content")
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(row.get("full_text"), str):
                content = str(row["full_text"])
            elif isinstance(row.get("highlights"), list):
                content = "\n".join(str(value) for value in row["highlights"])
            else:
                content = ""
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:research-document:{url}:{digest}")
            published = _optional_research_timestamp(row.get("published_at"))
            cursor.execute(
                "INSERT INTO research_documents "
                "(id, canonical_url, title, source_published_at, fetched_at, content_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (canonical_url, content_sha256) DO NOTHING",
                (
                    document_id,
                    url,
                    str(row.get("title") or ""),
                    published,
                    completed,
                    digest,
                ),
            )
            cursor.execute(
                "SELECT id FROM research_documents "
                "WHERE canonical_url = %s AND content_sha256 = %s",
                (url, digest),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise RuntimeError("research document disappeared after insert")
            document_id = uuid.UUID(str(existing[0]))
            cursor.execute(
                "INSERT INTO research_artifacts "
                "(id, tool_call_id, document_id, provider, query, artifact_uri, "
                "raw_sha256, source_cutoff, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vtrade:research-artifact:{tool_call_id}:{document_id}",
                    ),
                    tool_call_id,
                    document_id,
                    telemetry.provider,
                    str(arguments.get("query") or arguments.get("highlight_query") or ""),
                    telemetry.artifact_uri,
                    telemetry.raw_sha256,
                    _cutoff(claim),
                    completed,
                ),
            )

    def _cycle_intent_ids(self, cycle_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM order_intents WHERE agent_cycle_id = %s ORDER BY created_at, id",
                (cycle_id,),
            )
            return tuple(uuid.UUID(str(row[0])) for row in cursor.fetchall())


@dataclass(frozen=True, slots=True)
class _TradingContext:
    intent_id: uuid.UUID
    market_id: uuid.UUID
    outcome_id: uuid.UUID
    order: PaperOrder
    market: Market
    outcome: Outcome
    book: OrderBookSnapshot
    book_snapshot_id: uuid.UUID
    requested_at: datetime | None = None


class _PostgresTradingState:
    def __init__(
        self,
        database_url: str,
        *,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._connect = connect or _default_connect

    @contextmanager
    def locked_cursor(self, agent_id: uuid.UUID) -> Generator[_Cursor, None, None]:
        """Yield the single transaction used by an immediate order execution."""
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(agent_id),)
            )
            yield cursor

    @staticmethod
    def insert_intent(
        cursor: _Cursor,
        claim: CycleClaim,
        submission: Any,
        *,
        requested_at: datetime | None = None,
    ) -> None:
        requested_at = requested_at or submission.created_at
        cursor.execute(
            "INSERT INTO order_intents "
            "(id, agent_cycle_id, market_id, outcome_id, side, amount_micros, shares, "
            "strategy, thesis, estimated_probability, expected_value_micros, "
            "validation_status, idempotency_key, created_at, requested_at) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, "
            "'submitted_for_immediate_execution', %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (
                submission.intent_id,
                claim.cycle_id,
                submission.market_id,
                submission.outcome_id,
                submission.side,
                submission.amount_micros,
                submission.shares,
                "observed_place_market_order",
                "submitted through frozen tool contract",
                submission.confidence,
                f"intent:{submission.intent_id}",
                submission.created_at,
                requested_at,
            ),
        )

    def persisted_harness_intents(
        self, claim: CycleClaim, harness: Mapping[str, object]
    ) -> set[uuid.UUID]:
        raw_run_id = harness.get("harness_run_id")
        if not isinstance(raw_run_id, str):
            raise ProductionCompositionUnavailable("broker requires a persisted harness run")
        try:
            run_id = uuid.UUID(raw_run_id)
        except ValueError as exc:
            raise ProductionCompositionUnavailable("persisted harness run id is malformed") from exc
        payload_ids = set(_uuids(harness, "intent_ids"))
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM harness_runs WHERE id = %s AND agent_cycle_id = %s",
                (run_id, claim.cycle_id),
            )
            if cursor.fetchone() is None:
                raise ProductionCompositionUnavailable(
                    "broker cannot execute intents from an unpersisted harness"
                )
            cursor.execute(
                "SELECT id FROM order_intents WHERE agent_cycle_id = %s ORDER BY created_at, id",
                (claim.cycle_id,),
            )
            persisted_ids = {uuid.UUID(str(row[0])) for row in cursor.fetchall()}
        if payload_ids != persisted_ids:
            raise ProductionCompositionUnavailable(
                "harness checkpoint intent membership differs from persisted intents"
            )
        return persisted_ids

    def incomplete_intents(
        self, claim: CycleClaim, *, cursor: _Cursor | None = None
    ) -> tuple[_TradingContext, ...]:
        """Load restart candidates without requiring the frozen universe to be live."""
        if cursor is None:
            with (
                self._connect(self._database_url) as connection,
                connection.cursor() as local_cursor,
            ):
                return self.incomplete_intents(claim, cursor=local_cursor)
        cursor.execute(
            "SELECT oi.id, oi.market_id, oi.outcome_id, oi.side, oi.shares, "
            "oi.created_at, ms.payload, ms.volume_micros, ms.liquidity_micros, "
            "ms.status, obs.id, obs.cutoff, obs.source_created_at, obs.bids, obs.asks, "
            "obs.raw_artifact_uri, obs.raw_sha256, o.venue_token_id, oi.requested_at "
            "FROM order_intents oi JOIN markets m ON m.id = oi.market_id "
            "JOIN outcomes o ON o.id = oi.outcome_id "
            "JOIN LATERAL (SELECT * FROM market_snapshots snapshot "
            "WHERE snapshot.market_id = oi.market_id "
            "ORDER BY snapshot.cutoff DESC, snapshot.id DESC LIMIT 1) ms ON true "
            "JOIN LATERAL (SELECT * FROM order_book_snapshots snapshot "
            "WHERE snapshot.outcome_id = oi.outcome_id "
            "ORDER BY snapshot.cutoff DESC, snapshot.id DESC LIMIT 1) obs ON true "
            "WHERE oi.agent_cycle_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM orders existing "
            "WHERE existing.intent_id = oi.id) "
            "ORDER BY oi.created_at, oi.id",
            (claim.cycle_id,),
        )
        rows = cursor.fetchall()
        return tuple(self._trading_context(row, claim.agent_id) for row in rows)

    def pending_intents(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        *,
        cursor: _Cursor | None = None,
        include_existing: bool = False,
    ) -> tuple[_TradingContext, ...]:
        book_ids = _uuids(frozen, "order_book_snapshot_ids")
        market_snapshot_ids = _uuids(frozen, "market_snapshot_ids")
        if not book_ids or not market_snapshot_ids:
            raise ProductionCompositionUnavailable(
                "broker requires current-cycle market and order-book memberships"
            )
        if cursor is None:
            with (
                self._connect(self._database_url) as connection,
                connection.cursor() as local_cursor,
            ):
                return self.pending_intents(
                    claim,
                    frozen,
                    cursor=local_cursor,
                    include_existing=include_existing,
                )
        existing_clause = "" if include_existing else "AND existing.id IS NULL"
        cursor.execute(
            "SELECT oi.id, oi.market_id, oi.outcome_id, oi.side, oi.shares, "
            "oi.created_at, ms.payload, ms.volume_micros, ms.liquidity_micros, "
            "ms.status, obs.id, obs.cutoff, obs.source_created_at, obs.bids, obs.asks, "
            "obs.raw_artifact_uri, obs.raw_sha256, o.venue_token_id, oi.requested_at "
            "FROM order_intents oi JOIN markets m ON m.id = oi.market_id "
            "JOIN outcomes o ON o.id = oi.outcome_id "
            "JOIN market_snapshots ms ON ms.market_id = m.id "
            "JOIN order_book_snapshots obs ON obs.outcome_id = o.id "
            "LEFT JOIN orders existing ON existing.intent_id = oi.id "
            "WHERE oi.agent_cycle_id = %s "
            f"{existing_clause} "
            "AND ms.id = ANY(%s::uuid[]) AND obs.id = ANY(%s::uuid[]) "
            "AND ms.cutoff <= %s AND obs.cutoff <= %s "
            "AND ms.status = 'open' "
            "AND COALESCE((ms.payload->>'tradeable')::boolean, false) "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(ms.payload->'outcomes') frozen "
            "WHERE frozen->>'venue_token_id' = o.venue_token_id "
            "AND COALESCE((frozen->>'tradeable')::boolean, false)) "
            "ORDER BY oi.created_at, oi.id",
            (
                claim.cycle_id,
                list(market_snapshot_ids),
                list(book_ids),
                _cutoff(claim),
                _cutoff(claim),
            ),
        )
        rows = cursor.fetchall()
        return tuple(self._trading_context(row, claim.agent_id) for row in rows)

    @staticmethod
    def _trading_context(row: Sequence[object], agent_id: uuid.UUID) -> _TradingContext:
        intent_id = uuid.UUID(str(row[0]))
        market_id = uuid.UUID(str(row[1]))
        outcome_id = uuid.UUID(str(row[2]))
        if row[4] is None:
            raise ProductionCompositionUnavailable("order intent lacks normalized shares")
        payload = _mapping(row[6])
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, list):
            raise ProductionCompositionUnavailable("market snapshot outcomes are malformed")
        token_id = str(row[17])
        outcome_payload: dict[str, Any] | None = None
        for candidate in raw_outcomes:
            if isinstance(candidate, Mapping) and candidate.get("venue_token_id") == token_id:
                outcome_payload = _mapping(candidate)
                break
        if outcome_payload is None:
            raise ProductionCompositionUnavailable(
                "intent outcome is absent from its current-cycle market snapshot"
            )
        metadata = _mapping(payload.get("metadata"))
        outcome = Outcome(
            str(outcome_id),
            str(market_id),
            _required_payload_string(outcome_payload, "name"),
            token_id,
            None,
            None,
            MicroDollars(int(Decimal(str(outcome_payload["tick_size"])) * Decimal(1_000_000))),
            MicroDollars(
                int(Decimal(str(outcome_payload["minimum_order_size"])) * Decimal(1_000_000))
            ),
            (
                int(str(outcome_payload["outcome_index"]))
                if outcome_payload.get("outcome_index") is not None
                else None
            ),
            (
                Decimal(str(outcome_payload["indicative_price"]))
                if outcome_payload.get("indicative_price") is not None
                else None
            ),
            bool(outcome_payload.get("tradeable")),
            _mapping(outcome_payload.get("metadata")),
        )
        market = Market(
            str(market_id),
            _required_payload_string(payload, "venue_market_id"),
            _required_payload_string(payload, "event_id"),
            _required_payload_string(payload, "question"),
            str(payload.get("resolution_rules") or ""),
            _optional_payload_timestamp(payload.get("opens_at")),
            _optional_payload_timestamp(payload.get("closes_at")),
            MarketStatus(str(row[9])),
            str(payload["category"]) if payload.get("category") is not None else None,
            MicroDollars(int(str(row[7]))),
            MicroDollars(int(str(row[8]))),
            metadata,
            _required_payload_string(payload, "slug"),
            (
                str(payload["resolution_source"])
                if payload.get("resolution_source") is not None
                else None
            ),
            bool(payload.get("tradeable")),
            (outcome,),
            _optional_payload_timestamp(payload.get("observed_at")) or cast(datetime, row[11]),
            _optional_payload_timestamp(payload.get("source_updated_at")),
        )
        bids = _levels(row[13])
        asks = _levels(row[14])
        artifact = RawArtifact(str(row[16]), 0, str(row[15]))
        book = OrderBookSnapshot(
            token_id,
            str(metadata.get("condition_id") or ""),
            cast(datetime, row[11]),
            cast(datetime | None, row[12]),
            bids,
            asks,
            Decimal(str(outcome_payload["tick_size"])),
            Decimal(str(outcome_payload["minimum_order_size"])),
            bool(_mapping(outcome_payload.get("metadata")).get("negative_risk", False)),
            artifact,
        )
        order = PaperOrder(
            str(intent_id),
            str(agent_id),
            str(market_id),
            str(outcome_id),
            Side(str(row[3])),
            Decimal(str(row[4])),
            cast(datetime, row[5]),
        )
        return _TradingContext(
            intent_id,
            market_id,
            outcome_id,
            order,
            market,
            outcome,
            book,
            uuid.UUID(str(row[10])),
            cast(datetime | None, row[18]) if len(row) > 18 else cast(datetime, row[5]),
        )

    def prepare_virtual_liquidity(
        self,
        cursor: _Cursor,
        claim: CycleClaim,
        order: PaperOrder,
        *,
        snapshot_id: uuid.UUID,
        snapshot: OrderBookSnapshot,
        maximum_book_depth: int,
        ignored_best_levels: int = 0,
        maximum_ignored_depth_fraction: Decimal = Decimal(0),
        liquidity_rule_version: str = LIQUIDITY_HAIRCUT_RULE_VERSION,
    ) -> VirtualLiquidityReservation:
        """Lock and materialize the agent-private view of one frozen book.

        The context is ``agent_cycle_id + order_book_snapshot_id``.  A new
        immutable book therefore starts a new private context; historical
        consumption remains available for audit and is never carried into the
        new snapshot by changing the global book.  The raw six-level observation
        and the effective five-level execution view are both persisted.
        """

        order_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:order:{order.id}")
        agent_id = uuid.UUID(str(claim.agent_id))
        cycle_id = uuid.UUID(str(claim.cycle_id))
        context_version = _liquidity_context_version(
            cycle_id,
            snapshot_id,
            rule_version=liquidity_rule_version,
            maximum_book_depth=maximum_book_depth,
            ignored_best_levels=ignored_best_levels,
            maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
        )
        cursor.execute(
            "SELECT id, agent_id, agent_cycle_id, snapshot_id, token_id, side, "
            "context_version, requested_shares, available_shares, consumed_shares, "
            "cancelled_shares, remaining_shares, portfolio_before, execution_at, "
            "rule_version, ignored_best_levels, maximum_ignored_depth_fraction, "
            "raw_depth_shares, best_level_fraction, ignored_depth_shares, "
            "ignored_fraction, effective_depth_shares, best_level_price "
            "FROM virtual_liquidity_executions WHERE order_id = %s FOR UPDATE",
            (order_id,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            if (
                uuid.UUID(str(existing[1])) != agent_id
                or uuid.UUID(str(existing[2])) != cycle_id
                or uuid.UUID(str(existing[3])) != snapshot_id
                or str(existing[4]) != snapshot.token_id
                or str(existing[5]) != order.side.value
                or Decimal(str(existing[7])) != order.shares
            ):
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity execution context differs on retry"
                )
            cursor.execute(
                "SELECT level_index, price, displayed_shares, ignored_shares, "
                "effective_shares, available_shares, consumed_shares, cancelled_shares, "
                "remaining_shares, executable "
                "FROM virtual_liquidity_execution_levels WHERE execution_id = %s "
                "ORDER BY level_index",
                (uuid.UUID(str(existing[0])),),
            )
            level_rows = cursor.fetchall()
            existing_levels = tuple(
                VirtualLiquidityLevel(
                    level_index=int(str(row[0])),
                    price=Decimal(str(row[1])),
                    displayed_shares=Decimal(str(row[2])),
                    available_shares=Decimal(str(row[5])),
                    ignored_shares=Decimal(str(row[3])),
                    effective_shares=Decimal(str(row[4])),
                    executable=bool(row[9]),
                )
                for row in level_rows
            )
            metrics = _virtual_liquidity_metrics_from_rows(existing, level_rows)
            return VirtualLiquidityReservation(
                order_id=str(order_id),
                context_version=str(existing[6]),
                agent_id=str(agent_id),
                agent_cycle_id=str(cycle_id),
                snapshot_id=str(snapshot_id),
                token_id=snapshot.token_id,
                side=order.side,
                snapshot=private_snapshot(snapshot, side=order.side, levels=existing_levels),
                levels=existing_levels,
                existing_metrics=metrics,
                retry_portfolio=_portfolio_from_payload(existing[12]),
                retry_now=_aware(cast(datetime, existing[13])),
                rule_version=str(existing[14]),
                ignored_best_levels=int(str(existing[15])),
                maximum_ignored_depth_fraction=Decimal(str(existing[16])),
            )

        book = effective_liquidity_book(
            snapshot,
            side=order.side,
            maximum_book_depth=maximum_book_depth,
            ignored_best_levels=ignored_best_levels,
            maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
        )
        for level in book.raw_levels:
            cursor.execute(
                "INSERT INTO virtual_liquidity_levels "
                "(agent_id, agent_cycle_id, snapshot_id, token_id, side, level_index, "
                "price, displayed_shares, ignored_shares, effective_shares, executable, "
                "consumed_shares, cancelled_shares, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, %s, %s) "
                "ON CONFLICT (agent_id, agent_cycle_id, snapshot_id, token_id, side, "
                "level_index, price) DO NOTHING",
                (
                    agent_id,
                    cycle_id,
                    snapshot_id,
                    snapshot.token_id,
                    order.side.value,
                    level.level_index,
                    level.price,
                    level.displayed_shares,
                    level.ignored_shares,
                    level.effective_shares,
                    level.executable,
                    order.created_at,
                    order.created_at,
                ),
            )
        cursor.execute(
            "SELECT level_index, price, displayed_shares, ignored_shares, effective_shares, "
            "consumed_shares, executable "
            "FROM virtual_liquidity_levels WHERE agent_id = %s AND agent_cycle_id = %s "
            "AND snapshot_id = %s AND token_id = %s AND side = %s "
            "ORDER BY level_index FOR UPDATE",
            (agent_id, cycle_id, snapshot_id, snapshot.token_id, order.side.value),
        )
        rows = cursor.fetchall()
        if len(rows) != len(book.raw_levels):
            raise ProductionCompositionUnavailable("virtual-liquidity level state is incomplete")
        parsed_levels: list[VirtualLiquidityLevel] = []
        expected_by_index = {level.level_index: level for level in book.raw_levels}
        for row in rows:
            level_index = int(str(row[0]))
            displayed = Decimal(str(row[2]))
            ignored = Decimal(str(row[3]))
            effective = Decimal(str(row[4]))
            consumed = Decimal(str(row[5]))
            executable = bool(row[6])
            source = expected_by_index.get(level_index)
            if source is None:
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity level index is not present in the immutable book"
                )
            if (
                Decimal(str(row[1])) != source.price
                or displayed != source.displayed_shares
                or ignored != source.ignored_shares
                or effective != source.effective_shares
                or executable != source.executable
            ):
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity level differs from its immutable haircut view"
                )
            if consumed < 0 or consumed > effective:
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity consumption exceeds effective depth"
                )
            available = effective - consumed if executable else Decimal(0)
            if available < 0:
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity consumption exceeds effective depth"
                )
            parsed_levels.append(
                VirtualLiquidityLevel(
                    level_index=level_index,
                    price=source.price,
                    displayed_shares=displayed,
                    available_shares=available,
                    ignored_shares=ignored,
                    effective_shares=effective,
                    executable=executable,
                )
            )
        parsed_levels.sort(key=lambda level: level.level_index)
        return VirtualLiquidityReservation(
            order_id=str(order_id),
            context_version=context_version,
            agent_id=str(agent_id),
            agent_cycle_id=str(cycle_id),
            snapshot_id=str(snapshot_id),
            token_id=snapshot.token_id,
            side=order.side,
            snapshot=private_snapshot(snapshot, side=order.side, levels=tuple(parsed_levels)),
            levels=tuple(parsed_levels),
            rule_version=liquidity_rule_version,
            ignored_best_levels=ignored_best_levels,
            maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
        )

    def finalize_virtual_liquidity(
        self,
        cursor: _Cursor,
        reservation: VirtualLiquidityReservation,
        result: Any,
        *,
        completed_at: datetime,
    ) -> VirtualLiquidityMetrics:
        metrics = metrics_for_fills(
            reservation,
            result.fills,
            requested_shares=result.order.shares,
        )
        if reservation.existing_metrics is not None:
            return reservation.existing_metrics

        consumed = consumed_by_level(reservation, result.fills)
        for level in reservation.levels:
            amount = consumed[level.level_index]
            if amount <= 0:
                continue
            cursor.execute(
                "UPDATE virtual_liquidity_levels SET consumed_shares = "
                "consumed_shares + %s, updated_at = %s WHERE agent_id = %s "
                "AND agent_cycle_id = %s AND snapshot_id = %s AND token_id = %s "
                "AND side = %s AND level_index = %s AND price = %s "
                "AND executable = true AND effective_shares - consumed_shares >= %s "
                "RETURNING effective_shares, consumed_shares",
                (
                    amount,
                    completed_at,
                    uuid.UUID(reservation.agent_id),
                    uuid.UUID(reservation.agent_cycle_id),
                    uuid.UUID(reservation.snapshot_id),
                    reservation.token_id,
                    reservation.side.value,
                    level.level_index,
                    level.price,
                    amount,
                ),
            )
            updated = cursor.fetchone()
            if updated is None:
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity decrement lost its private capacity"
                )
            expected_remaining = level.available_shares - amount
            actual_remaining = Decimal(str(updated[0])) - Decimal(str(updated[1]))
            if actual_remaining != expected_remaining:
                raise ProductionCompositionUnavailable(
                    "virtual-liquidity decrement is not deterministic"
                )

        execution_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"vtrade:virtual-liquidity:{reservation.order_id}"
        )
        cursor.execute(
            "INSERT INTO virtual_liquidity_executions "
            "(id, order_id, agent_id, agent_cycle_id, snapshot_id, token_id, side, "
            "context_version, requested_shares, available_shares, consumed_shares, "
            "cancelled_shares, remaining_shares, portfolio_before, execution_at, "
            "idempotency_key, created_at, rule_version, ignored_best_levels, "
            "maximum_ignored_depth_fraction, raw_depth_shares, best_level_fraction, "
            "ignored_depth_shares, ignored_fraction, effective_depth_shares, "
            "best_level_price) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (order_id) DO NOTHING",
            (
                execution_id,
                uuid.UUID(reservation.order_id),
                uuid.UUID(reservation.agent_id),
                uuid.UUID(reservation.agent_cycle_id),
                uuid.UUID(reservation.snapshot_id),
                reservation.token_id,
                reservation.side.value,
                reservation.context_version,
                metrics.requested_shares,
                metrics.available_shares,
                metrics.consumed_shares,
                metrics.cancelled_shares,
                metrics.remaining_shares,
                json.dumps(_portfolio_payload(result.portfolio_before), sort_keys=True),
                completed_at,
                f"virtual-liquidity:{reservation.order_id}",
                completed_at,
                metrics.rule_version,
                metrics.ignored_best_levels,
                metrics.maximum_ignored_depth_fraction,
                metrics.raw_depth_shares,
                metrics.best_level_fraction,
                metrics.ignored_depth_shares,
                metrics.ignored_fraction,
                metrics.effective_depth_shares,
                metrics.best_level_price,
            ),
        )
        for metric_level in metrics.levels:
            cursor.execute(
                "INSERT INTO virtual_liquidity_execution_levels "
                "(execution_id, level_index, price, displayed_shares, ignored_shares, "
                "effective_shares, executable, available_shares, consumed_shares, "
                "cancelled_shares, remaining_shares) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    execution_id,
                    metric_level.level_index,
                    metric_level.price,
                    metric_level.displayed_shares,
                    metric_level.ignored_shares,
                    metric_level.effective_shares,
                    metric_level.executable,
                    metric_level.available_shares,
                    metric_level.consumed_shares,
                    metric_level.cancelled_shares,
                    metric_level.remaining_shares,
                ),
            )
        return metrics

    @staticmethod
    def virtual_liquidity_audit(
        cursor: _Cursor,
        *,
        agent_id: uuid.UUID,
        agent_cycle_id: uuid.UUID | None = None,
    ) -> tuple[Sequence[object], ...]:
        """Return aggregate private-depth metrics for the read-only audit surface."""

        where = "agent_id = %s"
        params: tuple[object, ...] = (agent_id,)
        if agent_cycle_id is not None:
            where += " AND agent_cycle_id = %s"
            params += (agent_cycle_id,)
        cursor.execute(
            "SELECT agent_id, agent_cycle_id, snapshot_id, token_id, side, "
            "sum(displayed_shares) AS displayed_shares, "
            "sum(ignored_shares) AS ignored_shares, "
            "sum(effective_shares) AS effective_shares, "
            "sum(effective_shares) FILTER (WHERE executable) AS executable_shares, "
            "sum(consumed_shares) AS consumed_shares, "
            "sum(effective_shares - consumed_shares) FILTER (WHERE executable) "
            "AS remaining_shares, "
            "COALESCE((SELECT sum(cancelled_shares) FROM virtual_liquidity_executions "
            "vle WHERE vle.agent_id = vll.agent_id "
            "AND vle.agent_cycle_id = vll.agent_cycle_id "
            "AND vle.snapshot_id = vll.snapshot_id AND vle.token_id = vll.token_id "
            "AND vle.side = vll.side), 0) AS cancelled_shares "
            "FROM virtual_liquidity_levels vll WHERE "
            + where
            + " GROUP BY agent_id, agent_cycle_id, snapshot_id, token_id, side "
            "ORDER BY agent_cycle_id, snapshot_id, token_id, side",
            params,
        )
        return tuple(cursor.fetchall())

    def portfolio(self, agent_id: uuid.UUID, *, cursor: _Cursor | None = None) -> PortfolioState:
        if cursor is None:
            with (
                self._connect(self._database_url) as connection,
                connection.cursor() as local_cursor,
            ):
                return self.portfolio(agent_id, cursor=local_cursor)
        cursor.execute(
            "SELECT COALESCE(sum(lp.amount_micros) FILTER "
            "(WHERE lp.account = 'cash'), 0), a.portfolio_version FROM agents a "
            "LEFT JOIN ledger_entries le ON le.agent_id = a.id "
            "LEFT JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
            "WHERE a.id = %s GROUP BY a.id",
            (agent_id,),
        )
        account = cursor.fetchone()
        cursor.execute(
            "SELECT m.id, p.outcome_id, p.shares, p.average_cost, "
            "p.cost_basis_micros, p.realized_pnl_micros, p.entry_fees_micros "
            "FROM positions p "
            "JOIN outcomes o ON o.id = p.outcome_id "
            "JOIN markets m ON m.id = o.market_id "
            "WHERE p.agent_id = %s AND p.shares > 0 ORDER BY p.outcome_id",
            (agent_id,),
        )
        positions = cursor.fetchall()
        if account is None:
            raise ProductionCompositionUnavailable("agent portfolio is missing")
        return PortfolioState(
            str(agent_id),
            MicroDollars(int(str(account[0]))),
            tuple(
                PositionState(
                    str(row[0]),
                    str(row[1]),
                    Decimal(str(row[2])),
                    Decimal(str(row[3])),
                    MicroDollars(int(str(row[4]))),
                    MicroDollars(int(str(row[5]))),
                    MicroDollars(int(str(row[6]))),
                )
                for row in positions
            ),
            pending_orders=(),
            version=int(str(account[1])),
        )

    def executable_bids(
        self,
        portfolio: PortfolioState,
        *,
        cutoff: datetime,
        order_book_snapshot_ids: Sequence[uuid.UUID],
        cursor: _Cursor | None = None,
    ) -> dict[str, ArchivedBid | None]:
        if not portfolio.positions:
            return {}
        outcomes = [uuid.UUID(row.outcome_id) for row in portfolio.positions]
        if cursor is None:
            with (
                self._connect(self._database_url) as connection,
                connection.cursor() as local_cursor,
            ):
                return self.executable_bids(
                    portfolio,
                    cutoff=cutoff,
                    order_book_snapshot_ids=order_book_snapshot_ids,
                    cursor=local_cursor,
                )
        cursor.execute(
            "SELECT DISTINCT ON (obs.outcome_id) obs.outcome_id, obs.best_bid, obs.cutoff "
            "FROM order_book_snapshots obs WHERE obs.outcome_id = ANY(%s::uuid[]) "
            "AND obs.id = ANY(%s::uuid[]) AND obs.cutoff <= %s "
            "ORDER BY obs.outcome_id, obs.cutoff DESC, obs.id DESC",
            (outcomes, list(order_book_snapshot_ids), cutoff),
        )
        rows = cursor.fetchall()
        found = {
            str(row[0]): (
                ArchivedBid(Decimal(str(row[1])), cast(datetime, row[2]))
                if row[1] is not None
                else None
            )
            for row in rows
        }
        return {
            position.outcome_id: found.get(position.outcome_id) for position in portfolio.positions
        }

    def live_executable_bids(
        self,
        portfolio: PortfolioState,
        *,
        as_of: datetime,
        maximum_bid_age: timedelta,
        cursor: _Cursor | None = None,
    ) -> dict[str, ArchivedBid | None]:
        """Read only recent historical bids needed to value existing positions."""
        if not portfolio.positions:
            return {}
        if maximum_bid_age < timedelta(0):
            raise ValueError("maximum bid age cannot be negative")
        outcomes = [uuid.UUID(row.outcome_id) for row in portfolio.positions]
        oldest = as_of - maximum_bid_age
        if cursor is None:
            with (
                self._connect(self._database_url) as connection,
                connection.cursor() as local_cursor,
            ):
                return self.live_executable_bids(
                    portfolio,
                    as_of=as_of,
                    maximum_bid_age=maximum_bid_age,
                    cursor=local_cursor,
                )
        cursor.execute(
            "SELECT DISTINCT ON (obs.outcome_id) obs.outcome_id, obs.best_bid, obs.cutoff "
            "FROM order_book_snapshots obs WHERE obs.outcome_id = ANY(%s::uuid[]) "
            "AND obs.best_bid IS NOT NULL AND obs.cutoff <= %s AND obs.cutoff >= %s "
            "AND (obs.source_created_at IS NULL OR obs.source_created_at <= %s) "
            "ORDER BY obs.outcome_id, obs.cutoff DESC, obs.id DESC",
            (outcomes, as_of, oldest, as_of),
        )
        rows = cursor.fetchall()
        found = {
            str(row[0]): ArchivedBid(Decimal(str(row[1])), cast(datetime, row[2])) for row in rows
        }
        return {
            position.outcome_id: found.get(position.outcome_id) for position in portfolio.positions
        }

    def archived_executable_bids(
        self,
        portfolio: PortfolioState,
        *,
        cutoff: datetime,
        maximum_bid_age: timedelta,
    ) -> tuple[dict[str, ArchivedBid | None], tuple[uuid.UUID, ...]]:
        if not portfolio.positions:
            return {}, ()
        outcomes = [uuid.UUID(row.outcome_id) for row in portfolio.positions]
        oldest = cutoff - maximum_bid_age
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT ON (obs.outcome_id) obs.outcome_id, obs.best_bid, "
                "obs.cutoff, obs.id FROM order_book_snapshots obs "
                "WHERE obs.outcome_id = ANY(%s::uuid[]) AND obs.best_bid IS NOT NULL "
                "AND obs.cutoff <= %s AND obs.cutoff >= %s "
                "AND (obs.source_created_at IS NULL OR obs.source_created_at <= %s) "
                "ORDER BY obs.outcome_id, obs.cutoff DESC, obs.id DESC",
                (outcomes, cutoff, oldest, cutoff),
            )
            rows = cursor.fetchall()
        found = {
            str(row[0]): ArchivedBid(Decimal(str(row[1])), cast(datetime, row[2])) for row in rows
        }
        return (
            {
                position.outcome_id: found.get(position.outcome_id)
                for position in portfolio.positions
            },
            tuple(uuid.UUID(str(row[3])) for row in rows),
        )


class ProductionBrokerPort:
    def __init__(
        self,
        database_url: str,
        market_repository: PostgresMarketDataRepository,
        *,
        clock: Callable[[], datetime],
        maximum_market_fraction: Decimal,
        maximum_bid_age: timedelta,
        paper_policy: PaperPolicy,
        liquidity_time_in_force: LiquidityTimeInForce,
        maximum_valuation_bid_age: timedelta | None = None,
        maximum_book_depth: int = 5,
        ignored_best_levels: int = 0,
        maximum_ignored_depth_fraction: Decimal = Decimal(0),
        liquidity_rule_version: str = LIQUIDITY_HAIRCUT_RULE_VERSION,
        live_context_provider: LiveOrderContextProvider | None = None,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._market_repository = market_repository
        self._clock = clock
        self._state = _PostgresTradingState(database_url, connect=connect)
        self._repository = PostgresBrokerRepository(database_url, connect=connect)
        self._liquidity_time_in_force = liquidity_time_in_force
        self._live_context_provider = live_context_provider
        self._maximum_live_observation_age = maximum_bid_age
        self._broker = PredictionArenaPaperBroker(
            policy=paper_policy,
            maximum_market_cost_basis_fraction=maximum_market_fraction,
            maximum_book_age=maximum_bid_age,
            maximum_book_depth=maximum_book_depth,
            ignored_best_levels=ignored_best_levels,
            maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
            liquidity_rule_version=liquidity_rule_version,
            maximum_valuation_bid_age=(
                maximum_bid_age if maximum_valuation_bid_age is None else maximum_valuation_bid_age
            ),
        )

    def execute(
        self, claim: CycleClaim, frozen: JsonObject, harness: JsonObject
    ) -> BrokerExecutionResult:
        live_provider = getattr(self, "_live_context_provider", None)
        if (
            getattr(self._broker, "policy", None) is PaperPolicy.LIQUIDITY_AWARE
            and live_provider is None
        ):
            raise ProductionCompositionUnavailable(
                "liquidity-aware broker requires a live order-context provider"
            )
        executor = MarketOrderExecutor(
            self._state,
            self._market_repository,
            self._repository,
            broker=self._broker,
            clock=self._clock,
            live_context_provider=live_provider,
            maximum_live_observation_age=getattr(
                self,
                "_maximum_live_observation_age",
                getattr(self._broker, "maximum_book_age", timedelta(minutes=5)),
            ),
        )
        if (
            claim.recovery
            and getattr(self._broker, "policy", None) is PaperPolicy.LIQUIDITY_AWARE
        ):
            cancelled = executor.cancel_incomplete_on_restart(claim, frozen)
            return BrokerExecutionResult(
                {
                    "order_ids": [str(receipt.order_id) for receipt in cancelled],
                    "rejections": [
                        {
                            "intent_id": str(receipt.result.order.id),
                            "code": receipt.result.rejection_code.value
                            if receipt.result.rejection_code
                            else None,
                        }
                        for receipt in cancelled
                    ],
                },
                (),
                0,
            )
        allowed_intents = self._state.persisted_harness_intents(claim, harness)
        if not allowed_intents:
            return BrokerExecutionResult({"order_ids": [], "rejections": []}, (), 0)
        created: list[str] = []
        rejected: list[JsonObject] = []
        accepted = 0
        for item in self._state.pending_intents(claim, frozen):
            if item.intent_id not in allowed_intents:
                raise ProductionCompositionUnavailable("broker encountered a foreign cycle intent")
            result = executor.execute(
                claim,
                frozen,
                item.intent_id,
                time_in_force=self._liquidity_time_in_force,
            )
            order = getattr(result, "order", item.order)
            created.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:order:{order.id}")))
            if result.status is ExecutionStatus.REJECTED:
                rejected.append(
                    {
                        "intent_id": str(item.intent_id),
                        "code": result.rejection_code.value if result.rejection_code else None,
                    }
                )
            else:
                accepted += 1
        return BrokerExecutionResult(
            {"order_ids": created, "rejections": rejected},
            (),
            accepted,
        )


class ProductionSettlementValuationPort:
    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], datetime],
        maximum_bid_age: timedelta,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._clock = clock
        self._maximum_bid_age = maximum_bid_age
        self._connect = connect or _default_connect
        self._state = _PostgresTradingState(database_url, connect=connect)
        self._repository = PostgresBrokerRepository(database_url, connect=connect)

    def settle_before_prompt(self, claim: CycleClaim, frozen: JsonObject) -> PreSettlementResult:
        cutoff = _cutoff(claim)
        settled_ids = self._settle_eligible(claim.agent_id, frozen, cutoff)
        return PreSettlementResult(
            {
                "settlement_ids": settled_ids,
                "settlement_cutoff": cutoff.isoformat(),
            },
            (),
            len(settled_ids),
        )

    def settle_and_value(
        self, claim: CycleClaim, frozen: JsonObject, broker: JsonObject
    ) -> SettlementValuationResult:
        del broker
        cutoff = _cutoff(claim)
        settled_ids = self._settle_eligible(claim.agent_id, frozen, cutoff)
        portfolio = self._state.portfolio(claim.agent_id)
        bids, valuation_book_ids = self._state.archived_executable_bids(
            portfolio,
            cutoff=cutoff,
            maximum_bid_age=self._maximum_bid_age,
        )
        account_value = int(
            portfolio.account_value_micros(
                bids,
                as_of=cutoff,
                maximum_bid_age=self._maximum_bid_age,
            )
        )
        liquidation = account_value - int(portfolio.cash_micros)
        basis = sum(int(position.cost_basis_micros) for position in portfolio.positions)
        realized = self._realized_pnl(claim.agent_id)
        entry_fees = sum(int(position.entry_fees_micros) for position in portfolio.positions)
        unrealized = liquidation - basis - entry_fees
        mismatch = self._ledger_mismatch(claim.agent_id)
        calculated = _aware(self._clock())
        self._persist_performance(
            claim,
            cash=int(portfolio.cash_micros),
            liquidation=liquidation,
            account_value=account_value,
            realized=realized,
            unrealized=unrealized,
            entry_fees=entry_fees,
            calculated=calculated,
            settlement_ids=settled_ids,
            bid_ids=valuation_book_ids,
        )
        peak = self._peak_account_value(claim.agent_id, account_value)
        return SettlementValuationResult(
            {
                "settlement_ids": settled_ids,
                "performance_snapshot_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:performance:{claim.cycle_id}")
                ),
                "valuation_cutoff": cutoff.isoformat(),
            },
            (),
            account_value,
            peak,
            mismatch,
        )

    def _settle_eligible(
        self,
        agent_id: uuid.UUID,
        frozen: Mapping[str, object],
        cutoff: datetime,
    ) -> list[str]:
        settled_ids: list[str] = []
        for row in self._settlement_candidates(agent_id, frozen, cutoff):
            portfolio = self._state.portfolio(agent_id)
            outcome_id = str(row[2])
            position = portfolio.position(outcome_id)
            if position is None:
                continue
            observation = SettlementObservation(
                str(row[0]),
                str(row[1]),
                str(row[3]) if row[3] is not None else None,
                cast(datetime, row[4]),
                cast(datetime, row[5]),
                cast(datetime, row[6]),
            )
            result = SettlementEngine().settle(
                resolution=observation,
                position=position,
                portfolio=portfolio,
                as_of=cutoff,
                settled_at=_aware(self._clock()),
            )
            persisted = self._repository.persist_settlement(
                result,
                agent_id=agent_id,
                position_id=uuid.UUID(str(row[7])),
                resolution_id=uuid.UUID(str(row[0])),
                market_id=uuid.UUID(str(row[1])),
                outcome_id=uuid.UUID(str(row[2])),
            )
            settled_ids.append(str(persisted.record_id))
        return settled_ids

    def _settlement_candidates(
        self,
        agent_id: uuid.UUID,
        frozen: Mapping[str, object],
        cutoff: datetime,
    ) -> Sequence[Sequence[object]]:
        resolution_ids = _uuids(frozen, "resolution_ids")
        if not resolution_ids:
            return ()
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.id, r.market_id, p.outcome_id, r.winning_outcome_id, "
                "r.source_created_at, r.observed_at, r.eligible_after, p.id "
                "FROM resolutions r JOIN outcomes o ON o.market_id = r.market_id "
                "JOIN positions p ON p.outcome_id = o.id "
                "LEFT JOIN settlements s ON s.position_id = p.id AND s.resolution_id = r.id "
                "WHERE p.agent_id = %s AND p.shares > 0 AND s.id IS NULL "
                "AND r.id = ANY(%s::uuid[]) AND r.observed_at <= %s "
                "AND r.source_created_at <= %s AND r.eligible_after <= %s "
                "ORDER BY r.observed_at, r.id, p.id",
                (agent_id, list(resolution_ids), cutoff, cutoff, cutoff),
            )
            return tuple(cursor.fetchall())

    def _realized_pnl(self, agent_id: uuid.UUID) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(sum(realized_pnl_micros), 0) FROM positions WHERE agent_id = %s",
                (agent_id,),
            )
            row = cursor.fetchone()
        return int(str(row[0])) if row else 0

    def _ledger_mismatch(self, agent_id: uuid.UUID) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "WITH ledger AS (SELECT lp.outcome_id, sum(lp.amount_micros) AS basis "
                "FROM ledger_postings lp JOIN ledger_entries le ON le.id = lp.ledger_entry_id "
                "WHERE le.agent_id = %s AND lp.account = 'position_cost' "
                "GROUP BY lp.outcome_id), cached AS (SELECT outcome_id, cost_basis_micros "
                "FROM positions WHERE agent_id = %s) "
                "SELECT COALESCE(sum(abs(COALESCE(ledger.basis, 0) - "
                "COALESCE(cached.cost_basis_micros, 0))), 0) FROM ledger FULL JOIN cached "
                "USING (outcome_id)",
                (agent_id, agent_id),
            )
            row = cursor.fetchone()
        return int(str(row[0])) if row else 0

    def _persist_performance(
        self,
        claim: CycleClaim,
        *,
        cash: int,
        liquidation: int,
        account_value: int,
        realized: int,
        unrealized: int,
        entry_fees: int,
        calculated: datetime,
        settlement_ids: Sequence[str],
        bid_ids: Sequence[uuid.UUID],
    ) -> None:
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:performance:{claim.cycle_id}")
        valuation_age_seconds = int(self._maximum_bid_age.total_seconds())
        calculation = {
            "valuation_policy": (
                "latest_archived_executable_bid_max_age_"
                f"{valuation_age_seconds}_seconds"
            ),
            "valuation_max_age_seconds": valuation_age_seconds,
            "valuation_cutoff": _cutoff(claim).isoformat(),
            "entry_fees_micros": entry_fees,
            "settlement_ids": list(settlement_ids),
            "eligible_order_book_snapshot_ids": [str(value) for value in bid_ids],
        }
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO performance_snapshots "
                "(id, agent_cycle_id, cash_micros, position_liquidation_micros, "
                "account_value_micros, realized_pnl_micros, unrealized_pnl_micros, "
                "calculated_at, calculation) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (agent_cycle_id) DO NOTHING",
                (
                    identifier,
                    claim.cycle_id,
                    cash,
                    liquidation,
                    account_value,
                    realized,
                    unrealized,
                    calculated,
                    json.dumps(calculation, sort_keys=True),
                ),
            )

    def _peak_account_value(self, agent_id: uuid.UUID, current: int) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(max(ps.account_value_micros), %s) "
                "FROM performance_snapshots ps JOIN agent_cycles ac "
                "ON ac.id = ps.agent_cycle_id WHERE ac.agent_id = %s",
                (current, agent_id),
            )
            row = cursor.fetchone()
        return max(current, int(str(row[0])) if row else current)


@dataclass(frozen=True, slots=True)
class ProductionWorker:
    runtime: HourlyRuntime
    retention: RetentionCleaner
    projection: ProjectionService
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    sleeper: Callable[[float], None]

    def run_once(self) -> RuntimeTickResult:
        result = self.runtime.tick()
        self.retention.run_once()
        return result

    def run_forever(
        self,
        *,
        poll_seconds: float = 30.0,
        projection_seconds: float = 3_600.0,
    ) -> None:
        if poll_seconds <= 0 or projection_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        last_maintenance = self.monotonic() - projection_seconds
        while True:
            self.runtime.tick()
            now = self.monotonic()
            if now - last_maintenance >= projection_seconds:
                self.retention.run_once()
                self.projection.calculate()
                last_maintenance = now
            self.sleeper(poll_seconds)


def build_production_worker(
    config: ExperimentConfig,
    *,
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProductionWorker:
    config.assert_runnable()
    _verify_frozen_artifact(config.raw, "tool_schemas")
    try:
        values = (
            dict(environment)
            if environment is not None
            else required_environment(
                (
                    "VTRADE_DATABASE_URL",
                    "VTRADE_SUPABASE_URL",
                    "VTRADE_SUPABASE_BUCKET",
                    "VTRADE_SUPABASE_SERVICE_ROLE_KEY",
                    "VTRADE_OPENROUTER_API_KEY",
                    "VTRADE_EXA_API_KEY",
                )
            )
        )
    except ConfigurationError as exc:
        raise ProductionCompositionUnavailable(str(exc)) from exc
    missing = [
        name
        for name in (
            "VTRADE_DATABASE_URL",
            "VTRADE_SUPABASE_URL",
            "VTRADE_SUPABASE_BUCKET",
            "VTRADE_SUPABASE_SERVICE_ROLE_KEY",
            "VTRADE_OPENROUTER_API_KEY",
            "VTRADE_EXA_API_KEY",
        )
        if not values.get(name) or values.get(name) == "REQUIRED"
    ]
    if missing:
        raise ProductionCompositionUnavailable(
            f"missing required production resources: {', '.join(missing)}"
        )
    database_url = values["VTRADE_DATABASE_URL"]
    store = SupabaseArtifactStore(
        values["VTRADE_SUPABASE_URL"],
        values["VTRADE_SUPABASE_BUCKET"],
        values["VTRADE_SUPABASE_SERVICE_ROLE_KEY"],
    )
    limits = _harness_limits(config.raw)
    maximum_beliefs_per_agent = _positive_integer(config.raw["limits"], "maximum_beliefs_per_agent")
    budget = PostgresBudgetGuard(
        database_url,
        limit_micros=_integer(config.raw["limits"], "monthly_external_api_budget_micros"),
        thresholds=cast(
            tuple[int, int, int],
            tuple(int(value) for value in config.raw["limits"]["budget_alert_micros"]),
        ),
        clock=clock,
    )
    gateway = OpenRouterModelGateway(
        values["VTRADE_OPENROUTER_API_KEY"], store, budget, clock=clock
    )
    exa = ExaResearchProvider(values["VTRADE_EXA_API_KEY"], store, budget, clock=clock)
    source_skew = float(config.raw["limits"]["maximum_source_clock_skew_seconds"])
    venue = PolymarketVenue(
        store,
        clock=clock,
        maximum_source_clock_skew_seconds=source_skew,
    )
    market_repository = PostgresMarketDataRepository(database_url)
    repository = PostgresRuntimeRepository(database_url)
    maximum_valuation_bid_age = timedelta(
        seconds=_integer(config.raw["limits"], "maximum_archived_bid_age_seconds")
    )
    maximum_order_book_age = _maximum_order_book_age(config.raw)
    maximum_order_book_depth = _order_book_depth(config.raw)
    ignored_best_levels = _ignored_best_levels(config.raw)
    maximum_ignored_depth_fraction = _maximum_ignored_depth_fraction(config.raw)
    paper_policy = _paper_policy(config.raw)
    live_context_provider: LiveOrderContextProvider | None = None
    if paper_policy is PaperPolicy.LIQUIDITY_AWARE:
        live_context_provider = PostgresLiveOrderContextProvider(
            market_repository,
            venue,
            clock=clock,
            monotonic=monotonic,
            maximum_book_age=maximum_order_book_age,
            maximum_source_skew=timedelta(seconds=min(source_skew, 5.0)),
        )
    settlement_valuation = ProductionSettlementValuationPort(
        database_url,
        clock=clock,
        maximum_bid_age=maximum_valuation_bid_age,
    )
    immediate_order_executor = MarketOrderExecutor(
        _PostgresTradingState(database_url),
        market_repository,
        PostgresBrokerRepository(database_url),
        broker=PredictionArenaPaperBroker(
            policy=paper_policy,
            maximum_market_cost_basis_fraction=Decimal(
                str(config.raw["limits"]["maximum_market_cost_basis_fraction"])
            ),
            maximum_book_age=maximum_order_book_age,
            maximum_book_depth=maximum_order_book_depth,
            ignored_best_levels=ignored_best_levels,
            maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
            liquidity_rule_version=LIQUIDITY_HAIRCUT_RULE_VERSION,
            maximum_valuation_bid_age=maximum_valuation_bid_age,
        ),
        clock=clock,
        live_context_provider=live_context_provider,
        maximum_live_observation_age=maximum_order_book_age,
    )
    orchestrator = CycleOrchestrator(
        repository=repository,
        market_freezer=PolymarketFreezeService(
            venue,
            market_repository,
            clock=clock,
        ),
        pre_settlement=settlement_valuation,
        prompt=ProductionPromptPort(
            database_url,
            store,
            clock=clock,
            maximum_market_cost_basis_fraction=Decimal(
                str(config.raw["limits"]["maximum_market_cost_basis_fraction"])
            ),
            maximum_valuation_bid_age=maximum_valuation_bid_age,
        ),
        harness=ProductionHarnessPort(
            database_url,
            store,
            gateway,
            exa,
            limits,
            clock=clock,
            monotonic=monotonic,
            schema_path=str(config.raw["artifacts"]["tool_schemas"]["path"]),
            maximum_beliefs_per_agent=maximum_beliefs_per_agent,
            maximum_book_age=maximum_order_book_age,
            maximum_order_book_depth=maximum_order_book_depth,
            immediate_order_executor=immediate_order_executor,
            require_live_order_execution=paper_policy is PaperPolicy.LIQUIDITY_AWARE,
        ),
        broker=ProductionBrokerPort(
            database_url,
            market_repository,
            clock=clock,
            maximum_market_fraction=Decimal(
                str(config.raw["limits"]["maximum_market_cost_basis_fraction"])
            ),
            maximum_bid_age=maximum_order_book_age,
            maximum_valuation_bid_age=maximum_valuation_bid_age,
            maximum_book_depth=maximum_order_book_depth,
            ignored_best_levels=ignored_best_levels,
            maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
            liquidity_rule_version=LIQUIDITY_HAIRCUT_RULE_VERSION,
            paper_policy=paper_policy,
            liquidity_time_in_force=_liquidity_time_in_force(config.raw),
            live_context_provider=live_context_provider,
        ),
        settlement_valuation=settlement_valuation,
        clock=clock,
        alert_policy=RuntimeAlertPolicy(
            maximum_data_age=maximum_valuation_bid_age,
            monthly_budget_micros=_integer(
                config.raw["limits"], "monthly_external_api_budget_micros"
            ),
        ),
    )
    lease_owner = values.get("VTRADE_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
    runtime = HourlyRuntime(
        repository=repository,
        orchestrator=orchestrator,
        lease_owner=lease_owner,
        clock=clock,
        batch_size=1,
    )
    return ProductionWorker(
        runtime,
        RetentionCleaner(
            repository=repository,
            deletion=store,
            lease_owner=f"{lease_owner}:retention",
            clock=clock,
        ),
        ProjectionService(repository=repository, clock=clock),
        clock,
        monotonic,
        sleeper,
    )


def run_worker(
    config_path: str | Path,
    *,
    worker: ProductionWorker | None = None,
    environment: Mapping[str, str] | None = None,
    forever: bool = False,
) -> RuntimeTickResult | None:
    config = load_experiment_config(config_path)
    config.assert_runnable()
    application = worker or build_production_worker(config, environment=environment)
    if forever:
        application.run_forever(
            poll_seconds=float(os.getenv("VTRADE_WORKER_POLL_SECONDS", "30")),
            projection_seconds=float(os.getenv("VTRADE_WORKER_PROJECTION_SECONDS", "3600")),
        )
        return None
    return application.run_once()


def main() -> None:
    config_path = os.getenv(
        "VTRADE_EXPERIMENT_CONFIG",
        "config/experiments/predictionarena-polymarket-v1-liquidity-aware.json",
    )
    try:
        run_worker(config_path, forever=True)
    except KeyboardInterrupt:
        return


def _harness_limits(raw: Mapping[str, Any]) -> HarnessLimits:
    limits = raw.get("limits")
    if not isinstance(limits, Mapping):
        raise ProductionCompositionUnavailable("experiment limits are missing")
    return HarnessLimits(
        _integer(limits, "maximum_model_turns"),
        _integer(limits, "maximum_total_tool_calls"),
        _integer(limits, "maximum_web_searches_per_cycle"),
        float(limits["maximum_cycle_wall_clock_seconds"]),
        _integer(limits, "maximum_model_context_tokens"),
        _integer(limits, "maximum_assembled_input_tokens"),
        _integer(limits, "reserved_model_output_tokens"),
        _integer(limits, "maximum_tool_call_argument_tokens"),
        _integer(limits, "default_maximum_tool_result_tokens"),
        _integer(limits, "get_portfolio_maximum_tool_result_tokens"),
    )


def _paper_policy(raw: Mapping[str, Any]) -> PaperPolicy:
    execution = raw.get("execution")
    if not isinstance(execution, Mapping):
        raise ProductionCompositionUnavailable("experiment execution configuration is missing")
    value = execution.get("paper_policy")
    try:
        return PaperPolicy(str(value))
    except ValueError as exc:
        raise ProductionCompositionUnavailable(f"unsupported paper policy: {value}") from exc


def _liquidity_time_in_force(raw: Mapping[str, Any]) -> LiquidityTimeInForce:
    execution = raw.get("execution")
    if not isinstance(execution, Mapping):
        raise ProductionCompositionUnavailable("experiment execution configuration is missing")
    value = execution.get("liquidity_time_in_force", "IOC")
    try:
        return LiquidityTimeInForce(str(value))
    except ValueError as exc:
        raise ProductionCompositionUnavailable(
            f"unsupported liquidity time in force: {value}"
        ) from exc


def _maximum_order_book_age(raw: Mapping[str, Any]) -> timedelta:
    execution = raw.get("execution")
    limits = raw.get("limits")
    if not isinstance(execution, Mapping) or not isinstance(limits, Mapping):
        raise ProductionCompositionUnavailable(
            "experiment execution and limits configuration are missing"
        )
    value = execution.get(
        "maximum_order_book_age_seconds",
        limits.get("maximum_archived_bid_age_seconds"),
    )
    return timedelta(
        seconds=_positive_integer(
            {"maximum_order_book_age_seconds": value},
            "maximum_order_book_age_seconds",
        )
    )


def _order_book_depth(raw: Mapping[str, Any]) -> int:
    execution = raw.get("execution")
    if not isinstance(execution, Mapping):
        raise ProductionCompositionUnavailable("experiment execution configuration is missing")
    return _positive_integer(
        {"order_book_depth": execution.get("order_book_depth", 5)},
        "order_book_depth",
    )


def _liquidity_context_version(
    cycle_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    rule_version: str,
    maximum_book_depth: int,
    ignored_best_levels: int,
    maximum_ignored_depth_fraction: Decimal,
) -> str:
    """Make the simulator rule part of the persisted private context identity."""

    return (
        f"{rule_version}:cycle:{cycle_id}:snapshot:{snapshot_id}:"
        f"raw-depth:{maximum_book_depth + ignored_best_levels}:"
        f"effective-depth:{maximum_book_depth}:ignored-levels:{ignored_best_levels}:"
        f"ignored-fraction:{maximum_ignored_depth_fraction}"
    )


def _ignored_best_levels(raw: Mapping[str, Any]) -> int:
    execution = raw.get("execution")
    if not isinstance(execution, Mapping):
        raise ProductionCompositionUnavailable("experiment execution configuration is missing")
    value = execution.get("ignored_best_levels", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProductionCompositionUnavailable(
            "configuration field ignored_best_levels must be a non-negative integer"
        )
    return value


def _maximum_ignored_depth_fraction(raw: Mapping[str, Any]) -> Decimal:
    execution = raw.get("execution")
    if not isinstance(execution, Mapping):
        raise ProductionCompositionUnavailable("experiment execution configuration is missing")
    value = execution.get("maximum_ignored_depth_fraction", 0)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProductionCompositionUnavailable(
            "configuration field maximum_ignored_depth_fraction must be numeric"
        ) from exc
    if not result.is_finite() or not Decimal(0) <= result <= Decimal(1):
        raise ProductionCompositionUnavailable(
            "configuration field maximum_ignored_depth_fraction must be between zero and one"
        )
    return result


def _verify_frozen_artifact(raw: Mapping[str, object], name: str) -> None:
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get(name), Mapping):
        raise ProductionCompositionUnavailable(f"frozen artifact {name} is missing")
    definition = cast(Mapping[str, object], artifacts[name])
    path = definition.get("path")
    expected = definition.get("sha256")
    if not isinstance(path, str) or not isinstance(expected, str) or len(expected) != 64:
        raise ProductionCompositionUnavailable(f"frozen artifact {name} is malformed")
    try:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ProductionCompositionUnavailable(f"cannot read frozen artifact {name}") from exc
    if actual != expected:
        raise ProductionCompositionUnavailable(f"frozen artifact {name} hash mismatch")


def _integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ProductionCompositionUnavailable(f"configuration field {key} must be integer")
    return result


def _positive_integer(value: Mapping[str, object], key: str) -> int:
    result = _integer(value, key)
    if result <= 0:
        raise ProductionCompositionUnavailable(f"configuration field {key} must be positive")
    return result


def _cutoff(claim: CycleClaim) -> datetime:
    if claim.data_cutoff is None:
        raise ProductionCompositionUnavailable("cycle cutoff is not finalized")
    return _aware(claim.data_cutoff)


def _canonical_rejection_code(value: object) -> str:
    code = "" if value is None else str(value).strip()
    return code or "unknown"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _strings(value: Mapping[str, object], key: str) -> list[str]:
    rows = value.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
        raise ProductionCompositionUnavailable(f"stage payload lacks string list {key}")
    return cast(list[str], rows)


def _uuids(value: Mapping[str, object], key: str) -> tuple[uuid.UUID, ...]:
    try:
        rows = tuple(uuid.UUID(item) for item in _strings(value, key))
    except ValueError as exc:
        raise ProductionCompositionUnavailable(f"stage payload has malformed {key}") from exc
    if len(set(rows)) != len(rows):
        raise ProductionCompositionUnavailable(f"stage payload has duplicate {key}")
    return rows


def _mapping(value: object) -> dict[str, Any]:
    return {str(key): child for key, child in value.items()} if isinstance(value, Mapping) else {}


def _portfolio_payload(portfolio: PortfolioState) -> dict[str, object]:
    return {
        "agent_id": portfolio.agent_id,
        "cash_micros": int(portfolio.cash_micros),
        "version": portfolio.version,
        "positions": [
            {
                "market_id": position.market_id,
                "outcome_id": position.outcome_id,
                "shares": str(position.shares),
                "average_cost": str(position.average_cost),
                "cost_basis_micros": int(position.cost_basis_micros),
                "realized_pnl_micros": int(position.realized_pnl_micros),
                "entry_fees_micros": int(position.entry_fees_micros),
            }
            for position in portfolio.positions
        ],
        "pending_orders": [
            {
                "id": pending.id,
                "market_id": pending.market_id,
                "outcome_id": pending.outcome_id,
                "side": pending.side.value,
                "reserved_cash_micros": int(pending.reserved_cash_micros),
                "reserved_shares": str(pending.reserved_shares),
                "reserved_cost_basis_micros": int(pending.reserved_cost_basis_micros),
            }
            for pending in portfolio.pending_orders
        ],
    }


def _portfolio_from_payload(value: object) -> PortfolioState:
    raw = json.loads(value) if isinstance(value, str) else value
    payload = _mapping(raw)
    positions_value = payload.get("positions", [])
    pending_value = payload.get("pending_orders", [])
    if not isinstance(positions_value, list) or not isinstance(pending_value, list):
        raise ProductionCompositionUnavailable("virtual-liquidity portfolio payload is malformed")
    try:
        positions = tuple(
            PositionState(
                str(_mapping(row)["market_id"]),
                str(_mapping(row)["outcome_id"]),
                Decimal(str(_mapping(row)["shares"])),
                Decimal(str(_mapping(row)["average_cost"])),
                MicroDollars(int(str(_mapping(row)["cost_basis_micros"]))),
                MicroDollars(int(str(_mapping(row).get("realized_pnl_micros", 0)))),
                MicroDollars(int(str(_mapping(row).get("entry_fees_micros", 0)))),
            )
            for row in positions_value
        )
        pending_orders = tuple(
            PendingOrder(
                str(_mapping(row)["id"]),
                str(_mapping(row)["market_id"]),
                str(_mapping(row)["outcome_id"]),
                Side(str(_mapping(row)["side"])),
                MicroDollars(int(str(_mapping(row).get("reserved_cash_micros", 0)))),
                Decimal(str(_mapping(row).get("reserved_shares", 0))),
                MicroDollars(int(str(_mapping(row).get("reserved_cost_basis_micros", 0)))),
            )
            for row in pending_value
        )
        return PortfolioState(
            str(payload["agent_id"]),
            MicroDollars(int(str(payload["cash_micros"]))),
            positions=positions,
            pending_orders=pending_orders,
            version=int(str(payload["version"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionCompositionUnavailable(
            "virtual-liquidity portfolio payload is malformed"
        ) from exc


def _virtual_liquidity_metrics_from_rows(
    execution_row: Sequence[object],
    level_rows: Sequence[Sequence[object]],
) -> VirtualLiquidityMetrics:
    return VirtualLiquidityMetrics(
        context_version=str(execution_row[6]),
        agent_id=str(execution_row[1]),
        agent_cycle_id=str(execution_row[2]),
        snapshot_id=str(execution_row[3]),
        token_id=str(execution_row[4]),
        side=Side(str(execution_row[5])),
        requested_shares=Decimal(str(execution_row[7])),
        available_shares=Decimal(str(execution_row[8])),
        consumed_shares=Decimal(str(execution_row[9])),
        cancelled_shares=Decimal(str(execution_row[10])),
        remaining_shares=Decimal(str(execution_row[11])),
        rule_version=str(execution_row[14]),
        ignored_best_levels=int(str(execution_row[15])),
        maximum_ignored_depth_fraction=Decimal(str(execution_row[16])),
        raw_depth_shares=Decimal(str(execution_row[17])),
        best_level_fraction=Decimal(str(execution_row[18])),
        ignored_depth_shares=Decimal(str(execution_row[19])),
        ignored_fraction=Decimal(str(execution_row[20])),
        effective_depth_shares=Decimal(str(execution_row[21])),
        best_level_price=(
            None if execution_row[22] is None else Decimal(str(execution_row[22]))
        ),
        levels=tuple(
            VirtualLiquidityLevelMetrics(
                level_index=int(str(row[0])),
                price=Decimal(str(row[1])),
                displayed_shares=Decimal(str(row[2])),
                ignored_shares=Decimal(str(row[3])),
                effective_shares=Decimal(str(row[4])),
                available_shares=Decimal(str(row[5])),
                consumed_shares=Decimal(str(row[6])),
                cancelled_shares=Decimal(str(row[7])),
                remaining_shares=Decimal(str(row[8])),
                executable=bool(row[9]),
            )
            for row in level_rows
        ),
    )


def _levels(value: object) -> tuple[PriceLevel, ...]:
    if not isinstance(value, list):
        raise ProductionCompositionUnavailable("frozen order-book levels are malformed")
    levels: list[PriceLevel] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ProductionCompositionUnavailable("frozen order-book level is malformed")
        levels.append(PriceLevel(Decimal(str(row.get("price"))), Decimal(str(row.get("size")))))
    return tuple(levels)


def _required_payload_string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProductionCompositionUnavailable(f"market snapshot lacks {key}")
    return result


def _optional_payload_timestamp(value: object) -> datetime | None:
    return _parse_timestamp(value) if value is not None else None


def _optional_research_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _parse_timestamp(value)
    except ProductionCompositionUnavailable:
        return None


def _plan(row: Mapping[str, object], agent_id: uuid.UUID) -> PlanRecord:
    due = row.get("due_at")
    return PlanRecord(
        str(row["id"]),
        str(agent_id),
        PlanType(str(row["plan_type"])),
        str(row["content"]),
        _parse_timestamp(due) if due is not None else None,
        _parse_timestamp(row["created_at"]),
    )


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as exc:
        raise ProductionCompositionUnavailable("persisted timestamp is malformed") from exc


def _registration(reference: Any, retained: datetime) -> ArtifactRegistration:
    return ArtifactRegistration(
        str(reference.uri),
        str(reference.sha256),
        int(reference.byte_length),
        retained,
    )


def _deduplicated_registrations(
    registrations: Sequence[ArtifactRegistration],
) -> tuple[ArtifactRegistration, ...]:
    unique: dict[tuple[str, str], ArtifactRegistration] = {}
    for registration in registrations:
        unique[(registration.uri, registration.sha256)] = registration
    return tuple(unique.values())


def _harness_artifact_registrations(
    transcript: Any,
    telemetry: Sequence[ProviderTelemetry],
    retained: datetime,
) -> tuple[ArtifactRegistration, ...]:
    return _deduplicated_registrations(
        (
            _registration(transcript, retained),
            *(
                ArtifactRegistration(
                    row.artifact_uri,
                    row.raw_sha256,
                    row.artifact_byte_length,
                    retained,
                )
                for row in telemetry
            ),
        )
    )


if __name__ == "__main__":
    main()
