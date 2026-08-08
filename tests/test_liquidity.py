from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from vtrade.domain.types import OrderBookSnapshot, PriceLevel, RawArtifact, Side
from vtrade.liquidity import (
    VirtualLiquidityLevel,
    VirtualLiquidityReservation,
    effective_liquidity_book,
    metrics_for_fills,
    private_snapshot,
)

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def reservation(*, agent_id: str, available: tuple[str, str] = ("1", "2")):
    snapshot = OrderBookSnapshot(
        token_id="token-yes",
        condition_id="condition-1",
        observed_at=NOW,
        source_created_at=NOW,
        bids=(PriceLevel(Decimal("0.39"), Decimal("10")),),
        asks=(
            PriceLevel(Decimal("0.40"), Decimal(available[0])),
            PriceLevel(Decimal("0.41"), Decimal(available[1])),
        ),
        tick_size=Decimal("0.01"),
        minimum_order_size=Decimal("1"),
        negative_risk=False,
        artifact=RawArtifact("a" * 64, 1, "memory://book"),
    )
    levels = tuple(
        VirtualLiquidityLevel(index, Decimal(price), Decimal(size), Decimal(size))
        for index, (price, size) in enumerate((("0.40", available[0]), ("0.41", available[1])))
    )
    return VirtualLiquidityReservation(
        order_id=f"order-{agent_id}",
        context_version="agent-cycle-v1:cycle-1:book-1",
        agent_id=agent_id,
        agent_cycle_id="cycle-1",
        snapshot_id="book-1",
        token_id="token-yes",
        side=Side.BUY,
        snapshot=private_snapshot(snapshot, side=Side.BUY, levels=levels),
        levels=levels,
    )


class VirtualLiquidityTests(unittest.TestCase):
    def test_haircut_aggregates_prices_and_keeps_five_effective_levels(self) -> None:
        source = reservation(agent_id="agent-1").snapshot
        snapshot = replace(
            source,
            asks=(
                PriceLevel(Decimal("0.40"), Decimal("30")),
                PriceLevel(Decimal("0.40"), Decimal("30")),
                PriceLevel(Decimal("0.41"), Decimal("1")),
                PriceLevel(Decimal("0.42"), Decimal("1")),
                PriceLevel(Decimal("0.43"), Decimal("1")),
                PriceLevel(Decimal("0.44"), Decimal("1")),
                PriceLevel(Decimal("0.45"), Decimal("1")),
            ),
        )

        result = effective_liquidity_book(
            snapshot,
            side=Side.BUY,
            maximum_book_depth=5,
            ignored_best_levels=1,
            maximum_ignored_depth_fraction=Decimal("0.5"),
        )

        self.assertEqual(len(result.raw_levels), 6)
        self.assertEqual(result.raw_levels[0].displayed_shares, Decimal("60"))
        self.assertEqual(result.raw_levels[0].ignored_shares, Decimal("32.5"))
        self.assertEqual(result.raw_levels[0].effective_shares, Decimal("27.5"))
        self.assertEqual(len(result.executable_levels), 5)
        self.assertEqual(result.executable_levels[-1].price, Decimal("0.44"))
        self.assertEqual(result.raw_levels[-1].executable, False)

    def test_haircut_fully_ignores_a_best_level_at_the_fifty_percent_boundary(self) -> None:
        source = reservation(agent_id="agent-1").snapshot
        snapshot = replace(
            source,
            asks=tuple(
                PriceLevel(Decimal(f"0.{40 + index}"), Decimal(5))
                for index in range(6)
            ),
        )

        result = effective_liquidity_book(
            snapshot,
            side=Side.BUY,
            maximum_book_depth=5,
            ignored_best_levels=1,
            maximum_ignored_depth_fraction=Decimal("0.5"),
        )

        self.assertEqual(result.raw_levels[0].effective_shares, Decimal(0))
        self.assertEqual(
            tuple(level.level_index for level in result.executable_levels),
            (1, 2, 3, 4, 5),
        )

    def test_haircut_orders_sell_bids_and_limits_one_level_to_half_when_alone(self) -> None:
        source = reservation(agent_id="agent-1").snapshot
        snapshot = replace(
            source,
            bids=(PriceLevel(Decimal("0.60"), Decimal(100)),),
        )

        result = effective_liquidity_book(
            snapshot,
            side=Side.SELL,
            maximum_book_depth=5,
            ignored_best_levels=1,
            maximum_ignored_depth_fraction=Decimal("0.5"),
        )

        self.assertEqual(result.raw_levels[0].price, Decimal("0.60"))
        self.assertEqual(result.raw_levels[0].ignored_shares, Decimal(50))
        self.assertEqual(result.raw_levels[0].effective_shares, Decimal(50))
        self.assertEqual(result.effective_depth_shares, Decimal(50))

    def test_private_snapshot_is_a_view_and_does_not_mutate_the_historical_book(self) -> None:
        first = reservation(agent_id="agent-1")
        consumed = metrics_for_fills(
            first,
            (SimpleNamespace(price=Decimal("0.40"), shares=Decimal("1")),),
            requested_shares=Decimal("1"),
        )
        second_levels = (
            replace(first.levels[0], available_shares=Decimal("0")),
            first.levels[1],
        )
        second_snapshot = private_snapshot(
            first.snapshot,
            side=Side.BUY,
            levels=second_levels,
        )

        self.assertEqual(first.snapshot.asks[0].size, Decimal("1"))
        self.assertEqual(consumed.available_shares, Decimal("3"))
        self.assertEqual(consumed.remaining_shares, Decimal("2"))
        self.assertEqual(second_snapshot.asks, (PriceLevel(Decimal("0.41"), Decimal("2")),))

    def test_successive_same_agent_orders_consume_the_private_remaining_depth(self) -> None:
        first = reservation(agent_id="agent-1")
        first_result = metrics_for_fills(
            first,
            (
                SimpleNamespace(price=Decimal("0.40"), shares=Decimal("1")),
                SimpleNamespace(price=Decimal("0.41"), shares=Decimal("1")),
            ),
            requested_shares=Decimal("2"),
        )
        second = replace(
            first,
            levels=(
                replace(first.levels[0], available_shares=Decimal("0")),
                replace(first.levels[1], available_shares=Decimal("1")),
            ),
        )
        second_result = metrics_for_fills(
            second,
            (SimpleNamespace(price=Decimal("0.41"), shares=Decimal("1")),),
            requested_shares=Decimal("2"),
        )

        self.assertEqual(first_result.consumed_shares, Decimal("2"))
        self.assertEqual(first_result.remaining_shares, Decimal("1"))
        self.assertEqual(second_result.available_shares, Decimal("1"))
        self.assertEqual(second_result.consumed_shares, Decimal("1"))
        self.assertEqual(second_result.cancelled_shares, Decimal("1"))
        self.assertEqual(second_result.remaining_shares, Decimal("0"))

    def test_retry_metrics_are_deterministic_and_other_agents_get_fresh_capacity(self) -> None:
        first = reservation(agent_id="agent-1")
        fills = (SimpleNamespace(price=Decimal("0.40"), shares=Decimal("1")),)
        initial = metrics_for_fills(first, fills, requested_shares=Decimal("1"))
        retry = metrics_for_fills(first, fills, requested_shares=Decimal("1"))
        other = metrics_for_fills(
            reservation(agent_id="agent-2"), fills, requested_shares=Decimal("1")
        )

        self.assertEqual(initial, retry)
        self.assertEqual(initial.available_shares, other.available_shares)
        self.assertEqual(initial.consumed_shares, other.consumed_shares)
        self.assertNotEqual(initial.agent_id, other.agent_id)


if __name__ == "__main__":
    unittest.main()
