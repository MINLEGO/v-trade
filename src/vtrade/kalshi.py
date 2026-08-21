"""Unauthenticated Kalshi public-REST adapter for the binary paper contract.

Only this module knows Kalshi endpoint paths, cursor parameter names, fixed-point
field names, and the bid-only order-book representation.  Callers receive the
semantic types from :mod:`vtrade.domain.types`; malformed or incomplete venue
data raises a typed error and is never converted into a plausible default.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx

from vtrade.artifacts import ArtifactRef
from vtrade.domain.ports import ArtifactReference, ArtifactStore
from vtrade.domain.types import (
    BinaryEvent,
    BinaryMarket,
    BinaryOutcome,
    CataloguePage,
    CatalogueSnapshot,
    ContractQuantity,
    EventKey,
    MarketContext,
    MarketKey,
    MarketStatus,
    MoneyMicros,
    OutcomeKey,
    OutcomeSide,
    PriceGrid,
    RawArtifact,
    ResolutionObservation,
    Series,
    SeriesKey,
    build_canonical_order_book,
    to_contract_quantity,
    to_money_micros,
)

KALSHI_PUBLIC_REST_ROOT = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_SCHEMA_VERSION = "vtrade-binary-market-v1"
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
ORDINARY_BINARY_FORBIDDEN_FIELDS = frozenset(
    {"token_id", "venue_token_id", "condition_id", "negative_risk", "shares"}
)
MULTIVARIATE_MARKET_FIELDS = frozenset(
    {"mve_collection_ticker", "mve_selected_legs", "multivariate", "combo"}
)


class KalshiError(RuntimeError):
    """Base class for public-REST and normalization failures."""


class KalshiPayloadError(KalshiError):
    """The venue response cannot enter the v1 semantic contract."""


class KalshiTransportError(KalshiError):
    """A request failed after the bounded retry policy."""


class KalshiHTTPError(KalshiTransportError):
    def __init__(self, status_code: int, endpoint: str) -> None:
        super().__init__(f"Kalshi public REST returned HTTP {status_code} for {endpoint}")
        self.status_code = status_code
        self.endpoint = endpoint


class KalshiCursorError(KalshiPayloadError):
    """A cursor page is malformed, repeated, or otherwise incomplete."""


class KalshiLookAheadError(KalshiPayloadError):
    """A source or receive observation is newer than its requested cutoff."""


class KalshiDeadlineExceeded(KalshiTransportError):
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

    def delay(self, retry_number: int) -> float:
        if retry_number < 1:
            raise ValueError("retry_number must be positive")
        return float(
            min(
                self.maximum_backoff_seconds,
                self.initial_backoff_seconds * (2 ** (retry_number - 1)),
            )
        )


@dataclass(frozen=True, slots=True)
class HistoricalCutoff:
    market_settled_ts: datetime
    observed_at: datetime
    audit: RawArtifact

    def __post_init__(self) -> None:
        for value, field in (
            (self.market_settled_ts, "market_settled_ts"),
            (self.observed_at, "observed_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"historical cutoff {field} must be timezone-aware")
        if self.market_settled_ts > self.observed_at:
            raise ValueError("historical market cutoff cannot be newer than its observation")

    def route(self, source_timestamp: datetime | None) -> str:
        if source_timestamp is not None and source_timestamp < self.market_settled_ts:
            return "historical"
        return "live"


@dataclass(frozen=True, slots=True)
class _ArchivedResponse:
    payload: Any
    content: bytes
    observed_at: datetime
    artifact: RawArtifact
    endpoint: str
    request_identity: str


@dataclass(frozen=True, slots=True)
class _Deadline:
    monotonic_deadline: float | None

    def check(self) -> None:
        if self.monotonic_deadline is not None and time.monotonic() >= self.monotonic_deadline:
            raise KalshiDeadlineExceeded("Kalshi freeze transport deadline exceeded")


def _aware(value: datetime, field: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise KalshiPayloadError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: object, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (bool, float)):
        raise KalshiPayloadError(
            f"{field} must be an integer epoch or timezone-aware ISO timestamp"
        )
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        try:
            numeric = Decimal(str(value))
        except InvalidOperation as exc:
            raise KalshiPayloadError(f"{field} is not a valid epoch") from exc
        if numeric > Decimal("100000000000"):
            numeric /= Decimal(1000)
        try:
            return datetime.fromtimestamp(float(numeric), tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise KalshiPayloadError(f"{field} is outside the supported timestamp range") from exc
    if not isinstance(value, str):
        raise KalshiPayloadError(f"{field} must be an integer epoch or ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KalshiPayloadError(f"{field} is not a valid timestamp") from exc
    return _aware(parsed, field)


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or not value.strip():
        raise KalshiPayloadError(f"{field} must be a non-empty exact string")
    return value


def _first_string(
    payload: Mapping[str, Any], fields: Sequence[str], *, label: str, required: bool = True
) -> str | None:
    present = [
        payload[field] for field in fields if field in payload and payload[field] not in (None, "")
    ]
    if not present:
        if required:
            raise KalshiPayloadError(f"{label} is required")
        return None
    if any(not isinstance(value, str) or not value or not value.strip() for value in present):
        raise KalshiPayloadError(f"{label} must be a non-empty exact string")
    if any(value != present[0] for value in present[1:]):
        raise KalshiPayloadError(f"{label} has conflicting aliases")
    return cast(str, present[0])


def _resolution_source(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("resolution_source")
    if value not in (None, ""):
        if not isinstance(value, str) or not value or not value.strip():
            raise KalshiPayloadError("resolution_source must be a non-empty string")
        return value
    value = payload.get("settlement_sources")
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if not isinstance(value, (list, tuple)):
        raise KalshiPayloadError("settlement_sources must be an array")
    sources: list[str] = []
    for item in value:
        if isinstance(item, str):
            if not item or not item.strip():
                raise KalshiPayloadError("settlement_sources must contain non-empty strings")
            sources.append(item)
            continue
        if not isinstance(item, Mapping):
            raise KalshiPayloadError("settlement_sources contains an invalid source")
        name = item.get("name")
        url = item.get("url")
        if name not in (None, "") and not isinstance(name, str):
            raise KalshiPayloadError("settlement source name must be a string")
        if url not in (None, "") and not isinstance(url, str):
            raise KalshiPayloadError("settlement source URL must be a string")
        rendered = ": ".join(
            value for value in (name, url) if isinstance(value, str) and value.strip()
        )
        if not rendered:
            raise KalshiPayloadError("settlement source requires a name or URL")
        sources.append(rendered)
    return "\n".join(sources) if sources else None


def _root(payload: object, label: str = "response") -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise KalshiPayloadError(f"{label} must be a JSON object")
    return payload


def _nested_or_root(root: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = root.get(key)
    if nested is None:
        return root
    return _root(nested, key)


def _status(value: object, field: str = "status") -> MarketStatus:
    if not isinstance(value, str):
        raise KalshiPayloadError(f"{field} must be a known lifecycle string")
    try:
        return MarketStatus(value)
    except ValueError as exc:
        raise KalshiPayloadError(f"unknown Kalshi lifecycle status {value!r}") from exc


def _optional_timestamp(
    payload: Mapping[str, Any], fields: Sequence[str], label: str
) -> datetime | None:
    values = [
        payload[field] for field in fields if field in payload and payload[field] not in (None, "")
    ]
    if len(values) > 1 and values[0] != values[1]:
        raise KalshiPayloadError(f"{label} has conflicting aliases")
    return _parse_timestamp(values[0], label) if values else None


def _resolution_timestamp(payload: Mapping[str, Any]) -> datetime | None:
    for field in ("settlement_ts", "updated_time", "updated_ts", "close_time"):
        if field in payload and payload[field] not in (None, ""):
            return _parse_timestamp(payload[field], f"resolution {field}")
    return None


def _check_cutoff(value: datetime | None, cutoff: datetime | None, field: str) -> None:
    if value is not None and cutoff is not None and _aware(value, field) > _aware(cutoff, "cutoff"):
        raise KalshiLookAheadError(f"{field} is newer than the requested cutoff")


def _artifact_from_reference(
    reference: object,
    *,
    endpoint: str,
    request_identity: str,
    observed_at: datetime,
    source_timestamp: datetime | None = None,
    historical_cutoff: datetime | None = None,
) -> RawArtifact:
    if isinstance(reference, RawArtifact):
        return replace(
            reference,
            source_endpoint=endpoint,
            request_identity=request_identity,
            source_timestamp=source_timestamp,
            observed_at=observed_at,
            historical_cutoff=historical_cutoff,
            schema_version=KALSHI_SCHEMA_VERSION,
        )
    if isinstance(reference, ArtifactRef):
        return reference.as_raw_artifact(
            source_endpoint=endpoint,
            request_identity=request_identity,
            source_timestamp=source_timestamp,
            observed_at=observed_at,
            historical_cutoff=historical_cutoff,
            schema_version=KALSHI_SCHEMA_VERSION,
        )
    try:
        artifact_reference = cast(ArtifactReference, reference)
        sha256 = str(artifact_reference.sha256)
        byte_length = int(artifact_reference.byte_length)
        uri = str(artifact_reference.uri)
    except (AttributeError, TypeError, ValueError) as exc:
        raise KalshiPayloadError("artifact store returned an invalid reference") from exc
    return RawArtifact(
        sha256,
        byte_length,
        uri,
        endpoint,
        request_identity,
        source_timestamp,
        observed_at,
        historical_cutoff,
        KALSHI_SCHEMA_VERSION,
    )


class KalshiPublicRestAdapter:
    """Deep public REST adapter implementing the three semantic data ports."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        client: httpx.Client | None = None,
        root_url: str = KALSHI_PUBLIC_REST_ROOT,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], None] = time.sleep,
        maximum_parallel_requests: int = 8,
        request_timeout_seconds: float = 15.0,
        connect_timeout_seconds: float = 5.0,
        freeze_deadline_seconds: float = 600.0,
    ) -> None:
        if maximum_parallel_requests < 1:
            raise ValueError("maximum_parallel_requests must be positive")
        if request_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("request timeouts must be positive")
        if freeze_deadline_seconds <= 0:
            raise ValueError("freeze_deadline_seconds must be positive")
        if not root_url.startswith("https://"):
            raise ValueError("Kalshi public REST root must use HTTPS")
        self._artifact_store = artifact_store
        if client is not None:
            sensitive_headers = {
                "authorization",
                "cookie",
                "kalshi-access-key",
                "kalshi-access-signature",
                "kalshi-access-timestamp",
            }
            if any(name.lower() in sensitive_headers for name in client.headers):
                raise ValueError("public Kalshi adapter cannot be configured with credentials")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(request_timeout_seconds, connect=connect_timeout_seconds),
            trust_env=False,
        )
        self._owns_client = client is None
        self._root_url = root_url.rstrip("/")
        self._retry_policy = retry_policy or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._maximum_parallel_requests = maximum_parallel_requests
        self._freeze_deadline_seconds = freeze_deadline_seconds
        self._market_cache: dict[MarketKey, BinaryMarket] = {}
        self._event_metadata_cache: dict[str, tuple[Mapping[str, Any], _ArchivedResponse]] = {}
        self._series_metadata_cache: dict[str, tuple[Mapping[str, Any], _ArchivedResponse]] = {}
        self._resolution_history: dict[MarketKey, ResolutionObservation] = {}
        self._last_historical_cutoff: HistoricalCutoff | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> KalshiPublicRestAdapter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def last_historical_cutoff(self) -> HistoricalCutoff | None:
        return self._last_historical_cutoff

    def _deadline(self) -> _Deadline:
        return _Deadline(time.monotonic() + self._freeze_deadline_seconds)

    def _endpoint(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("Kalshi endpoint path must start with /")
        return self._root_url + path

    @staticmethod
    def _request_identity(request: httpx.Request) -> str:
        query = request.url.query.decode("ascii") if request.url.query else ""
        return f"{request.method} {request.url.path}" + (f"?{query}" if query else "")

    @staticmethod
    def _source_endpoint(request: httpx.Request) -> str:
        return f"{request.method} {request.url.path}"

    def _request(
        self,
        path: str,
        params: Mapping[str, str] | Sequence[tuple[str, str]] = (),
        *,
        deadline: _Deadline | None = None,
    ) -> _ArchivedResponse:
        endpoint = self._endpoint(path)
        for attempt in range(1, self._retry_policy.maximum_attempts + 1):
            if deadline is not None:
                deadline.check()
            request_params = (
                tuple(params.items()) if isinstance(params, Mapping) else tuple(params)
            )
            request = self._client.build_request("GET", endpoint, params=request_params)
            try:
                response = self._client.send(request)
            except httpx.HTTPError as exc:
                if attempt == self._retry_policy.maximum_attempts:
                    raise KalshiTransportError(f"request failed for {path}") from exc
                self._backoff(attempt, deadline)
                continue
            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt == self._retry_policy.maximum_attempts:
                    raise KalshiHTTPError(response.status_code, path)
                self._backoff(attempt, deadline)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise KalshiHTTPError(response.status_code, path)
            observed_at = _aware(self._clock(), "local observation time")
            content = bytes(response.content)
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KalshiPayloadError(f"{path} did not return valid UTF-8 JSON") from exc
            request_identity = self._request_identity(request)
            reference = self._artifact_store.put(content)
            artifact = _artifact_from_reference(
                reference,
                endpoint=self._source_endpoint(request),
                request_identity=request_identity,
                observed_at=observed_at,
            )
            return _ArchivedResponse(
                payload,
                content,
                observed_at,
                artifact,
                self._source_endpoint(request),
                request_identity,
            )
        raise AssertionError("retry loop must return or raise")

    def _backoff(self, retry_number: int, deadline: _Deadline | None) -> None:
        delay = self._retry_policy.delay(retry_number)
        if deadline is not None and deadline.monotonic_deadline is not None:
            remaining = deadline.monotonic_deadline - time.monotonic()
            if remaining <= 0:
                raise KalshiDeadlineExceeded("Kalshi freeze transport deadline exceeded")
            delay = min(delay, remaining)
        if delay:
            self._sleep(delay)

    def get_historical_cutoff(self, *, deadline: _Deadline | None = None) -> HistoricalCutoff:
        archived = self._request("/historical/cutoff", deadline=deadline)
        root = _root(archived.payload)
        payload = _nested_or_root(root, "cutoff")
        market_settled_ts = _parse_timestamp(payload.get("market_settled_ts"), "market_settled_ts")
        if market_settled_ts is None:
            raise KalshiPayloadError("historical cutoff lacks market_settled_ts")
        cutoff = HistoricalCutoff(
            market_settled_ts,
            archived.observed_at,
            archived.artifact,
        )
        self._last_historical_cutoff = cutoff
        return cutoff

    def sync_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot:
        deadline = self._deadline()
        historical_cutoff = self.get_historical_cutoff(deadline=deadline)
        requested_cutoff = _aware(cutoff, "catalogue cutoff") if cutoff is not None else None
        pages: list[CataloguePage] = []
        seen_cursors: set[str] = set()
        seen_page_hashes: set[str] = set()
        seen_market_keys: set[MarketKey] = set()
        cursor: str | None = None
        while True:
            deadline.check()
            params: dict[str, str] = {
                "status": "open",
                "mve_filter": "exclude",
                "limit": "1000",
            }
            if cursor is not None:
                params["cursor"] = cursor
            archived = self._request("/markets", params, deadline=deadline)
            if archived.artifact.sha256 in seen_page_hashes:
                raise KalshiCursorError("catalogue returned a duplicate raw page")
            seen_page_hashes.add(archived.artifact.sha256)
            page = self._catalogue_page(
                archived, cursor, requested_cutoff, historical_cutoff, deadline
            )
            duplicate_keys = [
                market.key for market in page.markets if market.key in seen_market_keys
            ]
            page_keys = [market.key for market in page.markets]
            if len(set(page_keys)) != len(page_keys) or duplicate_keys:
                raise KalshiCursorError("catalogue contains a duplicate market identity")
            seen_market_keys.update(page_keys)
            pages.append(page)
            next_cursor = page.next_cursor
            if next_cursor is None:
                break
            if next_cursor in seen_cursors or next_cursor == cursor:
                raise KalshiCursorError("catalogue cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        data_cutoff = requested_cutoff or _aware(self._clock(), "catalogue completion time")
        for page in pages:
            _check_cutoff(page.observed_at, data_cutoff, "catalogue page observation")
        return CatalogueSnapshot(tuple(pages), data_cutoff, historical_cutoff.market_settled_ts)

    def read_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot:
        return self.sync_catalogue(cutoff=cutoff)

    def catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot:
        return self.sync_catalogue(cutoff=cutoff)

    def _catalogue_page(
        self,
        archived: _ArchivedResponse,
        cursor: str | None,
        cutoff: datetime | None,
        historical_cutoff: HistoricalCutoff,
        deadline: _Deadline,
    ) -> CataloguePage:
        root = _root(archived.payload)
        rows = root.get("markets")
        if not isinstance(rows, list):
            raise KalshiPayloadError("markets response lacks a markets array")
        next_cursor = self._next_cursor(root)
        markets: list[BinaryMarket] = []
        events: dict[EventKey, BinaryEvent] = {}
        series: dict[SeriesKey, Series] = {}
        metadata_audits: list[RawArtifact] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise KalshiPayloadError(f"markets[{index}] must be an object")
            event_ref = _first_string(
                row, ("event_ticker", "event_ref"), label="event ticker"
            )
            assert event_ref is not None
            event_metadata: Mapping[str, Any] | None = None
            event_archived: _ArchivedResponse | None = None
            series_ref = _first_string(
                row, ("series_ticker", "series_ref"), label="series ticker", required=False
            )
            if series_ref is None:
                event_metadata, event_archived = self._fetch_event_metadata(
                    event_ref, deadline=deadline
                )
                series_ref = _first_string(
                    event_metadata,
                    ("series_ticker", "series_ref"),
                    label="series ticker",
                )
                assert series_ref is not None
            series_metadata: Mapping[str, Any] | None = None
            series_archived: _ArchivedResponse | None = None
            if event_metadata is not None:
                series_metadata, series_archived = self._fetch_series_metadata(
                    series_ref, deadline=deadline
                )
            market = self._normalize_market(
                row,
                archived,
                cutoff,
                historical_cutoff,
                series_ref=series_ref,
                resolution_source=(
                    _resolution_source(event_metadata) if event_metadata is not None else None
                ),
            )
            markets.append(market)
            event = self._event_from_market(
                row,
                archived,
                market,
                metadata=event_metadata,
                metadata_archived=event_archived,
            )
            events[event.key] = event
            family = self._series_from_market(
                row,
                archived,
                market,
                metadata=series_metadata,
                metadata_archived=series_archived,
            )
            series[family.key] = family
            if event_archived is not None:
                metadata_audits.append(event_archived.artifact)
            if series_archived is not None:
                metadata_audits.append(series_archived.artifact)
            self._remember_market(market)
        return CataloguePage(
            cursor,
            next_cursor,
            archived.observed_at,
            tuple(series.values()),
            tuple(events.values()),
            tuple(markets),
            archived.artifact,
            tuple(dict.fromkeys(metadata_audits)),
        )

    def _fetch_event_metadata(
        self, event_ref: str, *, deadline: _Deadline | None = None
    ) -> tuple[Mapping[str, Any], _ArchivedResponse]:
        cached = self._event_metadata_cache.get(event_ref)
        if cached is not None:
            return cached
        archived = self._request(
            f"/events/{event_ref}",
            deadline=deadline,
        )
        payload = _nested_or_root(_root(archived.payload), "event")
        returned = _first_string(payload, ("event_ticker", "event_ref"), label="event ticker")
        if returned != event_ref:
            raise KalshiPayloadError("event response reference does not match request")
        cached = (payload, archived)
        self._event_metadata_cache[event_ref] = cached
        return cached

    def _fetch_series_metadata(
        self, series_ref: str, *, deadline: _Deadline | None = None
    ) -> tuple[Mapping[str, Any], _ArchivedResponse]:
        cached = self._series_metadata_cache.get(series_ref)
        if cached is not None:
            return cached
        archived = self._request(
            f"/series/{series_ref}",
            deadline=deadline,
        )
        payload = _nested_or_root(_root(archived.payload), "series")
        returned = _first_string(
            payload, ("ticker", "series_ticker", "series_ref"), label="series ticker"
        )
        if returned != series_ref:
            raise KalshiPayloadError("series response reference does not match request")
        cached = (payload, archived)
        self._series_metadata_cache[series_ref] = cached
        return cached

    def _remember_market(self, market: BinaryMarket) -> None:
        previous = self._market_cache.get(market.key)
        if previous is not None and (
            previous.event_key != market.event_key or previous.series_key != market.series_key
        ):
            raise KalshiPayloadError(
                "one market identity has conflicting event or series references"
            )
        self._market_cache[market.key] = market

    @staticmethod
    def _next_cursor(root: Mapping[str, Any]) -> str | None:
        candidates = [root[name] for name in ("cursor", "next_cursor") if name in root]
        if (
            len(candidates) > 1
            and candidates[0] not in (None, "")
            and candidates[1] not in (None, "")
            and candidates[0] != candidates[1]
        ):
            raise KalshiCursorError("response has conflicting cursor fields")
        value = next((candidate for candidate in candidates if candidate not in (None, "")), None)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise KalshiCursorError("catalogue cursor must be an opaque non-empty string")
        return value

    def _normalize_market(
        self,
        payload: Mapping[str, Any],
        archived: _ArchivedResponse,
        cutoff: datetime | None,
        historical_cutoff: HistoricalCutoff,
        *,
        series_ref: str | None = None,
        resolution_source: str | None = None,
    ) -> BinaryMarket:
        forbidden = sorted(
            field
            for field in ORDINARY_BINARY_FORBIDDEN_FIELDS
            if field in payload
        )
        if forbidden:
            raise KalshiPayloadError(f"market contains forbidden token-shaped fields: {forbidden}")
        for field in MULTIVARIATE_MARKET_FIELDS:
            value = payload.get(field)
            if value not in (None, False, "", [], {}):
                raise KalshiPayloadError("multivariate or combo market is outside v1")
        market_type = payload.get("market_type")
        if market_type not in (None, "binary", "Binary"):
            raise KalshiPayloadError("only ordinary binary markets are admitted")
        if "outcomes" in payload:
            raw_outcomes = payload["outcomes"]
            if not isinstance(raw_outcomes, (list, tuple)) or not all(
                isinstance(value, str) for value in raw_outcomes
            ) or set(raw_outcomes) != {"YES", "NO"}:
                raise KalshiPayloadError(
                    "ordinary binary markets require exactly YES and NO outcomes"
                )
        raw_result = payload.get("result")
        if raw_result not in (None, "", "yes", "no"):
            raise KalshiPayloadError("only binary YES/NO results are admitted")
        market_ref = _first_string(payload, ("ticker", "market_ticker"), label="market ticker")
        event_ref = _first_string(payload, ("event_ticker", "event_ref"), label="event ticker")
        series_ref = series_ref or _first_string(
            payload, ("series_ticker", "series_ref"), label="series ticker"
        )
        assert market_ref is not None and event_ref is not None and series_ref is not None
        key = MarketKey(market_ref)
        event_key = EventKey(event_ref)
        series_key = SeriesKey(series_ref)
        status = _status(payload.get("status"))
        question = _first_string(payload, ("title", "question"), label="market question")
        rules = _first_string(
            payload,
            ("rules_primary", "resolution_rules", "rules"),
            label="resolution rules",
        )
        assert question is not None and rules is not None
        resolution_source = _resolution_source(payload) or resolution_source
        open_time = _parse_timestamp(payload.get("open_time"), "open_time")
        if open_time is None:
            raise KalshiPayloadError("open_time is required")
        close_time = _optional_timestamp(payload, ("close_time",), "close_time")
        expected_expiration = _optional_timestamp(
            payload, ("expected_expiration_time",), "expected_expiration_time"
        )
        latest_expiration = _optional_timestamp(
            payload, ("latest_expiration_time",), "latest_expiration_time"
        )
        source_updated = _optional_timestamp(
            payload, ("updated_time", "updated_ts", "updated_at"), "updated_time"
        )
        for timestamp, field in (
            (open_time, "open_time"),
            (close_time, "close_time"),
            (expected_expiration, "expected_expiration_time"),
            (latest_expiration, "latest_expiration_time"),
            (source_updated, "updated_time"),
        ):
            if field == "updated_time":
                _check_cutoff(timestamp, cutoff, field)
                if timestamp is not None and timestamp > archived.observed_at:
                    raise KalshiLookAheadError(f"{field} is newer than local observation")
        grid = PriceGrid.from_ranges(payload.get("price_ranges"))
        labels = {
            OutcomeSide.YES: _first_string(
                payload, ("yes_sub_title", "yes_label"), label="YES label", required=False
            )
            or "YES",
            OutcomeSide.NO: _first_string(
                payload, ("no_sub_title", "no_label"), label="NO label", required=False
            )
            or "NO",
        }
        outcomes = tuple(
            BinaryOutcome(OutcomeKey(key, side), labels[side], status is MarketStatus.ACTIVE)
            for side in (OutcomeSide.YES, OutcomeSide.NO)
        )
        volume_raw = payload.get("volume_fp", payload.get("volume", 0))
        liquidity_raw = payload.get("liquidity_dollars", payload.get("liquidity", 0))
        try:
            volume = to_contract_quantity(volume_raw, field="volume_fp")
        except ValueError as exc:
            raise KalshiPayloadError(str(exc)) from exc
        try:
            liquidity = to_money_micros(liquidity_raw, field="liquidity_dollars")
        except ValueError as exc:
            raise KalshiPayloadError(str(exc)) from exc
        artifact = replace(
            archived.artifact,
            source_timestamp=source_updated,
            historical_cutoff=historical_cutoff.market_settled_ts,
        )
        return BinaryMarket(
            key,
            series_key,
            event_key,
            question,
            rules,
            resolution_source,
            open_time,
            close_time,
            expected_expiration,
            latest_expiration,
            status,
            status is MarketStatus.ACTIVE,
            grid,
            outcomes,  # type: ignore[arg-type]
            archived.observed_at,
            artifact,
            source_updated,
            ContractQuantity(int(volume)),
            MoneyMicros(int(liquidity)),
        )

    @staticmethod
    def _event_from_market(
        payload: Mapping[str, Any],
        archived: _ArchivedResponse,
        market: BinaryMarket,
        *,
        metadata: Mapping[str, Any] | None = None,
        metadata_archived: _ArchivedResponse | None = None,
    ) -> BinaryEvent:
        source = metadata or payload
        title = _first_string(
            source, ("title", "event_title", "event_name"), label="event title", required=False
        )
        return BinaryEvent(
            market.event_key,
            market.series_key,
            title or market.event_key.event_ref,
            _first_string(source, ("category",), label="event category", required=False),
            metadata_archived.observed_at if metadata_archived else archived.observed_at,
            metadata_archived.artifact if metadata_archived else archived.artifact,
        )

    @staticmethod
    def _series_from_market(
        payload: Mapping[str, Any],
        archived: _ArchivedResponse,
        market: BinaryMarket,
        *,
        metadata: Mapping[str, Any] | None = None,
        metadata_archived: _ArchivedResponse | None = None,
    ) -> Series:
        source = metadata or payload
        title = _first_string(
            source,
            ("title", "series_title", "series_name"),
            label="series title",
            required=False,
        )
        rules = _first_string(
            source,
            ("rules_primary", "series_rules"),
            label="series rules",
            required=False,
        )
        return Series(
            market.series_key,
            title or market.series_key.series_ref,
            rules,
            metadata_archived.observed_at if metadata_archived else archived.observed_at,
            metadata_archived.artifact if metadata_archived else archived.artifact,
        )

    def _market_path(self, market: BinaryMarket, historical_cutoff: HistoricalCutoff) -> str:
        source_time = market.latest_expiration_time or market.close_time
        prefix = "/historical" if historical_cutoff.route(source_time) == "historical" else ""
        return f"{prefix}/markets/{market.key.market_ref}"

    def _fetch_market(
        self,
        market_key: MarketKey,
        *,
        cutoff: datetime,
        historical_cutoff: HistoricalCutoff,
        deadline: _Deadline | None = None,
    ) -> BinaryMarket:
        cached = self._market_cache.get(market_key)
        if cached is not None:
            _check_cutoff(
                cached.source_updated_at or cached.observed_at, cutoff, "market observation"
            )
            return cached
        # A market not yet in the catalogue has no lifecycle timestamp with which
        # to route it. Try live first, then the historical single-market endpoint
        # on a definitive 404 so held old markets remain resolvable.
        path = f"/markets/{market_key.market_ref}"
        try:
            archived = self._request(path, deadline=deadline)
        except KalshiHTTPError as exc:
            if exc.status_code != 404:
                raise
            archived = self._request(
                f"/historical/markets/{market_key.market_ref}",
                deadline=deadline,
            )
        payload = _nested_or_root(_root(archived.payload), "market")
        series_ref = _first_string(
            payload, ("series_ticker", "series_ref"), label="series ticker", required=False
        )
        if series_ref is None:
            event_ref = _first_string(payload, ("event_ticker", "event_ref"), label="event ticker")
            assert event_ref is not None
            event_metadata, _ = self._fetch_event_metadata(event_ref, deadline=deadline)
            series_ref = _first_string(
                event_metadata, ("series_ticker", "series_ref"), label="series ticker"
            )
        market = self._normalize_market(
            payload,
            archived,
            cutoff,
            historical_cutoff,
            series_ref=series_ref,
            resolution_source=_resolution_source(event_metadata),
        )
        if market.key != market_key:
            raise KalshiPayloadError("market response ticker does not match the requested key")
        self._remember_market(market)
        return market

    def get_context(self, market_key: MarketKey, *, cutoff: datetime) -> MarketContext:
        requested_cutoff = _aware(cutoff, "context cutoff")
        historical_cutoff = self._last_historical_cutoff or self.get_historical_cutoff()
        deadline = self._deadline()
        market = self._fetch_market(
            market_key,
            cutoff=requested_cutoff,
            historical_cutoff=historical_cutoff,
            deadline=deadline,
        )
        path = f"/markets/{market.key.market_ref}/orderbook"
        archived = self._request(path, {"depth": "0"}, deadline=deadline)
        root = _root(archived.payload)
        book_payload = root.get("orderbook_fp")
        if book_payload is None:
            book_payload = root.get("orderbook")
        if book_payload is None:
            raise KalshiPayloadError("orderbook response lacks orderbook_fp")
        book_payload = _root(book_payload, "orderbook_fp")
        source_timestamp = _optional_timestamp(
            book_payload, ("timestamp", "updated_ts", "updated_time"), "orderbook timestamp"
        )
        _check_cutoff(source_timestamp, requested_cutoff, "orderbook timestamp")
        observed = archived.observed_at
        if observed > requested_cutoff:
            raise KalshiLookAheadError("orderbook observation is newer than the requested cutoff")
        artifact = replace(
            archived.artifact,
            source_timestamp=source_timestamp,
            historical_cutoff=historical_cutoff.market_settled_ts,
        )
        try:
            book = build_canonical_order_book(
                market.key,
                market.price_grid,
                book_payload.get("yes_dollars"),
                book_payload.get("no_dollars"),
                observed_at=observed,
                cutoff=requested_cutoff,
                artifact=artifact,
                source_timestamp=source_timestamp,
            )
        except ValueError as exc:
            raise KalshiPayloadError(str(exc)) from exc
        return MarketContext(market, book)

    def get_market_context(self, market_key: MarketKey, *, cutoff: datetime) -> MarketContext:
        return self.get_context(market_key, cutoff=cutoff)

    def get_contexts(
        self, market_keys: Sequence[MarketKey], *, cutoff: datetime
    ) -> tuple[MarketContext, ...]:
        unique = tuple(dict.fromkeys(market_keys))
        if len(unique) != len(market_keys):
            raise ValueError("context request contains duplicate market identities")
        if not unique:
            return ()
        with ThreadPoolExecutor(
            max_workers=min(self._maximum_parallel_requests, len(unique))
        ) as executor:
            return tuple(executor.map(lambda key: self.get_context(key, cutoff=cutoff), unique))

    def get_resolutions(
        self, market_keys: Sequence[MarketKey], *, cutoff: datetime
    ) -> tuple[ResolutionObservation, ...]:
        requested_cutoff = _aware(cutoff, "resolution cutoff")
        unique = tuple(dict.fromkeys(market_keys))
        if not unique:
            return ()
        historical_cutoff = self._last_historical_cutoff or self.get_historical_cutoff()
        deadline = self._deadline()

        def read(key: MarketKey) -> ResolutionObservation:
            market = self._market_cache.get(key)
            if market is None:
                market = self._fetch_market(
                    key,
                    cutoff=requested_cutoff,
                    historical_cutoff=historical_cutoff,
                    deadline=deadline,
                )
            path = self._market_path(market, historical_cutoff)
            archived = self._request(path, deadline=deadline)
            payload = _nested_or_root(_root(archived.payload), "market")
            returned = _first_string(
                payload, ("ticker", "market_ticker"), label="resolution ticker"
            )
            if returned != key.market_ref:
                raise KalshiPayloadError("resolution response ticker does not match request")
            status = _status(payload.get("status"), "resolution status")
            raw_result = payload.get("result")
            result: OutcomeSide | None
            if raw_result in (None, ""):
                result = None
            elif raw_result in ("yes", "no"):
                result = OutcomeSide.YES if raw_result == "yes" else OutcomeSide.NO
            else:
                raise KalshiPayloadError("resolution result is not binary YES/NO")
            source_timestamp = _resolution_timestamp(payload)
            settlement_ts = _parse_timestamp(payload.get("settlement_ts"), "settlement_ts")
            _check_cutoff(source_timestamp, requested_cutoff, "resolution source timestamp")
            artifact = replace(
                archived.artifact,
                source_timestamp=source_timestamp,
                historical_cutoff=historical_cutoff.market_settled_ts,
            )
            observation = ResolutionObservation(
                key,
                status,
                result,
                archived.observed_at,
                source_timestamp,
                settlement_ts,
                artifact,
            )
            previous = self._resolution_history.get(key)
            if previous is None or previous.observed_at <= observation.observed_at:
                self._resolution_history[key] = observation
            return observation

        with ThreadPoolExecutor(max_workers=self._maximum_parallel_requests) as executor:
            observations = tuple(executor.map(read, unique))
        return tuple(
            observation
            for observation in observations
            if observation.observed_at <= requested_cutoff
        )

    def sync_resolutions(
        self, market_keys: Sequence[MarketKey], *, cutoff: datetime
    ) -> tuple[ResolutionObservation, ...]:
        return self.get_resolutions(market_keys, cutoff=cutoff)

    def resolutions_as_of(
        self, market_keys: Sequence[MarketKey], *, cutoff: datetime
    ) -> tuple[ResolutionObservation, ...]:
        requested = _aware(cutoff, "resolution cutoff")
        wanted = set(market_keys)
        return tuple(
            observation
            for key, observation in sorted(
                self._resolution_history.items(), key=lambda item: item[0].canonical
            )
            if key in wanted and observation.observed_at <= requested
        )

    def fetch_event(self, event_key: EventKey, *, cutoff: datetime | None = None) -> BinaryEvent:
        deadline = self._deadline()
        archived = self._request(f"/events/{event_key.event_ref}", deadline=deadline)
        requested_cutoff = _aware(cutoff, "event cutoff") if cutoff else None
        _check_cutoff(archived.observed_at, requested_cutoff, "event observation")
        payload = _nested_or_root(_root(archived.payload), "event")
        returned = _first_string(payload, ("event_ticker", "event_ref"), label="event ticker")
        if returned != event_key.event_ref:
            raise KalshiPayloadError("event response reference does not match request")
        series_ref = _first_string(payload, ("series_ticker", "series_ref"), label="series ticker")
        title = _first_string(payload, ("title", "event_title"), label="event title")
        assert series_ref is not None and title is not None
        return BinaryEvent(
            event_key,
            SeriesKey(series_ref),
            title,
            _first_string(payload, ("category",), label="event category", required=False),
            archived.observed_at,
            archived.artifact,
        )

    def fetch_series(self, series_key: SeriesKey, *, cutoff: datetime | None = None) -> Series:
        deadline = self._deadline()
        archived = self._request(f"/series/{series_key.series_ref}", deadline=deadline)
        requested_cutoff = _aware(cutoff, "series cutoff") if cutoff else None
        _check_cutoff(archived.observed_at, requested_cutoff, "series observation")
        payload = _nested_or_root(_root(archived.payload), "series")
        returned = _first_string(
            payload, ("ticker", "series_ticker", "series_ref"), label="series ticker"
        )
        if returned != series_key.series_ref:
            raise KalshiPayloadError("series response reference does not match request")
        title = _first_string(payload, ("title", "name"), label="series title")
        rules = _first_string(
            payload, ("rules_primary", "rules"), label="series rules", required=False
        )
        assert title is not None
        return Series(series_key, title, rules, archived.observed_at, archived.artifact)


# Names used by the implementation notes and by callers that prefer the
# shorter Adapter/Venue terminology.  They refer to exactly the same deep
# implementation; no second transport implementation is maintained.
KalshiVenue = KalshiPublicRestAdapter
KalshiAdapter = KalshiPublicRestAdapter
KalshiPublicRESTAdapter = KalshiPublicRestAdapter
KalshiPublicRest = KalshiPublicRestAdapter


__all__ = [
    "KALSHI_PUBLIC_REST_ROOT",
    "KALSHI_SCHEMA_VERSION",
    "RETRYABLE_STATUS_CODES",
    "HistoricalCutoff",
    "KalshiAdapter",
    "KalshiCursorError",
    "KalshiDeadlineExceeded",
    "KalshiError",
    "KalshiHTTPError",
    "KalshiLookAheadError",
    "KalshiPayloadError",
    "KalshiPublicRESTAdapter",
    "KalshiPublicRest",
    "KalshiPublicRestAdapter",
    "KalshiTransportError",
    "KalshiVenue",
    "RetryPolicy",
]
