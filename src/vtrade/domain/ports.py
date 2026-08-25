from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from vtrade.domain.execution import FeePolicySnapshot, OrderRequest, OrderResult
from vtrade.domain.types import (
    CatalogueScanRequest,
    CatalogueScanResult,
    CatalogueSnapshot,
    MarketContext,
    MarketKey,
    ResolutionObservation,
)

JsonObject = dict[str, Any]


class ModelGateway(Protocol):
    def complete(
        self, messages: Sequence[JsonObject], tools: Sequence[JsonObject], model_config: JsonObject
    ) -> JsonObject: ...


class ResearchProvider(Protocol):
    def search(self, query: str, options: JsonObject) -> JsonObject: ...

    def fetch(self, url: str, options: JsonObject) -> JsonObject: ...


class CataloguePort(Protocol):
    """Semantic seam for a complete, fail-closed active-market catalogue."""

    def scan_catalogue(
        self, request: CatalogueScanRequest, *, deadline: float | None = None
    ) -> CatalogueScanResult: ...

    def sync_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot: ...


class MarketContextPort(Protocol):
    """Return one immutable market plus its canonical two-sided book."""

    def get_context(
        self,
        market_key: MarketKey,
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> MarketContext: ...


class ResolutionPort(Protocol):
    """Read venue resolution state without mutating portfolios or paying out."""

    def get_resolutions(
        self,
        market_keys: Sequence[MarketKey],
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> tuple[ResolutionObservation, ...]: ...


class FeePolicyPort(Protocol):
    """Return an immutable exact fee policy; missing data is not a zero fee."""

    def get_fee_policy(
        self, market_key: MarketKey, *, as_of: datetime, cutoff: datetime
    ) -> FeePolicySnapshot | None: ...


class SemanticOrderPort(Protocol):
    """Shared paper/future-real order seam with no venue transport fields."""

    def submit(
        self,
        request: OrderRequest,
        *,
        context: MarketContext | None,
        portfolio: JsonObject,
        fee_policy: FeePolicySnapshot | None,
    ) -> OrderResult: ...


class ArtifactReference(Protocol):
    @property
    def sha256(self) -> str: ...

    @property
    def byte_length(self) -> int: ...

    @property
    def uri(self) -> str: ...


class ArtifactStore(Protocol):
    def put(self, content: bytes) -> ArtifactReference: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
