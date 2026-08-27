from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from vtrade.domain.types import (
    BinaryMarket,
    BinaryOutcome,
    EventKey,
    MarketKey,
    MarketStatus,
    OutcomeKey,
    OutcomeSide,
    PriceGrid,
    RawArtifact,
    SeriesKey,
    build_canonical_order_book,
)
from vtrade.market_metrics import (
    MarketCandlestick,
    MarketCandlestickBatch,
    calculate_market_metrics,
)

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
MARKET_KEY = MarketKey("KXMETRICS-1")
ARTIFACT = RawArtifact("a" * 64, 1, "memory://metrics")
CANDLE_ARTIFACT = RawArtifact("b" * 64, 1, "memory://candles")


def make_market(*, volume_24h: int = 10_000) -> BinaryMarket:
    grid = PriceGrid.from_ranges([{"start": "0.00", "end": "1.00", "step": "0.01"}])
    outcomes = (
        BinaryOutcome(OutcomeKey(MARKET_KEY, OutcomeSide.YES), "YES", True),
        BinaryOutcome(OutcomeKey(MARKET_KEY, OutcomeSide.NO), "NO", True),
    )
    return BinaryMarket(
        key=MARKET_KEY,
        series_key=SeriesKey("KXMETRICS"),
        event_key=EventKey("KXMETRICS-EVENT"),
        question="Will the test market resolve YES?",
        resolution_rules="Resolve from the test source.",
        resolution_source=None,
        open_time=NOW - timedelta(days=2),
        close_time=NOW + timedelta(days=2),
        expected_expiration_time=NOW + timedelta(days=2),
        latest_expiration_time=NOW + timedelta(days=2),
        status=MarketStatus.ACTIVE,
        eligible=True,
        price_grid=grid,
        outcomes=outcomes,
        observed_at=NOW,
        audit=ARTIFACT,
        volume_24h=volume_24h,
    )


def make_book():
    grid = PriceGrid.from_ranges([{"start": "0.00", "end": "1.00", "step": "0.01"}])
    return build_canonical_order_book(
        MARKET_KEY,
        grid,
        [["0.40", "1.00"], ["0.39", "0.50"]],
        [["0.55", "2.00"], ["0.54", "0.25"]],
        observed_at=NOW,
        cutoff=NOW,
        artifact=ARTIFACT,
    )


def make_candles(
    *,
    missing_indexes: set[int] | None = None,
    baseline_volume: str = "1.00",
    recent_volume: str = "2.00",
) -> MarketCandlestickBatch:
    missing = missing_indexes or set()
    candles = tuple(
        MarketCandlestick(
            NOW - timedelta(hours=47 - index),
            400_000 + (index % 4) * 10_000,
            400_000,
            int(Decimal(baseline_volume if index < 24 else recent_volume) * 100),
        )
        for index in range(48)
        if index not in missing
    )
    return MarketCandlestickBatch(MARKET_KEY, candles, CANDLE_ARTIFACT)


def test_metrics_use_24_hour_windows_and_reciprocal_two_sided_spread() -> None:
    metric = calculate_market_metrics(
        make_market(), make_book(), make_candles(), data_cutoff=NOW
    )

    assert metric.recent_volume_units == 4_800
    assert metric.baseline_volume_units == 2_400
    assert metric.recent_bucket_count == 24
    assert metric.baseline_bucket_count == 24
    assert metric.volume_trend == "increasing"
    assert metric.volume_trend_delta == Decimal("1.0000000000")
    assert metric.indicative_yes_price_micros == 425_000
    assert metric.indicative_no_price_micros == 575_000
    assert metric.volatility_micros is not None and metric.volatility_micros > 0

    # Kalshi publishes two independent bid arrays.  YES ask and NO ask are
    # reciprocal views, so the spread is 1 - YES bid - NO bid = five cents.
    spread_score = Decimal(1) / (Decimal(1) + Decimal(5) / Decimal(10))
    depth_score = Decimal(150) / Decimal(10_150)
    activity_score = Decimal(10_000) / Decimal(20_000)
    expected_score = (
        Decimal("0.50") * spread_score
        + Decimal("0.30") * depth_score
        + Decimal("0.20") * activity_score
    ).quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)
    assert metric.competitive_score == expected_score


def test_zero_baseline_is_a_valid_increasing_trend_but_has_no_delta() -> None:
    metric = calculate_market_metrics(
        make_market(), make_book(), make_candles(baseline_volume="0.00"), data_cutoff=NOW
    )

    assert metric.volume_trend == "increasing"
    assert metric.volume_trend_delta is None
    assert metric.baseline_volume_units == 0


def test_missing_hour_is_explicitly_insufficient_for_volume_trend() -> None:
    metric = calculate_market_metrics(
        make_market(), make_book(), make_candles(missing_indexes={0}), data_cutoff=NOW
    )

    assert metric.volume_trend == "insufficient_data"
    assert metric.volume_trend_delta is None
    assert metric.baseline_bucket_count == 23


def test_missing_recent_hour_does_not_fabricate_volatility() -> None:
    metric = calculate_market_metrics(
        make_market(), make_book(), make_candles(missing_indexes={24}), data_cutoff=NOW
    )

    assert metric.volume_trend == "insufficient_data"
    assert metric.volatility_micros is None
    assert metric.volatility_sample_count == 0


def test_indicative_midpoints_remain_exact_complements_after_rounding() -> None:
    grid = PriceGrid.from_ranges([{"start": "0.0000", "end": "1.0000", "step": "0.0001"}])
    book = build_canonical_order_book(
        MARKET_KEY,
        grid,
        [["0.4001", "1.00"]],
        [["0.5500", "1.00"]],
        observed_at=NOW,
        cutoff=NOW,
        artifact=ARTIFACT,
    )
    metric = calculate_market_metrics(
        make_market(), book, make_candles(), data_cutoff=NOW
    )

    assert metric.indicative_yes_price_micros == 425_050
    assert metric.indicative_no_price_micros == 574_950
    assert metric.indicative_yes_price_micros + metric.indicative_no_price_micros == 1_000_000
