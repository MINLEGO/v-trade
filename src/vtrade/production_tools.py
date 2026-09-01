from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast

from vtrade.config import ACTIVE_FIXTURE_MANIFEST, ACTIVE_TOOL_SCHEMA
from vtrade.domain.execution import (
    OrderAmountType,
    OrderRequest,
    OrderResult,
    TimeInForce,
)
from vtrade.domain.ports import JsonObject
from vtrade.fixtures import require_kalshi_fixture_manifest
from vtrade.frozen_artifacts import FrozenArtifactError, canonical_artifact_file_sha256
from vtrade.harness import (
    BELIEF_CATEGORIES,
    BeliefRecord,
    PlanRecord,
    PlanType,
    ToolExecution,
    ToolHandlerError,
    ToolSpec,
)
from vtrade.harness_repository import PostgresHarnessRepository
from vtrade.market_metrics import format_metric_decimal
from vtrade.portfolio import PostgresContractPortfolioHandler
from vtrade.providers import ExaResearchProvider
from vtrade.runtime import CycleClaim


class ToolContextUnavailable(ToolHandlerError):
    pass


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]
_OrderExecutor = Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class ToolContext:
    database_url: str
    claim: CycleClaim
    exa: ExaResearchProvider
    memory: PostgresHarnessRepository
    portfolio: Callable[[JsonObject], JsonObject]
    connect: _Connect
    clock: Callable[[], datetime]
    market_snapshot_ids: tuple[uuid.UUID, ...] = ()
    order_book_snapshot_ids: tuple[uuid.UUID, ...] = ()
    fee_rate_snapshot_ids: tuple[uuid.UUID, ...] = ()
    maximum_default_result_tokens: int = 4_000
    maximum_book_age: timedelta = timedelta(minutes=5)
    maximum_order_book_depth: int = 6
    immediate_order_executor: _OrderExecutor | None = None
    live_order_execution: bool = False
    live_order_required: bool = False
    fixture_manifest_path: str | Path = ACTIVE_FIXTURE_MANIFEST

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ToolContextUnavailable("tools require a private database resource")
        if self.claim.data_cutoff is None:
            raise ToolContextUnavailable("tools require a finalized cycle cutoff")
        if self.maximum_default_result_tokens <= 0:
            raise ToolContextUnavailable("tool result ceiling must be positive")
        if self.maximum_book_age < timedelta(0):
            raise ToolContextUnavailable("order-book age ceiling cannot be negative")
        if (
            not isinstance(self.maximum_order_book_depth, int)
            or isinstance(self.maximum_order_book_depth, bool)
            or self.maximum_order_book_depth <= 0
        ):
            raise ToolContextUnavailable("order-book depth must be a positive integer")
        if self.live_order_execution or self.live_order_required:
            raise ToolContextUnavailable(
                "real order execution is disabled in the active Kalshi paper composition"
            )

    @property
    def cutoff(self) -> datetime:
        value = self.claim.data_cutoff
        if value is None:
            raise ToolContextUnavailable("cycle cutoff is not finalized")
        return _aware(value)

    def now(self) -> datetime:
        return _aware(self.clock())


_PAGINATED_TOOLS = frozenset(
    {
        "get_newest_markets",
        "discover_by_time_remaining",
        "discover_events",
        "list_top_events",
        "discover_by_price_volatility",
        "get_event_markets",
        "get_newest_events",
        "get_all_active_markets",
        "discover_by_volume_trend",
        "discover_by_competitive_score",
        "discover_by_date_range",
        "search_tags",
        "get_general_beliefs",
        "search_general_beliefs",
    }
)
MAX_PLAN_CONTENT_CHARACTERS = 4_000
TOOL_NAMES = (
    "get_newest_markets",
    "discover_by_time_remaining",
    "discover_events",
    "list_top_events",
    "get_market_details",
    "web_search",
    "fetch_webpage",
    "get_orderbook",
    "discover_by_price_volatility",
    "get_event_markets",
    "get_newest_events",
    "get_all_active_markets",
    "discover_by_volume_trend",
    "discover_by_competitive_score",
    "discover_by_date_range",
    "search_tags",
    "get_balance",
    "get_portfolio",
    "get_closed_trades",
    "get_settlements",
    "get_general_beliefs",
    "search_general_beliefs",
    "create_general_belief",
    "delete_general_belief",
    "create_long_term_plan",
    "create_next_cycle_plan",
    "place_market_order",
)


class ProductionToolRegistry:
    """The exact 27-tool agent surface for vtrade-kalshi-v1."""

    def __init__(
        self,
        context: ToolContext,
        *,
        schema_path: str | Path = ACTIVE_TOOL_SCHEMA,
    ) -> None:
        self._context = context
        self._mutation_sequence = 0
        manifest_path = getattr(context, "fixture_manifest_path", None)
        if manifest_path is not None:
            try:
                require_kalshi_fixture_manifest(manifest_path)
            except ValueError as exc:
                raise ToolContextUnavailable(str(exc)) from exc
        self._schemas, self._output_schemas = _load_schema_artifact(schema_path)
        handlers = self._handlers()
        if tuple(self._schemas) != TOOL_NAMES:
            raise ValueError("active tool schema names do not match the vtrade-kalshi-v1 contract")
        if set(handlers) != set(TOOL_NAMES) or len(handlers) != 27:
            raise ValueError("production handlers must exactly match all 27 frozen tool names")

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        handlers = self._handlers()
        return tuple(
            ToolSpec(
                schema=self._schemas[name],
                handler=self._bounded_handler(name, handlers[name]),
                category=self._category(name),
                mutates_financial_state=name == "place_market_order",
                output_schema=self._output_schemas[name],
            )
            for name in TOOL_NAMES
        )

    def _bounded_handler(
        self,
        name: str,
        handler: Callable[[JsonObject], JsonObject | ToolExecution],
    ) -> Callable[[JsonObject], JsonObject | ToolExecution]:
        if name == "get_portfolio" or name in _PAGINATED_TOOLS:
            return handler

        def bounded(arguments: JsonObject) -> JsonObject | ToolExecution:
            result = handler(arguments)
            if isinstance(result, ToolExecution):
                return ToolExecution(
                    _bounded_output(result.output, self._context.maximum_default_result_tokens),
                    result.telemetry,
                )
            return _bounded_output(result, self._context.maximum_default_result_tokens)

        return bounded

    def _handlers(self) -> dict[str, Callable[[JsonObject], JsonObject | ToolExecution]]:
        discovery = {
            name: (lambda arguments, tool_name=name: self._discover(tool_name, arguments))
            for name in TOOL_NAMES[:17]
            if name not in {"web_search", "fetch_webpage", "get_orderbook"}
        }
        return {
            **discovery,
            "web_search": self._web_search,
            "fetch_webpage": self._fetch_webpage,
            "get_orderbook": self._get_orderbook,
            "get_balance": self._get_balance,
            "get_portfolio": self._context.portfolio,
            "get_closed_trades": self._get_closed_trades,
            "get_settlements": self._get_settlements,
            "get_general_beliefs": self._get_beliefs,
            "search_general_beliefs": self._search_beliefs,
            "create_general_belief": self._create_belief,
            "delete_general_belief": self._delete_belief,
            "create_long_term_plan": self._create_long_term_plan,
            "create_next_cycle_plan": self._create_next_cycle_plan,
            "place_market_order": self._place_market_order,
        }

    def _discover(self, name: str, arguments: JsonObject) -> JsonObject:
        if name in {"discover_events", "list_top_events", "get_newest_events"}:
            return self._discover_event_groups(name, arguments)
        if name == "get_market_details":
            return self._get_market_details(arguments)
        rows = list(self._market_rows())
        rows = self._filter_market_rows(name, rows, arguments)
        return self._market_page(rows, name=name, arguments=arguments)

    def _filter_market_rows(
        self,
        name: str,
        rows: list[Sequence[object]],
        arguments: Mapping[str, object],
    ) -> list[Sequence[object]]:
        if name == "get_event_markets":
            event_ref = _required_string(arguments, "event_ref")
            rows = [row for row in rows if str(row[2]) == event_ref]
        minimum_liquidity = _nonnegative_int(
            arguments.get("min_liquidity_micros", 0), "min_liquidity_micros"
        )
        minimum_volume = _nonnegative_int(arguments.get("min_volume_units", 0), "min_volume_units")
        rows = [
            row
            for row in rows
            if _as_int(row[8]) >= minimum_volume and _as_int(row[9]) >= minimum_liquidity
        ]
        keyword = arguments.get("keyword") if name != "search_tags" else None
        keywords = _keywords(keyword)
        if keywords:
            rows = [
                row
                for row in rows
                if any(
                    term in f"{row[0]} {row[2]} {row[3]} {row[5]}".casefold() for term in keywords
                )
            ]
        if name == "get_newest_markets":
            hours = _decimal(arguments.get("hours_back", "24"), "hours_back", minimum=Decimal(0))
            rows = [row for row in rows if _within_hours(row[6], self._context.cutoff, hours)]
            rows.sort(key=lambda row: (_datetime_key(row[6]), str(row[0])), reverse=True)
        elif name == "discover_by_time_remaining":
            minimum = _decimal(arguments.get("hours_min", "0"), "hours_min", minimum=Decimal(0))
            maximum = _decimal(
                arguments.get("hours_max", "1000000000"), "hours_max", minimum=Decimal(0)
            )
            rows = [
                row
                for row in rows
                if minimum <= _hours_until(row[7], self._context.cutoff) <= maximum
            ]
            rows.sort(key=lambda row: (_hours_until(row[7], self._context.cutoff), str(row[0])))
        elif name == "discover_by_date_range":
            date_basis = str(arguments.get("date_basis", "close_time"))
            if date_basis not in {"close_time", "open_time"}:
                raise ValueError("date_basis must be close_time or open_time")
            start = str(arguments.get("start_date", ""))
            end = str(arguments.get("end_date", ""))
            date_index = 7 if date_basis == "close_time" else 6
            rows = [
                row
                for row in rows
                if row[date_index] is not None
                and (not start or str(row[date_index])[:10] >= start)
                and (not end or str(row[date_index])[:10] <= end)
            ]
        elif name == "discover_by_price_volatility":
            minimum_volatility = _nonnegative_int(
                arguments.get("min_volatility_micros", 0), "min_volatility_micros"
            )
            rows = [
                row
                for row in rows
                if (value := _row_volatility(row)) is not None and value >= minimum_volatility
            ]
            rows.sort(
                key=lambda row: (_row_volatility(row) or -1, _as_int(row[8]), str(row[0])),
                reverse=True,
            )
        elif name == "discover_by_volume_trend":
            trend = str(arguments.get("trend", "increasing"))
            if trend not in {"increasing", "decreasing", "flat", "insufficient_data"}:
                raise ValueError(
                    "trend must be increasing, decreasing, flat, or insufficient_data"
                )
            rows = [row for row in rows if _row_trend(row) == trend]
        elif name == "discover_by_competitive_score":
            minimum_score = _decimal(
                arguments.get("min_score", "0"), "min_score", minimum=Decimal(0)
            )
            rows = [
                row
                for row in rows
                if (
                    (value := _market_card(row)["competitive_score"]) is not None
                    and Decimal(str(value)) >= minimum_score
                )
            ]
            rows.sort(
                key=lambda row: (
                    Decimal(str(_market_card(row)["competitive_score"] or "-1")),
                    str(row[0]),
                ),
                reverse=True,
            )
        elif name == "search_tags":
            tag_keywords = _keywords(arguments.get("query"))
            rows = [
                row
                for row in rows
                if any(
                    term in {tag.casefold() for tag in _tags(row)} for term in tag_keywords
                )
            ]
        else:
            rows.sort(
                key=lambda row: (_as_int(row[8]), _as_int(row[9]), str(row[0])),
                reverse=True,
            )
        return rows

    def _discover_event_groups(self, name: str, arguments: JsonObject) -> JsonObject:
        rows = self._filter_market_rows("discover_events", list(self._market_rows()), arguments)
        grouped: dict[str, JsonObject] = {}
        for row in rows:
            event_ref = str(row[2])
            item = grouped.setdefault(
                event_ref,
                {
                    "event_ref": event_ref,
                    "series_ref": str(row[1]),
                    "title": str(row[3]),
                    "category": row[4]
                    if row[4] is None or isinstance(row[4], str)
                    else str(row[4]),
                    "markets": [],
                    "volume_24h_units": 0,
                    "total_volume_units": 0,
                    "newest_market_open_time": None,
                    "audit": _audit(row),
                },
            )
            cast(list[JsonObject], item["markets"]).append(_market_card(row))
            volume_24h = _row_volume_24h(row)
            if volume_24h is None:
                raise ToolContextUnavailable("market metrics lack volume_24h_units")
            item["volume_24h_units"] = _as_int(item["volume_24h_units"]) + volume_24h
            item["total_volume_units"] = _as_int(item["total_volume_units"]) + _as_int(row[8])
            opened = _iso(row[6])
            current = item["newest_market_open_time"]
            if opened is not None and (current is None or opened > str(current)):
                item["newest_market_open_time"] = opened
        values = list(grouped.values())
        if name == "get_newest_events":
            values.sort(key=lambda item: str(item["newest_market_open_time"] or ""), reverse=True)
        elif name == "discover_events":
            values.sort(key=lambda item: _as_int(item["volume_24h_units"]), reverse=True)
        else:
            values.sort(key=lambda item: _as_int(item["total_volume_units"]), reverse=True)
        return _page(
            "events",
            values,
            name=name,
            arguments=arguments,
            cutoff=self._context.cutoff,
            maximum_tokens=self._context.maximum_default_result_tokens,
        )

    def _get_market_details(self, arguments: JsonObject) -> JsonObject:
        market_ref = _required_string(arguments, "market_ref")
        rows = self._query(
            _MARKET_SELECT
            + " AND m.market_ref = %s ORDER BY m.observed_at DESC, m.id DESC LIMIT 1",
            (self._context.claim.cycle_id, self._context.cutoff, market_ref),
        )
        if not rows:
            raise ToolContextUnavailable("market is absent from the published Kalshi freeze")
        row = rows[0]
        ranges = self._query(
            "SELECT start_price_micros, end_price_micros, step_micros "
            "FROM market_price_grid_ranges WHERE market_id = "
            "(SELECT id FROM markets WHERE market_ref = %s AND venue = 'kalshi' "
            "AND kind = 'binary') "
            "ORDER BY ordinal",
            (market_ref,),
        )
        if not ranges:
            raise ToolContextUnavailable("market has no dynamic price grid")
        return {
            "as_of": self._context.cutoff.isoformat(),
            "data_cutoff": self._context.cutoff.isoformat(),
            "market": _market_card(row),
            "resolution_rules": str(row[_MARKET_RESOLUTION_RULES_INDEX]),
            "price_ranges": [
                {
                    "start_price_micros": _as_int(item[0]),
                    "end_price_micros": _as_int(item[1]),
                    "step_micros": _as_int(item[2]),
                }
                for item in ranges
            ],
            "audit": _audit(row),
        }

    def _market_rows(self) -> Sequence[Sequence[object]]:
        return self._query(
            _MARKET_SELECT
            + " AND state.eligible AND state.tradeable "
            + "AND m.lifecycle_status IN ('open', 'active') "
            "ORDER BY m.volume_units DESC, m.liquidity_micros DESC, m.market_ref ASC",
            (self._context.claim.cycle_id, self._context.cutoff),
        )

    def _market_page(
        self,
        rows: Sequence[Sequence[object]],
        *,
        name: str,
        arguments: Mapping[str, object],
    ) -> JsonObject:
        return _page(
            "markets",
            [_market_card(row) for row in rows],
            name=name,
            arguments=arguments,
            cutoff=self._context.cutoff,
            maximum_tokens=self._context.maximum_default_result_tokens,
        )

    def _web_search(self, arguments: JsonObject) -> ToolExecution:
        query = _required_string(arguments, "query")
        response = self._context.exa.search(
            query,
            {key: value for key, value in arguments.items() if key != "query"},
            now=self._context.now(),
        )
        return ToolExecution(response.output, (response.telemetry,))

    def _fetch_webpage(self, arguments: JsonObject) -> ToolExecution:
        url = _required_string(arguments, "url")
        response = self._context.exa.fetch(
            url,
            {key: value for key, value in arguments.items() if key != "url"},
        )
        return ToolExecution(response.output, (response.telemetry,))

    def _get_orderbook(self, arguments: JsonObject) -> JsonObject:
        market_ref = _required_string(arguments, "market_ref")
        snapshot_rows = self._query(
            "SELECT obs.id, obs.observed_at, obs.source_timestamp, obs.cutoff, "
            "obs.raw_artifact_id, ra.sha256, ra.observed_at, state.fee_policy_status, "
            "state.fee_policy_reason "
            "FROM order_book_snapshots obs JOIN markets m ON m.id = obs.market_id "
            "JOIN market_freezes mf ON mf.id = obs.freeze_id "
            "JOIN frozen_market_states state ON state.freeze_id = mf.id "
            "AND state.market_id = obs.market_id "
            "JOIN raw_artifacts ra ON ra.id = obs.raw_artifact_id "
            "WHERE m.market_ref = %s AND m.venue = 'kalshi' AND mf.agent_cycle_id = %s "
            "AND obs.cutoff <= %s ORDER BY obs.cutoff DESC, obs.id DESC LIMIT 1",
            (market_ref, self._context.claim.cycle_id, self._context.cutoff),
        )
        if not snapshot_rows:
            raise ToolContextUnavailable(
                "market has no canonical order book in the published freeze"
            )
        snapshot = snapshot_rows[0]
        observed_at = _datetime(snapshot[1], "order-book observed_at")
        source_timestamp = _optional_datetime(snapshot[2], "order-book source_timestamp")
        cutoff = _datetime(snapshot[3], "order-book cutoff")
        if observed_at > self._context.cutoff or cutoff > self._context.cutoff:
            raise ToolContextUnavailable("order book is newer than the cycle cutoff")
        if source_timestamp is not None and source_timestamp > self._context.cutoff:
            raise ToolContextUnavailable("order-book source data is newer than the cycle cutoff")
        if self._context.cutoff - observed_at > self._context.maximum_book_age:
            raise ToolContextUnavailable(
                "canonical order book is older than the configured age limit"
            )
        level_rows = self._query(
            "WITH ranked_levels AS ("
            "SELECT outcome_side, book_side, level_index, price_micros, contract_units, "
            "ROW_NUMBER() OVER (PARTITION BY outcome_side, book_side ORDER BY level_index) "
            "AS side_level FROM order_book_levels WHERE snapshot_id = %s) "
            "SELECT outcome_side, book_side, level_index, price_micros, contract_units "
            "FROM ranked_levels WHERE side_level <= %s "
            "ORDER BY outcome_side, book_side, level_index",
            (snapshot[0], self._context.maximum_order_book_depth),
        )
        levels: dict[tuple[str, str], list[JsonObject]] = {
            (side, book_side): [] for side in ("YES", "NO") for book_side in ("bid", "ask")
        }
        for row in level_rows:
            key = (str(row[0]), str(row[1]))
            if key in levels:
                levels[key].append(
                    {"price_micros": _as_int(row[3]), "contract_units": _as_int(row[4])}
                )
        fee_policy = self._fee_policy(market_ref)
        fee_policy_status = (
            str(snapshot[7]) if len(snapshot) > 7 and snapshot[7] is not None else None
        )
        fee_policy_reason = (
            str(snapshot[8]) if len(snapshot) > 8 and snapshot[8] is not None else None
        )
        if fee_policy_status is None:
            fee_policy_status = "AVAILABLE" if fee_policy is not None else "UNAVAILABLE"
        if fee_policy_status == "AVAILABLE" and fee_policy is None:
            raise ToolContextUnavailable("published freeze has an incomplete fee policy")
        if fee_policy_status != "AVAILABLE" and fee_policy_reason is None:
            fee_policy_reason = "FEE_POLICY_UNAVAILABLE"
        audit = {
            "artifact_id": str(snapshot[4]),
            "sha256": str(snapshot[5]),
            "observed_at": _iso(snapshot[6]),
        }
        return {
            "as_of": self._context.cutoff.isoformat(),
            "data_cutoff": self._context.cutoff.isoformat(),
            "book": {
                "market_ref": market_ref,
                "yes_bids": levels[("YES", "bid")],
                "yes_asks": levels[("YES", "ask")],
                "no_bids": levels[("NO", "bid")],
                "no_asks": levels[("NO", "ask")],
                "observed_at": observed_at.isoformat(),
                "data_cutoff": cutoff.isoformat(),
                "audit": audit,
            },
            "fee_policy": fee_policy,
            "fee_policy_status": fee_policy_status,
            "fee_policy_reason": fee_policy_reason,
            "audit": audit,
        }

    def _fee_policy(self, market_ref: str) -> JsonObject | None:
        rows = self._query(
            "SELECT fps.policy_version, fps.formula_version, fps.schedule_identity, "
            "fps.participant_role, fps.multiplier_numerator, fps.multiplier_denominator, "
            "fps.event_override_micros, fps.event_override_cleared, fps.effective_at, "
            "fps.as_of_at, fps.observed_at, fps.cutoff, fps.source_tier, "
            "fps.policy_fingerprint, fps.raw_artifact_id, ra.sha256, ra.observed_at, "
            "fps.fee_type, fps.series_multiplier_numerator, fps.series_multiplier_denominator, "
            "fps.event_override_numerator, fps.event_override_denominator, "
            "fps.event_override_fee_type, fps.rate_numerator, fps.rate_denominator, "
            "fps.scheduled_ts, fps.waiver, fps.schedule_sha256, fps.settlement_fee_micros, "
            "fps.exact_inputs, fps.waiver_evidence "
            "FROM fee_policy_snapshots fps JOIN markets m ON m.id = fps.market_id "
            "JOIN freeze_market_fee_policies fmp ON fmp.fee_policy_snapshot_id = fps.id "
            "JOIN market_freezes mf ON mf.id = fmp.freeze_id "
            "JOIN raw_artifacts ra ON ra.id = fps.raw_artifact_id "
            "WHERE m.market_ref = %s AND mf.agent_cycle_id = %s "
            "AND fmp.status = 'AVAILABLE' AND fps.observed_at <= %s "
            "AND fps.as_of_at <= %s AND fps.cutoff <= %s "
            "ORDER BY fps.observed_at DESC, fps.id DESC LIMIT 1",
            (
                market_ref,
                self._context.claim.cycle_id,
                self._context.cutoff,
                self._context.cutoff,
                self._context.cutoff,
            ),
        )
        if not rows:
            return None
        row = rows[0]
        evidence_rows = self._query(
            "SELECT fpa.evidence_role, ra.sha256 "
            "FROM fee_policy_snapshot_artifacts fpa "
            "JOIN fee_policy_snapshots fps ON fps.id = fpa.fee_policy_snapshot_id "
            "JOIN markets m ON m.id = fps.market_id "
            "JOIN raw_artifacts ra ON ra.id = fpa.raw_artifact_id "
            "WHERE m.market_ref = %s AND fps.policy_fingerprint = %s "
            "ORDER BY fpa.evidence_role, ra.sha256",
            (market_ref, str(row[13])),
        )
        return {
            "contract_version": "vtrade-binary-fee-settlement-v1",
            "schedule_version": str(row[2]),
            "formula_version": str(row[1]),
            "participant_role": str(row[3]).upper(),
            "multiplier_numerator": _as_int(row[4]),
            "multiplier_denominator": _as_int(row[5]),
            "event_override_micros": None if row[6] is None else _as_int(row[6]),
            "event_override_cleared": bool(row[7]),
            "effective_at": _datetime(row[8], "fee effective_at").isoformat(),
            "as_of_at": _datetime(row[9], "fee as_of_at").isoformat(),
            "observed_at": _datetime(row[10], "fee observed_at").isoformat(),
            "cutoff": _datetime(row[11], "fee cutoff").isoformat(),
            "source_tier": str(row[12]),
            "policy_fingerprint": str(row[13]),
            "fee_type": str(row[17]) if row[17] is not None else "quadratic",
            "series_multiplier_numerator": _as_int(row[18])
            if row[18] is not None
            else _as_int(row[4]),
            "series_multiplier_denominator": _as_int(row[19])
            if row[19] is not None
            else _as_int(row[5]),
            "event_override_numerator": (
                _as_int(row[20]) if row[20] is not None else None
            ),
            "event_override_denominator": (
                _as_int(row[21]) if row[21] is not None else None
            ),
            "event_override_fee_type": (
                str(row[22]) if row[22] is not None else None
            ),
            "rate_numerator": _as_int(row[23]) if row[23] is not None else None,
            "rate_denominator": _as_int(row[24]) if row[24] is not None else None,
            "scheduled_ts": _iso(row[25]),
            "waiver": bool(row[26]),
            "schedule_sha256": str(row[27]) if row[27] is not None else None,
            "settlement_fee_micros": _as_int(row[28]),
            "exact_inputs": (
                dict(row[29]) if isinstance(row[29], Mapping) else {}
            ),
            "waiver_evidence": (
                dict(row[30]) if isinstance(row[30], Mapping) else None
            ),
            "evidence_references": [
                {"role": str(item[0]), "sha256": str(item[1])}
                for item in evidence_rows
            ],
            "audit": {
                "artifact_id": str(row[14]),
                "sha256": str(row[15]),
                "observed_at": _iso(row[16]),
            },
        }

    def _get_balance(self, _arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT COALESCE(sum(lp.amount_micros) FILTER (WHERE lp.account = 'cash'), 0), "
            "a.portfolio_version FROM agents a LEFT JOIN ledger_entries le ON le.agent_id = a.id "
            "LEFT JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
            "WHERE a.id = %s GROUP BY a.id, a.portfolio_version",
            (self._context.claim.agent_id,),
        )
        if not rows:
            raise ToolContextUnavailable("agent balance is unavailable")
        return {"cash_micros": _as_int(rows[0][0]), "portfolio_version": _as_int(rows[0][1])}

    def _get_closed_trades(self, arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT p.id, m.market_ref, p.outcome_side, min(f.filled_at), max(f.filled_at), "
            "sum(CASE WHEN oo.order_side = 'BUY' THEN f.contract_units ELSE 0 END), "
            "sum(CASE WHEN oo.order_side = 'SELL' THEN f.contract_units ELSE 0 END), "
            "COALESCE(sum(CASE WHEN oo.order_side = 'BUY' "
            "THEN f.gross_cash_micros ELSE 0 END), 0), "
            "COALESCE(sum(CASE WHEN oo.order_side = 'SELL' "
            "THEN f.gross_cash_micros ELSE 0 END), 0), "
            "COALESCE(sum(f.authoritative_fee_micros), 0), p.realized_pnl_micros "
            "FROM fills f JOIN order_operations oo ON oo.id = f.operation_id "
            "JOIN positions p ON p.agent_id = oo.agent_id AND p.market_id = oo.market_id "
            "AND p.outcome_side = oo.outcome_side JOIN markets m ON m.id = oo.market_id "
            "WHERE oo.agent_id = %s GROUP BY p.id, m.market_ref, p.outcome_side, "
            "p.realized_pnl_micros "
            "HAVING sum(CASE WHEN oo.order_side = 'BUY' THEN f.contract_units "
            "ELSE -f.contract_units END) = 0 "
            "ORDER BY max(f.filled_at) DESC, p.id DESC LIMIT %s",
            (self._context.claim.agent_id, _limit(arguments)),
        )
        return {
            "trades": [
                {
                    "position_id": str(row[0]),
                    "market_ref": str(row[1]),
                    "outcome": str(row[2]),
                    "opened_at": _datetime(row[3], "trade opened_at").isoformat(),
                    "closed_at": _datetime(row[4], "trade closed_at").isoformat(),
                    "bought_contract_units": _as_int(row[5]),
                    "sold_contract_units": _as_int(row[6]),
                    "average_entry_price_micros": _average_price(row[7], row[5]),
                    "average_exit_price_micros": _average_price(row[8], row[6]),
                    "entry_cost_micros": _as_int(row[7]),
                    "exit_proceeds_micros": _as_int(row[8]),
                    "total_fees_micros": _as_int(row[9]),
                    "realized_pnl_micros": _as_int(row[10]),
                    "close_reason": "sold",
                }
                for row in rows
            ]
        }

    def _get_settlements(self, arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT s.id, s.position_id, m.market_ref, s.outcome_side, r.result, "
            "r.lifecycle_status, s.contract_units, s.gross_payout_micros, "
            "s.entry_fees_deducted_micros, s.realized_pnl_micros, s.settlement_ts, "
            "s.settled_at, r.raw_artifact_id, ra.sha256, ra.observed_at, m.question "
            "FROM settlements s JOIN positions p ON p.id = s.position_id "
            "JOIN markets m ON m.id = s.market_id JOIN resolution_observations r "
            "ON r.id = s.resolution_id JOIN raw_artifacts ra ON ra.id = r.raw_artifact_id "
            "WHERE s.agent_id = %s ORDER BY s.settled_at DESC, s.id DESC LIMIT %s",
            (self._context.claim.agent_id, _limit(arguments)),
        )
        return {
            "settlements": [
                {
                    "settlement_id": str(row[0]),
                    "position_id": str(row[1]),
                    "market_ref": str(row[2]),
                    "outcome": str(row[3]),
                    "result": None if row[4] is None else str(row[4]),
                    "resolution_status": str(row[5]).upper(),
                    "contract_units": _as_int(row[6]),
                    "gross_payout_micros": _as_int(row[7]),
                    "entry_fees_deducted_micros": _as_int(row[8]),
                    "realized_pnl_micros": _as_int(row[9]),
                    "settlement_ts": _iso(row[10]),
                    "settled_at": _datetime(row[11], "settlement settled_at").isoformat(),
                    "audit": {
                        "artifact_id": str(row[12]),
                        "sha256": str(row[13]),
                        "observed_at": _iso(row[14]),
                    },
                    "market_question": _nullable_market_question(row[15]),
                }
                for row in rows
            ]
        }

    def _get_beliefs(self, arguments: JsonObject) -> JsonObject:
        beliefs = self._beliefs(bool(arguments.get("include_inactive", False)))
        return _page(
            "beliefs",
            beliefs,
            name="get_general_beliefs",
            arguments=arguments,
            cutoff=self._context.cutoff,
            maximum_tokens=self._context.maximum_default_result_tokens,
        )

    def _search_beliefs(self, arguments: JsonObject) -> JsonObject:
        beliefs = self._beliefs(bool(arguments.get("include_inactive", False)))
        keywords = _keywords(arguments.get("keyword"))
        category = str(arguments.get("category", "")).casefold()
        matches = [
            item
            for item in beliefs
            if (
                not keywords
                or any(term in str(item.get("content", "")).casefold() for term in keywords)
            )
            and (not category or category == str(item.get("category", "")).casefold())
        ]
        return _page(
            "beliefs",
            matches,
            name="search_general_beliefs",
            arguments=arguments,
            cutoff=self._context.cutoff,
            maximum_tokens=self._context.maximum_default_result_tokens,
        )

    def _beliefs(self, include_inactive: bool) -> list[JsonObject]:
        if not include_inactive:
            rows = self._context.memory.read_beliefs(
                actor_id=self._context.claim.agent_id,
                target_agent_id=self._context.claim.agent_id,
            )
            return [{**item, "active": True} for item in rows]
        db_rows = self._query(
            "SELECT b.id, b.active, r.confidence, r.content, r.category, r.evidence, r.created_at "
            "FROM beliefs b JOIN LATERAL (SELECT * FROM belief_revisions WHERE belief_id = b.id "
            "ORDER BY revision DESC LIMIT 1) r ON true WHERE b.agent_id = %s "
            "ORDER BY r.created_at DESC, b.id DESC",
            (self._context.claim.agent_id,),
        )
        return [
            {
                "id": str(row[0]),
                "active": bool(row[1]),
                "confidence": str(row[2]),
                "content": str(row[3]),
                "category": str(row[4]),
                "evidence": list(row[5]) if isinstance(row[5], (list, tuple)) else row[5],
                "created_at": _datetime(row[6], "belief created_at").isoformat(),
            }
            for row in db_rows
        ]

    def _create_belief(self, arguments: JsonObject) -> JsonObject:
        confidence = _unit_interval(arguments.get("confidence"), "confidence")
        category = _belief_category(arguments.get("category"))
        now = self._context.now()
        belief = BeliefRecord(
            str(self._mutation_id("belief", arguments)),
            str(self._context.claim.agent_id),
            confidence,
            _required_string(arguments, "belief_content"),
            category,
            _evidence(arguments.get("evidence", [])),
            now,
        )
        self._context.memory.append_belief(
            belief, actor_id=self._context.claim.agent_id, cycle_id=self._context.claim.cycle_id
        )
        return {"belief_id": belief.id, "created_at": now.isoformat()}

    def _delete_belief(self, arguments: JsonObject) -> JsonObject:
        belief_id = uuid.UUID(_required_string(arguments, "belief_id"))
        with (
            self._context.connect(self._context.database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE beliefs SET active = false WHERE id = %s AND agent_id = %s "
                "AND active = true RETURNING id",
                (belief_id, self._context.claim.agent_id),
            )
            changed = cursor.fetchone()
            if changed is not None:
                return {"belief_id": str(belief_id), "deleted": True, "already_inactive": False}
            cursor.execute(
                "SELECT active FROM beliefs WHERE id = %s AND agent_id = %s",
                (belief_id, self._context.claim.agent_id),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise ToolContextUnavailable("belief is missing or foreign")
            if bool(existing[0]):
                raise ToolContextUnavailable("belief deactivation did not persist")
            return {"belief_id": str(belief_id), "deleted": True, "already_inactive": True}

    def _create_long_term_plan(self, arguments: JsonObject) -> JsonObject:
        return self._create_plan(PlanType.LONG_TERM, arguments, None)

    def _create_next_cycle_plan(self, arguments: JsonObject) -> JsonObject:
        due = arguments.get("cycle_date")
        due_at = _date_at_midnight(due) if due is not None else None
        return self._create_plan(PlanType.NEXT_CYCLE, arguments, due_at)

    def _create_plan(
        self, plan_type: PlanType, arguments: JsonObject, due_at: datetime | None
    ) -> JsonObject:
        content = _required_string(
            arguments, "plan_content", max_length=MAX_PLAN_CONTENT_CHARACTERS
        )
        now = self._context.now()
        plan = PlanRecord(
            str(self._mutation_id(f"plan:{plan_type.value}", arguments)),
            str(self._context.claim.agent_id),
            plan_type,
            content,
            due_at,
            now,
        )
        self._context.memory.append_plan(
            plan, actor_id=self._context.claim.agent_id, cycle_id=self._context.claim.cycle_id
        )
        return {"plan_id": plan.id, "created_at": now.isoformat()}

    def _place_market_order(self, arguments: JsonObject) -> JsonObject:
        market_ref = _required_string(arguments, "market_ref")
        outcome = _required_string(arguments, "outcome")
        action = _required_string(arguments, "action")
        amount_type = OrderAmountType(_required_string(arguments, "amount_type"))
        time_in_force = TimeInForce(_required_string(arguments, "time_in_force"))
        amount = _positive_integer_string(arguments.get("amount"), "amount")
        idempotency_key = _required_string(arguments, "idempotency_key", max_length=512)
        requested_at = self._context.now()
        limit_value = arguments.get("limit_price_micros")
        limit_price = (
            None if limit_value is None else _exact_integer(limit_value, "limit_price_micros")
        )
        request = OrderRequest(
            agent_id=str(self._context.claim.agent_id),
            market_ref=market_ref,
            outcome=outcome,
            action=action,
            amount=amount,
            amount_type=amount_type,
            idempotency_key=idempotency_key,
            limit_price=limit_price,
            time_in_force=time_in_force,
            frozen_context_id=str(self._context.claim.cycle_id),
            frozen_cutoff=self._context.cutoff,
            created_at=requested_at,
        )
        executor = self._context.immediate_order_executor
        if executor is None:
            raise ToolContextUnavailable("paper execution port is unavailable")
        try:
            result = executor(request)
        except (ToolContextUnavailable, ToolHandlerError):
            raise
        except Exception as exc:
            raise ToolContextUnavailable("paper execution failed closed") from exc
        if not isinstance(result, OrderResult):
            raise ToolContextUnavailable("paper execution returned an invalid semantic result")
        return _execution_output(result, audit=self._execution_audit(result))

    def _execution_audit(self, result: OrderResult) -> JsonObject | None:
        if result.execution_context_id is None:
            return None
        try:
            execution_context_id = uuid.UUID(result.execution_context_id)
        except ValueError:
            return None
        rows = self._query(
            "SELECT obs.raw_artifact_id, ra.sha256, ra.observed_at "
            "FROM execution_contexts context "
            "JOIN order_book_snapshots obs ON obs.execution_context_id = context.id "
            "JOIN raw_artifacts ra ON ra.id = obs.raw_artifact_id "
            "WHERE context.id = %s",
            (execution_context_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "artifact_id": str(row[0]),
            "sha256": str(row[1]),
            "observed_at": _iso(row[2]),
        }

    def _mutation_id(self, kind: str, arguments: Mapping[str, object]) -> uuid.UUID:
        sequence = self._mutation_sequence
        self._mutation_sequence += 1
        payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vtrade:{kind}:{self._context.claim.cycle_id}:{sequence}:{payload}",
        )

    def _query(self, sql: str, params: Sequence[object]) -> Sequence[Sequence[object]]:
        with (
            self._context.connect(self._context.database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, params)
            return tuple(cursor.fetchall())

    @staticmethod
    def _category(name: str) -> str:
        if name in {"web_search", "fetch_webpage"}:
            return "research"
        if name in {
            "get_balance",
            "get_portfolio",
            "get_closed_trades",
            "get_settlements",
            "get_general_beliefs",
            "search_general_beliefs",
            "create_general_belief",
            "delete_general_belief",
            "create_long_term_plan",
            "create_next_cycle_plan",
        }:
            return "account"
        if name == "place_market_order":
            return "trading"
        return "discovery"


def _load_schema_artifact(path: str | Path) -> tuple[dict[str, JsonObject], dict[str, JsonObject]]:
    source = Path(path)
    try:
        canonical_artifact_file_sha256(source, label="active tool schema")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, FrozenArtifactError) as exc:
        raise ValueError(f"cannot load active tool schema {source}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != "vtrade-kalshi-tools-v1":
        raise ValueError("tool schema artifact is not vtrade-kalshi-v1")
    rows = raw.get("tools")
    shared_defs = raw.get("$defs", {})
    if not isinstance(rows, list) or not isinstance(shared_defs, Mapping):
        raise ValueError("tool schema artifact lacks tools or definitions")
    schemas: dict[str, JsonObject] = {}
    outputs: dict[str, JsonObject] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            raise ValueError("tool schema row is malformed")
        name = str(row["name"])
        if name in schemas:
            raise ValueError(f"duplicate tool schema {name}")
        description = row.get("description")
        input_schema = row.get("input_schema")
        output_schema = row.get("output_schema")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"tool {name} lacks description")
        if not isinstance(input_schema, Mapping) or not isinstance(output_schema, Mapping):
            raise ValueError(f"tool {name} lacks input/output schema")
        schemas[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": _attach_defs(cast(JsonObject, dict(input_schema)), shared_defs),
            },
        }
        outputs[name] = _attach_defs(cast(JsonObject, dict(output_schema)), shared_defs)
    if len(schemas) != 27:
        raise ValueError("active schema must define exactly 27 tools")
    return schemas, outputs


def _attach_defs(schema: JsonObject, shared_defs: Mapping[str, object]) -> JsonObject:
    copied = cast(JsonObject, json.loads(json.dumps(schema)))
    if "$ref" in copied or _contains_ref(copied):
        copied["$defs"] = json.loads(json.dumps(shared_defs))
    return copied


def _contains_ref(value: object) -> bool:
    if isinstance(value, Mapping):
        return "$ref" in value or any(_contains_ref(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_ref(child) for child in value)
    return False


_MARKET_SELECT = (
    "SELECT m.market_ref, s.series_ref, e.event_ref, e.title, e.category, m.question, "
    "m.open_time, m.close_time, m.volume_units, m.liquidity_micros, m.lifecycle_status, "
    "state.eligible, state.tradeable, m.observed_at, m.source_updated_at, m.raw_artifact_id, "
    "ra.sha256, ra.observed_at, COALESCE((SELECT jsonb_agg(jsonb_build_object("
    "'outcome', o.outcome_side, 'label', o.label, 'eligible', o.eligible, "
    "'indicative_price_micros', CASE WHEN o.outcome_side = 'YES' "
    "THEN metric.indicative_yes_price_micros ELSE metric.indicative_no_price_micros END) "
    "ORDER BY o.outcome_side) FROM outcomes o WHERE o.market_id = m.id), '[]'::jsonb), "
    "m.resolution_rules, metric.volume_24h_units, metric.volatility_micros, "
    "metric.volume_trend, metric.volume_trend_delta, metric.competitive_score, "
    "metric.indicative_yes_price_micros, metric.indicative_no_price_micros, "
    "series_metadata.tags, state.fee_policy_status, state.fee_policy_reason "
    "FROM markets m JOIN series s ON s.id = m.series_id JOIN events e ON e.id = m.event_id "
    "JOIN market_freezes mf ON mf.agent_cycle_id = %s "
    "AND mf.publication_status = 'published' "
    "JOIN frozen_market_states state ON state.freeze_id = mf.id AND state.market_id = m.id "
    "JOIN raw_artifacts ra ON ra.id = m.raw_artifact_id "
    "LEFT JOIN market_metric_snapshots metric ON metric.freeze_id = mf.id "
    "AND metric.market_id = m.id "
    "LEFT JOIN series_metadata_snapshots series_metadata ON series_metadata.freeze_id = mf.id "
    "AND series_metadata.series_id = s.id "
    "WHERE m.venue = 'kalshi' AND m.kind = 'binary' AND mf.data_cutoff <= %s"
)
_MARKET_RESOLUTION_RULES_INDEX = 19
_MARKET_VOLUME_24H_INDEX = 20
_MARKET_VOLATILITY_INDEX = 21
_MARKET_TREND_INDEX = 22
_MARKET_TREND_DELTA_INDEX = 23
_MARKET_COMPETITIVE_SCORE_INDEX = 24
_MARKET_INDICATIVE_YES_INDEX = 25
_MARKET_INDICATIVE_NO_INDEX = 26
_MARKET_TAGS_INDEX = 27
_MARKET_FEE_POLICY_STATUS_INDEX = 28
_MARKET_FEE_POLICY_REASON_INDEX = 29


def _market_card(row: Sequence[object]) -> JsonObject:
    outcomes_value = row[18]
    outcomes: list[JsonObject] = []
    if isinstance(outcomes_value, (list, tuple)):
        for item in outcomes_value:
            if isinstance(item, Mapping):
                outcomes.append(
                    {
                        "outcome": str(item.get("outcome")),
                        "label": str(item.get("label", item.get("outcome"))),
                        "eligible": bool(item.get("eligible", False)),
                        "indicative_price_micros": (
                            None
                            if item.get("indicative_price_micros") is None
                            else _as_int(item["indicative_price_micros"])
                        ),
                    }
                )
    if len(outcomes) != 2:
        outcomes = [
            {
                "outcome": "YES",
                "label": "YES",
                "eligible": bool(row[11]),
                "indicative_price_micros": None,
            },
            {
                "outcome": "NO",
                "label": "NO",
                "eligible": bool(row[11]),
                "indicative_price_micros": None,
            },
        ]
    return {
        "market_ref": str(row[0]),
        "series_ref": str(row[1]),
        "event_ref": str(row[2]),
        "question": str(row[5]),
        "open_time": _iso(row[6]),
        "close_time": _iso(row[7]),
        "volume_units": _as_int(row[8]),
        "volume_24h_units": _row_volume_24h(row),
        "liquidity_micros": _as_int(row[9]),
        "status": str(row[10]).upper(),
        "eligible": bool(row[11]),
        "tradeable": bool(row[12]),
        "fee_policy_status": (
            str(row[_MARKET_FEE_POLICY_STATUS_INDEX])
            if len(row) > _MARKET_FEE_POLICY_STATUS_INDEX
            and row[_MARKET_FEE_POLICY_STATUS_INDEX] is not None
            else None
        ),
        "fee_policy_reason": (
            str(row[_MARKET_FEE_POLICY_REASON_INDEX])
            if len(row) > _MARKET_FEE_POLICY_REASON_INDEX
            and row[_MARKET_FEE_POLICY_REASON_INDEX] is not None
            else None
        ),
        "volatility_micros": _row_volatility(row),
        "volume_trend": _row_trend(row),
        "volume_trend_delta": _row_trend_delta(row),
        "competitive_score": _row_competitive_score(row),
        "tag_names": _tags(row),
        "outcomes": outcomes,
        "audit": _audit(row),
    }


def _audit(row: Sequence[object]) -> JsonObject:
    return {
        "artifact_id": str(row[15]) if len(row) > 15 and row[15] is not None else None,
        "sha256": str(row[16]) if len(row) > 16 and row[16] is not None else None,
        "observed_at": _iso(row[17]) if len(row) > 17 else None,
    }


def _tags(row: Sequence[object]) -> list[str]:
    value = row[_MARKET_TAGS_INDEX] if len(row) > _MARKET_TAGS_INDEX else None
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolContextUnavailable("series tags are malformed") from exc
    if not isinstance(value, (list, tuple)):
        raise ToolContextUnavailable("series tags are malformed")
    if any(not isinstance(tag, str) or not tag.strip() for tag in value):
        raise ToolContextUnavailable("series tags are malformed")
    return [str(tag) for tag in value]


def _page(
    item_key: str,
    items: Sequence[JsonObject],
    *,
    name: str,
    arguments: Mapping[str, object],
    cutoff: datetime,
    maximum_tokens: int,
) -> JsonObject:
    limit = _limit(arguments)
    offset = _cursor_offset(arguments.get("cursor"), name, cutoff)
    selected = list(items[offset : offset + limit])
    has_more = offset + len(selected) < len(items)
    truncated = False
    while True:
        output: JsonObject = {
            "as_of": cutoff.isoformat(),
            "data_cutoff": cutoff.isoformat(),
            item_key: selected,
            "next_cursor": _cursor(name, cutoff, offset + len(selected)) if has_more else None,
            "has_more": has_more,
            "payload_truncated": truncated,
        }
        if _output_tokens(output) <= maximum_tokens:
            return output
        if selected:
            selected.pop()
            has_more = True
            truncated = True
            continue
        raise ToolContextUnavailable("one tool page cannot fit the configured result ceiling")


def _cursor(name: str, cutoff: datetime, offset: int) -> str:
    payload = json.dumps(
        {"tool": name, "cutoff": cutoff.isoformat(), "offset": offset},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _cursor_offset(value: object, name: str, cutoff: datetime) -> int:
    if value is None:
        return 0
    if not isinstance(value, str) or not value:
        raise ValueError("cursor must be an opaque non-empty string")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if (
        not isinstance(decoded, Mapping)
        or decoded.get("tool") != name
        or decoded.get("cutoff") != cutoff.isoformat()
    ):
        raise ValueError("cursor is foreign to this tool or cutoff")
    offset = decoded.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("cursor offset is invalid")
    return offset


def _execution_output(
    result: OrderResult, *, audit: Mapping[str, object] | None = None
) -> JsonObject:
    request = result.request
    trade_fee_micros = sum(int(fill.trade_fee_micros) for fill in result.fills)
    rounding_fee_micros = sum(int(fill.rounding_fee_micros) for fill in result.fills)
    rebate_micros = sum(int(fill.rebate_micros) for fill in result.fills)
    fee_policy_fingerprint = result.fee_policy_fingerprint or next(
        (
            fill.fee_policy_fingerprint
            for fill in result.fills
            if fill.fee_policy_fingerprint is not None
        ),
        None,
    )
    fee_evidence_references = [
        dict(reference) for reference in result.fee_policy_evidence_references
    ]
    fills = [
        {
            "fill_id": fill.fill_id,
            "contract_units": int(fill.contract_units),
            "price_micros": int(fill.price_micros),
            "gross_cash_micros": int(fill.gross_cash_micros),
            "fee_micros": int(fill.fee_micros),
            "trade_fee_micros": int(fill.trade_fee_micros),
            "rounding_fee_micros": int(fill.rounding_fee_micros),
            "rebate_micros": int(fill.rebate_micros),
            "fee_policy_fingerprint": fill.fee_policy_fingerprint,
            "net_cash_delta_micros": int(fill.net_cash_delta_micros),
            "filled_at": fill.filled_at.isoformat(),
            "audit": dict(audit)
            if audit is not None
            else {
                "artifact_id": None,
                "sha256": fill.fingerprint,
                "observed_at": fill.filled_at.isoformat(),
            },
        }
        for fill in result.fills
    ]
    error_code = result.error_code
    return {
        "contract_version": result.contract_version,
        "operation_id": result.operation_id,
        "status": result.status.value,
        "reconciliation_state": result.reconciliation.value,
        "request": {
            "market_ref": request.market_key.market_ref,
            "outcome": str(request.outcome),
            "action": request.side.value,
            "amount": str(int(request.amount)),
            "amount_type": request.amount_kind.value,
            "limit_price_micros": (
                None if request.limit_price_micros is None else int(request.limit_price_micros)
            ),
            "time_in_force": TimeInForce(request.time_in_force).value,
            "idempotency_key": request.idempotency_key,
        },
        "requested_contract_units": int(result.requested_units),
        "filled_contract_units": int(result.filled_units),
        "remaining_contract_units": int(result.remaining_units),
        "cancelled_contract_units": int(result.cancelled_units),
        "fills": fills,
        "gross_cash_delta_micros": int(result.gross_cash_delta_micros),
        "fee_micros": int(result.fee_micros),
        "fee_components": {
            "trade_fee_micros": trade_fee_micros,
            "rounding_fee_micros": rounding_fee_micros,
            "rebate_micros": rebate_micros,
            "net_fee_micros": int(result.fee_micros),
        },
        "fee_policy_fingerprint": fee_policy_fingerprint,
        "fee_policy_evidence_references": fee_evidence_references,
        "net_cash_delta_micros": int(result.net_cash_delta_micros),
        "frozen_context_id": result.frozen_context_id,
        "execution_context_id": result.execution_context_id,
        "submitted_at": result.submitted_at.isoformat(),
        "updated_at": result.updated_at.isoformat(),
        "error_code": None if error_code is None else str(error_code),
        "message": result.message,
        "audit": [dict(audit)] if audit is not None else [],
    }


def _required_string(
    arguments: Mapping[str, object], key: str, *, max_length: int | None = None
) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    result = value.strip()
    if max_length is not None and len(result) > max_length:
        raise ValueError(f"{key} exceeds its maximum length")
    return result


def _positive_integer_string(value: object, name: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        raise ValueError(f"{name} must be a positive decimal string")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _exact_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an exact integer")
    if not 0 <= value <= 1_000_000:
        raise ValueError(f"{name} must be between zero and one dollar")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _as_int(value: object) -> int:
    return int(cast(Any, value))


def _decimal(value: object, name: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be an exact decimal string")
    try:
        parsed = Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an exact decimal") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise ValueError(f"{name} is outside its exact range")
    return parsed


def _unit_interval(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name, minimum=Decimal(0))
    if parsed > Decimal(1):
        raise ValueError(f"{name} must be between zero and one")
    return parsed


def _belief_category(value: object) -> str:
    category = _required_string({"category": value}, "category")
    if category not in BELIEF_CATEGORIES:
        raise ValueError(f"category must be one of {BELIEF_CATEGORIES}")
    return category


def _evidence(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("evidence must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("evidence must contain non-empty strings")
        result.append(item.strip())
    return tuple(result)


def _date_at_midnight(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("cycle_date must be an ISO date")
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("cycle_date must be an ISO date") from exc


def _keywords(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ValueError("keyword must be a string or an array of strings")
    result = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("keyword must contain non-empty strings")
        result.append(item.strip().casefold())
    return tuple(dict.fromkeys(result))


def _limit(arguments: Mapping[str, object]) -> int:
    value = arguments.get("limit", 100)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ValueError("limit must be an integer between 1 and 200")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ToolContextUnavailable(f"{field} is malformed")
    return _aware(value)


def _optional_datetime(value: object, field: str) -> datetime | None:
    return None if value is None else _datetime(value, field)


def _nullable_market_question(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return str(value)


def _datetime_key(value: object) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    return datetime.min.replace(tzinfo=UTC)


def _hours_until(value: object, cutoff: datetime) -> Decimal:
    if not isinstance(value, datetime):
        return Decimal("Infinity")
    return Decimal(str((_aware(value) - cutoff).total_seconds())) / Decimal(3600)


def _within_hours(value: object, cutoff: datetime, maximum: Decimal) -> bool:
    remaining = Decimal(str((cutoff - _datetime_key(value)).total_seconds())) / Decimal(3600)
    return Decimal(0) <= remaining <= maximum


def _row_value(row: Sequence[object], index: int) -> object | None:
    return row[index] if len(row) > index else None


def _row_volume_24h(row: Sequence[object]) -> int | None:
    value = _row_value(row, _MARKET_VOLUME_24H_INDEX)
    if value is None:
        return None
    parsed = _as_int(value)
    if parsed < 0:
        raise ToolContextUnavailable("market metrics contain negative volume_24h_units")
    return parsed


def _row_volatility(row: Sequence[object]) -> int | None:
    value = _row_value(row, _MARKET_VOLATILITY_INDEX)
    if value is None:
        return None
    parsed = _as_int(value)
    if parsed < 0:
        raise ToolContextUnavailable("market metrics contain negative volatility")
    return parsed


def _row_trend(row: Sequence[object]) -> str | None:
    value = _row_value(row, _MARKET_TREND_INDEX)
    if value is None:
        return None
    trend = str(value)
    if trend not in {"increasing", "decreasing", "flat", "insufficient_data"}:
        raise ToolContextUnavailable("market metrics contain an unknown volume trend")
    return trend


def _row_trend_delta(row: Sequence[object]) -> str | None:
    value = _row_value(row, _MARKET_TREND_DELTA_INDEX)
    if value is None:
        return None
    return format_metric_decimal(_decimal(value, "volume_trend_delta"))


def _row_competitive_score(row: Sequence[object]) -> str | None:
    value = _row_value(row, _MARKET_COMPETITIVE_SCORE_INDEX)
    if value is None:
        return None
    score = _decimal(value, "competitive_score", minimum=Decimal(0))
    if score > Decimal(1):
        raise ToolContextUnavailable("market metrics contain a competitive score above one")
    return format_metric_decimal(score)


def _average_price(gross: object, units: object) -> int:
    count = _as_int(units)
    return 0 if count == 0 else int(Decimal(_as_int(gross)) * Decimal(100) / Decimal(count))


def _output_tokens(value: object) -> int:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return max(1, (len(raw.encode("utf-8")) + 3) // 4)


def _bounded_output(value: JsonObject, maximum_tokens: int) -> JsonObject:
    """Compact a non-paginated result until it fits the conservative token ceiling."""
    copied = cast(JsonObject, json.loads(json.dumps(value, ensure_ascii=False, default=str)))
    if _output_tokens(copied) <= maximum_tokens:
        return copied

    # Web outputs keep their useful data below nested result/full_text/highlights
    # fields. Compact all nested strings before removing list entries so a large
    # result does not become an avoidable tool failure. The non-paginated output
    # schemas do not declare payload_truncated; pagination adds that marker in
    # _page(), so this helper communicates compaction through the ellipsis itself.
    _clip_strings(copied)
    while _output_tokens(copied) > maximum_tokens:
        lists: list[list[object]] = []
        strings: list[tuple[dict[str, object], str, str]] = []

        def collect(
            item: object,
            target_lists: list[list[object]],
            target_strings: list[tuple[dict[str, object], str, str]],
        ) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(child, str):
                        target_strings.append((item, key, child))
                    else:
                        collect(child, target_lists, target_strings)
            elif isinstance(item, list):
                if item:
                    target_lists.append(item)
                for child in item:
                    collect(child, target_lists, target_strings)

        collect(copied, lists, strings)
        if lists:
            target = max(
                lists,
                key=lambda rows: max(
                    (_output_tokens(item) for item in rows if isinstance(item, dict)),
                    default=len(rows),
                ),
            )
            target.pop()
            continue
        shrinkable = [item for item in strings if len(item[2]) > 32]
        if shrinkable:
            parent, key, raw = max(shrinkable, key=lambda item: len(item[2]))
            length = max(32, len(raw) // 2)
            parent[key] = raw[: length - 3] + "..."
            continue
        raise ToolContextUnavailable("tool result cannot fit its configured token ceiling")
    return copied


def _clip_strings(item: object, maximum_length: int = 512) -> None:
    if isinstance(item, dict):
        for key, child in tuple(item.items()):
            if isinstance(child, str) and len(child) > maximum_length:
                item[key] = child[: maximum_length - 3] + "..."
            else:
                _clip_strings(child, maximum_length)
    elif isinstance(item, list):
        for child in item:
            _clip_strings(child, maximum_length)


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def production_tool_context(
    database_url: str,
    claim: CycleClaim,
    exa: ExaResearchProvider,
    *,
    frozen: Mapping[str, object],
    clock: Callable[[], datetime],
    maximum_beliefs_per_agent: int = 100,
    maximum_book_age: timedelta = timedelta(minutes=5),
    maximum_order_book_depth: int = 6,
    immediate_order_executor: _OrderExecutor | None = None,
    live_order_execution: bool = False,
    live_order_required: bool = False,
    connect: _Connect | None = None,
    fixture_manifest_path: str | Path = ACTIVE_FIXTURE_MANIFEST,
) -> ToolContext:
    try:
        require_kalshi_fixture_manifest(fixture_manifest_path)
    except ValueError as exc:
        raise ToolContextUnavailable(str(exc)) from exc
    connector = connect or _default_connect
    _optional_uuid_list(frozen, "market_snapshot_ids")
    _optional_uuid_list(frozen, "order_book_snapshot_ids")
    _optional_uuid_list(frozen, "fee_rate_snapshot_ids")
    return ToolContext(
        database_url,
        claim,
        exa,
        PostgresHarnessRepository(
            database_url,
            maximum_beliefs_per_agent=maximum_beliefs_per_agent,
            connect=cast(Any, connect),
        ),
        PostgresContractPortfolioHandler(
            database_url,
            agent_id=claim.agent_id,
            connect=connector,
        ),
        connector,
        clock,
        fixture_manifest_path=fixture_manifest_path,
        maximum_book_age=maximum_book_age,
        maximum_order_book_depth=maximum_order_book_depth,
        immediate_order_executor=immediate_order_executor,
        live_order_execution=live_order_execution,
        live_order_required=live_order_required,
    )


def _optional_uuid_list(value: Mapping[str, object], key: str) -> tuple[uuid.UUID, ...]:
    rows = value.get(key)
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise ToolContextUnavailable(f"cycle freeze field {key} must be an array")
    try:
        parsed = tuple(uuid.UUID(str(item)) for item in rows)
    except ValueError as exc:
        raise ToolContextUnavailable(f"cycle freeze field {key} is malformed") from exc
    if len(set(parsed)) != len(parsed):
        raise ToolContextUnavailable(f"cycle freeze field {key} contains duplicates")
    return parsed
