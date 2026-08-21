"""Deterministic best-level haircut evidence for the Kalshi paper release.

The immutable canonical order book remains the source of truth. This module derives
an auditable private execution view without mutating market data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from vtrade.domain.execution import LIQUIDITY_HAIRCUT_CONTRACT_VERSION
from vtrade.domain.types import CanonicalLevel, CanonicalOrderBook, OutcomeSide, Side

LIQUIDITY_HAIRCUT_RULE_VERSION = "best-level-haircut-v1"

# ---------------------------------------------------------------------------
# best-level-haircut-v1 / canonical Kalshi books
# ---------------------------------------------------------------------------


class LiquidityEvidenceError(ValueError):
    """The captured book cannot support a conservative paper execution."""


@dataclass(frozen=True, slots=True)
class HaircutLevelAudit:
    level_index: int
    price_micros: int
    raw_quantity_units: int
    ignored_quantity_units: int
    effective_quantity_units: int
    consumed_quantity_units: int = 0
    cancelled_quantity_units: int = 0
    remaining_quantity_units: int | None = None
    executable: bool = True

    def __post_init__(self) -> None:
        if self.level_index < 0:
            raise ValueError("haircut level index cannot be negative")
        values = (
            self.raw_quantity_units,
            self.ignored_quantity_units,
            self.effective_quantity_units,
            self.consumed_quantity_units,
            self.cancelled_quantity_units,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("haircut quantities must be non-negative integers")
        if self.raw_quantity_units <= 0:
            raise ValueError("raw haircut levels must be positive")
        if self.ignored_quantity_units > self.raw_quantity_units:
            raise ValueError("ignored quantity exceeds raw quantity")
        if self.effective_quantity_units != self.raw_quantity_units - self.ignored_quantity_units:
            raise ValueError("effective quantity does not match raw less ignored")
        if self.consumed_quantity_units > self.effective_quantity_units:
            raise ValueError("consumed quantity exceeds effective quantity")
        remaining = (
            self.effective_quantity_units - self.consumed_quantity_units
            if self.remaining_quantity_units is None
            else self.remaining_quantity_units
        )
        if remaining != self.effective_quantity_units - self.consumed_quantity_units:
            raise ValueError("remaining quantity is inconsistent")
        object.__setattr__(self, "remaining_quantity_units", remaining)

    @property
    def raw_quantity(self) -> int:
        return self.raw_quantity_units

    @property
    def ignored_quantity(self) -> int:
        return self.ignored_quantity_units

    @property
    def effective_quantity(self) -> int:
        return self.effective_quantity_units


@dataclass(frozen=True, slots=True)
class HaircutAudit:
    market_ref: str
    outcome: OutcomeSide
    action: Side
    rule_version: str
    raw_quantity_units: int
    ignored_quantity_units: int
    effective_quantity_units: int
    consumed_quantity_units: int
    cancelled_quantity_units: int
    remaining_quantity_units: int
    levels: tuple[HaircutLevelAudit, ...]
    captured_level_count: int
    effective_level_count: int

    def __post_init__(self) -> None:
        if self.rule_version != LIQUIDITY_HAIRCUT_CONTRACT_VERSION:
            raise ValueError("unsupported liquidity haircut rule")
        if not self.market_ref:
            raise ValueError("haircut market_ref is required")
        values = (
            self.raw_quantity_units,
            self.ignored_quantity_units,
            self.effective_quantity_units,
            self.consumed_quantity_units,
            self.cancelled_quantity_units,
            self.remaining_quantity_units,
        )
        if any(value < 0 for value in values):
            raise ValueError("haircut totals cannot be negative")
        if self.ignored_quantity_units + self.effective_quantity_units != self.raw_quantity_units:
            raise ValueError("haircut raw/ignored/effective totals are inconsistent")
        if (
            self.consumed_quantity_units + self.remaining_quantity_units
            != self.effective_quantity_units
        ):
            raise ValueError("haircut consumed/remaining totals are inconsistent")
        if self.cancelled_quantity_units < 0:
            raise ValueError("haircut cancelled quantity cannot be negative")
        if sum(level.raw_quantity_units for level in self.levels) != self.raw_quantity_units:
            raise ValueError("haircut raw levels do not match the raw total")
        if (
            sum(level.ignored_quantity_units for level in self.levels)
            != self.ignored_quantity_units
        ):
            raise ValueError("haircut ignored levels do not match the ignored total")
        if (
            sum(level.effective_quantity_units for level in self.levels)
            != self.effective_quantity_units
        ):
            raise ValueError("haircut effective levels do not match the effective total")
        if (
            sum(level.consumed_quantity_units for level in self.levels)
            != self.consumed_quantity_units
        ):
            raise ValueError("haircut consumed levels do not match the consumed total")

    @property
    def retained_fraction(self) -> Decimal:
        if not self.raw_quantity_units:
            return Decimal(0)
        return Decimal(self.effective_quantity_units) / Decimal(self.raw_quantity_units)

    @property
    def raw_quantity(self) -> int:
        return self.raw_quantity_units

    @property
    def ignored_quantity(self) -> int:
        return self.ignored_quantity_units

    @property
    def effective_quantity(self) -> int:
        return self.effective_quantity_units

    @property
    def consumed_quantity(self) -> int:
        return self.consumed_quantity_units

    @property
    def cancelled_quantity(self) -> int:
        return self.cancelled_quantity_units

    @property
    def remaining_quantity(self) -> int:
        return self.remaining_quantity_units

    @property
    def executable_levels(self) -> tuple[HaircutLevelAudit, ...]:
        return tuple(
            level
            for level in self.levels
            if level.executable and level.effective_quantity_units
        )

    def after_fills(
        self,
        consumed_by_level: Mapping[int, int],
        *,
        requested_quantity_units: int,
    ) -> HaircutAudit:
        if requested_quantity_units < 0:
            raise ValueError("requested haircut quantity cannot be negative")
        updated: list[HaircutLevelAudit] = []
        for level in self.levels:
            consumed = consumed_by_level.get(level.level_index, 0)
            if consumed < 0 or consumed > level.effective_quantity_units:
                raise LiquidityEvidenceError("fill exceeds effective haircut depth")
            updated.append(
                HaircutLevelAudit(
                    level.level_index,
                    level.price_micros,
                    level.raw_quantity_units,
                    level.ignored_quantity_units,
                    level.effective_quantity_units,
                    consumed,
                    0,
                )
            )
        consumed_total = sum(item.consumed_quantity_units for item in updated)
        if consumed_total > requested_quantity_units:
            raise LiquidityEvidenceError("fills exceed requested quantity")
        cancelled = requested_quantity_units - consumed_total
        return HaircutAudit(
            self.market_ref,
            self.outcome,
            self.action,
            self.rule_version,
            self.raw_quantity_units,
            self.ignored_quantity_units,
            self.effective_quantity_units,
            consumed_total,
            cancelled,
            self.effective_quantity_units - consumed_total,
            tuple(updated),
            self.captured_level_count,
            self.effective_level_count,
        )


def apply_best_level_haircut(
    order_book: CanonicalOrderBook,
    *,
    outcome: OutcomeSide | str,
    action: Side | str,
    maximum_observed_levels: int = 6,
    maximum_effective_levels: int = 5,
) -> tuple[tuple[CanonicalLevel, ...], HaircutAudit]:
    """Expose the next five levels after ignoring the best captured level.

    The best level is never partially retained.  If removing it would retain
    less than half of the captured raw quantity, the evidence is rejected rather
    than being massaged into a non-contractual fill.
    """

    if maximum_observed_levels != 6 or maximum_effective_levels != 5:
        raise LiquidityEvidenceError(
            "best-level-haircut-v1 requires 6 observed and 5 effective levels"
        )
    side = OutcomeSide(outcome)
    order_side = Side(action)
    source = order_book.asks[side] if order_side is Side.BUY else order_book.bids[side]
    captured = tuple(source[:maximum_observed_levels])
    if not captured:
        raise LiquidityEvidenceError("order book side has no captured liquidity")
    raw_total = sum(int(level.quantity) for level in captured)
    ignored = int(captured[0].quantity)
    if ignored * 2 > raw_total:
        raise LiquidityEvidenceError(
            "ignored best-level depth would violate the 50% retained floor"
        )
    effective = tuple(captured[1 : 1 + maximum_effective_levels])
    effective_total = sum(int(level.quantity) for level in effective)
    if effective_total * 2 < raw_total:
        raise LiquidityEvidenceError("captured tail does not retain at least 50% of raw depth")
    levels = tuple(
        HaircutLevelAudit(
            index,
            int(level.price),
            int(level.quantity),
            int(level.quantity) if index == 0 else 0,
            0 if index == 0 else int(level.quantity),
            0,
            0,
        )
        for index, level in enumerate(captured)
    )
    audit = HaircutAudit(
        order_book.market_key.market_ref,
        side,
        order_side,
        LIQUIDITY_HAIRCUT_CONTRACT_VERSION,
        raw_total,
        ignored,
        effective_total,
        0,
        0,
        effective_total,
        levels,
        len(captured),
        len(effective),
    )
    return effective, audit


def effective_levels_v1(
    order_book: CanonicalOrderBook,
    outcome: OutcomeSide | str,
    action: Side | str,
) -> tuple[CanonicalLevel, ...]:
    return apply_best_level_haircut(order_book, outcome=outcome, action=action)[0]


# Contract-oriented aliases.
BestLevelHaircut = HaircutAudit
LiquidityHaircutAudit = HaircutAudit
build_best_level_haircut = apply_best_level_haircut
best_level_haircut = apply_best_level_haircut
calculate_haircut = apply_best_level_haircut


