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
    def __init__(self, *, book_observed_at: datetime = NOW) -> None:
        self.rows: list[tuple[object, ...]] = []
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.book_observed_at = book_observed_at

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
            self.rows = [
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


def _context(cursor: _Cursor, *, cutoff=NOW) -> ToolContext:
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
        cast(Any, object()),
        cast(Any, lambda arguments: {"items": [], "has_more": False}),
        lambda _url: connection,
        lambda: NOW,
        (market_snapshot_id,),
        (book_snapshot_id,),
    )


class ProductionToolRegistryTests(unittest.TestCase):
    def test_registry_has_exact_schema_parity_for_all_29_names(self) -> None:
        expected = {
            row["name"]
            for row in json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))[
                "tools"
            ]
        }
        names = {tool.name for tool in ProductionToolRegistry(_context(_Cursor())).tool_specs()}
        self.assertEqual(len(names), 29)
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

    def test_tools_refuse_unfinalized_cutoff(self) -> None:
        with self.assertRaisesRegex(ToolContextUnavailable, "finalized"):
            _context(_Cursor(), cutoff=None)


if __name__ == "__main__":
    unittest.main()
