from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from vtrade.domain.ports import ArtifactStore
from vtrade.domain.types import (
    Event,
    Market,
    MarketDelta,
    MarketStatus,
    MicroDollars,
    OrderBookSnapshot,
    Outcome,
    PriceLevel,
    RawArtifact,
    Resolution,
)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class PolymarketError(RuntimeError):
    pass


class PolymarketPayloadError(PolymarketError):
    pass


class PolymarketTransportError(PolymarketError):
    pass


class LookAheadError(PolymarketError):
    pass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    maximum_backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("retry delays cannot be negative")


class RequestRateLimiter:
    """Small client-side limiter intentionally far below public provider ceilings."""

    def __init__(
        self,
        minimum_interval_seconds: Mapping[str, float] | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._intervals = dict(
            minimum_interval_seconds
            or {
                "gamma-api.polymarket.com": 0.02,
                "clob.polymarket.com": 0.01,
            }
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        interval = self._intervals.get(host, 0.05)
        with self._lock:
            now = self._monotonic()
            remaining = interval - (now - self._last_request.get(host, -1e9))
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
            self._last_request[host] = now


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _ArchivedResponse:
    payload: Any
    observed_at: datetime
    artifact: RawArtifact


@dataclass(frozen=True, slots=True)
class FeeRateSnapshot:
    token_id: str
    base_fee_bps: int
    observed_at: datetime
    source_created_at: datetime | None
    artifact: RawArtifact

    def __post_init__(self) -> None:
        if not self.token_id or not 0 <= self.base_fee_bps <= 10_000:
            raise ValueError("fee rate requires a token and 0..10000 basis points")
        _aware_utc(self.observed_at)
        if self.source_created_at is not None:
            _aware_utc(self.source_created_at)
            if self.source_created_at > self.observed_at:
                raise ValueError("fee source timestamp cannot follow observation")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as-of timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_datetime(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        raw = Decimal(str(value))
        if raw > 10_000_000_000:
            raw /= 1000
        return datetime.fromtimestamp(float(raw), tz=UTC)
    if not isinstance(value, str):
        raise PolymarketPayloadError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolymarketPayloadError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise PolymarketPayloadError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _effective_observation_cutoff(
    local_observed_at: datetime,
    source_created_at: datetime | None,
    *,
    field: str,
    maximum_source_clock_skew_seconds: float,
) -> datetime:
    if source_created_at is None or source_created_at <= local_observed_at:
        return local_observed_at
    skew_seconds = (source_created_at - local_observed_at).total_seconds()
    if skew_seconds > maximum_source_clock_skew_seconds:
        raise LookAheadError(
            f"{field} exceeds local receive time by {skew_seconds:.6f}s, above the "
            f"{maximum_source_clock_skew_seconds:.6f}s clock-skew bound"
        )
    return source_created_at


def _required_str(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PolymarketPayloadError(f"{field} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PolymarketPayloadError(f"{field} must be a string when present")
    return value


def _decimal(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolymarketPayloadError(f"{field} must be decimal-compatible") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise PolymarketPayloadError(f"{field} is outside its valid range")
    return parsed


def _micro_dollars(value: Any, field: str) -> MicroDollars:
    amount = _decimal(value if value not in (None, "") else 0, field, minimum=Decimal(0))
    rounded = amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    return MicroDollars(int(rounded * 1_000_000))


def _json_string_array(
    value: Any, field: str, *, allow_null: bool = False
) -> tuple[Any, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PolymarketPayloadError(f"{field} contains invalid JSON") from exc
    if value is None and allow_null:
        return ()
    if not isinstance(value, list):
        raise PolymarketPayloadError(f"{field} must be a JSON array or JSON-string array")
    return tuple(value)


def _artifact(reference: Any) -> RawArtifact:
    return RawArtifact(reference.sha256, reference.byte_length, reference.uri)


class PolymarketVenue:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        client: httpx.Client | None = None,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RequestRateLimiter | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        maximum_response_bytes: int = 25_000_000,
        maximum_order_books_per_call: int = 20,
        maximum_source_clock_skew_seconds: float = 5.0,
    ) -> None:
        if maximum_source_clock_skew_seconds < 0:
            raise ValueError("maximum source clock skew cannot be negative")
        self._store = artifact_store
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0))
        self._retry = retry_policy or RetryPolicy()
        self._rate_limiter = rate_limiter or RequestRateLimiter()
        self._clock = clock
        self._sleep = sleep
        self._maximum_response_bytes = maximum_response_bytes
        self._maximum_order_books_per_call = maximum_order_books_per_call
        self._maximum_source_clock_skew_seconds = maximum_source_clock_skew_seconds
        self._resolution_history: list[Resolution] = []

    def _get(self, url: str, params: list[tuple[str, str]]) -> _ArchivedResponse:
        last_error: Exception | None = None
        query_params = httpx.QueryParams()
        for key, value in params:
            query_params = query_params.add(key, value)
        for attempt in range(self._retry.maximum_attempts):
            self._rate_limiter.wait(url)
            try:
                response = self._client.get(url, params=query_params)
                if response.status_code in _RETRYABLE_STATUS:
                    raise PolymarketTransportError(
                        f"retryable Polymarket response status {response.status_code}"
                    )
                response.raise_for_status()
                raw = response.content
                if len(raw) > self._maximum_response_bytes:
                    raise PolymarketPayloadError(
                        "Polymarket response exceeds configured byte limit"
                    )
                observed_at = _aware_utc(self._clock())
                reference = self._store.put(raw)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise PolymarketPayloadError("Polymarket response is not valid JSON") from exc
                return _ArchivedResponse(payload, observed_at, _artifact(reference))
            except (httpx.HTTPError, PolymarketTransportError) as exc:
                last_error = exc
                if attempt + 1 >= self._retry.maximum_attempts:
                    break
                delay = min(
                    self._retry.initial_backoff_seconds * (2**attempt),
                    self._retry.maximum_backoff_seconds,
                )
                self._sleep(delay)
        raise PolymarketTransportError("bounded Polymarket GET retries exhausted") from last_error

    def sync_events(self, cursor: str | None = None, *, limit: int = 100) -> MarketDelta:
        if not 1 <= limit <= 500:
            raise ValueError("events keyset limit must be between 1 and 500")
        params = [("limit", str(limit)), ("closed", "false")]
        if cursor is not None:
            params.append(("after_cursor", cursor))
        archived = self._get(f"{GAMMA_BASE_URL}/events/keyset", params)
        root = self._root_object(archived.payload)
        rows = root.get("events")
        if not isinstance(rows, list):
            raise PolymarketPayloadError("events/keyset response lacks events array")
        events: list[Event] = []
        markets: list[Market] = []
        for row in rows:
            event_payload = self._object(row, "event")
            event = self._normalize_event(event_payload, archived.observed_at)
            events.append(event)
            nested = event_payload.get("markets", [])
            if not isinstance(nested, list):
                raise PolymarketPayloadError("event markets must be an array")
            markets.extend(
                self._normalize_market(
                    self._object(item, "market"), archived.observed_at, parent_event=event
                )
                for item in nested
            )
        effective_cutoff = max(
            archived.observed_at,
            max((event.observed_at for event in events), default=archived.observed_at),
            max(
                (market.observed_at or archived.observed_at for market in markets),
                default=archived.observed_at,
            ),
        )
        return MarketDelta(
            resource="events",
            requested_cursor=cursor,
            next_cursor=self._next_cursor(root),
            observed_at=effective_cutoff,
            events=tuple(events),
            markets=tuple(markets),
            artifact=archived.artifact,
        )

    def sync_markets(self, cursor: str | None = None, *, limit: int = 100) -> MarketDelta:
        if not 1 <= limit <= 100:
            raise ValueError("markets keyset limit must be between 1 and 100")
        params = [("limit", str(limit)), ("closed", "false"), ("include_tag", "true")]
        if cursor is not None:
            params.append(("after_cursor", cursor))
        archived = self._get(f"{GAMMA_BASE_URL}/markets/keyset", params)
        root = self._root_object(archived.payload)
        rows = root.get("markets")
        if not isinstance(rows, list):
            raise PolymarketPayloadError("markets/keyset response lacks markets array")
        events: dict[str, Event] = {}
        markets: list[Market] = []
        for row in rows:
            market_payload = self._object(row, "market")
            event = self._event_from_market(market_payload, archived.observed_at)
            events[event.id] = event
            markets.append(
                self._normalize_market(market_payload, archived.observed_at, parent_event=event)
            )
        effective_cutoff = max(
            archived.observed_at,
            max(
                (event.observed_at for event in events.values()),
                default=archived.observed_at,
            ),
            max(
                (market.observed_at or archived.observed_at for market in markets),
                default=archived.observed_at,
            ),
        )
        return MarketDelta(
            resource="markets",
            requested_cursor=cursor,
            next_cursor=self._next_cursor(root),
            observed_at=effective_cutoff,
            events=tuple(events.values()),
            markets=tuple(markets),
            artifact=archived.artifact,
        )

    def sync_all_markets(
        self, cursor: str | None = None, *, maximum_pages: int = 20, page_limit: int = 100
    ) -> tuple[MarketDelta, ...]:
        if maximum_pages < 1:
            raise ValueError("maximum_pages must be positive")
        pages: list[MarketDelta] = []
        seen_cursors: set[str] = set()
        current = cursor
        for _ in range(maximum_pages):
            page = self.sync_markets(current, limit=page_limit)
            pages.append(page)
            if page.next_cursor is None:
                return tuple(pages)
            if page.next_cursor in seen_cursors:
                raise PolymarketPayloadError("markets keyset cursor repeated")
            seen_cursors.add(page.next_cursor)
            current = page.next_cursor
        return tuple(pages)

    def get_order_book(self, outcome_ids: Sequence[str]) -> tuple[OrderBookSnapshot, ...]:
        unique = tuple(dict.fromkeys(outcome_ids))
        if not unique or len(unique) > self._maximum_order_books_per_call:
            raise ValueError("order-book request count is empty or above the configured bound")
        snapshots: list[OrderBookSnapshot] = []
        for token_id in unique:
            if not token_id:
                raise ValueError("outcome token IDs cannot be empty")
            archived = self._get(f"{CLOB_BASE_URL}/book", [("token_id", token_id)])
            payload = self._root_object(archived.payload)
            returned_token = _required_str(payload, "asset_id")
            if returned_token != token_id:
                raise PolymarketPayloadError("CLOB book asset_id does not match requested token")
            source_created_at = _parse_datetime(payload.get("timestamp"), "timestamp")
            effective_cutoff = _effective_observation_cutoff(
                archived.observed_at,
                source_created_at,
                field="order-book timestamp",
                maximum_source_clock_skew_seconds=self._maximum_source_clock_skew_seconds,
            )
            snapshots.append(
                OrderBookSnapshot(
                    token_id=token_id,
                    condition_id=_required_str(payload, "market"),
                    observed_at=effective_cutoff,
                    source_created_at=source_created_at,
                    bids=self._levels(payload.get("bids"), "bids"),
                    asks=self._levels(payload.get("asks"), "asks"),
                    tick_size=_decimal(payload.get("tick_size"), "tick_size", minimum=Decimal(0)),
                    minimum_order_size=_decimal(
                        payload.get("min_order_size"), "min_order_size", minimum=Decimal(0)
                    ),
                    negative_risk=payload.get("neg_risk") is True,
                    artifact=archived.artifact,
                )
            )
        return tuple(snapshots)

    def get_fee_rates(self, outcome_ids: Sequence[str]) -> tuple[FeeRateSnapshot, ...]:
        """Archive and normalize the official public CLOB fee rate for each token."""
        unique = tuple(dict.fromkeys(outcome_ids))
        if not unique or len(unique) > self._maximum_order_books_per_call:
            raise ValueError("fee-rate request count is empty or above the configured bound")
        rates: list[FeeRateSnapshot] = []
        for token_id in unique:
            if not token_id:
                raise ValueError("fee-rate token IDs cannot be empty")
            archived = self._get(
                f"{CLOB_BASE_URL}/fee-rate/{quote(token_id, safe='')}", []
            )
            payload = self._root_object(archived.payload)
            base_fee = payload.get("base_fee")
            if not isinstance(base_fee, int) or isinstance(base_fee, bool):
                raise PolymarketPayloadError("fee-rate response requires integer base_fee")
            rates.append(
                FeeRateSnapshot(
                    token_id,
                    base_fee,
                    archived.observed_at,
                    None,
                    archived.artifact,
                )
            )
        return tuple(rates)

    def sync_resolutions(self, market_ids: Sequence[str]) -> tuple[Resolution, ...]:
        venue_ids = tuple(dict.fromkeys(self._venue_market_id(value) for value in market_ids))
        if not venue_ids or len(venue_ids) > 100:
            raise ValueError("resolution sync count is empty or above 100")
        params = [("limit", "100"), ("closed", "true")]
        params.extend(("id", value) for value in venue_ids)
        archived = self._get(f"{GAMMA_BASE_URL}/markets/keyset", params)
        root = self._root_object(archived.payload)
        rows = root.get("markets")
        if not isinstance(rows, list):
            raise PolymarketPayloadError("resolution response lacks markets array")
        requested = set(venue_ids)
        resolutions: list[Resolution] = []
        for row in rows:
            payload = self._object(row, "market")
            if _required_str(payload, "id") not in requested:
                raise PolymarketPayloadError("resolution response contains an unrequested market")
            resolution = self._normalize_resolution(payload, archived)
            if resolution is not None:
                resolutions.append(resolution)
        self._resolution_history.extend(resolutions)
        return tuple(resolutions)

    def get_resolutions(
        self, market_ids: Sequence[str], as_of: datetime
    ) -> tuple[Resolution, ...]:
        cutoff = _aware_utc(as_of)
        wanted = {self._canonical_market_id(self._venue_market_id(value)) for value in market_ids}
        latest: dict[str, Resolution] = {}
        for resolution in self._resolution_history:
            if resolution.market_id in wanted and resolution.observed_at <= cutoff:
                previous = latest.get(resolution.market_id)
                if previous is None or previous.observed_at < resolution.observed_at:
                    latest[resolution.market_id] = resolution
        return tuple(latest[key] for key in sorted(latest))

    @staticmethod
    def _root_object(value: Any) -> dict[str, Any]:
        return PolymarketVenue._object(value, "response")

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise PolymarketPayloadError(f"{label} must be an object")
        return value

    @staticmethod
    def _next_cursor(root: Mapping[str, Any]) -> str | None:
        value = root.get("next_cursor")
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise PolymarketPayloadError("next_cursor must be a string")
        return value

    def _event_from_market(self, payload: Mapping[str, Any], observed_at: datetime) -> Event:
        rows = payload.get("events")
        if not isinstance(rows, list) or not rows:
            raise PolymarketPayloadError("market must include at least one event relation")
        return self._normalize_event(self._object(rows[0], "market event"), observed_at)

    def _normalize_event(self, payload: Mapping[str, Any], observed_at: datetime) -> Event:
        venue_id = _required_str(payload, "id")
        source_updated_at = _parse_datetime(payload.get("updatedAt"), "event.updatedAt")
        effective_cutoff = _effective_observation_cutoff(
            observed_at,
            source_updated_at,
            field="event.updatedAt",
            maximum_source_clock_skew_seconds=self._maximum_source_clock_skew_seconds,
        )
        return Event(
            id=f"polymarket:event:{venue_id}",
            venue_id=venue_id,
            slug=_required_str(payload, "slug"),
            title=_required_str(payload, "title"),
            description=str(payload.get("description") or ""),
            resolution_source=_optional_str(payload, "resolutionSource"),
            opens_at=_parse_datetime(payload.get("startDate"), "event.startDate"),
            closes_at=_parse_datetime(payload.get("endDate"), "event.endDate"),
            active=payload.get("active") is True,
            closed=payload.get("closed") is True,
            archived=payload.get("archived") is True,
            observed_at=effective_cutoff,
            source_updated_at=source_updated_at,
            venue_metadata={"ticker": payload.get("ticker"), "tags": payload.get("tags", [])},
        )

    def _normalize_market(
        self, payload: Mapping[str, Any], observed_at: datetime, *, parent_event: Event
    ) -> Market:
        venue_id = _required_str(payload, "id")
        market_id = self._canonical_market_id(venue_id)
        source_updated_at = _parse_datetime(payload.get("updatedAt"), "market.updatedAt")
        effective_cutoff = _effective_observation_cutoff(
            max(observed_at, parent_event.observed_at),
            source_updated_at,
            field="market.updatedAt",
            maximum_source_clock_skew_seconds=self._maximum_source_clock_skew_seconds,
        )
        names = _json_string_array(payload.get("outcomes"), "outcomes")
        token_ids = _json_string_array(
            payload.get("clobTokenIds"), "clobTokenIds", allow_null=True
        )
        if token_ids and len(names) != len(token_ids):
            raise PolymarketPayloadError("outcomes and clobTokenIds must map 1:1")
        if not names:
            raise PolymarketPayloadError("outcomes must be non-empty")
        raw_prices = payload.get("outcomePrices")
        prices = (
            _json_string_array(raw_prices, "outcomePrices", allow_null=True)
            if raw_prices not in (None, "")
            else ()
        )
        if prices and len(prices) != len(names):
            raise PolymarketPayloadError("outcomePrices must map 1:1 to outcomes")
        if not prices:
            prices = tuple(None for _ in names)
        active = payload.get("active") is True
        closed = payload.get("closed") is True
        enabled = payload.get("enableOrderBook") is True
        accepting = payload.get("acceptingOrders") is True
        valid_tokens = len(token_ids) == len(names) and all(
            isinstance(value, str) and bool(value) for value in token_ids
        )
        tradeable = active and not closed and enabled and accepting and valid_tokens
        tick = _micro_dollars(payload.get("orderPriceMinTickSize") or "0.01", "tick_size")
        minimum = _micro_dollars(payload.get("orderMinSize") or 0, "minimum_order_size")
        outcomes: list[Outcome] = []
        for index, (name, token_id, price) in enumerate(
            zip(names, token_ids, prices, strict=False)
        ):
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(token_id, str)
                or not token_id
            ):
                raise PolymarketPayloadError(
                    "outcome names and token IDs must be non-empty strings"
                )
            indicative = (
                None
                if price is None
                else _decimal(price, "outcomePrice", minimum=Decimal(0))
            )
            if indicative is not None and indicative > 1:
                raise PolymarketPayloadError("outcomePrice must be between zero and one")
            outcomes.append(
                Outcome(
                    id=f"polymarket:outcome:{token_id}",
                    market_id=market_id,
                    name=name,
                    venue_token_id=token_id,
                    best_bid_micros=None,
                    best_ask_micros=None,
                    tick_size_micros=tick,
                    minimum_order_micros=minimum,
                    outcome_index=index,
                    indicative_price=indicative,
                    tradeable=tradeable,
                    venue_metadata={"negative_risk": payload.get("negRisk") is True},
                )
            )
        status = self._market_status(
            active, closed, tuple(outcomes), payload.get("umaResolutionStatus")
        )
        return Market(
            id=market_id,
            venue_id=venue_id,
            event_id=parent_event.id,
            question=_required_str(payload, "question"),
            resolution_rules=str(payload.get("description") or ""),
            opens_at=_parse_datetime(payload.get("startDate"), "market.startDate"),
            closes_at=_parse_datetime(payload.get("endDate"), "market.endDate"),
            status=status,
            category=_optional_str(payload, "category"),
            volume_micros=_micro_dollars(
                payload.get("volumeNum", payload.get("volume", 0)), "volume"
            ),
            liquidity_micros=_micro_dollars(
                payload.get("liquidityNum", payload.get("liquidity", 0)), "liquidity"
            ),
            venue_metadata={
                "condition_id": payload.get("conditionId"),
                "question_id": payload.get("questionID"),
                "enable_order_book": enabled,
                "accepting_orders": accepting,
                "negative_risk": payload.get("negRisk") is True,
                "uma_resolution_status": payload.get("umaResolutionStatus"),
                "tags": payload.get("tags", []),
                "created_at": payload.get("createdAt"),
                "volume_24hr": payload.get("volume24hr"),
                "volume_1wk": payload.get("volume1wk"),
                "one_hour_price_change": payload.get("oneHourPriceChange"),
                "one_day_price_change": payload.get("oneDayPriceChange"),
                "competitive": payload.get("competitive"),
                "unmapped_outcome_names": list(names) if not token_ids else [],
            },
            slug=_required_str(payload, "slug"),
            resolution_source=_optional_str(payload, "resolutionSource"),
            tradeable=tradeable,
            outcomes=tuple(outcomes),
            observed_at=effective_cutoff,
            source_updated_at=source_updated_at,
        )

    @staticmethod
    def _market_status(
        active: bool, closed: bool, outcomes: tuple[Outcome, ...], uma_status: Any
    ) -> MarketStatus:
        final = [outcome for outcome in outcomes if outcome.indicative_price == 1]
        singular_resolution = len(final) == 1 and all(
            outcome.indicative_price in (Decimal(0), Decimal(1)) for outcome in outcomes
        )
        split_resolution = len(outcomes) == 2 and all(
            outcome.indicative_price == Decimal("0.5") for outcome in outcomes
        )
        if closed and uma_status == "resolved" and (
            singular_resolution or split_resolution
        ):
            return MarketStatus.RESOLVED
        if closed:
            return MarketStatus.CLOSED
        if active:
            return MarketStatus.OPEN
        return MarketStatus.AMBIGUOUS

    def _normalize_resolution(
        self, payload: Mapping[str, Any], archived: _ArchivedResponse
    ) -> Resolution | None:
        event = self._event_from_market(payload, archived.observed_at)
        market = self._normalize_market(payload, archived.observed_at, parent_event=event)
        if payload.get("umaResolutionStatus") != "resolved":
            return None
        if market.status is not MarketStatus.RESOLVED:
            return None
        winners = [outcome for outcome in market.outcomes if outcome.indicative_price == 1]
        split_resolution = len(market.outcomes) == 2 and all(
            outcome.indicative_price == Decimal("0.5") for outcome in market.outcomes
        )
        if len(winners) != 1 and not split_resolution:
            return None
        source_created_at = (
            _parse_datetime(payload.get("closedTime"), "closedTime")
            or market.source_updated_at
            or market.closes_at
        )
        if source_created_at is None:
            raise PolymarketPayloadError("resolved market lacks a source resolution timestamp")
        effective_cutoff = _effective_observation_cutoff(
            market.observed_at or archived.observed_at,
            source_created_at,
            field="resolution timestamp",
            maximum_source_clock_skew_seconds=self._maximum_source_clock_skew_seconds,
        )
        eligible_after = market.closes_at or source_created_at
        return Resolution(
            market_id=market.id,
            winning_outcome_id=None if split_resolution else winners[0].id,
            result="50/50" if split_resolution else winners[0].name,
            source_created_at=source_created_at,
            observed_at=effective_cutoff,
            eligible_after=eligible_after,
            artifact=archived.artifact,
        )

    @staticmethod
    def _levels(value: Any, field: str) -> tuple[PriceLevel, ...]:
        if not isinstance(value, list):
            raise PolymarketPayloadError(f"{field} must be an array")
        levels: list[PriceLevel] = []
        for item in value:
            row = PolymarketVenue._object(item, f"{field} level")
            price = _decimal(row.get("price"), f"{field}.price", minimum=Decimal(0))
            size = _decimal(row.get("size"), f"{field}.size", minimum=Decimal(0))
            if price > 1 or size <= 0:
                raise PolymarketPayloadError(f"{field} contains an invalid price or size")
            levels.append(PriceLevel(price, size))
        return tuple(levels)

    @staticmethod
    def _venue_market_id(value: str) -> str:
        if not value:
            raise ValueError("market IDs cannot be empty")
        return value.removeprefix("polymarket:market:")

    @staticmethod
    def _canonical_market_id(venue_id: str) -> str:
        return f"polymarket:market:{venue_id}"


@dataclass(frozen=True, slots=True)
class DiscoveryQuery:
    text: str | None = None
    category: str | None = None
    tradeable_only: bool = True
    minimum_volume_micros: int = 0
    minimum_liquidity_micros: int = 0
    limit: int = 20


class MarketDiscoveryCache:
    """Versioned in-memory read cache; snapshots newer than as_of are never visible."""

    def __init__(self, maximum_deltas: int = 256) -> None:
        if maximum_deltas < 1:
            raise ValueError("maximum_deltas must be positive")
        self._maximum_deltas = maximum_deltas
        self._deltas: list[MarketDelta] = []
        self._order_books: list[OrderBookSnapshot] = []
        self._lock = threading.Lock()

    def ingest(self, delta: MarketDelta) -> None:
        with self._lock:
            self._deltas.append(delta)
            self._deltas.sort(key=lambda item: item.observed_at)
            self._deltas = self._deltas[-self._maximum_deltas :]

    def ingest_order_books(self, snapshots: Sequence[OrderBookSnapshot]) -> None:
        with self._lock:
            self._order_books.extend(snapshots)
            self._order_books.sort(key=lambda item: item.observed_at)
            maximum_books = self._maximum_deltas * 20
            self._order_books = self._order_books[-maximum_books:]

    def order_books_as_of(
        self, token_ids: Sequence[str], *, as_of: datetime
    ) -> tuple[OrderBookSnapshot, ...]:
        cutoff = _aware_utc(as_of)
        wanted = tuple(dict.fromkeys(token_ids))
        latest: dict[str, OrderBookSnapshot] = {}
        with self._lock:
            eligible = tuple(book for book in self._order_books if book.observed_at <= cutoff)
        for book in eligible:
            if book.token_id in wanted:
                latest[book.token_id] = book
        missing = [token_id for token_id in wanted if token_id not in latest]
        if missing:
            raise KeyError(f"no frozen order book at cutoff for token IDs: {', '.join(missing)}")
        return tuple(latest[token_id] for token_id in wanted)

    def markets_as_of(self, as_of: datetime) -> tuple[Market, ...]:
        cutoff = _aware_utc(as_of)
        latest: dict[str, Market] = {}
        with self._lock:
            eligible = tuple(delta for delta in self._deltas if delta.observed_at <= cutoff)
        for delta in eligible:
            for market in delta.markets:
                if market.observed_at is None or market.observed_at > cutoff:
                    raise LookAheadError("cache contains a market newer than the requested cutoff")
                latest[market.id] = market
        return tuple(latest[key] for key in sorted(latest))

    def events_as_of(self, as_of: datetime) -> tuple[Event, ...]:
        cutoff = _aware_utc(as_of)
        latest: dict[str, Event] = {}
        with self._lock:
            eligible = tuple(delta for delta in self._deltas if delta.observed_at <= cutoff)
        for delta in eligible:
            for event in delta.events:
                if event.observed_at > cutoff:
                    raise LookAheadError("cache contains an event newer than the requested cutoff")
                latest[event.id] = event
        return tuple(latest[key] for key in sorted(latest))

    def discover(self, query: DiscoveryQuery, *, as_of: datetime) -> tuple[Market, ...]:
        if not 1 <= query.limit <= 100:
            raise ValueError("discovery limit must be between 1 and 100")
        needle = query.text.casefold().strip() if query.text else None
        category = query.category.casefold().strip() if query.category else None
        matches: list[Market] = []
        for market in self.markets_as_of(as_of):
            if query.tradeable_only and not market.tradeable:
                continue
            if int(market.volume_micros) < query.minimum_volume_micros:
                continue
            if int(market.liquidity_micros) < query.minimum_liquidity_micros:
                continue
            if category and (market.category or "").casefold() != category:
                continue
            haystack = f"{market.question} {market.resolution_rules} {market.slug or ''}".casefold()
            if needle and needle not in haystack:
                continue
            matches.append(market)
        matches.sort(
            key=lambda item: (int(item.volume_micros), int(item.liquidity_micros), item.id),
            reverse=True,
        )
        return tuple(matches[: query.limit])


class MarketDiscoveryTools:
    def __init__(
        self, cache: MarketDiscoveryCache, *, as_of: datetime
    ) -> None:
        self._cache = cache
        self._as_of = _aware_utc(as_of)

    def search_markets(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        markets = self._cache.discover(
            DiscoveryQuery(text=query, limit=limit), as_of=self._as_of
        )
        return {"as_of": self._as_of.isoformat(), "markets": [self._summary(m) for m in markets]}

    def browse_markets(
        self, *, category: str | None = None, minimum_volume_micros: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        markets = self._cache.discover(
            DiscoveryQuery(
                category=category,
                minimum_volume_micros=minimum_volume_micros,
                limit=limit,
            ),
            as_of=self._as_of,
        )
        return {"as_of": self._as_of.isoformat(), "markets": [self._summary(m) for m in markets]}

    def get_market_details(self, market_id: str) -> dict[str, Any]:
        matches = [
            market
            for market in self._cache.markets_as_of(self._as_of)
            if market.id == market_id
        ]
        if len(matches) != 1:
            raise KeyError(f"market {market_id!r} is not present at the tool cutoff")
        market = matches[0]
        return {
            **self._summary(market),
            "resolution_rules": market.resolution_rules,
            "resolution_source": market.resolution_source,
            "outcomes": [
                {
                    "id": outcome.id,
                    "name": outcome.name,
                    "token_id": outcome.venue_token_id,
                    "indicative_price": str(outcome.indicative_price)
                    if outcome.indicative_price is not None
                    else None,
                }
                for outcome in market.outcomes
            ],
        }

    def get_orderbook(self, token_ids: Sequence[str]) -> dict[str, Any]:
        books = self._cache.order_books_as_of(token_ids, as_of=self._as_of)
        return {
            "as_of": self._as_of.isoformat(),
            "books": [
                {
                    "token_id": book.token_id,
                    "best_bid": str(book.best_bid) if book.best_bid is not None else None,
                    "best_ask": str(book.best_ask) if book.best_ask is not None else None,
                    "tick_size": str(book.tick_size),
                    "minimum_order_size": str(book.minimum_order_size),
                    "artifact_sha256": book.artifact.sha256,
                }
                for book in books
            ],
        }

    @staticmethod
    def _summary(market: Market) -> dict[str, Any]:
        return {
            "id": market.id,
            "event_id": market.event_id,
            "question": market.question,
            "status": market.status.value,
            "tradeable": market.tradeable,
            "category": market.category,
            "volume_micros": int(market.volume_micros),
            "liquidity_micros": int(market.liquidity_micros),
            "closes_at": market.closes_at.isoformat() if market.closes_at else None,
        }
