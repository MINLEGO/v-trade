from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from vtrade.domain.execution import EconomicFill, OrderRequest, OrderResult
from vtrade.production_tools import ProductionToolRegistry, _execution_output


def test_registry_has_exact_schema_parity_for_all_27_names() -> None:
    context = SimpleNamespace(
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {"items": [], "next_cursor": None, "has_more": False},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]
    names = {spec.name for spec in registry.tool_specs()}
    expected = {
        item["name"]
        for item in json.loads(Path("spec/tool-schemas-vtrade-kalshi-v1.json").read_text())["tools"]
    }
    assert names == expected
    assert len(names) == 27


def test_get_orderbook_preserves_the_configured_depth_on_all_four_sides() -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    snapshot_id = "snapshot-1"
    level_rows = tuple(
        (outcome, book_side, level, 100 * side_index + level, 10 + level)
        for side_index, (outcome, book_side) in enumerate(
            (("YES", "bid"), ("YES", "ask"), ("NO", "bid"), ("NO", "ask"))
        )
        for level in range(3)
    )
    snapshot_row = (
        snapshot_id,
        cutoff - timedelta(minutes=1),
        cutoff - timedelta(minutes=2),
        cutoff,
        "artifact-1",
        "sha256-1",
        cutoff - timedelta(minutes=1),
    )
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_book_age=timedelta(minutes=5),
        maximum_order_book_depth=2,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]

    def query(sql: str, params: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        if "FROM order_book_snapshots" in sql:
            return (snapshot_row,)
        if "FROM order_book_levels" in sql:
            ordered = tuple(sorted(level_rows, key=lambda row: (row[0], row[1], row[2])))
            depth = int(params[1])
            if "ROW_NUMBER() OVER" in sql:
                return tuple(
                    row
                    for key in {row[:2] for row in ordered}
                    for row in ordered
                    if row[:2] == key and row[2] < depth
                )
            return ordered[:depth]
        raise AssertionError(f"unexpected query: {sql}")

    with patch.object(registry, "_query", side_effect=query), patch.object(
        registry, "_fee_policy", return_value=None
    ):
        output = registry._get_orderbook({"market_ref": "KXTEST-1"})

    book = output["book"]
    assert {
        side: [level["price_micros"] for level in book[side]]
        for side in ("yes_bids", "yes_asks", "no_bids", "no_asks")
    } == {
        "yes_bids": [0, 1],
        "yes_asks": [100, 101],
        "no_bids": [200, 201],
        "no_asks": [300, 301],
    }


def test_get_market_details_returns_resolution_rules_not_question() -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    market_row = (
        "KXTEST-1",
        "SERIES-1",
        "EVENT-1",
        "Event title",
        "category",
        "Will this market resolve YES?",
        cutoff - timedelta(days=1),
        cutoff + timedelta(days=1),
        1234,
        12_500_000,
        "active",
        True,
        True,
        cutoff - timedelta(minutes=1),
        cutoff - timedelta(minutes=2),
        "artifact-1",
        "sha256-1",
        cutoff - timedelta(minutes=1),
        [
            {
                "outcome": "YES",
                "label": "YES",
                "eligible": True,
                "indicative_price_micros": 425_000,
            },
            {
                "outcome": "NO",
                "label": "NO",
                "eligible": True,
                "indicative_price_micros": 575_000,
            },
        ],
        "Resolve from the official source, as defined by the market rules.",
        567,
        12_345,
        "increasing",
        Decimal("0.0200000000"),
        Decimal("0.8000000000"),
        425_000,
        575_000,
        '["Macro", "Test"]',
    )
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]

    def query(sql: str, _params: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
        if "FROM market_price_grid_ranges" in sql:
            return ((0, 1_000_000, 10_000),)
        assert "m.resolution_rules" in sql
        return (market_row,)

    with patch.object(registry, "_query", side_effect=query):
        output = registry._get_market_details({"market_ref": "KXTEST-1"})

    assert output["market"]["question"] == "Will this market resolve YES?"
    assert output["resolution_rules"] == (
        "Resolve from the official source, as defined by the market rules."
    )
    assert output["market"]["volume_24h_units"] == 567
    assert output["market"]["volatility_micros"] == 12_345
    assert output["market"]["volume_trend"] == "increasing"
    assert output["market"]["volume_trend_delta"] == "0.0200000000"
    assert output["market"]["competitive_score"] == "0.8000000000"
    assert output["market"]["tag_names"] == ["Macro", "Test"]
    assert {
        item["outcome"]: item["indicative_price_micros"]
        for item in output["market"]["outcomes"]
    } == {"YES": 425_000, "NO": 575_000}


def test_date_range_supports_close_and_open_basis() -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]

    def market_row(
        market_ref: str, open_time: datetime, close_time: datetime | None
    ) -> tuple[object, ...]:
        return (
            market_ref,
            "SERIES-1",
            "EVENT-1",
            "Event title",
            "category",
            "Question",
            open_time,
            close_time,
            100,
            100,
        )

    rows = [
        market_row(
            "KXCLOSE",
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 20, tzinfo=UTC),
        ),
        market_row(
            "KXOPEN",
            datetime(2026, 8, 20, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
        ),
        market_row("KXMISSING", datetime(2026, 8, 20, tzinfo=UTC), None),
    ]

    close_matches = registry._filter_market_rows(
        "discover_by_date_range",
        rows,
        {"start_date": "2026-08-20", "end_date": "2026-08-20"},
    )
    open_matches = registry._filter_market_rows(
        "discover_by_date_range",
        rows,
        {
            "date_basis": "open_time",
            "start_date": "2026-08-20",
            "end_date": "2026-08-20",
        },
    )

    assert [row[0] for row in close_matches] == ["KXCLOSE"]
    assert [row[0] for row in open_matches] == ["KXOPEN", "KXMISSING"]


def test_get_event_markets_filters_by_exact_event_ref() -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]
    rows = [
        (
            "KX-A-1",
            "SERIES-1",
            "EVENT-A",
            "Event A",
            None,
            "Question A1",
            None,
            None,
            300,
            30,
        ),
        (
            "KX-A-2",
            "SERIES-1",
            "EVENT-A",
            "Event A",
            None,
            "Question A2",
            None,
            None,
            200,
            20,
        ),
        (
            "KX-A-OTHER",
            "SERIES-1",
            "EVENT-A-OTHER",
            "Other",
            None,
            "Question O",
            None,
            None,
            1_000,
            100,
        ),
        (
            "KX-A-CASE",
            "SERIES-1",
            "event-a",
            "Case variant",
            None,
            "Question C",
            None,
            None,
            900,
            90,
        ),
    ]

    filtered = registry._filter_market_rows(
        "get_event_markets", rows, {"event_ref": "EVENT-A"}
    )

    assert [row[0] for row in filtered] == ["KX-A-1", "KX-A-2"]


@pytest.mark.parametrize("arguments", [{}, {"event_ref": " "}])
def test_get_event_markets_requires_a_nonempty_event_ref(arguments: dict[str, object]) -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="event_ref is required"):
        registry._filter_market_rows("get_event_markets", [], arguments)


def test_get_event_markets_filters_before_pagination() -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)

    def market_row(market_ref: str, event_ref: str, volume: int) -> tuple[object, ...]:
        return (
            market_ref,
            "SERIES-1",
            event_ref,
            f"{event_ref} title",
            "category",
            f"Question {market_ref}",
            cutoff - timedelta(days=1),
            cutoff + timedelta(days=1),
            volume,
            100,
            "active",
            True,
            True,
            cutoff,
            cutoff,
            "artifact-1",
            "a" * 64,
            cutoff,
            [],
            "Rules",
            volume,
            0,
            "flat",
            None,
            Decimal("0.5000000000"),
            500_000,
            500_000,
            "[]",
        )

    rows = [
        market_row("KX-OTHER", "EVENT-OTHER", 1_000),
        market_row("KX-TARGET-1", "EVENT-TARGET", 300),
        market_row("KX-TARGET-2", "EVENT-TARGET", 200),
    ]
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]

    with patch.object(registry, "_market_rows", return_value=rows):
        first_page = registry._discover(
            "get_event_markets", {"event_ref": "EVENT-TARGET", "limit": 1}
        )
        second_page = registry._discover(
            "get_event_markets",
            {
                "event_ref": "EVENT-TARGET",
                "limit": 1,
                "cursor": first_page["next_cursor"],
            },
        )

    assert [item["market_ref"] for item in first_page["markets"]] == ["KX-TARGET-1"]
    assert first_page["has_more"] is True
    assert [item["market_ref"] for item in second_page["markets"]] == ["KX-TARGET-2"]
    assert second_page["has_more"] is False


def test_search_tags_uses_exact_case_insensitive_membership() -> None:
    cutoff = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    context = SimpleNamespace(
        claim=SimpleNamespace(cycle_id="cycle-1"),
        cutoff=cutoff,
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]
    common = (
        "KXTEST-1",
        "SERIES-1",
        "EVENT-1",
        "Event title",
        "category",
        "Question",
        cutoff - timedelta(days=1),
        cutoff + timedelta(days=1),
        100,
        100,
        "active",
        True,
        True,
        cutoff,
        cutoff,
        "artifact-1",
        "a" * 64,
        cutoff,
        [],
        "Rules",
        50,
        100,
        "flat",
        None,
        Decimal("0.5000000000"),
        500_000,
        500_000,
    )
    exact = (*common, '["Macro"]')
    substring_only = (*common[:0], "KXTEST-2", *common[1:], '["Macroeconomics"]')

    filtered = registry._filter_market_rows(
        "search_tags", [exact, substring_only], {"query": "macro"}
    )

    assert [row[0] for row in filtered] == ["KXTEST-1"]


def test_order_output_uses_contract_units_prices_fees_and_reconciliation() -> None:
    now = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    request = OrderRequest(
        agent_id="agent-1",
        market_ref="KXEXAMPLE-1",
        outcome="YES",
        action="BUY",
        amount=100,
        amount_type="CONTRACTS",
        idempotency_key="cycle-1-order-1",
        limit_price=400_000,
        time_in_force="IOC",
        frozen_context_id="cycle-1",
        frozen_cutoff=now,
        created_at=now,
    )
    fill = EconomicFill(
        fill_id="fill-1",
        contract_units=100,
        price_micros=400_000,
        gross_cash_micros=400_000,
        fee_micros=1_000,
        net_cash_delta_micros=-401_000,
        filled_at=now,
    )
    result = OrderResult(
        request=request,
        operation_id="operation-1",
        state="FILLED",
        reconciliation_state="NOT_REQUIRED",
        requested_units=100,
        filled_units=100,
        remaining_units=0,
        cancelled_units=0,
        fills=(fill,),
        gross_cash_delta_micros=-400_000,
        fee_micros=1_000,
        net_cash_delta_micros=-401_000,
        frozen_context_id="cycle-1",
        execution_context_id="execution-1",
        submitted_at=now,
        updated_at=now,
    )
    output = _execution_output(result)
    assert output["status"] == "FILLED"
    assert output["reconciliation_state"] == "NOT_REQUIRED"
    assert output["request"]["market_ref"] == "KXEXAMPLE-1"
    assert output["request"]["outcome"] == "YES"
    assert output["requested_contract_units"] == 100
    assert output["fills"][0]["price_micros"] == 400_000
    assert output["fee_micros"] == 1_000
