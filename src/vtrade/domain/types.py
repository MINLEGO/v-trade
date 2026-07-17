from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, NewType

MicroDollars = NewType("MicroDollars", int)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_micro_dollars(value: Decimal | str) -> MicroDollars:
    amount = Decimal(value)
    scaled = amount * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise ValueError("money has precision finer than one micro-dollar")
    return MicroDollars(int(scaled))


class Classification(StrEnum):
    DOCUMENTED = "documented"
    INFERRED = "inferred"
    VTRADE_DEVIATION = "vtrade_deviation"


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class RawArtifact:
    sha256: str
    byte_length: int
    uri: str


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    venue_id: str
    slug: str
    title: str
    description: str
    resolution_source: str | None
    opens_at: datetime | None
    closes_at: datetime | None
    active: bool
    closed: bool
    archived: bool
    observed_at: datetime
    source_updated_at: datetime | None
    venue_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Outcome:
    id: str
    market_id: str
    name: str
    venue_token_id: str
    best_bid_micros: MicroDollars | None
    best_ask_micros: MicroDollars | None
    tick_size_micros: MicroDollars
    minimum_order_micros: MicroDollars
    outcome_index: int | None = None
    indicative_price: Decimal | None = None
    tradeable: bool = False
    venue_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Market:
    id: str
    venue_id: str
    event_id: str
    question: str
    resolution_rules: str
    opens_at: datetime | None
    closes_at: datetime | None
    status: MarketStatus
    category: str | None
    volume_micros: MicroDollars
    liquidity_micros: MicroDollars
    venue_metadata: dict[str, Any] = field(default_factory=dict)
    slug: str | None = None
    resolution_source: str | None = None
    tradeable: bool = False
    outcomes: tuple[Outcome, ...] = ()
    observed_at: datetime | None = None
    source_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    token_id: str
    condition_id: str
    observed_at: datetime
    source_created_at: datetime | None
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    tick_size: Decimal
    minimum_order_size: Decimal
    negative_risk: bool
    artifact: RawArtifact

    @property
    def best_bid(self) -> Decimal | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> Decimal | None:
        return min((level.price for level in self.asks), default=None)


@dataclass(frozen=True, slots=True)
class MarketDelta:
    resource: str
    requested_cursor: str | None
    next_cursor: str | None
    observed_at: datetime
    events: tuple[Event, ...]
    markets: tuple[Market, ...]
    artifact: RawArtifact


@dataclass(frozen=True, slots=True)
class Resolution:
    market_id: str
    winning_outcome_id: str
    result: str
    source_created_at: datetime
    observed_at: datetime
    eligible_after: datetime
    artifact: RawArtifact


@dataclass(frozen=True, slots=True)
class OrderIntent:
    id: str
    agent_id: str
    market_id: str
    outcome_id: str
    side: Side
    amount_micros: MicroDollars | None
    shares: Decimal | None
    strategy: str
    thesis: str
    estimated_probability: Decimal
    expected_value_micros: MicroDollars
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CycleSnapshot:
    id: str
    agent_id: str
    cutoff: datetime
    market_snapshot_ids: tuple[str, ...]
    account_state: dict[str, Any]
    history: tuple[dict[str, Any], ...]
