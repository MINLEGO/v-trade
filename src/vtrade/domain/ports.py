from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from vtrade.domain.types import (
    CycleSnapshot,
    MarketDelta,
    OrderBookSnapshot,
    OrderIntent,
    Resolution,
)

JsonObject = dict[str, Any]


class ModelGateway(Protocol):
    def complete(
        self, messages: Sequence[JsonObject], tools: Sequence[JsonObject], model_config: JsonObject
    ) -> JsonObject: ...


class ResearchProvider(Protocol):
    def search(self, query: str, options: JsonObject) -> JsonObject: ...

    def fetch(self, url: str, options: JsonObject) -> JsonObject: ...


class MarketVenue(Protocol):
    def sync_markets(self, cursor: str | None) -> MarketDelta: ...

    def get_order_book(self, outcome_ids: Sequence[str]) -> tuple[OrderBookSnapshot, ...]: ...

    def get_resolutions(
        self, market_ids: Sequence[str], as_of: datetime
    ) -> tuple[Resolution, ...]: ...


class ArtifactReference(Protocol):
    @property
    def sha256(self) -> str: ...

    @property
    def byte_length(self) -> int: ...

    @property
    def uri(self) -> str: ...


class ArtifactStore(Protocol):
    def put(self, content: bytes) -> ArtifactReference: ...


class Broker(Protocol):
    def place(
        self, order: OrderIntent, portfolio: JsonObject, snapshot: CycleSnapshot
    ) -> JsonObject: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
