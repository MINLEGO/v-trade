from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from vtrade.domain.types import (
    BinaryMarket,
    CatalogueSnapshot,
    MarketContext,
    MarketKey,
    RawArtifact,
    ResolutionObservation,
)


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

    catalogue: CatalogueSnapshot
    discovery_market_keys: tuple[MarketKey, ...]
    resolution_market_keys: tuple[MarketKey, ...]
    contexts: tuple[MarketContext, ...]
    resolutions: tuple[ResolutionObservation, ...]
    data_cutoff: datetime
    artifacts: tuple[RawArtifact, ...]

    def __post_init__(self) -> None:
        if self.data_cutoff.tzinfo is None or self.data_cutoff.utcoffset() is None:
            raise ValueError("freeze data_cutoff must be timezone-aware")
        context_keys = tuple(context.market.key for context in self.contexts)
        if context_keys != self.discovery_market_keys:
            raise ValueError("freeze context order/membership is incomplete")
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
    def sync_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot: ...

    def get_context(self, market_key: MarketKey, *, cutoff: datetime) -> MarketContext: ...

    def get_resolutions(
        self, market_keys: Sequence[MarketKey], *, cutoff: datetime
    ) -> tuple[ResolutionObservation, ...]: ...


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

    def freeze(self, request: KalshiFreezeRequest | None = None) -> KalshiMarketFreeze:
        freeze_request = request or KalshiFreezeRequest()
        self._aware(self._clock(), "freeze start")
        deadline = time.monotonic() + self._freeze_deadline_seconds
        catalogue = self._venue.sync_catalogue(cutoff=freeze_request.cutoff)
        by_key = {market.key: market for market in catalogue.markets}
        operation_cutoff = self._aware(
            freeze_request.cutoff
            or self._clock() + timedelta(seconds=self._freeze_deadline_seconds),
            "freeze operation cutoff",
        )
        discovery_keys = self._select_discovery_keys(freeze_request, catalogue.markets)
        context_keys = tuple(key for key in discovery_keys if key in by_key)
        contexts = self._read_contexts(context_keys, operation_cutoff, deadline)
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
        resolutions = self._venue.get_resolutions(resolution_keys, cutoff=resolution_cutoff)
        completed = self._aware(self._clock(), "freeze completion")
        if time.monotonic() > deadline:
            raise RuntimeError("Kalshi freeze deadline exceeded before publication")
        data_cutoff = self._aware(freeze_request.cutoff or completed, "freeze data cutoff")
        finalized_contexts = tuple(
            MarketContext(
                context.market,
                replace(context.order_book, cutoff=data_cutoff),
            )
            for context in contexts
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
            extra=(cutoff_artifact,) if cutoff_artifact is not None else (),
        )
        return KalshiMarketFreeze(
            catalogue,
            context_keys,
            resolution_keys,
            finalized_contexts,
            resolutions,
            data_cutoff,
            artifacts,
        )

    freeze_markets = freeze

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _select_discovery_keys(
        request: KalshiFreezeRequest, markets: Sequence[BinaryMarket]
    ) -> tuple[MarketKey, ...]:
        by_key = {market.key: market for market in markets}
        retained: list[MarketKey] = []
        for key in (
            *request.historical_markets[: request.maximum_historical_markets],
            *request.held_markets,
            *request.touched_markets,
        ):
            market = by_key.get(key)
            if market is not None and market.tradeable and key not in retained:
                retained.append(key)
        candidates = sorted(
            (market for market in markets if market.tradeable and market.key not in retained),
            key=lambda market: (
                -int(market.volume),
                -int(market.liquidity_micros),
                market.key.market_ref,
            ),
        )
        retained.extend(
            market.key
            for market in candidates[: request.maximum_additional_markets]
            if market.key not in retained
        )
        return tuple(retained)

    def _read_contexts(
        self, keys: Sequence[MarketKey], cutoff: datetime, deadline: float
    ) -> tuple[MarketContext, ...]:
        if not keys:
            return ()

        def read(key: MarketKey) -> MarketContext:
            if time.monotonic() > deadline:
                raise RuntimeError("Kalshi freeze deadline exceeded while reading books")
            return self._venue.get_context(key, cutoff=cutoff)

        with ThreadPoolExecutor(
            max_workers=min(self._maximum_parallel_book_requests, len(keys))
        ) as executor:
            return tuple(executor.map(read, keys))

    @staticmethod
    def _collect_artifacts(
        catalogue: CatalogueSnapshot,
        contexts: Sequence[MarketContext],
        resolutions: Sequence[ResolutionObservation],
        *,
        extra: Sequence[RawArtifact] = (),
    ) -> tuple[RawArtifact, ...]:
        values = [*catalogue.artifacts, *extra]
        values.extend(item.audit for page in catalogue.pages for item in page.series)
        values.extend(item.audit for page in catalogue.pages for item in page.events)
        values.extend(market.audit for market in catalogue.markets)
        values.extend(context.market.audit for context in contexts)
        values.extend(context.order_book.artifact for context in contexts)
        values.extend(observation.audit for observation in resolutions)
        unique: dict[str, RawArtifact] = {}
        for artifact in values:
            unique.setdefault(artifact.sha256, artifact)
        return tuple(unique.values())


__all__ = ["KalshiFreezeRequest", "KalshiMarketFreeze", "KalshiMarketFreezeService"]
