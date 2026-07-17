from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from vtrade.providers import BudgetExceeded, BudgetReservation


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    threshold_micros: int
    projected_spend_micros: int
    opened_at: datetime


class MonthlyBudgetCircuitBreaker:
    """Thread-safe domain breaker; PostgreSQL provides the multi-process equivalent."""

    def __init__(
        self,
        *,
        limit_micros: int = 40_000_000,
        alert_thresholds_micros: tuple[int, ...] = (20_000_000, 32_000_000, 40_000_000),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if limit_micros <= 0:
            raise ValueError("monthly budget limit must be positive")
        if tuple(sorted(set(alert_thresholds_micros))) != alert_thresholds_micros:
            raise ValueError("budget thresholds must be unique and sorted")
        if any(value <= 0 or value > limit_micros for value in alert_thresholds_micros):
            raise ValueError("budget thresholds must be in (0, limit]")
        self.limit_micros = limit_micros
        self.alert_thresholds_micros = alert_thresholds_micros
        self._clock = clock or (lambda: datetime.now(UTC))
        self._month: str | None = None
        self._billed = 0
        self._nominal = 0
        self._reserved: dict[str, tuple[str, int]] = {}
        self._alerted: set[int] = set()
        self.alerts: list[BudgetAlert] = []
        self._lock = threading.Lock()

    def reserve(
        self,
        provider: str,
        estimated_cost_micros: int,
        *,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> BudgetReservation:
        if not provider or estimated_cost_micros < 0:
            raise ValueError("provider and non-negative estimate are required")
        if request_count < 0 or credit_count < 0:
            raise ValueError("provider usage reservations cannot be negative")
        with self._lock:
            self._roll_month()
            dollar_estimate = 0 if provider == "exa" else estimated_cost_micros
            projected = self._billed + sum(value for _, value in self._reserved.values())
            projected += dollar_estimate
            if projected > self.limit_micros:
                raise BudgetExceeded("request estimate would exceed the $40 monthly budget")
            self._open_alerts(projected)
            reservation = BudgetReservation(
                str(uuid.uuid4()),
                estimated_cost_micros,
                provider,
                request_count,
                credit_count,
            )
            self._reserved[reservation.id] = (provider, dollar_estimate)
            return reservation

    def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        billed_cost_micros: int,
        nominal_cost_micros: int,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> None:
        if billed_cost_micros < 0 or nominal_cost_micros < 0:
            raise ValueError("provider costs cannot be negative")
        if request_count < 0 or credit_count < 0:
            raise ValueError("provider usage cannot be negative")
        with self._lock:
            self._roll_month()
            pending = self._reserved.pop(reservation.id, None)
            expected = 0 if reservation.provider == "exa" else reservation.estimated_cost_micros
            if pending is None or pending != (reservation.provider, expected):
                raise ValueError("unknown or inconsistent budget reservation")
            if reservation.provider != "exa":
                self._billed += billed_cost_micros
            self._nominal += nominal_cost_micros
            projected = self._billed + sum(value for _, value in self._reserved.values())
            self._open_alerts(projected)
            exceeded = projected > self.limit_micros
        if exceeded:
            raise BudgetExceeded(
                "actual provider cost exceeded its estimate; usage recorded and circuit halted"
            )

    @property
    def billed_cost_micros(self) -> int:
        return self._billed

    @property
    def nominal_cost_micros(self) -> int:
        return self._nominal

    def _roll_month(self) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("budget clock must return timezone-aware timestamps")
        month = now.astimezone(UTC).strftime("%Y-%m")
        if self._month != month:
            self._month = month
            self._billed = 0
            self._nominal = 0
            self._reserved.clear()
            self._alerted.clear()

    def _open_alerts(self, projected: int) -> None:
        for threshold in self.alert_thresholds_micros:
            if projected >= threshold and threshold not in self._alerted:
                self._alerted.add(threshold)
                self.alerts.append(BudgetAlert(threshold, projected, self._clock()))
