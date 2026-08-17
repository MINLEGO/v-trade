from __future__ import annotations

import unittest
from decimal import Decimal

from vtrade.risk import calculate_market_capacity


class MarketCapacityTests(unittest.TestCase):
    def test_capacity_uses_half_up_and_both_cost_basis_components(self) -> None:
        capacity = calculate_market_capacity(
            10,
            Decimal("0.15"),
            held_cost_basis_micros=1,
            pending_buy_reserved_cost_basis_micros=1,
        )

        self.assertEqual(capacity.market_limit_micros, 2)
        self.assertEqual(capacity.remaining_capacity_micros, 0)

    def test_capacity_clamps_at_zero(self) -> None:
        capacity = calculate_market_capacity(
            10_000_000,
            Decimal("0.15"),
            held_cost_basis_micros=1_000_000,
            pending_buy_reserved_cost_basis_micros=1_000_000,
        )

        self.assertEqual(capacity.market_limit_micros, 1_500_000)
        self.assertEqual(capacity.remaining_capacity_micros, 0)

    def test_capacity_rejects_negative_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "pending BUY"):
            calculate_market_capacity(
                10,
                Decimal("0.15"),
                pending_buy_reserved_cost_basis_micros=-1,
            )


if __name__ == "__main__":
    unittest.main()
