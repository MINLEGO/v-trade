from __future__ import annotations

import json
import unittest
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from vtrade.production_tools import (
    ProductionToolRegistry,
    ToolContext,
    ToolContextUnavailable,
)
from vtrade.runtime import CycleClaim

NOW = datetime(2026, 7, 16, 10, 5, tzinfo=UTC)


class _Cursor:
    def __init__(
        self,
        *,
        book_observed_at: datetime = NOW,
        market_rows: list[tuple[object, ...]] | None = None,
        closed_trade_rows: list[tuple[object, ...]] | None = None,
        settlement_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.rows: list[tuple[object, ...]] = []
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.book_observed_at = book_observed_at
        self.market_rows = market_rows
        self.closed_trade_rows = closed_trade_rows
        self.settlement_rows = settlement_rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query: str, params: Sequence[object] = ()):
        self.queries.append((query, tuple(params)))
        if query.startswith("SELECT obs.id"):
            self.rows = [
                (
                    uuid.uuid4(),
                    self.book_observed_at,
                    self.book_observed_at,
                    [{"price": "0.49", "size": "10"}],
                    [{"price": "0.51", "size": "10"}],
                    Decimal("0.49"),
                    Decimal("0.51"),
                    "a" * 64,
                )
            ]
        elif query.startswith("SELECT o.id, o.market_id"):
            self.rows = [(uuid.uuid4(), uuid.uuid4())]
        elif query.startswith("SELECT obs.best_ask"):
            self.rows = [(Decimal("0.51"),)]
        elif query.startswith("SELECT m.id"):
            self.rows = self.market_rows or [
                (
                    uuid.uuid4(),
                    "venue-market",
                    "snapshot-slug",
                    uuid.uuid4(),
                    "Snapshot question",
                    "Snapshot rules",
                    NOW - timedelta(days=1),
                    NOW + timedelta(days=1),
                    1_000_000,
                    2_000_000,
                    "open",
                    True,
                    {"tags": [{"label": "Politics"}]},
                    [{"venue_token_id": "token", "name": "Yes"}],
                )
            ]
        elif query.startswith("SELECT b.id, b.active"):
            self.rows = [(uuid.uuid4(), False, Decimal("0.4"), "old", "macro", [], NOW)]
        elif query.startswith("WITH fill_events AS"):
            self.rows = self.closed_trade_rows or []
        elif query.startswith("SELECT s.id, s.position_id"):
            self.rows = self.settlement_rows or []
        else:
            self.rows = []
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


class _Memory:
    def __init__(
        self,
        beliefs: list[dict[str, object]],
        *,
        active_beliefs: list[dict[str, object]] | None = None,
    ) -> None:
        self.beliefs = beliefs
        self.active_beliefs = beliefs if active_beliefs is None else active_beliefs
        self.appended_beliefs = []

    def read_beliefs(self, *, actor_id: uuid.UUID, target_agent_id: uuid.UUID):
        assert actor_id == target_agent_id
        return list(self.active_beliefs)

    def append_belief(self, belief, *, actor_id: uuid.UUID, cycle_id: uuid.UUID):
        assert actor_id
        assert cycle_id
        self.appended_beliefs.append(belief)


def _context(
    cursor: _Cursor,
    *,
    cutoff=NOW,
    maximum_default_result_tokens: int = 4_000,
    memory: _Memory | None = None,
) -> ToolContext:
    claim = CycleClaim(
        uuid.uuid4(),
        uuid.uuid4(),
        NOW - timedelta(minutes=1),
        cutoff,
        "worker",
        NOW + timedelta(minutes=10),
    )
    connection = _Connection(cursor)
    market_snapshot_id = uuid.uuid4()
    book_snapshot_id = uuid.uuid4()
    return ToolContext(
        "postgresql://unused",
        claim,
        cast(Any, object()),
        cast(Any, memory if memory is not None else object()),
        cast(Any, lambda arguments: {"items": [], "has_more": False}),
        lambda _url: connection,
        lambda: NOW,
        (market_snapshot_id,),
        (book_snapshot_id,),
        maximum_default_result_tokens=maximum_default_result_tokens,
    )


class ProductionToolRegistryTests(unittest.TestCase):
    def test_registry_has_exact_schema_parity_for_all_27_names(self) -> None:
        expected = {
            row["name"]
            for row in json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))[
                "tools"
            ]
        }
        names = {tool.name for tool in ProductionToolRegistry(_context(_Cursor())).tool_specs()}
        self.assertEqual(len(names), 27)
        self.assertEqual(names, expected)

    def test_orderbook_reads_only_snapshot_at_finalized_cutoff(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        output = tools["get_orderbook"].handler({"token_id": "token"})
        self.assertEqual(output["best_bid"], "0.49")
        self.assertEqual(output["lookup"], {"token_id": "token"})
        self.assertEqual(output["depth"], 5)
        query, params = cursor.queries[0]
        self.assertIn("obs.cutoff <= %s", query)
        self.assertEqual(params[0], "token")
        self.assertEqual(len(params[1]), 1)
        self.assertEqual(params[2], NOW)
        self.assertIn("obs.id = ANY(%s::uuid[])", query)

    def test_closed_trades_aggregate_a_sell_to_zero(self) -> None:
        position_id, market_id, outcome_id = (uuid.uuid4() for _ in range(3))
        cursor = _Cursor(
            closed_trade_rows=[
                (
                    position_id,
                    market_id,
                    "Will the event happen?",
                    outcome_id,
                    "YES",
                    NOW - timedelta(minutes=5),
                    NOW,
                    "100",
                    "100",
                    "0.42",
                    "0.57",
                    42_000_000,
                    57_000_000,
                    0,
                    15_000_000,
                    "0.3571",
                    "sold",
                )
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["get_closed_trades"].handler({"limit": 10})

        self.assertEqual(output["trades"][0]["position_id"], str(position_id))
        self.assertEqual(output["trades"][0]["total_bought_shares"], "100")
        self.assertEqual(output["trades"][0]["total_sold_shares"], "100")
        self.assertEqual(output["trades"][0]["average_entry_price"], "0.42")
        self.assertEqual(output["trades"][0]["average_exit_price"], "0.57")
        self.assertEqual(output["trades"][0]["realized_pnl_micros"], 15_000_000)
        self.assertEqual(output["trades"][0]["return_on_cost"], "0.3571")
        self.assertEqual(output["trades"][0]["close_reason"], "sold")
        query, params = cursor.queries[0]
        self.assertIn("HAVING SUM(signed_shares) = 0", query)
        self.assertIn("side = 'SELL'", query)
        self.assertEqual(params[1], 10)

    def test_partial_sell_does_not_create_a_closed_trade(self) -> None:
        cursor = _Cursor(closed_trade_rows=[])
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["get_closed_trades"].handler({})

        self.assertEqual(output, {"trades": []})

    def test_settlements_include_position_market_outcome_and_winner(self) -> None:
        settlement_id, position_id, market_id, outcome_id = (uuid.uuid4() for _ in range(4))
        cursor = _Cursor(
            settlement_rows=[
                (
                    settlement_id,
                    position_id,
                    market_id,
                    "Will the event happen?",
                    outcome_id,
                    "No",
                    "Yes",
                    "100",
                    0,
                    -42_000_000,
                    NOW,
                )
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["get_settlements"].handler({"limit": 10})

        settlement = output["settlements"][0]
        self.assertEqual(
            settlement,
            {
                "id": str(settlement_id),
                "position_id": str(position_id),
                "market_id": str(market_id),
                "market_question": "Will the event happen?",
                "outcome_id": str(outcome_id),
                "outcome": "No",
                "winning_outcome": "Yes",
                "shares": "100",
                "payout_micros": 0,
                "realized_pnl_micros": -42_000_000,
                "settled_at": str(NOW),
            },
        )
        query, params = cursor.queries[0]
        self.assertIn("LEFT JOIN outcomes winning_o", query)
        self.assertIn("r.winning_outcome_id", query)
        self.assertEqual(params[1], 10)

    def test_place_order_persists_only_pending_intent(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        output = tools["place_market_order"].handler(
            {"token_id": "token", "side": "BUY", "amount": 10, "conviction": 0.7}
        )
        self.assertEqual(output["status"], "pending_broker_validation")
        insert = next(
            query for query, _params in cursor.queries if "INSERT INTO order_intents" in query
        )
        self.assertIn("pending_broker_validation", insert)
        self.assertIn("amount_micros, shares", insert)
        self.assertFalse(any("INSERT INTO orders" in query for query, _ in cursor.queries))

    def test_orderbook_rejects_current_cycle_member_older_than_five_minutes(self) -> None:
        cursor = _Cursor(book_observed_at=NOW - timedelta(minutes=5, microseconds=1))
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        with self.assertRaisesRegex(ToolContextUnavailable, "older than 300 seconds"):
            tools["get_orderbook"].handler({"token_id": "token"})

    def test_discovery_reads_snapshot_payload_and_current_cycle_membership(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        output = tools["get_all_active_markets"].handler({"limit": 1})
        card = output["markets"][0]
        self.assertEqual(card["question"], "Snapshot question")
        self.assertEqual(card["market_ref"], "venue-market")
        self.assertEqual(card["outcomes"][0], {"name": "Yes", "indicative_price": ""})
        self.assertNotIn("slug", card)
        self.assertNotIn("token_id", card["outcomes"][0])
        query, params = cursor.queries[0]
        self.assertIn("snapshot.payload->>'question'", query)
        self.assertIn("ms.id = ANY(%s::uuid[])", query)
        self.assertEqual(len(params[1]), 1)

    def test_discover_by_price_volatility_is_sorted_descending(self) -> None:
        def market_row(
            market_ref: str,
            one_hour_change: str,
            one_day_change: str,
            volume: int,
        ) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                uuid.uuid4(),
                "Snapshot question",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                volume,
                2_000_000,
                "open",
                True,
                {
                    "one_hour_price_change": one_hour_change,
                    "one_day_price_change": one_day_change,
                },
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row("low-volatility-high-volume", "0.10", "0.20", 9_000_000),
                market_row("high-volatility-low-volume", "-0.75", "0.10", 1_000_000),
                market_row("mid-volatility", "0.40", "-0.50", 5_000_000),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["discover_by_price_volatility"].handler({"limit": 3})

        self.assertEqual(
            [item["market_ref"] for item in output["markets"]],
            ["high-volatility-low-volume", "mid-volatility", "low-volatility-high-volume"],
        )

    def test_discover_by_competitive_score_is_sorted_descending(self) -> None:
        def market_row(
            market_ref: str,
            competitive: str,
            volume: int,
        ) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                uuid.uuid4(),
                "Snapshot question",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                volume,
                2_000_000,
                "open",
                True,
                {"competitive": competitive},
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row("low-score-high-volume", "0.20", 9_000_000),
                market_row("high-score-low-volume", "0.90", 1_000_000),
                market_row("mid-score", "0.60", 5_000_000),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["discover_by_competitive_score"].handler({"limit": 3})

        self.assertEqual(
            [item["market_ref"] for item in output["markets"]],
            ["high-score-low-volume", "mid-score", "low-score-high-volume"],
        )
        self.assertEqual(
            [item["competitive"] for item in output["markets"]],
            [0.9, 0.6, 0.2],
        )

    def test_discover_by_time_remaining_is_sorted_soonest_first(self) -> None:
        def market_row(market_ref: str, closes_in_hours: int, volume: int) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                uuid.uuid4(),
                "Snapshot question",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(hours=closes_in_hours),
                volume,
                2_000_000,
                "open",
                True,
                {"volume_24hr": "1", "volume_1wk": "7"},
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row("far-close-high-volume", 24, 9_000_000),
                market_row("near-close-low-volume", 2, 1_000_000),
                market_row("mid-close", 8, 5_000_000),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["discover_by_time_remaining"].handler(
            {"hours_min": 0, "hours_max": 48, "limit": 3}
        )

        self.assertEqual(
            [item["market_ref"] for item in output["markets"]],
            ["near-close-low-volume", "mid-close", "far-close-high-volume"],
        )

    def test_discover_by_volume_trend_is_sorted_by_strength(self) -> None:
        def market_row(
            market_ref: str,
            volume_24hr: int,
            volume_1wk: int,
            volume: int,
        ) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                uuid.uuid4(),
                "Snapshot question",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                volume,
                2_000_000,
                "open",
                True,
                {"volume_24hr": str(volume_24hr), "volume_1wk": str(volume_1wk)},
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row("weak-increase-high-volume", 15, 70, 9_000_000),
                market_row("strong-increase-low-volume", 60, 70, 1_000_000),
                market_row("mid-increase", 30, 70, 5_000_000),
                market_row("weak-decrease-high-volume", 9, 70, 9_000_000),
                market_row("strong-decrease-low-volume", 1, 70, 1_000_000),
                market_row("mid-decrease", 5, 70, 5_000_000),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        increasing = tools["discover_by_volume_trend"].handler({"trend": "increasing", "limit": 3})
        decreasing = tools["discover_by_volume_trend"].handler({"trend": "decreasing", "limit": 3})

        self.assertEqual(
            [item["market_ref"] for item in increasing["markets"]],
            ["strong-increase-low-volume", "mid-increase", "weak-increase-high-volume"],
        )
        self.assertEqual(
            [item["market_ref"] for item in decreasing["markets"]],
            ["strong-decrease-low-volume", "mid-decrease", "weak-decrease-high-volume"],
        )

    def test_search_tags_matches_only_tag_names(self) -> None:
        def market_row(market_ref: str, metadata: dict[str, object]) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                uuid.uuid4(),
                "Snapshot question",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                1_000_000,
                2_000_000,
                "open",
                True,
                metadata,
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row("tag-match", {"tags": [{"label": "Politics"}]}),
                market_row(
                    "metadata-only-match",
                    {"description": "Politics", "tags": [{"label": "Sports"}]},
                ),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["search_tags"].handler({"query": "politics"})

        self.assertEqual([market["market_ref"] for market in output["markets"]], ["tag-match"])

    def test_search_tags_accepts_multiple_keywords(self) -> None:
        def market_row(market_ref: str, tags: list[str]) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                uuid.uuid4(),
                "Snapshot question",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                1_000_000,
                2_000_000,
                "open",
                True,
                {"tags": [{"label": tag} for tag in tags]},
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row("politics", ["Politics"]),
                market_row("sports", ["Sports"]),
                market_row("both", ["Politics", "Sports"]),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["search_tags"].handler({"query": ("politics", "sports")})

        self.assertEqual(
            [market["market_ref"] for market in output["markets"]],
            ["politics", "sports", "both"],
        )

    def test_market_details_resolves_candidate_market_ref_and_returns_canonical_slug(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        output = tools["get_market_details"].handler({"market_ref": "venue-market"})
        self.assertEqual(output["market"]["market_ref"], "venue-market")
        self.assertEqual(output["market"]["canonical_slug"], "snapshot-slug")
        query, params = cursor.queries[0]
        self.assertIn("COALESCE(snapshot.payload->>'venue_market_id', m.id::text) = %s", query)
        self.assertEqual(params[2], "venue-market")

    def test_market_details_requires_one_explicit_lookup_reference(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            tools["get_market_details"].handler({})

    def test_discovery_paginates_after_filtering_the_frozen_universe(self) -> None:
        rows = [
            (
                uuid.uuid4(),
                f"venue-market-{index}",
                f"snapshot-slug-{index}",
                uuid.uuid4(),
                f"Snapshot question {index}",
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                1_000_000 - index,
                2_000_000,
                "open",
                True,
                {"tags": [{"label": "Politics"}]},
                [{"venue_token_id": f"token-{index}", "name": "Yes"}],
            )
            for index in range(3)
        ]
        cursor = _Cursor(market_rows=rows)
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        first = tools["get_all_active_markets"].handler({"limit": 2})
        self.assertEqual(len(first["markets"]), 2)
        self.assertTrue(first["has_more"])
        self.assertFalse(first["payload_truncated"])
        self.assertIsInstance(first["next_cursor"], str)

        second = tools["get_all_active_markets"].handler(
            {"limit": 2, "cursor": first["next_cursor"]}
        )
        self.assertEqual([item["market_ref"] for item in second["markets"]], ["venue-market-2"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])

    def test_event_discovery_applies_market_filters_before_grouping(self) -> None:
        event_a = uuid.uuid4()
        event_b = uuid.uuid4()

        def market_row(
            event_id: uuid.UUID,
            market_ref: str,
            liquidity: int,
            volume_24hr: int,
        ) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                event_id,
                market_ref,
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                volume_24hr * 1_000_000,
                liquidity * 1_000_000,
                "open",
                True,
                {"volume_24hr": str(volume_24hr), "created_at": NOW.isoformat()},
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        rows = [
            market_row(event_a, "qualifying", liquidity=20, volume_24hr=30),
            market_row(event_a, "low-volume", liquidity=20, volume_24hr=5),
            market_row(event_b, "low-liquidity", liquidity=5, volume_24hr=30),
        ]

        cases = (
            ("discover_events", {"min_liquidity": 10, "min_volume_24hr": 10}, 1),
            ("list_top_events", {"min_liquidity": 10, "min_volume_24hr": 10}, 1),
            ("get_newest_events", {"min_liquidity": 10}, 2),
        )
        for name, arguments, expected_market_count in cases:
            with self.subTest(name=name):
                cursor = _Cursor(market_rows=rows)
                tools = {
                    tool.name: tool
                    for tool in ProductionToolRegistry(_context(cursor)).tool_specs()
                }
                output = tools[name].handler(arguments)

                self.assertEqual(len(output["events"]), 1)
                self.assertEqual(output["events"][0]["event_id"], str(event_a))
                self.assertEqual(
                    len(output["events"][0]["markets"]), expected_market_count
                )

    def test_event_discovery_accepts_multiple_keywords(self) -> None:
        event_a = uuid.uuid4()
        event_b = uuid.uuid4()

        def market_row(event_id: uuid.UUID, market_ref: str) -> tuple[object, ...]:
            return (
                uuid.uuid4(),
                market_ref,
                f"{market_ref}-slug",
                event_id,
                market_ref,
                "Snapshot rules",
                NOW - timedelta(days=1),
                NOW + timedelta(days=1),
                30_000_000,
                20_000_000,
                "open",
                True,
                {"volume_24hr": "30", "created_at": NOW.isoformat()},
                [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
            )

        cursor = _Cursor(
            market_rows=[
                market_row(event_a, "alpha market"),
                market_row(event_b, "beta market"),
                market_row(event_a, "gamma market"),
            ]
        )
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}

        output = tools["discover_events"].handler({"keyword": ["alpha", "beta"]})

        self.assertEqual(
            {event["event_id"] for event in output["events"]},
            {str(event_a), str(event_b)},
        )
        self.assertEqual(
            [len(event["markets"]) for event in output["events"]],
            [1, 1],
        )

    def test_newest_markets_is_sorted_by_creation_date_descending(self) -> None:
        rows = []
        for market_ref, created_at in (
            ("older", NOW - timedelta(hours=3)),
            ("newer", NOW - timedelta(hours=1)),
        ):
            rows.append(
                (
                    uuid.uuid4(),
                    market_ref,
                    f"{market_ref}-slug",
                    uuid.uuid4(),
                    market_ref,
                    "Snapshot rules",
                    NOW - timedelta(days=1),
                    NOW + timedelta(days=1),
                    1_000_000,
                    2_000_000,
                    "open",
                    True,
                    {"created_at": created_at.isoformat()},
                    [{"venue_token_id": f"{market_ref}-token", "name": "Yes"}],
                )
            )

        cursor = _Cursor(market_rows=rows)
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        output = tools["get_newest_markets"].handler({"hours_back": 24})

        self.assertEqual(
            [item["market_ref"] for item in output["markets"]], ["newer", "older"]
        )

    def test_discovery_cursor_is_bound_to_its_arguments_and_cutoff(self) -> None:
        row = (
            uuid.uuid4(),
            "venue-market",
            "snapshot-slug",
            uuid.uuid4(),
            "Snapshot question",
            "Snapshot rules",
            NOW - timedelta(days=1),
            NOW + timedelta(days=1),
            1_000_000,
            2_000_000,
            "open",
            True,
            {"tags": [{"label": "Politics"}]},
            [{"venue_token_id": "token", "name": "Yes"}],
        )
        cursor = _Cursor(market_rows=[row, row])
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        first = tools["get_all_active_markets"].handler({"limit": 1})
        with self.assertRaisesRegex(ValueError, "does not match"):
            tools["get_all_active_markets"].handler(
                {"limit": 1, "cursor": first["next_cursor"], "min_liquidity": 1}
            )

    def test_discovery_reports_payload_truncation_separately_from_more_rows(self) -> None:
        row = (
            uuid.uuid4(),
            "venue-market",
            "snapshot-slug",
            uuid.uuid4(),
            "Snapshot question",
            "Snapshot rules",
            NOW - timedelta(days=1),
            NOW + timedelta(days=1),
            1_000_000,
            2_000_000,
            "open",
            True,
            {"tags": [{"label": "Politics"}]},
            [{"venue_token_id": "token", "name": "Yes"}],
        )
        cursor = _Cursor(market_rows=[row, row, row])
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(
                _context(cursor, maximum_default_result_tokens=200)
            ).tool_specs()
        }
        output = tools["get_all_active_markets"].handler({"limit": 3})
        self.assertTrue(output["payload_truncated"])
        self.assertTrue(output["has_more"])
        self.assertIsInstance(output["next_cursor"], str)

    def test_orderbook_accepts_typed_outcome_reference(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        outcome_id = str(uuid.uuid4())
        output = tools["get_orderbook"].handler({"outcome_id": outcome_id})
        self.assertEqual(output["lookup"]["outcome_id"], outcome_id)
        query, params = cursor.queries[0]
        self.assertIn("o.id = %s::uuid", query)
        self.assertEqual(params[0], outcome_id)

    def test_include_inactive_beliefs_really_returns_inactive_rows(self) -> None:
        cursor = _Cursor()
        tools = {tool.name: tool for tool in ProductionToolRegistry(_context(cursor)).tool_specs()}
        output = tools["get_general_beliefs"].handler({"include_inactive": True})
        self.assertFalse(output["beliefs"][0]["active"])
        self.assertIn("confidence", output["beliefs"][0])
        self.assertNotIn("probability", output["beliefs"][0])
        belief_query, _params = cursor.queries[0]
        self.assertIn("ORDER BY r.created_at DESC, b.id DESC", belief_query)

    def test_belief_tools_default_to_active_and_search_can_include_inactive(self) -> None:
        active = _test_beliefs(1)[0]
        inactive = {**active, "id": str(uuid.uuid4()), "content": "old belief", "active": False}
        cursor = _Cursor()
        memory = _Memory([active, inactive], active_beliefs=[active])
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(_context(cursor, memory=memory)).tool_specs()
        }

        general = tools["get_general_beliefs"].handler({})
        search_default = tools["search_general_beliefs"].handler({"keyword": "old"})
        self.assertEqual([row["id"] for row in general["beliefs"]], [active["id"]])
        self.assertEqual(search_default["beliefs"], [])
        self.assertEqual(cursor.queries, [])

        search_with_inactive = tools["search_general_beliefs"].handler(
            {"keyword": "old", "include_inactive": True}
        )
        self.assertEqual(len(search_with_inactive["beliefs"]), 1)
        self.assertFalse(search_with_inactive["beliefs"][0]["active"])

    def test_search_general_beliefs_accepts_multiple_keywords(self) -> None:
        beliefs = _test_beliefs(2)
        beliefs[0] = {**beliefs[0], "content": "world cup thesis"}
        beliefs[1] = {**beliefs[1], "content": "risk management"}
        cursor = _Cursor()
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(
                _context(cursor, memory=_Memory(beliefs))
            ).tool_specs()
        }

        output = tools["search_general_beliefs"].handler({"keyword": ("world cup", "risk")})

        self.assertEqual(
            [belief["id"] for belief in output["beliefs"]],
            [belief["id"] for belief in beliefs],
        )

    def test_create_general_belief_persists_evidence_and_defaults_to_empty(self) -> None:
        memory = _Memory([])
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(_context(_Cursor(), memory=memory)).tool_specs()
        }
        create = tools["create_general_belief"].handler
        create(
            {
                "belief_content": "Evidence-backed thesis",
                "category": "event_analysis",
                "confidence": 0.8,
                "evidence": ["source-a", "source-b"],
            }
        )
        create(
            {
                "belief_content": "Belief without evidence",
                "category": "risk_assessment",
                "confidence": 0.4,
            }
        )

        self.assertEqual(memory.appended_beliefs[0].evidence, ("source-a", "source-b"))
        self.assertEqual(memory.appended_beliefs[1].evidence, ())

    def test_create_general_belief_rejects_malformed_evidence(self) -> None:
        memory = _Memory([])
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(_context(_Cursor(), memory=memory)).tool_specs()
        }
        with self.assertRaisesRegex(ValueError, "evidence"):
            tools["create_general_belief"].handler(
                {
                    "belief_content": "Malformed evidence",
                    "category": "event_analysis",
                    "confidence": 0.5,
                    "evidence": ["", 123],
                }
            )
        self.assertEqual(memory.appended_beliefs, [])

    def test_general_beliefs_paginate_to_the_result_token_ceiling(self) -> None:
        beliefs = _test_beliefs(100)
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(
                _context(_Cursor(), memory=_Memory(beliefs))
            ).tool_specs()
        }

        page = tools["get_general_beliefs"].handler({})
        collected = list(page["beliefs"])
        self.assertLess(len(collected), len(beliefs))
        self.assertTrue(page["payload_truncated"])
        self.assertTrue(page["has_more"])
        while page["next_cursor"] is not None:
            page = tools["get_general_beliefs"].handler({"cursor": page["next_cursor"]})
            collected.extend(page["beliefs"])

        self.assertEqual([row["id"] for row in collected], [row["id"] for row in beliefs])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])

    def test_search_general_beliefs_paginates_and_binds_cursor_to_filters(self) -> None:
        beliefs = _test_beliefs(100)
        tools = {
            tool.name: tool
            for tool in ProductionToolRegistry(
                _context(_Cursor(), memory=_Memory(beliefs))
            ).tool_specs()
        }

        first = tools["search_general_beliefs"].handler({"keyword": "thesis"})
        self.assertTrue(first["has_more"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            tools["search_general_beliefs"].handler(
                {"keyword": "different", "cursor": first["next_cursor"]}
            )

        collected = list(first["beliefs"])
        page = first
        while page["next_cursor"] is not None:
            page = tools["search_general_beliefs"].handler(
                {"keyword": "thesis", "cursor": page["next_cursor"]}
            )
            collected.extend(page["beliefs"])
        self.assertEqual([row["id"] for row in collected], [row["id"] for row in beliefs])

    def test_tools_refuse_unfinalized_cutoff(self) -> None:
        with self.assertRaisesRegex(ToolContextUnavailable, "finalized"):
            _context(_Cursor(), cutoff=None)


def _test_beliefs(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"belief-{index}")),
            "confidence": "0.7",
            "content": f"thesis {index} " + ("evidence " * 40),
            "category": "event_analysis",
            "evidence": [f"source-{index}"],
            "created_at": (NOW - timedelta(minutes=index)).isoformat(),
        }
        for index in range(count)
    ]


if __name__ == "__main__":
    unittest.main()
