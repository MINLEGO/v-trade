from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from vtrade.deadline import check_deadline, deadline_remaining
from vtrade.domain.types import (
    BinaryMarket,
    CatalogueScanRequest,
    CatalogueScanResult,
    MarketContext,
    MarketKey,
    RawArtifact,
    ResolutionObservation,
)
from vtrade.market_metrics import (
    MarketCandlestickBatch,
    MarketMetricSnapshot,
    calculate_market_metrics,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KalshiFreezeRequest:
    """Inputs owned by the cycle boundary, not by the public REST adapter."""

    held_markets: tuple[MarketKey, ...] = ()
    touched_markets: tuple[MarketKey, ...] = ()
    historical_markets: tuple[MarketKey, ...] = ()
    cutoff: datetime | None = None
    maximum_historical_markets: int = 20
    maximum_additional_markets: int = 80

    def __post_init__(self) -> None:
        if self.cutoff is not None and (
            self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None
        ):
            raise ValueError("freeze cutoff must be timezone-aware")
        if self.maximum_historical_markets < 0 or self.maximum_additional_markets < 0:
            raise ValueError("freeze retention limits cannot be negative")
        for name, values in (
            ("held_markets", self.held_markets),
            ("touched_markets", self.touched_markets),
            ("historical_markets", self.historical_markets),
        ):
            if any(not isinstance(value, MarketKey) for value in values):
                raise ValueError(f"{name} must contain MarketKey values")


@dataclass(frozen=True, slots=True)
class KalshiMarketFreeze:
    """The only publishable result of a complete catalogue/book cycle."""

    catalogue: CatalogueScanResult
    discovery_market_keys: tuple[MarketKey, ...]
    resolution_market_keys: tuple[MarketKey, ...]
    contexts: tuple[MarketContext, ...]
    resolutions: tuple[ResolutionObservation, ...]
    data_cutoff: datetime
    artifacts: tuple[RawArtifact, ...]
    market_metrics: tuple[MarketMetricSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if self.data_cutoff.tzinfo is None or self.data_cutoff.utcoffset() is None:
            raise ValueError("freeze data_cutoff must be timezone-aware")
        context_keys = tuple(context.market.key for context in self.contexts)
        if context_keys != self.discovery_market_keys:
            raise ValueError("freeze context order/membership is incomplete")
        metric_keys = tuple(metric.market_key for metric in self.market_metrics)
        if metric_keys != self.discovery_market_keys:
            raise ValueError("freeze metric order/membership is incomplete")
        if any(
            observation.observed_at > self.data_cutoff
            or (
                observation.audit.observed_at is not None
                and observation.audit.observed_at > self.data_cutoff
            )
            for observation in self.resolutions
        ):
            raise ValueError("freeze contains evidence newer than its data cutoff")

    @property
    def markets(self) -> tuple[BinaryMarket, ...]:
        return tuple(context.market for context in self.contexts)


class _KalshiFreezeVenue(Protocol):
    def scan_catalogue(
        self, request: CatalogueScanRequest, *, deadline: float | None = None
    ) -> CatalogueScanResult: ...

    def get_context(
        self,
        market_key: MarketKey,
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> MarketContext: ...

    def get_resolutions(
        self,
        market_keys: Sequence[MarketKey],
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> tuple[ResolutionObservation, ...]: ...

    def get_market_candlesticks(
        self,
        market_keys: Sequence[MarketKey],
        *,
        start: datetime,
        end: datetime,
        period_interval_minutes: int = 60,
        deadline: float | None = None,
    ) -> tuple[MarketCandlestickBatch, ...]: ...


class KalshiMarketFreezeService:
    """Apply deterministic retention and bounded book reads around Kalshi REST."""

    def __init__(
        self,
        venue: _KalshiFreezeVenue,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        maximum_parallel_book_requests: int = 8,
        freeze_deadline_seconds: float = 600.0,
    ) -> None:
        if maximum_parallel_book_requests < 1:
            raise ValueError("maximum_parallel_book_requests must be positive")
        if freeze_deadline_seconds <= 0:
            raise ValueError("freeze_deadline_seconds must be positive")
        self._venue = venue
        self._clock = clock
        self._maximum_parallel_book_requests = maximum_parallel_book_requests
        self._freeze_deadline_seconds = freeze_deadline_seconds

    def freeze(
        self,
        request: KalshiFreezeRequest | None = None,
        *,
        deadline: float | None = None,
    ) -> KalshiMarketFreeze:
        freeze_request = request or KalshiFreezeRequest()
        self._aware(self._clock(), "freeze start")
        freeze_deadline = (
            deadline if deadline is not None else time.monotonic() + self._freeze_deadline_seconds
        )
        self._check_deadline(freeze_deadline, "before catalogue scan")
        catalogue = self._venue.scan_catalogue(
            CatalogueScanRequest(
                held_markets=freeze_request.held_markets,
                touched_markets=freeze_request.touched_markets,
                historical_markets=freeze_request.historical_markets,
                cutoff=freeze_request.cutoff,
                maximum_historical_markets=freeze_request.maximum_historical_markets,
                maximum_additional_markets=freeze_request.maximum_additional_markets,
            ),
            deadline=freeze_deadline,
        )
        self._check_deadline(freeze_deadline, "after catalogue scan")
        by_key = {market.key: market for market in catalogue.markets}
        operation_cutoff = self._aware(
            freeze_request.cutoff
            or self._clock() + timedelta(seconds=self._freeze_deadline_seconds),
            "freeze operation cutoff",
        )
        discovery_keys = catalogue.discovery_market_keys
        context_keys = tuple(key for key in discovery_keys if key in by_key)
        contexts = self._read_contexts(context_keys, operation_cutoff, freeze_deadline)
        metric_cutoff = self._aware(
            freeze_request.cutoff or operation_cutoff, "metric request cutoff"
        )
        self._check_deadline(freeze_deadline, "before candlestick reads")
        candle_batches = (
            self._venue.get_market_candlesticks(
                context_keys,
                start=metric_cutoff - timedelta(hours=48),
                end=metric_cutoff,
                period_interval_minutes=60,
                deadline=freeze_deadline,
            )
            if context_keys
            else ()
        )
        self._check_deadline(freeze_deadline, "after candlestick reads")
        candles_by_key = {batch.market_key: batch for batch in candle_batches}
        if set(candles_by_key) != set(context_keys):
            raise RuntimeError("candlestick reads are incomplete for the discovery universe")
        resolution_keys = tuple(
            dict.fromkeys(
                (
                    *freeze_request.held_markets,
                    *freeze_request.touched_markets,
                    *freeze_request.historical_markets,
                )
            )
        )
        resolution_cutoff = self._aware(
            freeze_request.cutoff
            or self._clock() + timedelta(seconds=self._freeze_deadline_seconds),
            "resolution operation cutoff",
        )
        self._check_deadline(freeze_deadline, "before resolution reads")
        resolutions = self._venue.get_resolutions(
            resolution_keys,
            cutoff=resolution_cutoff,
            deadline=freeze_deadline,
        )
        self._check_deadline(freeze_deadline, "after resolution reads")
        completed = self._aware(self._clock(), "freeze completion")
        self._check_deadline(freeze_deadline, "before publication")
        data_cutoff = self._aware(freeze_request.cutoff or completed, "freeze data cutoff")
        finalized_contexts = tuple(
            MarketContext(
                context.market,
                replace(context.order_book, cutoff=data_cutoff),
            )
            for context in contexts
        )
        market_metrics = tuple(
            calculate_market_metrics(
                context.market,
                context.order_book,
                candles_by_key[context.market.key],
                data_cutoff=data_cutoff,
            )
            for context in finalized_contexts
        )
        for context in finalized_contexts:
            if (
                context.order_book.observed_at > data_cutoff
                or context.market.observed_at > data_cutoff
            ):
                raise RuntimeError("book observation is newer than the freeze cutoff")
        historical_cutoff = getattr(self._venue, "last_historical_cutoff", None)
        cutoff_artifact = historical_cutoff.audit if historical_cutoff is not None else None
        artifacts = self._collect_artifacts(
            catalogue,
            finalized_contexts,
            resolutions,
            market_metrics,
            extra=(cutoff_artifact,) if cutoff_artifact is not None else (),
        )
        result = KalshiMarketFreeze(
            catalogue,
            context_keys,
            resolution_keys,
            finalized_contexts,
            resolutions,
            data_cutoff,
            artifacts,
            market_metrics,
        )
        _LOGGER.info(
            "market_freeze stage_boundary event=venue_complete pages=%s "
            "scanned_market_count=%s discovery_count=%s resolution_count=%s "
            "deadline_remaining_ms=%.3f",
            len(catalogue.pages),
            catalogue.scanned_market_count,
            len(result.discovery_market_keys),
            len(result.resolution_market_keys),
            deadline_remaining(freeze_deadline) * 1000,
        )
        return result

    freeze_markets = freeze

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _check_deadline(deadline: float, stage: str) -> None:
        check_deadline(deadline, f"Kalshi freeze {stage}")

    def _read_contexts(
        self, keys: Sequence[MarketKey], cutoff: datetime, deadline: float
    ) -> tuple[MarketContext, ...]:
        if not keys:
            return ()

        def read(key: MarketKey) -> MarketContext:
            self._check_deadline(deadline, "while reading books")
            return self._venue.get_context(key, cutoff=cutoff, deadline=deadline)

        executor = ThreadPoolExecutor(
            max_workers=min(self._maximum_parallel_book_requests, len(keys))
        )
        futures = [executor.submit(read, key) for key in keys]
        try:
            results: list[MarketContext] = []
            for future in futures:
                self._check_deadline(deadline, "while waiting for books")
                try:
                    results.append(
                        future.result(timeout=max(0.0, deadline - time.monotonic()))
                    )
                except TimeoutError as exc:
                    raise RuntimeError(
                        "Kalshi freeze deadline exceeded while waiting for books"
                    ) from exc
            self._check_deadline(deadline, "after reading books")
        except BaseException:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        self._check_deadline(deadline, "while shutting down book reads")
        executor.shutdown(wait=True)
        return tuple(results)

    @staticmethod
    def _collect_artifacts(
        catalogue: CatalogueScanResult,
        contexts: Sequence[MarketContext],
        resolutions: Sequence[ResolutionObservation],
        market_metrics: Sequence[MarketMetricSnapshot],
        *,
        extra: Sequence[RawArtifact] = (),
    ) -> tuple[RawArtifact, ...]:
        values = [*catalogue.artifacts, *extra]
        values.extend(item.audit for page in catalogue.pages for item in page.series)
        values.extend(item.audit for page in catalogue.pages for item in page.events)
        values.extend(market.audit for market in catalogue.markets)
        values.extend(context.market.audit for context in contexts)
        values.extend(context.order_book.artifact for context in contexts)
        values.extend(
            artifact for metric in market_metrics for artifact in metric.source_artifacts
        )
        values.extend(observation.audit for observation in resolutions)
        unique: dict[str, RawArtifact] = {}
        for artifact in values:
            unique.setdefault(artifact.sha256, artifact)
        return tuple(unique.values())


__all__ = ["KalshiFreezeRequest", "KalshiMarketFreeze", "KalshiMarketFreezeService"]
