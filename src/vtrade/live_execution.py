"""Refreshed paper-context seam; real venue execution is intentionally disabled."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from vtrade.domain.types import MarketContext


class OrderExecutionUnavailable(RuntimeError):
    """A safe paper execution context could not be obtained."""


class LiveContextError(OrderExecutionUnavailable):
    """The refreshed paper context is missing, stale, or inconsistent."""


@dataclass(frozen=True, slots=True)
class RefreshedPaperContext:
    context_id: str
    market_context: MarketContext
    refreshed_at: datetime

    def __post_init__(self) -> None:
        if not self.context_id:
            raise ValueError("paper context id is required")
        if self.refreshed_at.tzinfo is None or self.refreshed_at.utcoffset() is None:
            raise ValueError("paper context timestamp must be timezone-aware")
        if self.market_context.order_book.observed_at > self.refreshed_at:
            raise ValueError("paper context cannot be observed in the future")
        object.__setattr__(self, "refreshed_at", self.refreshed_at.astimezone(UTC))


class RealExecutionDisabled(OrderExecutionUnavailable):
    """The Kalshi v1 paper release has no authenticated order-submission path."""


class DisabledRealExecutionAdapter:
    """Explicit fail-closed placeholder for a future separately approved adapter."""

    def submit(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise RealExecutionDisabled(
            "real execution is disabled by the vtrade-kalshi-v1 paper release"
        )


__all__ = [
    "DisabledRealExecutionAdapter",
    "LiveContextError",
    "OrderExecutionUnavailable",
    "RealExecutionDisabled",
    "RefreshedPaperContext",
]
