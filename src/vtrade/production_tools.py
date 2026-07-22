from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from vtrade.domain.ports import JsonObject
from vtrade.harness import (
    BeliefRecord,
    PlanRecord,
    PlanType,
    ToolExecution,
    ToolHandlerError,
    ToolSpec,
)
from vtrade.harness_repository import PostgresHarnessRepository
from vtrade.portfolio import PostgresPortfolioHandler
from vtrade.providers import ExaResearchProvider
from vtrade.runtime import CycleClaim


class ToolContextUnavailable(ToolHandlerError):
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
    market_snapshot_ids: tuple[uuid.UUID, ...] = ()
    order_book_snapshot_ids: tuple[uuid.UUID, ...] = ()
    maximum_default_result_tokens: int = 4_000
    maximum_book_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not self.database_url or self.claim.data_cutoff is None:
            raise ToolContextUnavailable("tools require a finalized cycle cutoff")
        if self.maximum_default_result_tokens <= 0:
            raise ToolContextUnavailable("tool result ceiling must be positive")
        if self.maximum_book_age < timedelta(0):
            raise ToolContextUnavailable("order-book age ceiling cannot be negative")

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
        self._mutation_sequence = 0
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
                self._bounded_handler(name, handlers[name]),
                self._category(name),
                mutates_financial_state=name == "place_market_order",
            )
            for name in self._schemas
        )

    def _bounded_handler(
        self,
        name: str,
        handler: Callable[[JsonObject], JsonObject | ToolExecution],
    ) -> Callable[[JsonObject], JsonObject | ToolExecution]:
        if name == "get_portfolio":
            return handler

        def bounded(arguments: JsonObject) -> JsonObject | ToolExecution:
            result = handler(arguments)
            if isinstance(result, ToolExecution):
                return ToolExecution(
                    _bounded_output(
                        result.output,
                        self._context.maximum_default_result_tokens,
                    ),
                    result.telemetry,
                )
            return _bounded_output(result, self._context.maximum_default_result_tokens)

        return bounded

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
            lookup_key, lookup_value = _market_lookup(arguments)
            if lookup_key == "slug":
                predicate = "snapshot.payload->>'slug' = %s"
            elif lookup_key == "market_ref":
                predicate = "COALESCE(snapshot.payload->>'venue_market_id', m.id::text) = %s"
            else:
                predicate = "m.id::text = %s"
            rows = self._query(
                _MARKET_SELECT + " AND " + predicate + " "
                "ORDER BY snapshot.cutoff DESC LIMIT 1",
                (self._context.cutoff, list(self._context.market_snapshot_ids), lookup_value),
            )
            if not rows:
                raise ToolContextUnavailable("market is absent from frozen snapshots")
            market = _market_row(rows[0])
            market["canonical_slug"] = market["slug"]
            return {"as_of": self._context.cutoff.isoformat(), "market": market}
        if name == "get_event_markets":
            event_id = _required_string(arguments, "event_id")
            rows = self._query(
                _MARKET_SELECT
                + " AND (snapshot.payload->>'event_id' = %s OR e.venue_event_id = %s) "
                "ORDER BY snapshot.volume_micros DESC LIMIT 100",
                (
                    self._context.cutoff,
                    list(self._context.market_snapshot_ids),
                    event_id,
                    event_id,
                ),
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
            cast(list[JsonObject], event["markets"]).append(_discovery_card(row))
            event["volume_24hr_micros"] = int(event["volume_24hr_micros"]) + _metadata_money(
                row[12], "volume_24hr"
            )
            event["total_volume_micros"] = int(event["total_volume_micros"]) + int(str(row[8]))
            created = _metadata_string(row[12], "created_at")
            if created and (
                event["newest_market_created_at"] is None
                or created > str(event["newest_market_created_at"])
            ):
                event["newest_market_created_at"] = created
        values = list(grouped.values())
        if name == "get_newest_events":
            values.sort(
                key=lambda event: str(event["newest_market_created_at"] or ""), reverse=True
            )
        elif name == "discover_events":
            values.sort(key=lambda event: int(event["volume_24hr_micros"]), reverse=True)
        else:
            values.sort(key=lambda event: int(event["total_volume_micros"]), reverse=True)
        return {"as_of": self._context.cutoff.isoformat(), "events": values[:limit]}

    def _market_rows(self, limit: int) -> Sequence[Sequence[object]]:
        return self._query(
            _MARKET_SELECT + " AND snapshot.status = 'open' "
            "AND COALESCE((snapshot.payload->>'tradeable')::boolean, false) "
            "ORDER BY snapshot.volume_micros DESC, m.id LIMIT %s",
            (self._context.cutoff, list(self._context.market_snapshot_ids), limit),
        )

    def _market_output(self, rows: Sequence[Sequence[object]]) -> JsonObject:
        return {
            "as_of": self._context.cutoff.isoformat(),
            "markets": [_discovery_card(row) for row in rows],
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
        lookup_key, lookup_value = _orderbook_lookup(arguments)
        predicate = (
            "o.id = %s::uuid"
            if lookup_key == "outcome_id"
            else "o.venue_token_id = %s"
        )
        rows = self._query(
            "SELECT obs.id, obs.cutoff, obs.source_created_at, obs.bids, obs.asks, "
            "obs.best_bid, obs.best_ask, obs.raw_sha256 FROM order_book_snapshots obs "
            "JOIN outcomes o ON o.id = obs.outcome_id WHERE " + predicate + " "
            "AND obs.id = ANY(%s::uuid[]) AND obs.cutoff <= %s "
            "ORDER BY obs.cutoff DESC, obs.id DESC LIMIT 1",
            (
                lookup_value,
                list(self._context.order_book_snapshot_ids),
                self._context.cutoff,
            ),
        )
        if not rows:
            raise ToolContextUnavailable("token has no order book frozen at cycle cutoff")
        row = rows[0]
        observed_at = row[1]
        if not isinstance(observed_at, datetime):
            raise ToolContextUnavailable("frozen order-book timestamp is malformed")
        observed_at = observed_at.astimezone(UTC)
        source_created_at = row[2]
        if source_created_at is not None:
            if not isinstance(source_created_at, datetime):
                raise ToolContextUnavailable("frozen order-book source timestamp is malformed")
            source_created_at = source_created_at.astimezone(UTC)
            if source_created_at > observed_at or source_created_at > self._context.cutoff:
                raise ToolContextUnavailable("frozen order book violates cutoff causality")
        if self._context.cutoff - observed_at > self._context.maximum_book_age:
            raise ToolContextUnavailable("frozen order book is older than 300 seconds")
        return {
            "as_of": self._context.cutoff.isoformat(),
            "snapshot_id": str(row[0]),
            "observed_at": observed_at.isoformat(),
            "source_created_at": source_created_at.isoformat() if source_created_at else None,
            "lookup": {lookup_key: lookup_value},
            "bids": _book_levels(row[3]),
            "asks": _book_levels(row[4]),
            "best_bid": str(row[5]) if row[5] is not None else None,
            "best_ask": str(row[6]) if row[6] is not None else None,
            "raw_sha256": str(row[7]),
            "depth": 5,
        }

    def _get_balance(self, _arguments: JsonObject) -> JsonObject:
        rows = self._query(
            "SELECT COALESCE(sum(lp.amount_micros) FILTER "
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
            "SELECT oi.id, oi.validation_status, oi.shares, oi.created_at "
            "FROM order_intents oi JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id "
            "LEFT JOIN orders o ON o.intent_id = oi.id WHERE ac.agent_id = %s "
            "AND o.id IS NULL AND oi.validation_status = 'pending_broker_validation' "
            "ORDER BY oi.created_at, oi.id LIMIT 100",
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
        if not bool(arguments.get("include_inactive", False)):
            rows = self._context.memory.read_beliefs(
                actor_id=self._context.claim.agent_id,
                target_agent_id=self._context.claim.agent_id,
            )
            return {"beliefs": rows[: _limit(arguments, default=100)]}
        records = self._query(
            "SELECT b.id, b.active, r.probability, r.content, r.category, "
            "r.evidence, r.created_at FROM beliefs b JOIN LATERAL "
            "(SELECT * FROM belief_revisions WHERE belief_id = b.id "
            "ORDER BY revision DESC LIMIT 1) r ON true WHERE b.agent_id = %s "
            "ORDER BY r.created_at, b.id LIMIT %s",
            (self._context.claim.agent_id, _limit(arguments, default=100)),
        )
        return {
            "beliefs": [
                {
                    "id": str(row[0]),
                    "active": bool(row[1]),
                    "probability": str(row[2]),
                    "content": str(row[3]),
                    "category": str(row[4]),
                    "evidence": row[5],
                    "created_at": str(row[6]),
                }
                for row in records
            ]
        }

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
        confidence = _probability(arguments.get("confidence"), "confidence")
        now = self._context.now()
        belief = BeliefRecord(
            str(self._mutation_id("belief", arguments)),
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
                cursor.execute(
                    "SELECT active FROM beliefs WHERE id = %s AND agent_id = %s",
                    (belief_id, self._context.claim.agent_id),
                )
                existing = cursor.fetchall()
                if not existing:
                    raise ToolContextUnavailable("belief is missing or foreign")
                if bool(existing[0][0]):
                    raise ToolContextUnavailable("belief deletion did not persist")
                return {
                    "belief_id": str(belief_id),
                    "deleted": True,
                    "already_inactive": True,
                }
        return {"belief_id": str(belief_id), "deleted": True, "already_inactive": False}

    def _create_long_term_plan(self, arguments: JsonObject) -> JsonObject:
        return self._create_plan(PlanType.LONG_TERM, arguments, None)

    def _get_next_cycle_plan(self, _arguments: JsonObject) -> JsonObject:
        rows = self._context.memory.read_plans(
            actor_id=self._context.claim.agent_id, target_agent_id=self._context.claim.agent_id
        )
        return {"plans": [row for row in rows if row.get("plan_type") == PlanType.NEXT_CYCLE.value]}

    def _create_next_cycle_plan(self, arguments: JsonObject) -> JsonObject:
        due = arguments.get("cycle_date")
        due_at = _date_at_midnight_utc(due) if due else None
        return self._create_plan(PlanType.NEXT_CYCLE, arguments, due_at)

    def _create_plan(
        self, plan_type: PlanType, arguments: JsonObject, due_at: datetime | None
    ) -> JsonObject:
        now = self._context.now()
        plan = PlanRecord(
            str(self._mutation_id(f"plan:{plan_type.value}", arguments)),
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
        confidence = _probability(arguments.get("conviction", "0.5"), "conviction")
        rows = self._query(
            "SELECT o.id, o.market_id FROM outcomes o JOIN market_snapshots ms "
            "ON ms.market_id = o.market_id WHERE o.venue_token_id = %s "
            "AND ms.id = ANY(%s::uuid[]) AND ms.cutoff <= %s AND ms.status = 'open' "
            "AND COALESCE((ms.payload->>'tradeable')::boolean, false) "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(ms.payload->'outcomes') frozen "
            "WHERE frozen->>'venue_token_id' = %s "
            "AND COALESCE((frozen->>'tradeable')::boolean, false))",
            (
                token,
                list(self._context.market_snapshot_ids),
                self._context.cutoff,
                token,
            ),
        )
        if len(rows) != 1:
            raise ToolContextUnavailable("trade token is absent or not tradeable in frozen data")
        books = self._query(
            "SELECT obs.best_ask FROM order_book_snapshots obs JOIN outcomes o "
            "ON o.id = obs.outcome_id WHERE o.venue_token_id = %s "
            "AND obs.id = ANY(%s::uuid[]) AND obs.cutoff <= %s "
            "ORDER BY obs.cutoff DESC, obs.id DESC LIMIT 1",
            (
                token,
                list(self._context.order_book_snapshot_ids),
                self._context.cutoff,
            ),
        )
        if not books:
            raise ToolContextUnavailable(
                "trade token has no order book in the current-cycle frozen universe"
            )
        # The source traces do not publish ``amount`` semantics. V1 freezes the
        # Polymarket market-order convention: BUY is USD notional and SELL is shares.
        # For a quote-less BUY, the positive amount is retained only to let the broker
        # persist REQUIRED_QUOTE_ABSENT; it can never become a fill.
        best_ask = Decimal(str(books[0][0])) if books[0][0] is not None else None
        shares = amount
        if side == "BUY" and best_ask is not None and best_ask != 0:
            shares = amount / best_ask
        intent_id = self._mutation_id("intent", arguments)
        with (
            self._context.connect(self._context.database_url) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO order_intents "
                "(id, agent_cycle_id, market_id, outcome_id, side, amount_micros, shares, "
                "strategy, thesis, estimated_probability, expected_value_micros, "
                "validation_status, idempotency_key, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, "
                "'pending_broker_validation', %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    intent_id,
                    self._context.claim.cycle_id,
                    rows[0][1],
                    rows[0][0],
                    side,
                    int(amount * Decimal(1_000_000)),
                    shares,
                    "observed_place_market_order",
                    "submitted through frozen tool contract",
                    confidence,
                    f"intent:{intent_id}",
                    self._context.now(),
                ),
            )
        return {"intent_id": str(intent_id), "status": "pending_broker_validation"}

    def _mutation_id(self, kind: str, arguments: Mapping[str, object]) -> uuid.UUID:
        """Stable across a restart that replays the same ordered tool transcript."""
        sequence = self._mutation_sequence
        self._mutation_sequence += 1
        payload = json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
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
    "SELECT m.id, snapshot.payload->>'venue_market_id', snapshot.payload->>'slug', "
    "(snapshot.payload->>'event_id')::uuid, snapshot.payload->>'question', "
    "snapshot.payload->>'resolution_rules', "
    "NULLIF(snapshot.payload->>'opens_at', '')::timestamptz, "
    "NULLIF(snapshot.payload->>'closes_at', '')::timestamptz, "
    "snapshot.volume_micros, snapshot.liquidity_micros, snapshot.status, "
    "COALESCE((snapshot.payload->>'tradeable')::boolean, false), "
    "COALESCE(snapshot.payload->'metadata', '{}'::jsonb), "
    "COALESCE(snapshot.payload->'outcomes', '[]'::jsonb) "
    "FROM markets m JOIN events e ON e.id = m.event_id "
    "JOIN LATERAL (SELECT * FROM market_snapshots ms WHERE ms.market_id = m.id "
    "AND ms.cutoff <= %s AND ms.id = ANY(%s::uuid[]) "
    "ORDER BY ms.cutoff DESC, ms.id DESC LIMIT 1) snapshot ON true "
    "WHERE true"
)


def _market_row(row: Sequence[object]) -> JsonObject:
    raw_outcomes = row[13]
    if isinstance(raw_outcomes, list):
        outcomes: list[object] = []
        for o in raw_outcomes:
            if isinstance(o, dict):
                entry: JsonObject = dict(o)
                if "venue_token_id" in entry and "token_id" not in entry:
                    # Keep the legacy alias in full details only.
                    entry["token_id"] = str(entry["venue_token_id"])
                outcomes.append(entry)
            else:
                outcomes.append(o)
    else:
        outcomes = []
    return {
        "id": str(row[0]),
        "venue_market_id": str(row[1]),
        "market_ref": _market_ref(row),
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
        "outcomes": outcomes,
    }


_DISCOVERY_CARD_LOG = logging.getLogger("vtrade.discovery_card")


def _discovery_card(row: Sequence[object]) -> JsonObject:
    meta = _metadata(row[12])
    tag_names: list[str] = []
    raw_tags = meta.get("tags")
    tag_list = raw_tags if isinstance(raw_tags, list) else []
    for t in tag_list:
        if isinstance(t, dict):
            label = t.get("label") or t.get("name")
            if label:
                tag_names.append(str(label))
            else:
                _DISCOVERY_CARD_LOG.warning(
                    "skipping tag with no label/name: %s", t
                )
        elif isinstance(t, str):
            tag_names.append(t)
    raw_outcomes = row[13]
    outcomes: list[JsonObject] = []
    if isinstance(raw_outcomes, list):
        for o in raw_outcomes:
            if isinstance(o, dict):
                outcomes.append({
                    "name": o.get("name", ""),
                    "indicative_price": str(o.get("price", "")),
                })
    return {
        "market_ref": _market_ref(row),
        "question": str(row[4]),
        "closes_at": str(row[7]) if row[7] else None,
        "volume_24h_micros": _metadata_money(row[12], "volume_24hr"),
        "liquidity_micros": int(str(row[9])),
        "status": str(row[10]),
        "tradeable": bool(row[11]),
        "competitive": float(str(_metadata_decimal(row[12], "competitive"))),
        "tag_names": tag_names,
        "outcomes": outcomes,
    }


def _market_lookup(arguments: Mapping[str, object]) -> tuple[str, str]:
    supplied = [
        (key, arguments.get(key))
        for key in ("market_ref", "market_id", "slug")
        if isinstance(arguments.get(key), str) and str(arguments[key]).strip()
    ]
    if len(supplied) != 1:
        raise ValueError("exactly one of market_ref, market_id, or slug is required")
    key, value = supplied[0]
    return key, str(value).strip()


def _market_ref(row: Sequence[object]) -> str:
    venue_market_id = row[1]
    if venue_market_id is not None and str(venue_market_id).strip():
        return str(venue_market_id)
    return str(row[0])


def _orderbook_lookup(arguments: Mapping[str, object]) -> tuple[str, str]:
    supplied = [
        (key, arguments.get(key))
        for key in ("venue_token_id", "outcome_id", "token_id")
        if isinstance(arguments.get(key), str) and str(arguments[key]).strip()
    ]
    if len(supplied) != 1:
        raise ValueError(
            "exactly one of venue_token_id, outcome_id, or token_id is required"
        )
    key, value = supplied[0]
    return key, str(value).strip()


def _book_levels(value: object, maximum: int = 5) -> list[object]:
    if not isinstance(value, list):
        return []
    return value[:maximum]


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


def _probability(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite() or not Decimal(0) <= result <= Decimal(1):
        raise ValueError(f"{name} must be between zero and one")
    return result


def _date_at_midnight_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("cycle_date must be an ISO date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("cycle_date must be an ISO date") from exc
    return parsed.replace(tzinfo=UTC)


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


def _bounded_output(value: JsonObject, maximum_tokens: int) -> JsonObject:
    """Apply the harness' conservative UTF-8 upper bound before returning a tool result.

    The baseline schemas do not expose pagination for tools other than ``get_portfolio``.
    Lists are therefore shortened deterministically and long descriptive strings are
    clipped, while identifiers and scalar accounting fields remain intact.
    """
    copied = cast(
        JsonObject,
        json.loads(json.dumps(value, ensure_ascii=False, default=str)),
    )
    if _output_tokens(copied) <= maximum_tokens:
        return copied

    def clip_strings(item: object) -> None:
        if isinstance(item, dict):
            for key, child in tuple(item.items()):
                if isinstance(child, str) and len(child) > 512:
                    item[key] = child[:509] + "..."
                else:
                    clip_strings(child)
        elif isinstance(item, list):
            for child in item:
                clip_strings(child)

    clip_strings(copied)
    truncated = True
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
    if truncated and "truncated" not in copied:
        probe = {**copied, "truncated": True}
        if _output_tokens(probe) <= maximum_tokens:
            copied = probe
    return copied


def _output_tokens(value: object) -> int:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return max(1, (len(raw.encode("utf-8")) + 3) // 4)


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
) -> ToolContext:
    market_snapshot_ids = _uuid_list(frozen, "market_snapshot_ids")
    order_book_snapshot_ids = _uuid_list(frozen, "order_book_snapshot_ids")
    if not market_snapshot_ids:
        raise ToolContextUnavailable("market freeze contains no market snapshot membership")
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
        market_snapshot_ids,
        order_book_snapshot_ids,
    )


def _uuid_list(value: Mapping[str, object], key: str) -> tuple[uuid.UUID, ...]:
    rows = value.get(key)
    if not isinstance(rows, list):
        raise ToolContextUnavailable(f"market freeze lacks {key}")
    try:
        result = tuple(uuid.UUID(str(item)) for item in rows)
    except ValueError as exc:
        raise ToolContextUnavailable(f"market freeze has malformed {key}") from exc
    if len(set(result)) != len(result):
        raise ToolContextUnavailable(f"market freeze has duplicate {key}")
    return result
