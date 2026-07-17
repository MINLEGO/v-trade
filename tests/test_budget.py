from __future__ import annotations

import unittest
from datetime import UTC, datetime

from vtrade.budget import MonthlyBudgetCircuitBreaker
from vtrade.providers import BudgetExceeded

NOW = datetime(2026, 7, 16, tzinfo=UTC)


class BudgetTests(unittest.TestCase):
    def test_pre_request_reservations_include_pending_and_stop_over_limit(self) -> None:
        budget = MonthlyBudgetCircuitBreaker(clock=lambda: NOW)
        budget.reserve("openrouter", 30_000_000)
        with self.assertRaises(BudgetExceeded):
            budget.reserve("tavily", 10_000_001)
        self.assertEqual([alert.threshold_micros for alert in budget.alerts], [20_000_000])

    def test_billed_and_nominal_costs_are_separate_and_alerts_cross_once(self) -> None:
        budget = MonthlyBudgetCircuitBreaker(clock=lambda: NOW)
        first = budget.reserve("openrouter", 25_000_000)
        budget.reconcile(first, billed_cost_micros=20_000_000, nominal_cost_micros=30_000_000)
        second = budget.reserve("tavily", 15_000_000)
        budget.reconcile(second, billed_cost_micros=12_000_000, nominal_cost_micros=12_000_000)
        self.assertEqual(budget.billed_cost_micros, 32_000_000)
        self.assertEqual(budget.nominal_cost_micros, 42_000_000)
        self.assertEqual(
            [alert.threshold_micros for alert in budget.alerts], [20_000_000, 32_000_000]
        )

    def test_actual_overestimate_is_recorded_then_halts(self) -> None:
        budget = MonthlyBudgetCircuitBreaker(clock=lambda: NOW)
        reservation = budget.reserve("openrouter", 1_000_000)
        with self.assertRaises(BudgetExceeded):
            budget.reconcile(
                reservation,
                billed_cost_micros=41_000_000,
                nominal_cost_micros=41_000_000,
            )
        self.assertEqual(budget.billed_cost_micros, 41_000_000)
        with self.assertRaises(BudgetExceeded):
            budget.reserve("tavily", 0)

    def test_exa_nominal_value_is_recorded_but_excluded_from_dollar_breaker(self) -> None:
        budget = MonthlyBudgetCircuitBreaker(clock=lambda: NOW)
        model = budget.reserve("openrouter", 40_000_000)
        exa = budget.reserve("exa", 20_000, request_count=1)
        budget.reconcile(
            exa,
            billed_cost_micros=0,
            nominal_cost_micros=20_000,
            request_count=1,
        )
        self.assertEqual(budget.billed_cost_micros, 0)
        self.assertEqual(budget.nominal_cost_micros, 20_000)
        budget.reconcile(
            model,
            billed_cost_micros=40_000_000,
            nominal_cost_micros=40_000_000,
        )


if __name__ == "__main__":
    unittest.main()
