from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from vtrade.domain.ports import JsonObject
from vtrade.harness import BeliefRecord, PlanRecord, PlanType, ToolExecution, ToolSpec
from vtrade.harness_repository import PostgresHarnessRepository
from vtrade.portfolio import PostgresPortfolioHandler
from vtrade.providers import ExaResearchProvider
from vtrade.runtime import CycleClaim


class ToolContextUnavailable(RuntimeError):
    pass


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class ToolContext:
    database_url: str
    claim: CycleClaim
    exa: ExaResearchProvider
    memory: PostgresHarnessRepository
    portfolio: PostgresPortfolioHandler
    connect: _Connect
    clock: Callable[[], datetime]

    def __post_init__(self) -> None:
        if not self.database_url or self.claim.data_cutoff is None:
            raise ToolContextUnavailable("tools require a finalized cycle cutoff")

    @property
    def cutoff(self) -> datetime:
        value = self.claim.data_cutoff
        if value is None:
            raise ToolContextUnavailable("cycle cutoff is not finalized")
        return value

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ToolContextUnavailable("tool clock must be timezone-aware")
        return value.astimezone(UTC)


class ProductionToolRegistry:
    """Exact 29-name registry backed only by frozen DB state and real providers."""

    def __init__(
        self,
        context: ToolContext,
        *,
        schema_path: str | Path = "spec/tool-schemas-v1.json",
    ) -> None:
        self._context = context
        raw = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        rows = raw.get("tools")
        if not isinstance(rows, list):
            raise ValueError("tool schema artifact lacks tools")
        self._schemas: dict[str, JsonObject] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ValueError("tool schema row is malformed")
            name = str(row["name"])
            parameters = row.get("input_schema")
            if not isinstance(parameters, dict):
                raise ValueError(f"tool {name} lacks input schema")
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"V-Trade frozen provider-neutral {name} tool.",
                    "parameters": parameters,
                },
            }
        expected = set(self._handlers())
        if set(self._schemas) != expected or len(expected) != 29:
            raise ValueError("production handlers must exactly match all 29 frozen tool names")

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        handlers = self._handlers()
        return tuple(
            ToolSpec(
                self._schemas[name],
                handlers[name],
                self._category(name),
                mutates_financial_state=name == "place_market_order",
            )
            for name in self._schemas
        )

    def _handlers(self) -> dict[str, Callable[[JsonObject], JsonObject | ToolExecution]]:
        discovery = {
            name: (lambda arguments, tool_name=name: self._discover(tool_name, arguments))
            for name in (
                "discover_hot_markets",
                "discover_by_time_remaining",
                "discover_events",
                "list_top_events",
                "get_market_details",
                "browse_markets_by_volume",
                "discover_by_price_volatility",
                "get_event_markets",
                "get_newest_events",
                "get_all_active_markets",
                "discover_by_volume_trend",
                "discover_by_competitive_score",
                "discover_by_date_range",
                "search_tags",
            )
        }
        return {
            **discovery,
            "web_search": self._web_search,
            "get_orderbook": self._get_orderbook,
            "get_balance": self._get_balance,
            "get_portfolio": self._context.portfolio,
            "get_open_orders": self._get_open_orders,
            "get_closed_trades": self._get_closed_trades,
            "get_settlements": self._get_settlements,
            "get_general_beliefs": self._get_beliefs,
            "search_general_beliefs": self._search_beliefs,
            "create_general_belief": self._create_belief,
            "delete_general_belief": self._delete_belief,
            "create_long_term_plan": self._create_long_term_plan,
            "get_next_cycle_plan": self._get_next_cycle_plan,
            "create_next_cycle_plan": self._create_next_cycle_plan,
            "place_market_order": self._place_market_order,
        }

    def _discover(self, name: str, arguments: JsonObject) -> JsonObject:
        if name in {"discover_events", "list_top_events", "get_newest_events"}:
            return self._discover_event_groups(name, arguments)
        if name == "get_market_details":
            slug = _required_string(arguments, "slug")
            rows = self._query(
                _MARKET_SELECT + " AND m.slug = %s ORDER BY snapshot.cutoff DESC LIMIT 1",
                (self._context.cutoff, slug),
            )
            if not rows:
                raise ToolContextUnavailable("market slug is absent from frozen snapshots")
            return {"as_of": self._context.cutoff.isoformat(), "market": _market_row(rows[0])}
        if name == "get_event_markets":
            event_id = _required_string(arguments, "event_id")
            rows = self._query(
                _MARKET_SELECT + " AND (e.id::text = %s OR e.venue_event_id = %s) "
                "ORDER BY snapshot.volume_micros DESC LIMIT 100",
                (self._context.cutoff, event_id, event_id),
            )
            return self._market_output(rows)
        if name == "search_tags":
            query = _required_string(arguments, "query").casefold()
            rows = self._market_rows(100)
            return self._market_output([row for row in rows if query in str(row[12]).casefold()])
        limit = _limit(arguments, default=20)
        rows = list(self._market_rows(100))
        keyword = str(arguments.get("keyword") or "").casefold()
        minimum_liquidity = _money_filter(arguments.get("min_liquidity", 0))
        minimum_volume = _money_filter(arguments.get("min_volume_24hr", 0))
        rows = [
            row
            for row in rows
            if int(str(row[9])) >= minimum_liquidity
            and _metadata_money(row[12], "volume_24hr") >= minimum_volume
            and (not keyword or keyword in f"{row[4]} {row[5]} {row[12]}".casefold())
        ]
        if name == "discover_by_time_remaining":
            minimum = Decimal(str(arguments.get("hours_min", 0)))
            maximum = Decimal(str(arguments.get("hours_max", "1e12")))
            rows = [
                row
                for row in rows
                if _hours_remaining(row[7], self._context.cutoff, minimum, maximum)
            ]
        elif name == "discover_by_date_range":
            start = str(arguments.get("start_date") or "")
            end = str(arguments.get("end_date") or "")
            rows = [
                row
                for row in rows
                if (not start or str(row[7])[:10] >= start) and (not end or str(row[7])[:10] <= end)
            ]
        elif name == "discover_by_price_volatility":
            minimum = Decimal(str(arguments.get("min_volatility", 0)))
            rows = [row for row in rows if _price_volatility(row[12]) >= minimum]
        elif name == "discover_by_volume_trend":
            trend = str(arguments.get("trend") or "increasing").casefold()
            if trend not in {"increasing", "decreasing"}:
                raise ValueError("trend must be increasing or decreasing")
            rows = [row for row in rows if _volume_trend(row[12]) == trend]
        elif name == "discover_by_competitive_score":
            minimum = Decimal(str(arguments.get("min_score", 0)))
            rows = [row for row in rows if _metadata_decimal(row[12], "competitive") >= minimum]
        elif name == "discover_hot_markets":
            hours = Decimal(str(arguments.get("hours_back", 24)))
            rows = [row for row in rows if _created_within(row[12], self._context.cutoff, hours)]
        elif name == "get_newest_events":
            rows.sort(key=lambda row: (str(row[6]), str(row[0])), reverse=True)
        else:
            rows.sort(
                key=lambda row: (int(str(row[8])), int(str(row[9])), str(row[0])), reverse=True
            )
        return self._market_output(rows[:limit])

    def _discover_event_groups(self, name: str, arguments: JsonObject) -> JsonObject:
        limit = _limit(arguments, default=20)
        keyword = str(arguments.get("keyword") or "").casefold()
        markets = list(self._market_rows(100))
        grouped: dict[str, JsonObject] = {}
        for row in markets:
            event_id = str(row[3])
            if keyword and keyword not in f"{row[4]} {row[12]}".casefold():
                continue
            event = grouped.setdefault(
                event_id,
                {
                    "event_id": event_id,
                    "markets": [],
                    "volume_24hr_micros": 0,
                    "total_volume_micros": 0,
                    "newest_market_created_at": None,
                },
            )
            cast(list[JsonObject], event["markets"]).append(_market_row(row))
            event["volume_24hr_micros"] = int(event["volume_24hr_micros"]) + _metadata_money(
                row[12], "volume_24hr"
            )
            event["total_volume_micros"] = int(event["total_volume_micros"]) + int(str(row[8]))
            created = _metadata_string(row[12], "created_at")
            if created and (event["newest_market_created_at"] is None or created > str(event["newest_market_created_at"])):
                event["newest_market_created_at"] = created
        values = list(grouped.values())
        if name == "get_newest_events":
            values.sort(key=lambda event: str(event["newest_market_created_at"] or ""), reverse=True)
        elif name == "discover_events":
            values.sort(key=lambda event: int(event["volume_24hr_micros"]), reverse=True)
        else:
            values.sort(key=lambda event: int(event["total_volume_micros"]), reverse=True)
        return {"as_of": self._context.cutoff.isoformat(), "events": values[:limit]}

    def _market_rows(self, limit: int) -> Sequence[Sequence[object]]:
        return self._query(
            _MARKET_SELECT + " AND m.tradeable = true AND m.status = 'open' "
            "ORDER BY snapshot.volume_micros DESC, m.id LIMIT %s",
            (self._context.cutoff, limit),
        )

    def _market_output(self, rows: Sequence[Sequence[object]]) -> JsonObject:
        return {
            "as_of": self._context.cutoff.isoformat(),
            "markets": [_market_row(row) for row in rows],
        }

    def _web_search(self, arguments: JsonObject) -> ToolExecution:
        response = self._context.exa.search(
            _required_string(arguments, "query"),
            {
                "num_results": 10,
                "include_domains": [],
                "exclude_domains": [],
            },
        )
        return ToolExecution(response.output, (response.telemetry,))

    def _get_orderbook(self, arguments: JsonObject) -> JsonObject:
        token = _required_string(arguments, "token_id")
        rows = self._query(
            "SELECT obs.id, obs.cutoff, obs.source_created_at, obs.bids, obs.asks, "
            "obs.best_bid, obs.best_ask, obs.raw_sha256 FROM order_book_snapshots obs "
            "JOIN outcomes o ON o.id = obs.outcome_id WHERE o.venue_token_id = %s "
            "AND obs.cutoff <= %s ORDER BY obs.cutoff DESC, obs.id DESC LIMIT 1",
            (token, self._context.cutoff),
        )
        if not rows:
            raise ToolContextUnavailable("token has no order book frozen at cycle cutoff")
        row = rows[0]
        return {
            "as_of": self._context.cutoff.isoformat(),
            "snapshot_id": str(row[0]),
            "observed_at": str(row[1]),
            "source_created_at": str(row[2]) if row[2] is not None else None,
            "bids": row[3],
            "asks": row[4],
            "best_bid": str(row[5]) if row[5] is not None else None,
            "best_ask": str(row[6]) if row[6] is not None else None,
            "raw_sha256": str(row[7]),
        }

    def _get_balance(self, _arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT a.initial_cash_micros + COALESCE(sum(lp.amount_micros) FILTER "
            "(WHERE lp.account = 'cash'), 0), a.portfolio_version FROM agents a "
            "LEFT JOIN ledger_entries le ON le.agent_id = a.id "
            "LEFT JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
            "WHERE a.id = %s GROUP BY a.id",
            (self._context.claim.agent_id,),
        )
        if not rows:
            raise ToolContextUnavailable("agent balance is unavailable")
        return {"cash_micros": int(str(rows[0][0])), "portfolio_version": int(str(rows[0][1]))}

    def _get_open_orders(self, _arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT o.id, o.status, o.requested_shares, o.created_at FROM orders o "
            "JOIN order_intents oi ON oi.id = o.intent_id JOIN agent_cycles ac "
            "ON ac.id = oi.agent_cycle_id WHERE ac.agent_id = %s "
            "AND o.status IN ('accepted', 'partial') ORDER BY o.created_at, o.id LIMIT 100",
            (self._context.claim.agent_id,),
        )
        return {"orders": [_named(row, ("id", "status", "shares", "created_at")) for row in rows]}

    def _get_closed_trades(self, arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT f.id, oi.side, f.shares, f.price, f.gross_micros, f.fee_micros, "
            "f.filled_at FROM fills f JOIN orders o ON o.id = f.order_id "
            "JOIN order_intents oi ON oi.id = o.intent_id JOIN agent_cycles ac "
            "ON ac.id = oi.agent_cycle_id WHERE ac.agent_id = %s "
            "ORDER BY f.filled_at DESC, f.id DESC LIMIT %s",
            (self._context.claim.agent_id, _limit(arguments, default=100)),
        )
        return {
            "trades": [
                _named(
                    row,
                    ("id", "side", "shares", "price", "gross_micros", "fee_micros", "filled_at"),
                )
                for row in rows
            ]
        }

    def _get_settlements(self, arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT id, shares, payout_micros, realized_pnl_micros, settled_at "
            "FROM settlements WHERE agent_id = %s ORDER BY settled_at DESC, id DESC LIMIT %s",
            (self._context.claim.agent_id, _limit(arguments, default=100)),
        )
        return {
            "settlements": [
                _named(row, ("id", "shares", "payout_micros", "realized_pnl_micros", "settled_at"))
                for row in rows
            ]
        }

    def _get_beliefs(self, arguments: JsonObject) -> JsonObject:
        rows = self._context.memory.read_beliefs(
            actor_id=self._context.claim.agent_id,
            target_agent_id=self._context.claim.agent_id,
        )
        if not bool(arguments.get("include_inactive", False)):
            rows = [row for row in rows if row.get("active", True)]
        return {"beliefs": rows[: _limit(arguments, default=100)]}

    def _search_beliefs(self, arguments: JsonObject) -> JsonObject:
        rows = cast(list[JsonObject], self._get_beliefs({"limit": 100})["beliefs"])
        keyword = str(arguments.get("keyword") or "").casefold()
        category = str(arguments.get("category") or "").casefold()
        matches = [
            row
            for row in rows
            if (not keyword or keyword in str(row.get("content", "")).casefold())
            and (not category or category == str(row.get("category", "")).casefold())
        ]
        return {"beliefs": matches[: _limit(arguments, default=100)]}

    def _create_belief(self, arguments: JsonObject) -> JsonObject:
        confidence = Decimal(str(arguments.get("confidence")))
        now = self._context.now()
        belief = BeliefRecord(
            str(uuid.uuid4()),
            str(self._context.claim.agent_id),
            confidence,
            _required_string(arguments, "belief_content"),
            _required_string(arguments, "category"),
            (),
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
            rows = cursor.fetchall()
        if not rows:
            raise ToolContextUnavailable("belief is missing, foreign, or already inactive")
        return {"belief_id": str(belief_id), "deleted": True}

    def _create_long_term_plan(self, arguments: JsonObject) -> JsonObject:
        return self._create_plan(PlanType.LONG_TERM, arguments, None)

    def _get_next_cycle_plan(self, _arguments: JsonObject) -> JsonObject:
        rows = self._context.memory.read_plans(
            actor_id=self._context.claim.agent_id, target_agent_id=self._context.claim.agent_id
        )
        return {"plans": [row for row in rows if row.get("plan_type") == PlanType.NEXT_CYCLE.value]}

    def _create_next_cycle_plan(self, arguments: JsonObject) -> JsonObject:
        due = arguments.get("cycle_date")
        due_at = datetime.fromisoformat(str(due)).replace(tzinfo=UTC) if due else None
        return self._create_plan(PlanType.NEXT_CYCLE, arguments, due_at)

    def _create_plan(
        self, plan_type: PlanType, arguments: JsonObject, due_at: datetime | None
    ) -> JsonObject:
        now = self._context.now()
        plan = PlanRecord(
            str(uuid.uuid4()),
            str(self._context.claim.agent_id),
            plan_type,
            _required_string(arguments, "plan_content"),
            due_at,
            now,
        )
        self._context.memory.append_plan(
            plan, actor_id=self._context.claim.agent_id, cycle_id=self._context.claim.cycle_id
        )
        return {"plan_id": plan.id, "created_at": now.isoformat()}

    def _place_market_order(self, arguments: JsonObject) -> JsonObject:
        token = _required_string(arguments, "token_id")
        side = _required_string(arguments, "side")
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        amount = _positive_decimal(arguments.get("amount"), "amount")
        confidence = Decimal(str(arguments.get("conviction", "0.5")))
        if not Decimal(0) <= confidence <= Decimal(1):
            raise ValueError("conviction must be between zero and one")
        rows = self._query(
            "SELECT o.id, o.market_id FROM outcomes o WHERE o.venue_token_id = %s "
            "AND o.tradeable = true",
            (token,),
        )
        if len(rows) != 1:
            raise ToolContextUnavailable("trade token is absent or not tradeable in frozen data")
        intent_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vtrade:intent:{self._context.claim.cycle_id}:{token}:{side}:{amount}",
        )
        with (
            self._context.connect(self._context.database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO order_intents "
                "(id, agent_cycle_id, market_id, outcome_id, side, amount_micros, shares, "
                "strategy, thesis, estimated_probability, expected_value_micros, "
                "validation_status, idempotency_key, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, 0, "
                "'pending_broker_validation', %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    intent_id,
                    self._context.claim.cycle_id,
                    rows[0][1],
                    rows[0][0],
                    side,
                    int(amount * Decimal(1_000_000)),
                    "observed_place_market_order",
                    "submitted through frozen tool contract",
                    confidence,
                    f"intent:{intent_id}",
                    self._context.now(),
                ),
            )
        return {"intent_id": str(intent_id), "status": "pending_broker_validation"}

    def _query(self, sql: str, params: Sequence[object]) -> Sequence[Sequence[object]]:
        with (
            self._context.connect(self._context.database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, params)
            return tuple(cursor.fetchall())

    @staticmethod
    def _category(name: str) -> str:
        if name == "web_search":
            return "research"
        if name == "place_market_order":
            return "financial"
        if "belief" in name or "plan" in name:
            return "memory"
        if name.startswith("get_") and name in {
            "get_balance",
            "get_portfolio",
            "get_open_orders",
            "get_closed_trades",
            "get_settlements",
        }:
            return "account"
        return "market"


_MARKET_SELECT = (
    "SELECT m.id, m.venue_market_id, m.slug, e.id, m.question, m.resolution_rules, "
    "m.opens_at, m.closes_at, snapshot.volume_micros, snapshot.liquidity_micros, "
    "m.status, m.tradeable, m.metadata FROM markets m JOIN events e ON e.id = m.event_id "
    "JOIN LATERAL (SELECT * FROM market_snapshots ms WHERE ms.market_id = m.id "
    "AND ms.cutoff <= %s ORDER BY ms.cutoff DESC, ms.id DESC LIMIT 1) snapshot ON true "
    "WHERE true"
)


def _market_row(row: Sequence[object]) -> JsonObject:
    return {
        "id": str(row[0]),
        "venue_market_id": str(row[1]),
        "slug": str(row[2]),
        "event_id": str(row[3]),
        "question": str(row[4]),
        "resolution_rules": str(row[5]),
        "opens_at": str(row[6]) if row[6] else None,
        "closes_at": str(row[7]) if row[7] else None,
        "volume_micros": int(str(row[8])),
        "liquidity_micros": int(str(row[9])),
        "status": str(row[10]),
        "tradeable": bool(row[11]),
        "metadata": row[12],
    }


def _named(row: Sequence[object], names: Sequence[str]) -> JsonObject:
    return {
        name: (str(value) if isinstance(value, (uuid.UUID, datetime, Decimal)) else value)
        for name, value in zip(names, row, strict=True)
    }


def _required_string(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _limit(arguments: Mapping[str, object], *, default: int) -> int:
    value = arguments.get("limit", default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _money_filter(value: object) -> int:
    return (
        int(_positive_decimal(value, "money") * Decimal(1_000_000))
        if Decimal(str(value)) > 0
        else 0
    )


def _hours_remaining(value: object, cutoff: datetime, minimum: Decimal, maximum: Decimal) -> bool:
    if not isinstance(value, datetime):
        return False
    hours = Decimal(str((value - cutoff).total_seconds())) / Decimal(3600)
    return minimum <= hours <= maximum


def _metadata(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _metadata_decimal(value: object, key: str) -> Decimal:
    raw = _metadata(value).get(key)
    if raw in (None, ""):
        return Decimal(0)
    try:
        result = Decimal(str(raw))
    except InvalidOperation:
        return Decimal(0)
    return result if result.is_finite() else Decimal(0)


def _metadata_money(value: object, key: str) -> int:
    amount = _metadata_decimal(value, key)
    return int(amount * Decimal(1_000_000)) if amount > 0 else 0


def _metadata_string(value: object, key: str) -> str | None:
    raw = _metadata(value).get(key)
    return raw if isinstance(raw, str) and raw else None


def _price_volatility(value: object) -> Decimal:
    return max(
        abs(_metadata_decimal(value, "one_hour_price_change")),
        abs(_metadata_decimal(value, "one_day_price_change")),
    )


def _volume_trend(value: object) -> str:
    daily = _metadata_decimal(value, "volume_24hr")
    weekly_daily_average = _metadata_decimal(value, "volume_1wk") / Decimal(7)
    return "increasing" if daily >= weekly_daily_average else "decreasing"


def _created_within(value: object, cutoff: datetime, hours: Decimal) -> bool:
    if hours < 0:
        raise ValueError("hours_back cannot be negative")
    raw = _metadata_string(value, "created_at")
    if raw is None:
        return False
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return False
    age_hours = Decimal(str((cutoff - created).total_seconds())) / Decimal(3600)
    return Decimal(0) <= age_hours <= hours


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def production_tool_context(
    database_url: str,
    claim: CycleClaim,
    exa: ExaResearchProvider,
    *,
    clock: Callable[[], datetime],
) -> ToolContext:
    return ToolContext(
        database_url,
        claim,
        exa,
        PostgresHarnessRepository(database_url),
        PostgresPortfolioHandler(
            database_url, agent_id=claim.agent_id, agent_cycle_id=claim.cycle_id
        ),
        _default_connect,
        clock,
    )
