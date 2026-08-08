"""Private, auditable virtual-liquidity state for paper execution.

The immutable order-book snapshot is the displayed source of truth.  This module
contains the deterministic conservative execution view derived from that snapshot
and the per-agent state used to consume it.  It never mutates a market-data object.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, cast

from vtrade.domain.types import OrderBookSnapshot, PriceLevel, Side

LIQUIDITY_HAIRCUT_RULE_VERSION = "best-level-haircut-v1"
_AUDIT_QUANTUM = Decimal("0.000000000001")


class _Fill(Protocol):
    price: Decimal
    shares: Decimal


@dataclass(frozen=True, slots=True)
class HaircutBookLevel:
    """One normalized displayed price level and its simulator-only view."""

    level_index: int
    price: Decimal
    displayed_shares: Decimal
    ignored_shares: Decimal
    effective_shares: Decimal
    executable: bool = True

    def __post_init__(self) -> None:
        if self.level_index < 0:
            raise ValueError("haircut level index cannot be negative")
        values = (
            self.price,
            self.displayed_shares,
            self.ignored_shares,
            self.effective_shares,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("haircut level values must be finite")
        if not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("haircut level price must be between zero and one")
        if self.displayed_shares <= 0:
            raise ValueError("haircut displayed shares must be positive")
        if not Decimal(0) <= self.ignored_shares <= self.displayed_shares:
            raise ValueError("haircut ignored shares are outside displayed depth")
        if self.effective_shares != self.displayed_shares - self.ignored_shares:
            raise ValueError("haircut effective shares are inconsistent")
        if self.effective_shares < 0:
            raise ValueError("haircut effective shares cannot be negative")


@dataclass(frozen=True, slots=True)
class EffectiveLiquidityBook:
    """Normalized raw depth plus the bounded executable levels."""

    side: Side
    raw_levels: tuple[HaircutBookLevel, ...]
    executable_levels: tuple[HaircutBookLevel, ...]
    raw_depth_shares: Decimal
    best_level_fraction: Decimal
    ignored_depth_shares: Decimal
    ignored_fraction: Decimal
    effective_depth_shares: Decimal
    best_level_price: Decimal | None
    maximum_book_depth: int
    ignored_best_levels: int
    maximum_ignored_depth_fraction: Decimal

    def __post_init__(self) -> None:
        if self.maximum_book_depth <= 0:
            raise ValueError("maximum executable book depth must be positive")
        if self.ignored_best_levels < 0:
            raise ValueError("ignored best levels cannot be negative")
        if not Decimal(0) <= self.maximum_ignored_depth_fraction <= Decimal(1):
            raise ValueError("maximum ignored depth fraction must be between zero and one")
        if any(not level.executable for level in self.executable_levels):
            raise ValueError("executable levels must be marked executable")
        if len(self.executable_levels) > self.maximum_book_depth:
            raise ValueError("effective book exceeds the configured executable depth")


def effective_liquidity_book(
    snapshot: OrderBookSnapshot,
    *,
    side: Side,
    maximum_book_depth: int,
    ignored_best_levels: int = 0,
    maximum_ignored_depth_fraction: Decimal = Decimal(0),
) -> EffectiveLiquidityBook:
    """Build the deterministic private execution view for one book side.

    Price levels are aggregated before ordering.  The raw selection includes the
    executable depth plus the configured number of best levels reserved for the
    haircut.  For the active rule (one ignored level), the ignored quantity is
    exactly ``min(best_level_size, maximum_fraction * raw_depth)``.  Supporting
    more than one ignored level applies the same bounded haircut to each of those
    leading levels while retaining the same raw-depth and executable-depth caps.
    """

    if (
        not isinstance(maximum_book_depth, int)
        or isinstance(maximum_book_depth, bool)
        or maximum_book_depth <= 0
    ):
        raise ValueError("maximum executable book depth must be a positive integer")
    if (
        not isinstance(ignored_best_levels, int)
        or isinstance(ignored_best_levels, bool)
        or ignored_best_levels < 0
    ):
        raise ValueError("ignored best levels must be a non-negative integer")
    if (
        not maximum_ignored_depth_fraction.is_finite()
        or not Decimal(0) <= maximum_ignored_depth_fraction <= Decimal(1)
    ):
        raise ValueError("maximum ignored depth fraction must be between zero and one")

    source = snapshot.asks if side is Side.BUY else snapshot.bids
    aggregated: dict[Decimal, Decimal] = {}
    for level in source:
        if (
            not level.price.is_finite()
            or not Decimal(0) <= level.price <= Decimal(1)
            or not level.size.is_finite()
            or level.size <= 0
        ):
            raise ValueError("order-book level has an invalid price or size")
        aggregated[level.price] = aggregated.get(level.price, Decimal(0)) + level.size

    ordered = sorted(
        aggregated.items(),
        key=lambda item: item[0],
        reverse=side is Side.SELL,
    )
    raw_limit = maximum_book_depth + ignored_best_levels
    selected = ordered[:raw_limit]
    raw_depth = sum((size for _price, size in selected), start=Decimal(0))
    ignored_cap = raw_depth * maximum_ignored_depth_fraction
    raw_levels: list[HaircutBookLevel] = []
    for index, (price, displayed) in enumerate(selected):
        ignored = (
            min(displayed, ignored_cap) if index < ignored_best_levels else Decimal(0)
        )
        raw_levels.append(
            HaircutBookLevel(
                level_index=index,
                price=price,
                displayed_shares=displayed,
                ignored_shares=ignored,
                effective_shares=displayed - ignored,
            )
        )

    positive = [level for level in raw_levels if level.effective_shares > 0]
    executable_indexes = {level.level_index for level in positive[:maximum_book_depth]}
    normalized_levels = tuple(
        HaircutBookLevel(
            level.level_index,
            level.price,
            level.displayed_shares,
            level.ignored_shares,
            level.effective_shares,
            level.level_index in executable_indexes,
        )
        for level in raw_levels
    )
    executable = tuple(level for level in normalized_levels if level.executable)
    ignored = sum((level.ignored_shares for level in normalized_levels), start=Decimal(0))
    effective = sum((level.effective_shares for level in executable), start=Decimal(0))
    best_fraction = (
        normalized_levels[0].displayed_shares / raw_depth
        if normalized_levels and raw_depth
        else Decimal(0)
    )
    ignored_fraction = ignored / raw_depth if raw_depth else Decimal(0)
    return EffectiveLiquidityBook(
        side=side,
        raw_levels=normalized_levels,
        executable_levels=executable,
        raw_depth_shares=raw_depth,
        best_level_fraction=best_fraction,
        ignored_depth_shares=ignored,
        ignored_fraction=ignored_fraction,
        effective_depth_shares=effective,
        best_level_price=normalized_levels[0].price if normalized_levels else None,
        maximum_book_depth=maximum_book_depth,
        ignored_best_levels=ignored_best_levels,
        maximum_ignored_depth_fraction=maximum_ignored_depth_fraction,
    )


@dataclass(frozen=True, slots=True)
class VirtualLiquidityLevel:
    """One displayed level and the capacity private to the current agent."""

    level_index: int
    price: Decimal
    displayed_shares: Decimal
    available_shares: Decimal
    ignored_shares: Decimal = Decimal(0)
    effective_shares: Decimal | None = None
    executable: bool = True

    def __post_init__(self) -> None:
        if self.level_index < 0:
            raise ValueError("virtual-liquidity level index cannot be negative")
        effective = (
            self.displayed_shares - self.ignored_shares
            if self.effective_shares is None
            else self.effective_shares
        )
        if self.effective_shares is None:
            object.__setattr__(self, "effective_shares", effective)
        values = (
            self.price,
            self.displayed_shares,
            self.available_shares,
            self.ignored_shares,
            effective,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("virtual-liquidity level values must be finite")
        if (
            not Decimal(0) <= self.price <= Decimal(1)
            or self.displayed_shares <= 0
            or not Decimal(0) <= self.ignored_shares <= self.displayed_shares
            or effective != self.displayed_shares - self.ignored_shares
        ):
            raise ValueError("virtual-liquidity level values are invalid")
        if not self.executable and self.available_shares != 0:
            raise ValueError("non-executable virtual depth cannot be available")
        if not Decimal(0) <= self.available_shares <= effective:
            raise ValueError("virtual-liquidity available depth is invalid")


@dataclass(frozen=True, slots=True)
class VirtualLiquidityLevelMetrics:
    level_index: int
    price: Decimal
    displayed_shares: Decimal
    available_shares: Decimal
    consumed_shares: Decimal
    cancelled_shares: Decimal
    remaining_shares: Decimal
    ignored_shares: Decimal = Decimal(0)
    effective_shares: Decimal | None = None
    executable: bool = True

    def __post_init__(self) -> None:
        effective = (
            self.displayed_shares - self.ignored_shares
            if self.effective_shares is None
            else self.effective_shares
        )
        if self.effective_shares is None:
            object.__setattr__(self, "effective_shares", effective)
        values = (
            self.price,
            self.displayed_shares,
            self.available_shares,
            self.consumed_shares,
            self.cancelled_shares,
            self.remaining_shares,
            self.ignored_shares,
            effective,
        )
        if self.level_index < 0 or any(
            not value.is_finite() or value < 0 for value in values
        ):
            raise ValueError("virtual-liquidity metric dimensions are invalid")
        if (
            not Decimal(0) <= self.price <= Decimal(1)
            or self.displayed_shares <= 0
            or not Decimal(0) <= self.ignored_shares <= self.displayed_shares
            or effective != self.displayed_shares - self.ignored_shares
        ):
            raise ValueError("virtual-liquidity metric values are invalid")
        if not self.executable and self.available_shares != 0:
            raise ValueError("non-executable metric depth cannot be available")
        if self.available_shares > effective:
            raise ValueError("virtual-liquidity available depth exceeds effective depth")
        if self.consumed_shares > self.available_shares:
            raise ValueError("virtual-liquidity level consumption exceeds available depth")
        if self.remaining_shares != self.available_shares - self.consumed_shares:
            raise ValueError("virtual-liquidity level remaining depth is inconsistent")


@dataclass(frozen=True, slots=True)
class VirtualLiquidityMetrics:
    """Audit metrics for one order in one versioned private liquidity context."""

    context_version: str
    agent_id: str
    agent_cycle_id: str
    snapshot_id: str
    token_id: str
    side: Side
    requested_shares: Decimal
    available_shares: Decimal
    consumed_shares: Decimal
    cancelled_shares: Decimal
    remaining_shares: Decimal
    levels: tuple[VirtualLiquidityLevelMetrics, ...] = ()
    rule_version: str = LIQUIDITY_HAIRCUT_RULE_VERSION
    ignored_best_levels: int = 0
    maximum_ignored_depth_fraction: Decimal = Decimal(0)
    raw_depth_shares: Decimal = Decimal(0)
    best_level_fraction: Decimal = Decimal(0)
    ignored_depth_shares: Decimal = Decimal(0)
    ignored_fraction: Decimal = Decimal(0)
    effective_depth_shares: Decimal = Decimal(0)
    best_level_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.context_version,
                self.agent_id,
                self.agent_cycle_id,
                self.snapshot_id,
                self.token_id,
                self.rule_version,
            )
        ):
            raise ValueError("virtual-liquidity metric identity is required")
        if self.ignored_best_levels < 0:
            raise ValueError("ignored best levels cannot be negative")
        if (
            not self.maximum_ignored_depth_fraction.is_finite()
            or not Decimal(0) <= self.maximum_ignored_depth_fraction <= Decimal(1)
        ):
            raise ValueError("maximum ignored depth fraction is invalid")
        values = (
            self.requested_shares,
            self.available_shares,
            self.consumed_shares,
            self.cancelled_shares,
            self.remaining_shares,
            self.raw_depth_shares,
            self.best_level_fraction,
            self.ignored_depth_shares,
            self.ignored_fraction,
            self.effective_depth_shares,
        )
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("virtual-liquidity totals cannot be negative")
        if self.best_level_fraction > Decimal(1) or self.ignored_fraction > Decimal(1):
            raise ValueError("virtual-liquidity fractions cannot exceed one")
        if self.consumed_shares > self.available_shares:
            raise ValueError("virtual-liquidity consumption exceeds available depth")
        if self.cancelled_shares != self.requested_shares - self.consumed_shares:
            raise ValueError("virtual-liquidity cancellation total is inconsistent")
        if self.remaining_shares != self.available_shares - self.consumed_shares:
            raise ValueError("virtual-liquidity remaining total is inconsistent")
        if self.raw_depth_shares and self.ignored_depth_shares > self.raw_depth_shares:
            raise ValueError("virtual-liquidity ignored depth exceeds raw depth")
        if self.raw_depth_shares and self.ignored_fraction != _audit_ratio(
            self.ignored_depth_shares, self.raw_depth_shares
        ):
            raise ValueError("virtual-liquidity ignored fraction is inconsistent")
        if self.levels:
            displayed = sum((level.displayed_shares for level in self.levels), start=Decimal(0))
            ignored = sum((level.ignored_shares for level in self.levels), start=Decimal(0))
            effective = sum(
                (
                    cast(Decimal, level.effective_shares)
                    for level in self.levels
                    if level.executable
                ),
                start=Decimal(0),
            )
            if displayed != self.raw_depth_shares:
                raise ValueError("virtual-liquidity raw depth does not match its levels")
            if ignored != self.ignored_depth_shares:
                raise ValueError("virtual-liquidity ignored depth does not match its levels")
            if effective != self.effective_depth_shares:
                raise ValueError("virtual-liquidity effective depth does not match its levels")
            if self.available_shares != sum(
                (level.available_shares for level in self.levels), start=Decimal(0)
            ):
                raise ValueError("virtual-liquidity available depth does not match its levels")
            best = min(self.levels, key=lambda level: level.level_index)
            expected_best_fraction = (
                _audit_ratio(best.displayed_shares, displayed)
                if displayed
                else Decimal(0)
            )
            if self.best_level_fraction != expected_best_fraction:
                raise ValueError("virtual-liquidity best-level fraction is inconsistent")
            if self.best_level_price != best.price:
                raise ValueError("virtual-liquidity best-level price is inconsistent")


@dataclass(frozen=True, slots=True)
class VirtualLiquidityReservation:
    """The private book view reserved for one deterministic execution attempt."""

    order_id: str
    context_version: str
    agent_id: str
    agent_cycle_id: str
    snapshot_id: str
    token_id: str
    side: Side
    snapshot: OrderBookSnapshot
    levels: tuple[VirtualLiquidityLevel, ...]
    existing_metrics: VirtualLiquidityMetrics | None = None
    retry_portfolio: object | None = None
    retry_now: datetime | None = None
    rule_version: str = LIQUIDITY_HAIRCUT_RULE_VERSION
    ignored_best_levels: int = 0
    maximum_ignored_depth_fraction: Decimal = Decimal(0)


def private_snapshot(
    snapshot: OrderBookSnapshot,
    *,
    side: Side,
    levels: Sequence[VirtualLiquidityLevel],
) -> OrderBookSnapshot:
    """Return a snapshot with only this agent's remaining side depth displayed."""

    remaining = tuple(
        PriceLevel(level.price, level.available_shares)
        for level in levels
        if level.available_shares > 0
    )
    if side is Side.BUY:
        return OrderBookSnapshot(
            snapshot.token_id,
            snapshot.condition_id,
            snapshot.observed_at,
            snapshot.source_created_at,
            snapshot.bids,
            remaining,
            snapshot.tick_size,
            snapshot.minimum_order_size,
            snapshot.negative_risk,
            snapshot.artifact,
        )
    return OrderBookSnapshot(
        snapshot.token_id,
        snapshot.condition_id,
        snapshot.observed_at,
        snapshot.source_created_at,
        remaining,
        snapshot.asks,
        snapshot.tick_size,
        snapshot.minimum_order_size,
        snapshot.negative_risk,
        snapshot.artifact,
    )


def consumed_by_level(
    reservation: VirtualLiquidityReservation,
    fills: Sequence[_Fill],
) -> dict[int, Decimal]:
    """Map deterministic fills back to immutable level indexes."""

    consumed = {level.level_index: Decimal(0) for level in reservation.levels}
    for fill in fills:
        price = fill.price
        shares = fill.shares
        remaining = shares
        for level in reservation.levels:
            if level.price != price:
                continue
            capacity = level.available_shares - consumed[level.level_index]
            if capacity <= 0:
                continue
            portion = min(remaining, capacity)
            consumed[level.level_index] += portion
            remaining -= portion
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError("execution fill exceeds its private virtual liquidity view")
    return consumed


def metrics_for_fills(
    reservation: VirtualLiquidityReservation,
    fills: Sequence[_Fill],
    *,
    requested_shares: Decimal,
) -> VirtualLiquidityMetrics:
    consumed_by_index = consumed_by_level(reservation, fills)
    consumed = sum(consumed_by_index.values(), start=Decimal(0))
    if consumed > requested_shares:
        raise ValueError("virtual-liquidity fills exceed requested shares")
    cancelled = requested_shares - consumed
    level_metrics = tuple(
        VirtualLiquidityLevelMetrics(
            level_index=level.level_index,
            price=level.price,
            displayed_shares=level.displayed_shares,
            available_shares=level.available_shares,
            consumed_shares=consumed_by_index[level.level_index],
            # Cancellation remains an order-level remainder.  Ignored depth is
            # never represented as cancelled capacity.
            cancelled_shares=Decimal(0),
            remaining_shares=(level.available_shares - consumed_by_index[level.level_index]),
            ignored_shares=level.ignored_shares,
            effective_shares=level.effective_shares,
            executable=level.executable,
        )
        for level in reservation.levels
    )
    raw_depth = sum((level.displayed_shares for level in level_metrics), start=Decimal(0))
    ignored_depth = sum((level.ignored_shares for level in level_metrics), start=Decimal(0))
    effective_depth = sum(
        (
            cast(Decimal, level.effective_shares)
            for level in level_metrics
            if level.executable
        ),
        start=Decimal(0),
    )
    best = min(level_metrics, key=lambda level: level.level_index) if level_metrics else None
    return VirtualLiquidityMetrics(
        context_version=reservation.context_version,
        agent_id=reservation.agent_id,
        agent_cycle_id=reservation.agent_cycle_id,
        snapshot_id=reservation.snapshot_id,
        token_id=reservation.token_id,
        side=reservation.side,
        requested_shares=requested_shares,
        available_shares=sum(
            (level.available_shares for level in level_metrics), start=Decimal(0)
        ),
        consumed_shares=consumed,
        cancelled_shares=cancelled,
        remaining_shares=sum(
            (metric.remaining_shares for metric in level_metrics), start=Decimal(0)
        ),
        levels=level_metrics,
        rule_version=reservation.rule_version,
        ignored_best_levels=reservation.ignored_best_levels,
        maximum_ignored_depth_fraction=reservation.maximum_ignored_depth_fraction,
        raw_depth_shares=raw_depth,
        best_level_fraction=(
            _audit_ratio(best.displayed_shares, raw_depth)
            if best and raw_depth
            else Decimal(0)
        ),
        ignored_depth_shares=ignored_depth,
        ignored_fraction=(
            _audit_ratio(ignored_depth, raw_depth) if raw_depth else Decimal(0)
        ),
        effective_depth_shares=effective_depth,
        best_level_price=best.price if best else None,
    )


def _audit_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return (numerator / denominator).quantize(_AUDIT_QUANTUM, rounding=ROUND_HALF_UP)
