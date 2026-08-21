"""Canonical Kalshi binary market and execution value types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import NewType
from uuid import NAMESPACE_URL, UUID, uuid5

MicroDollars = NewType("MicroDollars", int)
MoneyMicros = NewType("MoneyMicros", int)
PriceMicros = NewType("PriceMicros", int)
ContractQuantity = NewType("ContractQuantity", int)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_micro_dollars(value: Decimal | str | int) -> MicroDollars:
    if isinstance(value, float):
        raise ValueError("floating-point values are not accepted for exact money")
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("money must be an exact decimal value") from exc
    if not amount.is_finite():
        raise ValueError("money must be finite")
    scaled = amount * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise ValueError("money has precision finer than one micro-dollar")
    return MicroDollars(int(scaled))


class MarketStatus(StrEnum):
    INITIALIZED = "initialized"
    ACTIVE = "active"
    INACTIVE = "inactive"
    OPEN = "open"
    CLOSED = "closed"
    DETERMINED = "determined"
    DISPUTED = "disputed"
    AMENDED = "amended"
    FINALIZED = "finalized"
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
    source_endpoint: str | None = None
    request_identity: str | None = None
    source_timestamp: datetime | None = None
    observed_at: datetime | None = None
    historical_cutoff: datetime | None = None
    schema_version: str = "vtrade-binary-market-v1"

    def __post_init__(self) -> None:
        if (
            len(self.sha256) != 64
            or self.sha256.lower() != self.sha256
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("artifact sha256 must be a lowercase 64-character digest")
        if self.byte_length < 0:
            raise ValueError("artifact byte length cannot be negative")
        if not self.uri:
            raise ValueError("artifact URI is required")
        for timestamp in (self.source_timestamp, self.observed_at, self.historical_cutoff):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("artifact timestamps must be timezone-aware")
        if not self.schema_version:
            raise ValueError("artifact schema version is required")


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    size: Decimal

def _require_opaque_reference(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty opaque string")
    return value


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _exact_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{field} must be an exact decimal string or Decimal")
    try:
        parsed = Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def to_price_micros(value: Decimal | str | int, *, field: str = "price") -> PriceMicros:
    """Parse an exact dollar value into an inclusive [0, $1] micro-dollar price."""

    if isinstance(value, str) and value.startswith("$"):
        value = value[1:]
    amount = _exact_decimal(value, field)
    scaled = amount * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field} has precision finer than one micro-dollar")
    integer = int(scaled)
    if not 0 <= integer <= 1_000_000:
        raise ValueError(f"{field} must be between zero and one dollar")
    return PriceMicros(integer)


def to_money_micros(value: Decimal | str | int, *, field: str = "money") -> MoneyMicros:
    amount = to_micro_dollars(value)
    return MoneyMicros(int(amount))


def to_contract_quantity(
    value: Decimal | str | int, *, field: str = "contract quantity"
) -> ContractQuantity:
    """Parse exact hundredths of a contract (1.55 contracts -> 155 units)."""

    amount = _exact_decimal(value, field)
    scaled = amount * Decimal(100)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field} must be representable in hundredths of a contract")
    integer = int(scaled)
    if integer < 0:
        raise ValueError(f"{field} cannot be negative")
    return ContractQuantity(integer)


class OutcomeSide(StrEnum):
    YES = "YES"
    NO = "NO"


def _stable_key_uuid(venue: str, kind: str, reference: str) -> UUID:
    return uuid5(NAMESPACE_URL, "vtrade-key\x1f" + venue + "\x1f" + kind + "\x1f" + reference)


@dataclass(frozen=True, slots=True, init=False)
class SeriesKey:
    venue: str
    kind: str
    series_ref: str

    def __init__(
        self,
        series_ref: str | None = None,
        *,
        venue: str = "kalshi",
        kind: str = "series",
        ref: str | None = None,
    ) -> None:
        if series_ref is not None and ref is not None and series_ref != ref:
            raise ValueError("series_ref and ref conflict")
        reference = series_ref if series_ref is not None else ref
        object.__setattr__(self, "venue", _require_opaque_reference(venue, "venue"))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "series_ref", _require_opaque_reference(reference, "series_ref"))
        if self.venue != "kalshi" or self.kind != "series":
            raise ValueError("SeriesKey must use the kalshi series namespace")

    @classmethod
    def from_ref(cls, series_ref: str) -> SeriesKey:
        return cls(series_ref)

    @property
    def ref(self) -> str:
        return self.series_ref

    @property
    def canonical(self) -> str:
        return f"{self.venue}:{self.kind}:{self.series_ref}"

    @property
    def stable_id(self) -> UUID:
        return _stable_key_uuid(self.venue, self.kind, self.series_ref)


@dataclass(frozen=True, slots=True, init=False)
class EventKey:
    venue: str
    kind: str
    event_ref: str

    def __init__(
        self,
        event_ref: str | None = None,
        *,
        venue: str = "kalshi",
        kind: str = "event",
        ref: str | None = None,
    ) -> None:
        if event_ref is not None and ref is not None and event_ref != ref:
            raise ValueError("event_ref and ref conflict")
        reference = event_ref if event_ref is not None else ref
        object.__setattr__(self, "venue", _require_opaque_reference(venue, "venue"))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "event_ref", _require_opaque_reference(reference, "event_ref"))
        if self.venue != "kalshi" or self.kind != "event":
            raise ValueError("EventKey must use the kalshi event namespace")

    @classmethod
    def from_ref(cls, event_ref: str) -> EventKey:
        return cls(event_ref)

    @property
    def ref(self) -> str:
        return self.event_ref

    @property
    def canonical(self) -> str:
        return f"{self.venue}:{self.kind}:{self.event_ref}"

    @property
    def stable_id(self) -> UUID:
        return _stable_key_uuid(self.venue, self.kind, self.event_ref)


@dataclass(frozen=True, slots=True, init=False)
class MarketKey:
    venue: str
    kind: str
    market_ref: str

    def __init__(
        self,
        market_ref: str | None = None,
        *,
        venue: str = "kalshi",
        kind: str = "market",
        ref: str | None = None,
    ) -> None:
        if market_ref is not None and ref is not None and market_ref != ref:
            raise ValueError("market_ref and ref conflict")
        reference = market_ref if market_ref is not None else ref
        object.__setattr__(self, "venue", _require_opaque_reference(venue, "venue"))
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "market_ref", _require_opaque_reference(reference, "market_ref"))
        if self.venue != "kalshi" or self.kind != "market":
            raise ValueError("MarketKey must use the kalshi market namespace")

    @classmethod
    def from_ref(cls, market_ref: str) -> MarketKey:
        return cls(market_ref)

    @property
    def ref(self) -> str:
        return self.market_ref

    @property
    def canonical(self) -> str:
        return f"{self.venue}:{self.kind}:{self.market_ref}"

    @property
    def stable_id(self) -> UUID:
        return _stable_key_uuid(self.venue, self.kind, self.market_ref)


@dataclass(frozen=True, slots=True, init=False)
class OutcomeKey:
    market_key: MarketKey
    outcome_side: OutcomeSide

    def __init__(self, market_key: MarketKey, outcome_side: OutcomeSide | str) -> None:
        if not isinstance(market_key, MarketKey):
            raise ValueError("OutcomeKey requires a MarketKey")
        try:
            side = OutcomeSide(outcome_side)
        except ValueError as exc:
            raise ValueError("outcome side must be exactly YES or NO") from exc
        object.__setattr__(self, "market_key", market_key)
        object.__setattr__(self, "outcome_side", side)

    @property
    def canonical(self) -> str:
        return f"{self.market_key.canonical}:{self.outcome_side.value}"

    @property
    def side(self) -> OutcomeSide:
        return self.outcome_side

    @property
    def stable_id(self) -> UUID:
        return _stable_key_uuid(
            self.market_key.venue,
            "outcome",
            self.market_key.market_ref + "\x1f" + self.outcome_side.value,
        )


@dataclass(frozen=True, slots=True)
class PriceRange:
    start: PriceMicros
    end: PriceMicros
    step: PriceMicros

    def __post_init__(self) -> None:
        values = (self.start, self.end, self.step)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("price range values must be integer microdollars")
        if not 0 <= self.start < self.end <= 1_000_000:
            raise ValueError("price range must be within [0, 1] and have positive width")
        if self.step <= 0:
            raise ValueError("price range step must be positive")
        if (self.end - self.start) % self.step:
            raise ValueError("price range end must lie on its declared grid")

    def contains(self, price: PriceMicros | int) -> bool:
        integer = int(price)
        return self.start <= integer <= self.end and (integer - self.start) % self.step == 0


@dataclass(frozen=True, slots=True)
class PriceGrid:
    ranges: tuple[PriceRange, ...]

    def __post_init__(self) -> None:
        if not self.ranges:
            raise ValueError("price grid requires at least one range")
        previous: PriceRange | None = None
        for current in self.ranges:
            if previous is not None and current.start < previous.end:
                raise ValueError("price ranges overlap ambiguously")
            previous = current

    @classmethod
    def from_ranges(cls, raw_ranges: object) -> PriceGrid:
        if not isinstance(raw_ranges, (list, tuple)) or not raw_ranges:
            raise ValueError("price_ranges must be a non-empty array")
        ranges: list[PriceRange] = []
        for index, raw_range in enumerate(raw_ranges):
            if not isinstance(raw_range, Mapping):
                raise ValueError(f"price_ranges[{index}] must be an object")
            missing = [
                field_name for field_name in ("start", "end", "step") if field_name not in raw_range
            ]
            if missing:
                raise ValueError(f"price_ranges[{index}] is missing {', '.join(missing)}")
            ranges.append(
                PriceRange(
                    to_price_micros(raw_range["start"], field=f"price_ranges[{index}].start"),
                    to_price_micros(raw_range["end"], field=f"price_ranges[{index}].end"),
                    to_price_micros(raw_range["step"], field=f"price_ranges[{index}].step"),
                )
            )
        return cls(tuple(ranges))

    def contains(self, price: PriceMicros | int) -> bool:
        return any(price_range.contains(price) for price_range in self.ranges)

    def require(self, price: PriceMicros | int, *, field: str = "price") -> PriceMicros:
        integer = PriceMicros(int(price))
        if not self.contains(integer):
            raise ValueError(f"{field} is not on the market price grid")
        return integer


@dataclass(frozen=True, slots=True)
class CanonicalLevel:
    price: PriceMicros
    quantity: ContractQuantity

    def __post_init__(self) -> None:
        if (
            isinstance(self.price, bool)
            or isinstance(self.quantity, bool)
            or not isinstance(self.price, int)
            or not isinstance(self.quantity, int)
        ):
            raise ValueError("canonical levels require integer price and quantity units")
        if not 0 <= self.price <= 1_000_000:
            raise ValueError("canonical level price is outside [0, 1]")
        if self.quantity <= 0:
            raise ValueError("canonical level quantity must be positive")


@dataclass(frozen=True, slots=True)
class CanonicalOrderBook:
    market_key: MarketKey
    yes_bids: tuple[CanonicalLevel, ...]
    yes_asks: tuple[CanonicalLevel, ...]
    no_bids: tuple[CanonicalLevel, ...]
    no_asks: tuple[CanonicalLevel, ...]
    observed_at: datetime
    cutoff: datetime
    artifact: RawArtifact
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "book observed_at")
        _require_aware(self.cutoff, "book cutoff")
        if self.source_timestamp is not None:
            _require_aware(self.source_timestamp, "book source_timestamp")
            if self.source_timestamp > self.cutoff:
                raise ValueError("book source timestamp is newer than the requested cutoff")
        if self.observed_at > self.cutoff:
            raise ValueError("book observation is newer than the requested cutoff")
        for bids, asks in (
            (self.yes_bids, self.yes_asks),
            (self.no_bids, self.no_asks),
        ):
            if any(left.price <= right.price for left, right in pairwise(bids)):
                raise ValueError("bids must be ordered by descending price")
            if any(left.price >= right.price for left, right in pairwise(asks)):
                raise ValueError("asks must be ordered by ascending price")

    @property
    def bids(self) -> Mapping[OutcomeSide, tuple[CanonicalLevel, ...]]:
        return {OutcomeSide.YES: self.yes_bids, OutcomeSide.NO: self.no_bids}

    @property
    def asks(self) -> Mapping[OutcomeSide, tuple[CanonicalLevel, ...]]:
        return {OutcomeSide.YES: self.yes_asks, OutcomeSide.NO: self.no_asks}

    def best_bid(self, side: OutcomeSide | str) -> CanonicalLevel | None:
        return self.bids[OutcomeSide(side)][0] if self.bids[OutcomeSide(side)] else None

    def best_ask(self, side: OutcomeSide | str) -> CanonicalLevel | None:
        return self.asks[OutcomeSide(side)][0] if self.asks[OutcomeSide(side)] else None

    @property
    def snapshot_id(self) -> UUID:
        return _stable_key_uuid(
            self.market_key.venue,
            "book-snapshot",
            self.market_key.market_ref
            + "\x1f"
            + self.observed_at.isoformat()
            + "\x1f"
            + self.artifact.sha256,
        )


def _parse_book_levels(
    raw_levels: object,
    grid: PriceGrid,
    *,
    field: str,
) -> tuple[CanonicalLevel, ...]:
    if not isinstance(raw_levels, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    parsed: list[CanonicalLevel] = []
    seen_prices: set[int] = set()
    for index, raw_level in enumerate(raw_levels):
        if not isinstance(raw_level, (list, tuple)) or len(raw_level) != 2:
            raise ValueError(f"{field}[{index}] must be [price, quantity]")
        price = grid.require(
            to_price_micros(raw_level[0], field=f"{field}[{index}].price"),
            field=f"{field}[{index}].price",
        )
        quantity = to_contract_quantity(raw_level[1], field=f"{field}[{index}].quantity")
        if int(price) in seen_prices:
            raise ValueError(f"{field} contains duplicate prices")
        seen_prices.add(int(price))
        parsed.append(CanonicalLevel(price, quantity))
    return tuple(sorted(parsed, key=lambda level: level.price, reverse=True))


def build_canonical_order_book(
    market_key: MarketKey,
    grid: PriceGrid,
    yes_bids: object,
    no_bids: object,
    *,
    observed_at: datetime,
    cutoff: datetime,
    artifact: RawArtifact,
    source_timestamp: datetime | None = None,
) -> CanonicalOrderBook:
    """Normalize Kalshi's bid-only book into deterministic reciprocal bids/asks."""

    yes = _parse_book_levels(yes_bids, grid, field="yes_dollars")
    no = _parse_book_levels(no_bids, grid, field="no_dollars")

    def reciprocal(levels: tuple[CanonicalLevel, ...], field: str) -> tuple[CanonicalLevel, ...]:
        derived: list[CanonicalLevel] = []
        for level in levels:
            price = PriceMicros(1_000_000 - level.price)
            grid.require(price, field=field)
            derived.append(CanonicalLevel(price, level.quantity))
        return tuple(sorted(derived, key=lambda level: level.price))

    yes_asks = reciprocal(no, "derived YES ask") if no else ()
    no_asks = reciprocal(yes, "derived NO ask") if yes else ()
    best_yes_bid = yes[0].price if yes else None
    best_no_bid = no[0].price if no else None
    if (
        best_yes_bid is not None
        and best_no_bid is not None
        and best_yes_bid + best_no_bid >= 1_000_000
    ):
        raise ValueError("canonical reciprocal book is crossed")
    return CanonicalOrderBook(
        market_key,
        yes,
        yes_asks,
        no,
        no_asks,
        _require_aware(observed_at, "book observed_at"),
        _require_aware(cutoff, "book cutoff"),
        artifact,
        _require_aware(source_timestamp, "book source_timestamp") if source_timestamp else None,
    )


@dataclass(frozen=True, slots=True)
class Series:
    key: SeriesKey
    title: str
    rules: str | None
    observed_at: datetime
    audit: RawArtifact

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("series title is required")
        _require_aware(self.observed_at, "series observed_at")

    @property
    def series_ref(self) -> str:
        return self.key.series_ref


@dataclass(frozen=True, slots=True)
class BinaryEvent:
    key: EventKey
    series_key: SeriesKey
    title: str
    category: str | None
    observed_at: datetime
    audit: RawArtifact

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("event title is required")
        _require_aware(self.observed_at, "event observed_at")

    @property
    def event_ref(self) -> str:
        return self.key.event_ref

    @property
    def series_ref(self) -> str:
        return self.series_key.series_ref


@dataclass(frozen=True, slots=True)
class CataloguePage:
    requested_cursor: str | None
    next_cursor: str | None
    observed_at: datetime
    series: tuple[Series, ...]
    events: tuple[BinaryEvent, ...]
    markets: tuple[BinaryMarket, ...]
    audit: RawArtifact
    metadata_audits: tuple[RawArtifact, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "catalogue page observed_at")
        if self.next_cursor == "":
            raise ValueError("catalogue cursor must use null for the terminal page")
        if self.next_cursor is not None and not self.next_cursor:
            raise ValueError("catalogue cursor cannot be empty")


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    pages: tuple[CataloguePage, ...]
    data_cutoff: datetime
    historical_cutoff: datetime

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("catalogue snapshot requires at least one page")
        _require_aware(self.data_cutoff, "catalogue data_cutoff")
        _require_aware(self.historical_cutoff, "catalogue historical_cutoff")

    @property
    def markets(self) -> tuple[BinaryMarket, ...]:
        unique: dict[MarketKey, BinaryMarket] = {}
        for page in self.pages:
            for market in page.markets:
                previous = unique.get(market.key)
                if previous is not None and previous != market:
                    raise ValueError("catalogue contains conflicting observations for one market")
                unique[market.key] = market
        return tuple(unique.values())

    @property
    def artifacts(self) -> tuple[RawArtifact, ...]:
        return tuple(
            artifact
            for page in self.pages
            for artifact in (page.audit, *page.metadata_audits)
        )


@dataclass(frozen=True, slots=True)
class BinaryOutcome:
    key: OutcomeKey
    label: str
    eligible: bool

    def __post_init__(self) -> None:
        if self.key.outcome_side not in (OutcomeSide.YES, OutcomeSide.NO):
            raise ValueError("binary outcome side must be YES or NO")

    @property
    def side(self) -> OutcomeSide:
        return self.key.outcome_side


@dataclass(frozen=True, slots=True)
class BinaryMarket:
    key: MarketKey
    series_key: SeriesKey
    event_key: EventKey
    question: str
    resolution_rules: str
    resolution_source: str | None
    open_time: datetime
    close_time: datetime | None
    expected_expiration_time: datetime | None
    latest_expiration_time: datetime | None
    status: MarketStatus
    eligible: bool
    price_grid: PriceGrid
    outcomes: tuple[BinaryOutcome, BinaryOutcome]
    observed_at: datetime
    audit: RawArtifact
    source_updated_at: datetime | None = None
    volume: ContractQuantity = ContractQuantity(0)
    liquidity_micros: MoneyMicros = MoneyMicros(0)

    def __post_init__(self) -> None:
        if not self.question or not self.resolution_rules:
            raise ValueError("binary market question and resolution rules are required")
        if self.status not in {
            MarketStatus.INITIALIZED,
            MarketStatus.ACTIVE,
            MarketStatus.INACTIVE,
            MarketStatus.CLOSED,
            MarketStatus.DETERMINED,
            MarketStatus.DISPUTED,
            MarketStatus.AMENDED,
            MarketStatus.FINALIZED,
        }:
            raise ValueError("binary market has an unsupported lifecycle status")
        if {outcome.key.outcome_side for outcome in self.outcomes} != {
            OutcomeSide.YES,
            OutcomeSide.NO,
        }:
            raise ValueError("binary market must contain exactly YES and NO outcomes")
        if any(outcome.key.market_key != self.key for outcome in self.outcomes):
            raise ValueError("outcome belongs to a different market")
        observed = _require_aware(self.observed_at, "market observed_at")
        object.__setattr__(self, "observed_at", observed)
        if self.close_time is not None:
            _require_aware(self.close_time, "market close_time")
        if self.open_time.tzinfo is None or self.open_time.utcoffset() is None:
            raise ValueError("market open_time must be timezone-aware")
        if self.source_updated_at is not None:
            _require_aware(self.source_updated_at, "market source_updated_at")
        if self.volume < 0 or self.liquidity_micros < 0:
            raise ValueError("market volume and liquidity cannot be negative")

    @property
    def market_ref(self) -> str:
        return self.key.market_ref

    @property
    def event_ref(self) -> str:
        return self.event_key.event_ref

    @property
    def series_ref(self) -> str:
        return self.series_key.series_ref

    @property
    def tradeable(self) -> bool:
        return self.eligible and self.status is MarketStatus.ACTIVE

    @property
    def yes(self) -> BinaryOutcome:
        return next(
            outcome for outcome in self.outcomes if outcome.key.outcome_side is OutcomeSide.YES
        )

    @property
    def no(self) -> BinaryOutcome:
        return next(
            outcome for outcome in self.outcomes if outcome.key.outcome_side is OutcomeSide.NO
        )

    @property
    def snapshot_id(self) -> UUID:
        return _stable_key_uuid(
            self.key.venue,
            "market-snapshot",
            self.key.market_ref
            + "\x1f"
            + self.observed_at.isoformat()
            + "\x1f"
            + self.audit.sha256,
        )


@dataclass(frozen=True, slots=True)
class MarketContext:
    market: BinaryMarket
    order_book: CanonicalOrderBook

    def __post_init__(self) -> None:
        if self.market.key != self.order_book.market_key:
            raise ValueError("market context book belongs to a different market")


@dataclass(frozen=True, slots=True)
class ResolutionObservation:
    market_key: MarketKey
    status: MarketStatus
    result: OutcomeSide | None
    observed_at: datetime
    source_timestamp: datetime | None
    settlement_ts: datetime | None
    audit: RawArtifact
    blocked: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "resolution observed_at")
        if self.source_timestamp is not None:
            _require_aware(self.source_timestamp, "resolution source_timestamp")
        if self.settlement_ts is not None:
            _require_aware(self.settlement_ts, "resolution settlement_ts")
        if self.result is not None and not isinstance(self.result, OutcomeSide):
            object.__setattr__(self, "result", OutcomeSide(self.result))
        incomplete_final = self.status is MarketStatus.FINALIZED and (
            self.result is None or self.settlement_ts is None
        )
        if incomplete_final:
            object.__setattr__(self, "blocked", True)

    @property
    def terminal(self) -> bool:
        return self.status is MarketStatus.FINALIZED and not self.blocked

    @property
    def snapshot_id(self) -> UUID:
        return _stable_key_uuid(
            self.market_key.venue,
            "resolution-snapshot",
            self.market_key.market_ref
            + "\x1f"
            + self.observed_at.isoformat()
            + "\x1f"
            + self.audit.sha256,
        )


# Explicit names for callers that want to make the venue-neutral Kalshi v1
# implementation visible in type annotations without colliding with the
# pre-cutover compatibility model above.
KalshiMarket = BinaryMarket
KalshiOutcome = BinaryOutcome
KalshiMarketStatus = MarketStatus
NormalizedMarket = BinaryMarket
NormalizedOutcome = BinaryOutcome
CanonicalBook = CanonicalOrderBook


