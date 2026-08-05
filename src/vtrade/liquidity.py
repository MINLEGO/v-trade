"""Private, auditable virtual-liquidity state for paper execution.

The immutable order-book snapshot is the displayed source of truth.  This module
contains only the per-agent view derived from that snapshot; it never mutates a
market-data object.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from vtrade.domain.types import OrderBookSnapshot, PriceLevel, Side


class _Fill(Protocol):
    price: Decimal
    shares: Decimal


@dataclass(frozen=True, slots=True)
class VirtualLiquidityLevel:
    """One displayed level and the capacity private to the current agent."""

    level_index: int
    price: Decimal
    displayed_shares: Decimal
    available_shares: Decimal

    def __post_init__(self) -> None:
        if self.level_index < 0:
            raise ValueError("virtual-liquidity level index cannot be negative")
        if (
            not self.price.is_finite()
            or not Decimal(0) <= self.price <= Decimal(1)
            or not self.displayed_shares.is_finite()
            or self.displayed_shares <= 0
            or not self.available_shares.is_finite()
            or not Decimal(0) <= self.available_shares <= self.displayed_shares
        ):
            raise ValueError("virtual-liquidity level values are invalid")


@dataclass(frozen=True, slots=True)
class VirtualLiquidityLevelMetrics:
    level_index: int
    price: Decimal
    displayed_shares: Decimal
    available_shares: Decimal
    consumed_shares: Decimal
    cancelled_shares: Decimal
    remaining_shares: Decimal

    def __post_init__(self) -> None:
        values = (
            self.displayed_shares,
            self.available_shares,
            self.consumed_shares,
            self.cancelled_shares,
            self.remaining_shares,
        )
        if self.level_index < 0 or not self.price.is_finite():
            raise ValueError("virtual-liquidity metric dimensions are invalid")
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("virtual-liquidity metric values cannot be negative")
        if self.available_shares > self.displayed_shares:
            raise ValueError("virtual-liquidity available depth exceeds displayed depth")
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

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.context_version,
                self.agent_id,
                self.agent_cycle_id,
                self.snapshot_id,
                self.token_id,
            )
        ):
            raise ValueError("virtual-liquidity metric identity is required")
        values = (
            self.requested_shares,
            self.available_shares,
            self.consumed_shares,
            self.cancelled_shares,
            self.remaining_shares,
        )
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("virtual-liquidity totals cannot be negative")
        if self.consumed_shares > self.available_shares:
            raise ValueError("virtual-liquidity consumption exceeds available depth")
        if self.cancelled_shares != self.requested_shares - self.consumed_shares:
            raise ValueError("virtual-liquidity cancellation total is inconsistent")
        if self.remaining_shares != self.available_shares - self.consumed_shares:
            raise ValueError("virtual-liquidity remaining total is inconsistent")


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
            cancelled_shares=Decimal(0),
            remaining_shares=(
                level.available_shares - consumed_by_index[level.level_index]
            ),
        )
        for level in reservation.levels
    )
    return VirtualLiquidityMetrics(
        context_version=reservation.context_version,
        agent_id=reservation.agent_id,
        agent_cycle_id=reservation.agent_cycle_id,
        snapshot_id=reservation.snapshot_id,
        token_id=reservation.token_id,
        side=reservation.side,
        requested_shares=requested_shares,
        available_shares=sum(
            (level.available_shares for level in reservation.levels), start=Decimal(0)
        ),
        consumed_shares=consumed,
        cancelled_shares=cancelled,
        remaining_shares=sum(
            (metric.remaining_shares for metric in level_metrics), start=Decimal(0)
        ),
        levels=level_metrics,
    )
