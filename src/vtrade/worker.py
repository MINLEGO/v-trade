"""Production worker composition for the Kalshi-only paper release."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from vtrade.artifacts import SupabaseArtifactStore
from vtrade.config import (
    ConfigurationError,
    ExperimentConfig,
    load_experiment_config,
    required_environment,
)
from vtrade.deadline import check_deadline, deadline_remaining, run_with_deadline
from vtrade.domain.execution import OrderRequest
from vtrade.domain.ports import ArtifactStore, JsonObject
from vtrade.domain.types import Side
from vtrade.frozen_artifacts import FrozenArtifactError, canonical_artifact_file_sha256
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
from vtrade.kalshi import KalshiPublicRestAdapter
from vtrade.kalshi_freeze import KalshiFreezeRequest, KalshiMarketFreezeService
from vtrade.kalshi_persistence import PostgresKalshiFreezeRepository
from vtrade.market_metrics import format_metric_decimal
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
    CycleClaim,
    CycleOrchestrator,
    HarnessExecutionResult,
    HourlyRuntime,
    MarketFreezeResult,
    ProjectionService,
    PromptResult,
    RetentionCleaner,
    RuntimeAlertPolicy,
    RuntimeTickResult,
    six_month_retain_until,
)
from vtrade.semantic_runtime import (
    ProductionSemanticBrokerPort,
    ProductionSemanticOrderExecutor,
    ProductionSemanticSettlementPort,
)

_LOGGER = logging.getLogger(__name__)
_RECOVERY_WITHOUT_HARNESS_RUN_ERROR = (
    "ProductionCompositionUnavailable: recovery found no completed persisted harness run; "
    "provider replay is forbidden"
)
_PRE_PROVIDER_FAILURE_PREFIX = "ProviderConfigurationError:"


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


class _ImmediateSemanticExecutor(Protocol):
    def submit_and_execute(
        self, claim: CycleClaim, frozen: JsonObject, request: OrderRequest
    ) -> Any: ...


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
    market_ref: str
    outcome: str
    question: str
    closes_at: datetime | None
    contract_units: int
    gross_cost_basis_micros: int
    entry_fees_micros: int
    realized_pnl_micros: int
    updated_at: datetime
    bid_price_micros: int | None
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
                "SELECT 'settlement', m.market_ref, s.outcome_side, "
                "s.realized_pnl_micros, s.settled_at, '', s.id::text "
                "FROM settlements s JOIN markets m ON m.id = s.market_id "
                "WHERE s.agent_id = %s AND s.settled_at > %s AND s.settled_at <= %s "
                "ORDER BY s.settled_at DESC, s.id DESC LIMIT %s",
                (agent_id, delta_oldest, cutoff, self._RECENT_ACTIVITY_LIMIT + 1),
            )
            rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT 'rejection', m.market_ref, oo.outcome_side, "
                "0, oo.created_at, "
                "COALESCE(NULLIF(BTRIM(lifecycle.reason), ''), 'unknown'), oo.id::text "
                "FROM order_operations oo JOIN markets m ON m.id = oo.market_id "
                "JOIN order_operation_current current_state "
                "ON current_state.operation_id = oo.id "
                "LEFT JOIN LATERAL (SELECT reason FROM order_lifecycle_events event "
                "WHERE event.operation_id = oo.id AND event.state = 'REJECTED' "
                "ORDER BY event.sequence_number DESC, event.id DESC LIMIT 1) lifecycle ON true "
                "WHERE oo.agent_id = %s AND current_state.state = 'REJECTED' "
                "AND oo.created_at > %s AND oo.created_at <= %s "
                "ORDER BY oo.created_at DESC, oo.id DESC LIMIT %s",
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
                "SELECT COALESCE(NULLIF(BTRIM(lifecycle.reason), ''), 'unknown'), count(*) "
                "FROM order_operations oo JOIN order_operation_current current_state "
                "ON current_state.operation_id = oo.id "
                "LEFT JOIN LATERAL (SELECT reason FROM order_lifecycle_events event "
                "WHERE event.operation_id = oo.id AND event.state = 'REJECTED' "
                "ORDER BY event.sequence_number DESC, event.id DESC LIMIT 1) lifecycle ON true "
                "WHERE oo.agent_id = %s AND current_state.state = 'REJECTED' "
                "AND oo.created_at > %s AND oo.created_at <= %s "
                "GROUP BY COALESCE(NULLIF(BTRIM(lifecycle.reason), ''), 'unknown') "
                "ORDER BY COALESCE(NULLIF(BTRIM(lifecycle.reason), ''), 'unknown')",
                (agent_id, summary_oldest, cutoff),
            )
            rejection_summary_rows = cursor.fetchall()
        events = tuple(
            RecentActivityEvent(
                kind=str(row[0]),
                market_ref=str(row[1]),
                outcome=str(row[2]) if row[2] is not None else None,
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
                    item.market_ref,
                    item.outcome or "",
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
            "market_ref": event.market_ref,
            "outcome": event.outcome,
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
        pending_orders: Sequence[object] = (),
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
                "SELECT m.market_ref, p.outcome_side, m.question, m.close_time, "
                "p.contract_units, p.gross_cost_basis_micros, p.entry_fees_micros, "
                "p.realized_pnl_micros, p.updated_at, book.price_micros, book.cutoff "
                "FROM positions p JOIN markets m ON m.id = p.market_id "
                "LEFT JOIN LATERAL ("
                "SELECT obl.price_micros, obs.cutoff FROM order_book_snapshots obs "
                "JOIN order_book_levels obl ON obl.snapshot_id = obs.id "
                "WHERE obs.market_id = p.market_id AND obl.outcome_side = p.outcome_side "
                "AND obl.book_side = 'bid' AND obs.id = ANY(%s::uuid[]) "
                "AND obs.cutoff <= %s AND obs.cutoff >= %s "
                "AND (obs.source_timestamp IS NULL OR obs.source_timestamp <= %s) "
                "ORDER BY obs.cutoff DESC, obs.id DESC, obl.price_micros DESC LIMIT 1"
                ") book ON TRUE "
                "WHERE p.agent_id = %s AND p.contract_units > 0 "
                "ORDER BY m.market_ref, p.outcome_side",
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
            held_basis_by_market[position.market_ref] = (
                held_basis_by_market.get(position.market_ref, 0)
                + position.gross_cost_basis_micros
            )
            value = self._liquidation_value(position) if self._valid_bid(position) else None
            previous = liquidation_by_market.get(position.market_ref, 0)
            liquidation_by_market[position.market_ref] = (
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
            - sum(position.gross_cost_basis_micros for position in positions)
            - entry_fees_micros
            if total_liquidation is not None
            else None
        )
        pending_basis_by_market: dict[str, int] = {}
        for pending in pending_orders:
            pending_side = getattr(pending, "side", None)
            pending_market_ref = getattr(pending, "market_ref", None)
            reserved_basis = getattr(pending, "reserved_cost_basis_micros", None)
            if pending_side is Side.BUY and isinstance(pending_market_ref, str):
                pending_basis_by_market[pending_market_ref] = (
                    pending_basis_by_market.get(pending_market_ref, 0)
                    + int(reserved_basis or 0)
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
                    held_basis_by_market[position.market_ref]
                    + pending_basis_by_market.get(position.market_ref, 0)
                ),
                market_capacity=capacity_by_market[position.market_ref],
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
                    "market_ref": market_id,
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
        closes_at = _aware(cast(datetime, row[3])) if row[3] is not None else None
        bid = int(str(row[9])) if row[9] is not None else None
        bid_observed_at = _aware(cast(datetime, row[10])) if row[10] is not None else None
        return _PromptPosition(
            market_ref=str(row[0]),
            outcome=str(row[1]),
            question=str(row[2]),
            closes_at=closes_at,
            contract_units=int(str(row[4])),
            gross_cost_basis_micros=int(str(row[5])),
            entry_fees_micros=int(str(row[6])),
            realized_pnl_micros=int(str(row[7])),
            updated_at=_aware(cast(datetime, row[8])),
            bid_price_micros=bid,
            bid_observed_at=bid_observed_at,
        )

    def _valid_bid(self, position: _PromptPosition) -> bool:
        return (
            position.bid_price_micros is not None
            and 0 <= position.bid_price_micros <= 1_000_000
            and position.bid_observed_at is not None
        )

    @staticmethod
    def _liquidation_value(position: _PromptPosition) -> int:
        if position.bid_price_micros is None:
            raise ValueError("liquidation value requires a bid")
        numerator = position.contract_units * position.bid_price_micros
        return (numerator + 50) // 100

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
            liquidation - position.gross_cost_basis_micros - position.entry_fees_micros
            if liquidation is not None
            else None
        )
        hours_to_close = (
            round((position.closes_at - cutoff).total_seconds() / 3600, 2)
            if position.closes_at is not None
            else None
        )
        return {
            "market_ref": position.market_ref,
            "question": position.question,
            "outcome": position.outcome,
            "contract_units": position.contract_units,
            "gross_cost_basis_micros": position.gross_cost_basis_micros,
            "entry_fees_micros": position.entry_fees_micros,
            "bid_price_micros": position.bid_price_micros if valid else None,
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
            str(item["market_ref"]),
            str(item["outcome"]),
        )

    @staticmethod
    def _aggregate_positions(positions: Sequence[JsonObject]) -> JsonObject:
        liquidation_values = [item["liquidation_value_micros"] for item in positions]
        unrealized_values = [item["unrealized_pnl_micros"] for item in positions]
        return {
            "count": len(positions),
            "market_count": len({str(item["market_ref"]) for item in positions}),
            "gross_cost_basis_micros": sum(
                int(item["gross_cost_basis_micros"]) for item in positions
            ),
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
        immediate_order_executor: _ImmediateSemanticExecutor | None = None,
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
        if require_live_order_execution:
            raise ProductionCompositionUnavailable(
                "real order execution is disabled in the active Kalshi paper composition"
            )

    def run(
        self, claim: CycleClaim, frozen: JsonObject, prompt: JsonObject
    ) -> HarnessExecutionResult:
        del prompt
        # A recovered claim may have failed before the harness stage started. In
        # that case no provider execution needs replaying; only an existing
        # harness checkpoint requires rehydration from its persisted run.
        if claim.recovery and self._harness_stage_requires_persisted_run(claim):
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
            # The active release has no real execution adapter.  The callback
            # is the semantic paper executor and never exposes venue credentials.
            live_order_execution=False,
            live_order_required=False,
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
        operation_ids = self._cycle_operation_ids(claim.cycle_id)
        return HarnessExecutionResult(
            {
                "harness_run_id": str(run_id),
                "termination_status": result.termination_status,
                "operation_ids": [str(value) for value in operation_ids],
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
        operation_ids = self._cycle_operation_ids(claim.cycle_id)
        return HarnessExecutionResult(
            {
                "harness_run_id": str(run[0]),
                "termination_status": str(run[1]),
                "operation_ids": [str(value) for value in operation_ids],
                "transcript_sha256": str(run[5]),
                "recovered_from_persisted_run": True,
            },
            registrations,
            int(str(run[3])),
            int(str(run[2])),
        )

    def _harness_stage_requires_persisted_run(self, claim: CycleClaim) -> bool:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT error FROM runtime_cycle_steps WHERE agent_cycle_id = %s "
                "AND stage = %s",
                (claim.cycle_id, "harness"),
            )
            row = cursor.fetchone()
        if row is None:
            return False
        # This exact error is emitted before any provider call. It is the safe
        # marker left by the previous recovery implementation, so the stage may
        # be retried once without replaying an in-flight provider execution.
        error = str(row[0])
        return error != _RECOVERY_WITHOUT_HARNESS_RUN_ERROR and not error.startswith(
            _PRE_PROVIDER_FAILURE_PREFIX
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

    def _cycle_operation_ids(self, cycle_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM order_operations WHERE agent_cycle_id = %s ORDER BY created_at, id",
                (cycle_id,),
            )
            return tuple(uuid.UUID(str(row[0])) for row in cursor.fetchall())




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


class ProductionKalshiFreezePort:
    """Publish one immutable, semantic Kalshi freeze for a cycle.

    The adapter is deliberately read-only.  The port turns its typed result into
    the checkpoint JSON consumed by the prompt and tools, while the runtime
    repository records every content-addressed source reference before the
    checkpoint becomes visible.
    """

    def __init__(
        self,
        service: KalshiMarketFreezeService,
        repository: PostgresRuntimeRepository,
        persistence: PostgresKalshiFreezeRepository,
        *,
        clock: Callable[[], datetime],
        maximum_historical_markets: int = 20,
        maximum_additional_markets: int = 80,
    ) -> None:
        if maximum_historical_markets < 0 or maximum_additional_markets < 0:
            raise ValueError("Kalshi freeze retention limits cannot be negative")
        self._service = service
        self._repository = repository
        self._persistence = persistence
        self._clock = clock
        self._maximum_historical_markets = maximum_historical_markets
        self._maximum_additional_markets = maximum_additional_markets

    def freeze(self, claim: CycleClaim, *, deadline: float | None = None) -> MarketFreezeResult:
        if deadline is not None:
            held_markets = run_with_deadline(
                lambda: self._persistence.market_refs_for_agent(
                    claim.agent_id, deadline=deadline
                ),
                deadline=deadline,
                label="Kalshi held-market lookup",
            )
        else:
            held_markets = self._persistence.market_refs_for_agent(claim.agent_id)
        result = self._service.freeze(
            KalshiFreezeRequest(
                held_markets=held_markets,
                historical_markets=held_markets,
                cutoff=claim.data_cutoff,
                maximum_historical_markets=self._maximum_historical_markets,
                maximum_additional_markets=self._maximum_additional_markets,
            )
            if claim.data_cutoff
            else KalshiFreezeRequest(
                held_markets=held_markets,
                historical_markets=held_markets,
                maximum_historical_markets=self._maximum_historical_markets,
                maximum_additional_markets=self._maximum_additional_markets,
            ),
            deadline=deadline,
        )
        if deadline is not None:
            check_deadline(deadline, "after Kalshi venue freeze")
        retained = six_month_retain_until(_aware(self._clock()))
        published_at = _aware(self._clock())
        raw_persistence_started = time.monotonic()
        if deadline is not None:
            artifact_ids = run_with_deadline(
                lambda: self._repository.persist_raw_artifacts(
                    result.artifacts,
                    claim=claim,
                    now=published_at,
                    deadline=deadline,
                ),
                deadline=deadline,
                label="Kalshi raw artifact persistence",
            )
        else:
            artifact_ids = self._repository.persist_raw_artifacts(result.artifacts)
        _LOGGER.info(
            "market_freeze stage_boundary event=raw_artifacts_persisted cycle_id=%s "
            "attempt=%s artifact_count=%s elapsed_ms=%.3f deadline_remaining_ms=%.3f",
            claim.cycle_id,
            claim.attempt,
            len(result.artifacts),
            (time.monotonic() - raw_persistence_started) * 1000,
            deadline_remaining(deadline) * 1000 if deadline is not None else -1.0,
        )
        registrations = tuple(
            ArtifactRegistration(artifact.uri, artifact.sha256, artifact.byte_length, retained)
            for artifact in result.artifacts
        )
        freeze_persistence_started = time.monotonic()
        if deadline is not None:
            persisted = run_with_deadline(
                lambda: self._persistence.persist(
                    result,
                    agent_cycle_id=claim.cycle_id,
                    raw_artifact_ids=artifact_ids,
                    published_at=published_at,
                    lease_owner=claim.lease_owner,
                    deadline=deadline,
                ),
                deadline=deadline,
                label="Kalshi freeze PostgreSQL persistence",
            )
            check_deadline(deadline, "after Kalshi PostgreSQL persistence")
        else:
            persisted = self._persistence.persist(
                result,
                agent_cycle_id=claim.cycle_id,
                raw_artifact_ids=artifact_ids,
                published_at=published_at,
            )
        _LOGGER.info(
            "market_freeze stage_boundary event=postgres_persisted cycle_id=%s "
            "attempt=%s elapsed_ms=%.3f deadline_remaining_ms=%.3f",
            claim.cycle_id,
            claim.attempt,
            (time.monotonic() - freeze_persistence_started) * 1000,
            deadline_remaining(deadline) * 1000 if deadline is not None else -1.0,
        )
        _LOGGER.info(
            "market_freeze stage_boundary event=publication_ready cycle_id=%s "
            "attempt=%s artifact_count=%s artifact_bytes=%s deadline_remaining_ms=%.3f",
            claim.cycle_id,
            claim.attempt,
            len(result.artifacts),
            sum(artifact.byte_length for artifact in result.artifacts),
            deadline_remaining(deadline) * 1000 if deadline is not None else -1.0,
        )
        payload = _kalshi_freeze_payload(
            result,
            cycle_id=claim.cycle_id,
            market_snapshot_ids=persisted.market_snapshot_ids,
            order_book_snapshot_ids=persisted.order_book_snapshot_ids,
            resolution_ids=persisted.resolution_ids,
        )
        observed = [artifact.observed_at for artifact in result.artifacts if artifact.observed_at]
        freshest = max(observed, default=result.data_cutoff)
        return MarketFreezeResult(
            payload,
            tuple(registrations),
            _aware(freshest),
        )


def _kalshi_freeze_payload(
    result: Any,
    *,
    cycle_id: uuid.UUID,
    market_snapshot_ids: Sequence[uuid.UUID] | None = None,
    order_book_snapshot_ids: Sequence[uuid.UUID] | None = None,
    resolution_ids: Sequence[uuid.UUID] | None = None,
) -> JsonObject:
    def timestamp(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def level(value: Any) -> JsonObject:
        return {"price_micros": int(value.price), "contract_units": int(value.quantity)}

    metrics_by_key = {
        item.market_key: item for item in getattr(result, "market_metrics", ())
    }
    series_by_key = {
        item.key: item
        for page in result.catalogue.pages
        for item in page.series
    }

    def market(value: Any) -> JsonObject:
        metric = metrics_by_key.get(value.key)
        series = series_by_key.get(value.series_key)
        indicative_by_side = (
            {
                "YES": metric.indicative_yes_price_micros,
                "NO": metric.indicative_no_price_micros,
            }
            if metric is not None
            else {"YES": None, "NO": None}
        )
        return {
            "market_ref": value.market_ref,
            "series_ref": value.series_ref,
            "event_ref": value.event_ref,
            "question": value.question,
            "resolution_rules": value.resolution_rules,
            "resolution_source": value.resolution_source,
            "open_time": timestamp(value.open_time),
            "close_time": timestamp(value.close_time),
            "expected_expiration_time": timestamp(value.expected_expiration_time),
            "latest_expiration_time": timestamp(value.latest_expiration_time),
            "status": str(value.status),
            "eligible": value.eligible,
            "tradeable": value.tradeable,
            "volume_units": int(value.volume),
            "volume_24h_units": (
                metric.volume_24h_units if metric is not None else None
            ),
            "liquidity_micros": int(value.liquidity_micros),
            "volatility_micros": metric.volatility_micros if metric is not None else None,
            "volume_trend": metric.volume_trend if metric is not None else "insufficient_data",
            "volume_trend_delta": (
                format_metric_decimal(metric.volume_trend_delta) if metric is not None else None
            ),
            "competitive_score": (
                format_metric_decimal(metric.competitive_score) if metric is not None else None
            ),
            "tag_names": list(series.tags) if series is not None else [],
            "observed_at": timestamp(value.observed_at),
            "outcomes": [
                {
                    "outcome": str(item.side),
                    "label": item.label,
                    "eligible": item.eligible,
                    "indicative_price_micros": indicative_by_side[str(item.side).upper()],
                }
                for item in value.outcomes
            ],
            "price_ranges": [
                {"start": int(item.start), "end": int(item.end), "step": int(item.step)}
                for item in value.price_grid.ranges
            ],
            "audit": {
                "uri": value.audit.uri,
                "sha256": value.audit.sha256,
                "byte_length": value.audit.byte_length,
            },
        }

    contexts: list[JsonObject] = []
    for context in result.contexts:
        book = context.order_book
        contexts.append(
            {
                "market_ref": context.market.market_ref,
                "market": market(context.market),
                "order_book": {
                    "yes_bids": [level(item) for item in book.yes_bids],
                    "yes_asks": [level(item) for item in book.yes_asks],
                    "no_bids": [level(item) for item in book.no_bids],
                    "no_asks": [level(item) for item in book.no_asks],
                    "observed_at": timestamp(book.observed_at),
                    "cutoff": timestamp(book.cutoff),
                    "source_timestamp": timestamp(book.source_timestamp),
                    "snapshot_id": str(book.snapshot_id),
                    "audit": {
                        "uri": book.artifact.uri,
                        "sha256": book.artifact.sha256,
                        "byte_length": book.artifact.byte_length,
                    },
                },
            }
        )
    resolutions = [
        {
            "market_ref": item.market_key.market_ref,
            "status": str(item.status),
            "result": str(item.result) if item.result is not None else None,
            "observed_at": timestamp(item.observed_at),
            "source_timestamp": timestamp(item.source_timestamp),
            "settlement_ts": timestamp(item.settlement_ts),
            "blocked": item.blocked,
            "snapshot_id": str(item.snapshot_id),
            "audit": {
                "uri": item.audit.uri,
                "sha256": item.audit.sha256,
                "byte_length": item.audit.byte_length,
            },
        }
        for item in result.resolutions
    ]
    return {
        "venue": "kalshi",
        "cycle_id": str(cycle_id),
        "data_cutoff": timestamp(result.data_cutoff),
        "historical_cutoff": timestamp(result.catalogue.historical_cutoff),
        "discovery_market_refs": [item.market_ref for item in result.discovery_market_keys],
        "resolution_market_refs": [item.market_ref for item in result.resolution_market_keys],
        "market_snapshot_ids": [
            str(value)
            for value in (
                market_snapshot_ids
                if market_snapshot_ids is not None
                else tuple(item.market.snapshot_id for item in result.contexts)
            )
        ],
        "order_book_snapshot_ids": [
            str(value)
            for value in (
                order_book_snapshot_ids
                if order_book_snapshot_ids is not None
                else tuple(item.order_book.snapshot_id for item in result.contexts)
            )
        ],
        "resolution_ids": [
            str(value)
            for value in (
                resolution_ids
                if resolution_ids is not None
                else tuple(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:resolution:{item.snapshot_id}")
                    for item in result.resolutions
                )
            )
        ],
        "markets": [market(item) for item in result.markets],
        "contexts": contexts,
        "resolutions": resolutions,
        "audit_references": [
            {"uri": item.uri, "sha256": item.sha256, "byte_length": item.byte_length}
            for item in result.artifacts
        ],
    }


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
    discovery = config.raw.get("discovery")
    if not isinstance(discovery, Mapping):
        raise ProductionCompositionUnavailable("experiment discovery configuration is missing")
    venue = KalshiPublicRestAdapter(
        store,
        clock=clock,
        maximum_parallel_requests=_positive_integer(
            discovery, "maximum_concurrent_orderbooks"
        ),
        request_timeout_seconds=float(discovery.get("request_timeout_seconds", 15)),
        connect_timeout_seconds=float(discovery.get("connect_timeout_seconds", 5)),
        catalogue_sync_deadline_seconds=float(
            discovery.get("catalogue_sync_deadline_seconds", 300)
        ),
        freeze_deadline_seconds=float(discovery.get("freeze_deadline_seconds", 600)),
    )
    repository = PostgresRuntimeRepository(database_url)
    freeze_persistence = PostgresKalshiFreezeRepository(database_url)
    try:
        repository.migration_status()
    except Exception as exc:
        raise ProductionCompositionUnavailable(
            "private PostgreSQL is not at the verified migration readiness point"
        ) from exc
    maximum_valuation_bid_age = timedelta(
        seconds=_integer(config.raw["limits"], "maximum_archived_bid_age_seconds")
    )
    maximum_order_book_age = _maximum_order_book_age(config.raw)
    maximum_order_book_depth = _order_book_depth(config.raw)
    execution = config.raw.get("execution")
    if not isinstance(execution, Mapping) or execution.get("paper_policy") != "liquidity_aware":
        raise ProductionCompositionUnavailable(
            "the active Kalshi composition requires liquidity-aware paper execution"
        )
    settlement_valuation = ProductionSemanticSettlementPort(
        database_url,
        clock=clock,
        maximum_bid_age=maximum_valuation_bid_age,
    )
    immediate_order_executor = ProductionSemanticOrderExecutor(
        database_url,
        clock=clock,
        maximum_book_age=maximum_order_book_age,
        maximum_market_fraction=Decimal(
            str(config.raw["limits"]["maximum_market_cost_basis_fraction"])
        ),
    )
    orchestrator = CycleOrchestrator(
        repository=repository,
        market_freezer=ProductionKalshiFreezePort(
            KalshiMarketFreezeService(
                venue,
                clock=clock,
                maximum_parallel_book_requests=_positive_integer(
                    discovery, "maximum_concurrent_orderbooks"
                ),
                freeze_deadline_seconds=float(discovery.get("freeze_deadline_seconds", 600)),
            ),
            repository,
            freeze_persistence,
            clock=clock,
            maximum_historical_markets=_nonnegative_integer(
                discovery, "retained_historical_outcomes"
            ),
            maximum_additional_markets=_nonnegative_integer(discovery, "additional_markets"),
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
            require_live_order_execution=False,
        ),
        broker=ProductionSemanticBrokerPort(database_url),
        settlement_valuation=settlement_valuation,
        clock=clock,
        alert_policy=RuntimeAlertPolicy(
            maximum_data_age=maximum_valuation_bid_age,
            monthly_budget_micros=_integer(
                config.raw["limits"], "monthly_external_api_budget_micros"
            ),
        ),
        market_freeze_deadline_seconds=float(discovery.get("freeze_deadline_seconds", 600)),
        monotonic=monotonic,
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
    if worker is None:
        config.assert_runnable()
        application = build_production_worker(config, environment=environment)
    else:
        application = worker
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
        "config/experiments/vtrade-kalshi-v1.json",
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
        actual = canonical_artifact_file_sha256(
            path,
            label=f"frozen artifact {name}",
        )
    except OSError as exc:
        raise ProductionCompositionUnavailable(f"cannot read frozen artifact {name}") from exc
    except FrozenArtifactError as exc:
        raise ProductionCompositionUnavailable(str(exc)) from exc
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


def _nonnegative_integer(value: Mapping[str, object], key: str) -> int:
    result = _integer(value, key)
    if result < 0:
        raise ProductionCompositionUnavailable(
            f"configuration field {key} must be non-negative"
        )
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


def _optional_research_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _parse_timestamp(value)
    except ProductionCompositionUnavailable:
        return None


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
