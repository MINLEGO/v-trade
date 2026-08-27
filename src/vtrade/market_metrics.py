"""Pure, freeze-scoped calculations for Kalshi discovery metrics.

The transport adapter owns the Kalshi candlestick representation.  This module
owns the metric interface and keeps all calculations independent of HTTP and
PostgreSQL so the same rules are exercised by production and replay tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from vtrade.domain.types import (
    BinaryMarket,
    CanonicalLevel,
    CanonicalOrderBook,
    MarketKey,
    OutcomeSide,
    RawArtifact,
)

METRIC_FORMULA_VERSION = "kalshi-market-metrics-v1"
CANDLE_INTERVAL = timedelta(hours=1)
METRIC_WINDOW = timedelta(hours=24)
METRIC_LOOKBACK = timedelta(hours=48)
SPREAD_HALF_SCORE_CENTS = Decimal("10")
DEPTH_SATURATION_UNITS = Decimal("10000")
ACTIVITY_SATURATION_UNITS = Decimal("10000")
DEPTH_BAND_MICROS = 50_000
DECIMAL_PLACES = Decimal("0.0000000001")


def format_metric_decimal(value: Decimal | None) -> str | None:
    """Render a metric decimal without introducing binary floating-point noise."""

    if value is None:
        return None
    return format(value.quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True, slots=True)
class MarketCandlestick:
    """One normalized hourly market candle returned by Kalshi."""

    end_period: datetime
    close_price_micros: int | None
    previous_price_micros: int | None
    volume_units: int
    synthetic: bool = False

    def __post_init__(self) -> None:
        if self.end_period.tzinfo is None or self.end_period.utcoffset() is None:
            raise ValueError("candlestick end_period must be timezone-aware")
        if self.close_price_micros is not None and not 0 <= self.close_price_micros <= 1_000_000:
            raise ValueError("candlestick close price is outside [0, 1]")
        if (
            self.previous_price_micros is not None
            and not 0 <= self.previous_price_micros <= 1_000_000
        ):
            raise ValueError("candlestick previous price is outside [0, 1]")
        if self.volume_units < 0:
            raise ValueError("candlestick volume cannot be negative")


@dataclass(frozen=True, slots=True)
class MarketCandlestickBatch:
    """Candles and the immutable raw response that supplied them."""

    market_key: MarketKey
    candlesticks: tuple[MarketCandlestick, ...]
    audit: RawArtifact

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.candlesticks, key=lambda item: item.end_period))
        if len({item.end_period for item in ordered}) != len(ordered):
            raise ValueError("candlestick timestamps must be unique")
        object.__setattr__(self, "candlesticks", ordered)


@dataclass(frozen=True, slots=True)
class MarketMetricSnapshot:
    """All discovery metrics for one market at one immutable freeze."""

    market_key: MarketKey
    volume_24h_units: int
    volatility_micros: int | None
    volume_trend: str
    volume_trend_delta: Decimal | None
    competitive_score: Decimal | None
    indicative_yes_price_micros: int | None
    indicative_no_price_micros: int | None
    recent_volume_units: int
    baseline_volume_units: int
    volatility_sample_count: int
    recent_bucket_count: int
    baseline_bucket_count: int
    as_of_at: datetime
    formula_version: str = METRIC_FORMULA_VERSION
    source_artifacts: tuple[RawArtifact, ...] = ()

    def __post_init__(self) -> None:
        if self.volume_24h_units < 0:
            raise ValueError("24-hour volume cannot be negative")
        if self.volatility_micros is not None and self.volatility_micros < 0:
            raise ValueError("volatility cannot be negative")
        if self.volume_trend not in {"increasing", "decreasing", "flat", "insufficient_data"}:
            raise ValueError("unsupported volume trend")
        for value, field in (
            (self.indicative_yes_price_micros, "YES indicative price"),
            (self.indicative_no_price_micros, "NO indicative price"),
        ):
            if value is not None and not 0 <= value <= 1_000_000:
                raise ValueError(f"{field} is outside [0, 1]")
        if (
            self.indicative_yes_price_micros is not None
            and self.indicative_no_price_micros is not None
            and self.indicative_yes_price_micros + self.indicative_no_price_micros != 1_000_000
        ):
            raise ValueError("indicative YES and NO prices must be exact complements")
        for count, field in (
            (self.recent_volume_units, "recent volume"),
            (self.baseline_volume_units, "baseline volume"),
            (self.volatility_sample_count, "volatility sample count"),
            (self.recent_bucket_count, "recent bucket count"),
            (self.baseline_bucket_count, "baseline bucket count"),
        ):
            if count < 0:
                raise ValueError(f"{field} cannot be negative")
        for decimal_value, field in (
            (self.competitive_score, "competitive score"),
            (self.volume_trend_delta, "volume trend delta"),
        ):
            if decimal_value is not None and not decimal_value.is_finite():
                raise ValueError(f"{field} must be finite")
        if self.competitive_score is not None and not 0 <= self.competitive_score <= 1:
            raise ValueError("competitive score must be in [0, 1]")
        if self.volume_trend == "insufficient_data" and self.volume_trend_delta is not None:
            raise ValueError("insufficient volume data cannot have a trend delta")
        if self.baseline_volume_units == 0 and self.volume_trend_delta is not None:
            raise ValueError("zero baseline volume cannot have a trend delta")
        if (
            self.volume_trend != "insufficient_data"
            and self.baseline_volume_units > 0
            and self.volume_trend_delta is None
        ):
            raise ValueError("complete volume data requires a trend delta")
        if (
            self.volume_trend_delta is not None
            and self.baseline_volume_units > 0
            and self.volume_trend_delta.quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP)
            != (
                Decimal(self.recent_volume_units - self.baseline_volume_units)
                / Decimal(self.baseline_volume_units)
            ).quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP)
        ):
            raise ValueError("volume trend delta does not match the recorded windows")
        if self.as_of_at.tzinfo is None or self.as_of_at.utcoffset() is None:
            raise ValueError("metric as_of_at must be timezone-aware")
        object.__setattr__(self, "as_of_at", self.as_of_at.astimezone(UTC))
        object.__setattr__(
            self,
            "competitive_score",
            None
            if self.competitive_score is None
            else self.competitive_score.quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP),
        )
        object.__setattr__(
            self,
            "volume_trend_delta",
            None
            if self.volume_trend_delta is None
            else self.volume_trend_delta.quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP),
        )


def calculate_market_metrics(
    market: BinaryMarket,
    order_book: CanonicalOrderBook,
    candles: MarketCandlestickBatch,
    *,
    data_cutoff: datetime,
) -> MarketMetricSnapshot:
    """Calculate the immutable metric snapshot for one selected market."""

    cutoff = _aware(data_cutoff, "metric data_cutoff")
    if candles.market_key != market.key or order_book.market_key != market.key:
        raise ValueError("metric inputs belong to different market identities")
    if any(item.end_period > cutoff for item in candles.candlesticks):
        raise ValueError("candlestick observation is newer than the metric cutoff")

    observed_candles = tuple(
        item for item in candles.candlesticks if not item.synthetic and item.end_period <= cutoff
    )
    recent = _window(observed_candles, cutoff - METRIC_WINDOW, cutoff)
    baseline = _window(observed_candles, cutoff - METRIC_LOOKBACK, cutoff - METRIC_WINDOW)
    recent_complete = _complete_hourly_window(recent)
    baseline_complete = _complete_hourly_window(baseline)
    recent_volume = sum(item.volume_units for item in recent)
    baseline_volume = sum(item.volume_units for item in baseline)
    if recent_complete and baseline_complete:
        trend = _volume_trend(recent_volume, baseline_volume)
        delta = (
            None
            if baseline_volume == 0
            else Decimal(recent_volume - baseline_volume) / Decimal(baseline_volume)
        )
    else:
        trend = "insufficient_data"
        delta = None

    volatility, volatility_sample_count = (
        _volatility(recent) if recent_complete else (None, 0)
    )
    yes_mid = _midpoint(order_book.best_bid(OutcomeSide.YES), order_book.best_ask(OutcomeSide.YES))
    no_mid = _midpoint(order_book.best_bid(OutcomeSide.NO), order_book.best_ask(OutcomeSide.NO))
    if yes_mid is not None and no_mid is not None:
        # Round one side once, then derive its complement.  Independently
        # rounding both half-cent midpoints can otherwise produce a one-micro
        # mismatch even though Kalshi exposes one binary contract.
        no_mid = 1_000_000 - yes_mid
    competitive = _competitive_score(
        order_book,
        yes_mid,
        no_mid,
        volume_24h_units=int(market.volume_24h),
    )
    observed_times = [item.end_period for item in observed_candles]
    as_of_at = max(observed_times, default=cutoff)
    source_artifacts = _unique_artifacts((market.audit, order_book.artifact, candles.audit))
    return MarketMetricSnapshot(
        market.key,
        int(market.volume_24h),
        volatility,
        trend,
        delta,
        competitive,
        yes_mid,
        no_mid,
        recent_volume,
        baseline_volume,
        volatility_sample_count,
        len(recent),
        len(baseline),
        min(as_of_at, cutoff),
        source_artifacts=source_artifacts,
    )


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _window(
    candles: Sequence[MarketCandlestick], start: datetime, end: datetime
) -> tuple[MarketCandlestick, ...]:
    return tuple(item for item in candles if start < item.end_period <= end)


def _complete_hourly_window(candles: Sequence[MarketCandlestick]) -> bool:
    if len(candles) != 24:
        return False
    return all(
        right.end_period - left.end_period == CANDLE_INTERVAL
        for left, right in pairwise(candles)
    )


def _volume_trend(recent: int, baseline: int) -> str:
    if recent > baseline:
        return "increasing"
    if recent < baseline:
        return "decreasing"
    return "flat"


def _volatility(candles: Sequence[MarketCandlestick]) -> tuple[int | None, int]:
    deltas: list[int] = []
    previous_timestamp: datetime | None = None
    previous_price: int | None = None
    for candle in candles:
        if candle.close_price_micros is None:
            previous_timestamp = None
            previous_price = None
            continue
        if (
            previous_timestamp is not None
            and previous_price is not None
            and candle.end_period - previous_timestamp == CANDLE_INTERVAL
        ):
            deltas.append(candle.close_price_micros - previous_price)
        previous_timestamp = candle.end_period
        previous_price = candle.close_price_micros
    if len(deltas) < 3:
        return None, len(deltas)
    mean = Decimal(sum(deltas)) / Decimal(len(deltas))
    variance = sum((Decimal(delta) - mean) ** 2 for delta in deltas) / Decimal(len(deltas) - 1)
    return int(variance.sqrt().quantize(Decimal("1"), rounding=ROUND_HALF_UP)), len(deltas)


def _midpoint(left: CanonicalLevel | None, right: CanonicalLevel | None) -> int | None:
    if left is None or right is None:
        return None
    return (int(left.price) + int(right.price) + 1) // 2


def _competitive_score(
    order_book: CanonicalOrderBook,
    yes_mid: int | None,
    no_mid: int | None,
    *,
    volume_24h_units: int,
) -> Decimal | None:
    yes_bid = order_book.best_bid(OutcomeSide.YES)
    no_bid = order_book.best_bid(OutcomeSide.NO)
    if yes_bid is None or no_bid is None or yes_mid is None or no_mid is None:
        return None
    spread_micros = 1_000_000 - int(yes_bid.price) - int(no_bid.price)
    if spread_micros < 0:
        raise ValueError("reciprocal order book has a negative spread")
    spread_cents = Decimal(spread_micros) / Decimal(10_000)
    spread_score = Decimal(1) / (Decimal(1) + spread_cents / SPREAD_HALF_SCORE_CENTS)
    yes_depth = _depth_within_band(order_book.yes_bids, yes_mid)
    no_depth = _depth_within_band(order_book.no_bids, no_mid)
    depth = min(yes_depth, no_depth)
    depth_score = Decimal(depth) / (Decimal(depth) + DEPTH_SATURATION_UNITS)
    activity = Decimal(volume_24h_units)
    activity_score = activity / (activity + ACTIVITY_SATURATION_UNITS)
    return (
        Decimal("0.50") * spread_score
        + Decimal("0.30") * depth_score
        + Decimal("0.20") * activity_score
    ).quantize(DECIMAL_PLACES, rounding=ROUND_HALF_UP)


def _depth_within_band(levels: Iterable[CanonicalLevel], midpoint: int) -> int:
    return sum(
        int(level.quantity)
        for level in levels
        if abs(int(level.price) - midpoint) <= DEPTH_BAND_MICROS
    )


def _unique_artifacts(artifacts: Iterable[RawArtifact]) -> tuple[RawArtifact, ...]:
    unique: dict[str, RawArtifact] = {}
    for artifact in artifacts:
        unique.setdefault(artifact.sha256, artifact)
    return tuple(unique.values())


__all__ = [
    "ACTIVITY_SATURATION_UNITS",
    "DEPTH_BAND_MICROS",
    "DEPTH_SATURATION_UNITS",
    "METRIC_FORMULA_VERSION",
    "MarketCandlestick",
    "MarketCandlestickBatch",
    "MarketMetricSnapshot",
    "calculate_market_metrics",
    "format_metric_decimal",
]
