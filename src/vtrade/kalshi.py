"""Unauthenticated Kalshi public-REST adapter for the binary paper contract.

Only this module knows Kalshi endpoint paths, cursor parameter names, fixed-point
field names, and the bid-only order-book representation.  Callers receive the
semantic types from :mod:`vtrade.domain.types`; malformed or incomplete venue
data raises a typed error and is never converted into a plausible default.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, cast

import httpx

from vtrade.artifacts import ArtifactRef
from vtrade.deadline import DeadlineExceeded, deadline_remaining, run_with_deadline
from vtrade.domain.ports import ArtifactReference, ArtifactStore
from vtrade.domain.types import (
    BinaryEvent,
    BinaryMarket,
    BinaryOutcome,
    CataloguePage,
    CatalogueScanRequest,
    CatalogueScanResult,
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
_LOGGER = logging.getLogger(__name__)
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


def _parallel_map[T, R](
    values: Sequence[T],
    worker: Callable[[T], R],
    *,
    maximum_workers: int,
    deadline: _Deadline,
) -> tuple[R, ...]:
    executor = ThreadPoolExecutor(max_workers=maximum_workers)
    futures = [executor.submit(worker, value) for value in values]
    try:
        results: list[R] = []
        for future in futures:
            deadline.check()
            timeout = (
                None
                if deadline.monotonic_deadline is None
                else max(0.0, deadline.monotonic_deadline - time.monotonic())
            )
            try:
                results.append(future.result(timeout=timeout))
            except TimeoutError as exc:
                raise KalshiDeadlineExceeded(
                    "Kalshi freeze deadline exceeded while waiting for parallel work"
                ) from exc
        deadline.check()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    if (
        deadline.monotonic_deadline is not None
        and time.monotonic() >= deadline.monotonic_deadline
    ):
        executor.shutdown(wait=False, cancel_futures=True)
        raise KalshiDeadlineExceeded(
            "Kalshi freeze deadline exceeded while shutting down parallel work"
        )
    executor.shutdown(wait=True)
    return tuple(results)


@dataclass(frozen=True, slots=True)
class _CatalogueCandidate:
    key: MarketKey
    payload: Mapping[str, Any]
    page_index: int
    row_index: int
    volume: int
    liquidity_micros: int
    tradeable: bool


@dataclass(frozen=True, slots=True)
class _CataloguePageEvidence:
    requested_cursor: str | None
    next_cursor: str | None
    observed_at: datetime
    artifact: RawArtifact
    endpoint: str
    request_identity: str
    record_count: int

    def archived(self) -> _ArchivedResponse:
        return _ArchivedResponse(
            None,
            b"",
            self.observed_at,
            self.artifact,
            self.endpoint,
            self.request_identity,
        )


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
        catalogue_sync_deadline_seconds: float = 300.0,
        freeze_deadline_seconds: float = 600.0,
    ) -> None:
        if maximum_parallel_requests < 1:
            raise ValueError("maximum_parallel_requests must be positive")
        if request_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("request timeouts must be positive")
        if catalogue_sync_deadline_seconds <= 0:
            raise ValueError("catalogue sync deadline must be positive")
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
        self._catalogue_sync_deadline_seconds = catalogue_sync_deadline_seconds
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

    def _deadline(self, monotonic_deadline: float | None = None) -> _Deadline:
        return _Deadline(
            monotonic_deadline
            if monotonic_deadline is not None
            else time.monotonic() + self._freeze_deadline_seconds
        )

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
            request_identity = self._request_identity(request)
            operation_started = time.monotonic()
            try:
                if deadline is None or deadline.monotonic_deadline is None:
                    response = self._client.send(request)
                else:
                    response = run_with_deadline(
                        partial(self._client.send, request),
                        deadline=deadline.monotonic_deadline,
                        label=f"Kalshi request {request_identity}",
                    )
            except DeadlineExceeded as exc:
                raise KalshiDeadlineExceeded(str(exc)) from exc
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
            if deadline is not None:
                deadline.check()
            content = bytes(response.content)
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KalshiPayloadError(f"{path} did not return valid UTF-8 JSON") from exc
            if deadline is not None:
                deadline.check()
            artifact_started = time.monotonic()
            try:
                if deadline is None or deadline.monotonic_deadline is None:
                    reference = self._artifact_store.put(content)
                else:
                    reference = run_with_deadline(
                        partial(self._artifact_store.put, content),
                        deadline=deadline.monotonic_deadline,
                        label=f"Kalshi artifact upload {request_identity}",
                    )
            except DeadlineExceeded as exc:
                raise KalshiDeadlineExceeded(str(exc)) from exc
            if deadline is not None:
                deadline.check()
            remaining_ms = (
                deadline_remaining(deadline.monotonic_deadline) * 1000
                if deadline is not None and deadline.monotonic_deadline is not None
                else -1.0
            )
            _LOGGER.info(
                "market_freeze stage_boundary event=external_complete endpoint=%s "
                "request_identity=%s attempt=%s elapsed_ms=%.3f bytes=%s "
                "artifact_upload_ms=%.3f deadline_remaining_ms=%.3f",
                self._source_endpoint(request),
                request_identity,
                attempt,
                (time.monotonic() - operation_started) * 1000,
                len(content),
                (time.monotonic() - artifact_started) * 1000,
                remaining_ms,
            )
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
            try:
                if deadline is None or deadline.monotonic_deadline is None:
                    self._sleep(delay)
                else:
                    run_with_deadline(
                        lambda: self._sleep(delay),
                        deadline=deadline.monotonic_deadline,
                        label="Kalshi retry backoff",
                    )
            except DeadlineExceeded as exc:
                raise KalshiDeadlineExceeded(str(exc)) from exc

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

    def scan_catalogue(
        self,
        request: CatalogueScanRequest,
        *,
        deadline: float | None = None,
    ) -> CatalogueScanResult:
        """Scan every page while retaining only the bounded semantic shortlist."""

        outer_deadline = self._deadline(deadline)
        scan_deadline = _Deadline(
            min(
                value
                for value in (
                    outer_deadline.monotonic_deadline,
                    time.monotonic() + self._catalogue_sync_deadline_seconds,
                )
                if value is not None
            )
        )
        self._market_cache.clear()
        self._event_metadata_cache.clear()
        self._series_metadata_cache.clear()
        historical_cutoff = self.get_historical_cutoff(deadline=scan_deadline)
        requested_cutoff = (
            _aware(request.cutoff, "catalogue cutoff") if request.cutoff is not None else None
        )
        page_evidence: list[_CataloguePageEvidence] = []
        seen_cursors: set[str] = set()
        seen_page_hashes: set[str] = set()
        seen_market_keys: set[MarketKey] = set()
        historical_keys = request.historical_markets[: request.maximum_historical_markets]
        historical_key_set = set(historical_keys)
        always_retained_keys = tuple(
            dict.fromkeys((*request.held_markets, *request.touched_markets))
        )
        always_retained_key_set = set(always_retained_keys)
        retained: dict[MarketKey, _CatalogueCandidate] = {}
        additional: dict[MarketKey, _CatalogueCandidate] = {}
        cursor: str | None = None
        scanned_market_count = 0
        try:
            while True:
                scan_deadline.check()
                params: dict[str, str] = {
                    "status": "open",
                    "mve_filter": "exclude",
                    "limit": "1000",
                }
                if cursor is not None:
                    params["cursor"] = cursor
                archived = self._request("/markets", params, deadline=scan_deadline)
                if archived.artifact.sha256 in seen_page_hashes:
                    raise KalshiCursorError("catalogue returned a duplicate raw page")
                seen_page_hashes.add(archived.artifact.sha256)
                root = _root(archived.payload)
                rows = root.get("markets")
                if not isinstance(rows, list):
                    raise KalshiPayloadError("markets response lacks a markets array")
                next_cursor = self._next_cursor(root)
                page_index = len(page_evidence)
                page_evidence.append(
                    _CataloguePageEvidence(
                        cursor,
                        next_cursor,
                        archived.observed_at,
                        archived.artifact,
                        archived.endpoint,
                        archived.request_identity,
                        len(rows),
                    )
                )
                _LOGGER.info(
                    "market_freeze stage_boundary event=catalogue_page page=%s "
                    "cursor=%s next_cursor=%s record_count=%s "
                    "deadline_remaining_ms=%.3f",
                    page_index,
                    cursor,
                    next_cursor,
                    len(rows),
                    deadline_remaining(scan_deadline.monotonic_deadline) * 1000
                    if scan_deadline.monotonic_deadline is not None
                    else -1.0,
                )
                scanned_market_count += len(rows)
                for row_index, row in enumerate(rows):
                    scan_deadline.check()
                    if not isinstance(row, Mapping):
                        raise KalshiPayloadError(f"markets[{row_index}] must be an object")
                    candidate = self._catalogue_candidate(
                        row,
                        page_index=page_index,
                        row_index=row_index,
                        observed_at=archived.observed_at,
                        cutoff=requested_cutoff,
                    )
                    if candidate is None:
                        continue
                    if candidate.key in seen_market_keys:
                        raise KalshiCursorError("catalogue contains a duplicate market identity")
                    seen_market_keys.add(candidate.key)
                    if not candidate.tradeable:
                        continue
                    if (
                        candidate.key in historical_key_set
                        or candidate.key in always_retained_key_set
                    ):
                        retained[candidate.key] = candidate
                        continue
                    if request.maximum_additional_markets <= 0:
                        continue
                    if len(additional) < request.maximum_additional_markets:
                        additional[candidate.key] = candidate
                        continue
                    worst_key = max(
                        additional,
                        key=lambda key: self._candidate_rank(additional[key]),
                    )
                    if self._candidate_rank(candidate) < self._candidate_rank(
                        additional[worst_key]
                    ):
                        del additional[worst_key]
                        additional[candidate.key] = candidate
                if next_cursor is None:
                    break
                if next_cursor in seen_cursors or next_cursor == cursor:
                    raise KalshiCursorError("catalogue cursor repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            scan_deadline.check()
            data_cutoff = requested_cutoff or _aware(
                self._clock(), "catalogue completion time"
            )
            for page in page_evidence:
                _check_cutoff(page.observed_at, data_cutoff, "catalogue page observation")
            discovery_keys = self._discovery_keys(
                request,
                retained,
                additional,
            )
            selected = {key: retained.get(key) or additional.get(key) for key in discovery_keys}
            selected_candidates = {
                key: candidate for key, candidate in selected.items() if candidate is not None
            }
            outer_deadline.check()
            pages = self._materialize_catalogue_pages(
                page_evidence,
                selected_candidates,
                cutoff=requested_cutoff,
                historical_cutoff=historical_cutoff,
                deadline=outer_deadline,
            )
            outer_deadline.check()
            return CatalogueScanResult(
                tuple(pages),
                discovery_keys,
                data_cutoff,
                historical_cutoff.market_settled_ts,
                scanned_market_count,
            )
        finally:
            seen_cursors.clear()
            seen_page_hashes.clear()
            seen_market_keys.clear()
            historical_key_set.clear()
            always_retained_key_set.clear()
            retained.clear()
            additional.clear()
            page_evidence.clear()

    def sync_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot:
        return self.scan_catalogue(CatalogueScanRequest(cutoff=cutoff)).snapshot

    def read_catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot:
        return self.sync_catalogue(cutoff=cutoff)

    def catalogue(self, *, cutoff: datetime | None = None) -> CatalogueSnapshot:
        return self.sync_catalogue(cutoff=cutoff)

    @staticmethod
    def _candidate_rank(candidate: _CatalogueCandidate) -> tuple[int, int, str]:
        return (-candidate.volume, -candidate.liquidity_micros, candidate.key.market_ref)

    @staticmethod
    def _discovery_keys(
        request: CatalogueScanRequest,
        retained: Mapping[MarketKey, _CatalogueCandidate],
        additional: Mapping[MarketKey, _CatalogueCandidate],
    ) -> tuple[MarketKey, ...]:
        values: list[MarketKey] = []
        for key in (
            *request.historical_markets[: request.maximum_historical_markets],
            *request.held_markets,
            *request.touched_markets,
        ):
            if key in retained and key not in values:
                values.append(key)
        values.extend(
            key
            for key, _candidate in sorted(
                additional.items(),
                key=lambda item: KalshiPublicRestAdapter._candidate_rank(item[1]),
            )
            if key not in values
        )
        return tuple(values)

    @staticmethod
    def _catalogue_candidate(
        row: Mapping[str, Any],
        *,
        page_index: int,
        row_index: int,
        observed_at: datetime,
        cutoff: datetime | None,
    ) -> _CatalogueCandidate | None:
        forbidden = sorted(field for field in ORDINARY_BINARY_FORBIDDEN_FIELDS if field in row)
        if forbidden:
            raise KalshiPayloadError(
                f"market contains forbidden token-shaped fields: {forbidden}"
            )
        for field in MULTIVARIATE_MARKET_FIELDS:
            value = row.get(field)
            if value not in (None, False, "", [], {}):
                return None
        market_type = row.get("market_type")
        if market_type is not None and not isinstance(market_type, str):
            raise KalshiPayloadError("market_type must be a string when present")
        if market_type not in (None, "binary", "Binary"):
            return None
        if "outcomes" in row:
            raw_outcomes = row["outcomes"]
            if not isinstance(raw_outcomes, (list, tuple)) or not all(
                isinstance(value, str) for value in raw_outcomes
            ):
                raise KalshiPayloadError("market outcomes must be an array of strings")
            if set(raw_outcomes) != {"YES", "NO"}:
                return None
        raw_result = row.get("result")
        if raw_result not in (None, "", "yes", "no"):
            raise KalshiPayloadError("only binary YES/NO results are admitted")
        market_ref = _first_string(row, ("ticker", "market_ticker"), label="market ticker")
        _first_string(row, ("event_ticker", "event_ref"), label="event ticker")
        _first_string(
            row,
            ("series_ticker", "series_ref"),
            label="series ticker",
            required=False,
        )
        status = _status(row.get("status"))
        source_updated = _optional_timestamp(
            row, ("updated_time", "updated_ts", "updated_at"), "updated_time"
        )
        _check_cutoff(source_updated, cutoff, "updated_time")
        if source_updated is not None and source_updated > observed_at:
            raise KalshiLookAheadError("updated_time is newer than local observation")
        volume_raw = row.get("volume_fp", row.get("volume", 0))
        liquidity_raw = row.get("liquidity_dollars", row.get("liquidity", 0))
        try:
            volume = int(to_contract_quantity(volume_raw, field="volume_fp"))
        except ValueError as exc:
            raise KalshiPayloadError(str(exc)) from exc
        try:
            liquidity_micros = int(to_money_micros(liquidity_raw, field="liquidity_dollars"))
        except ValueError as exc:
            raise KalshiPayloadError(str(exc)) from exc
        return _CatalogueCandidate(
            MarketKey(market_ref),
            row,
            page_index,
            row_index,
            volume,
            liquidity_micros,
            status is MarketStatus.ACTIVE,
        )

    def _materialize_catalogue_pages(
        self,
        evidence: Sequence[_CataloguePageEvidence],
        candidates: Mapping[MarketKey, _CatalogueCandidate],
        *,
        cutoff: datetime | None,
        historical_cutoff: HistoricalCutoff,
        deadline: _Deadline,
    ) -> tuple[CataloguePage, ...]:
        markets_by_page: dict[int, list[BinaryMarket]] = {
            index: [] for index in range(len(evidence))
        }
        events_by_page: dict[int, dict[EventKey, BinaryEvent]] = {
            index: {} for index in range(len(evidence))
        }
        series_by_page: dict[int, dict[SeriesKey, Series]] = {
            index: {} for index in range(len(evidence))
        }
        metadata_audits_by_page: dict[int, list[RawArtifact]] = {
            index: [] for index in range(len(evidence))
        }
        for candidate in sorted(
            candidates.values(), key=lambda value: (value.page_index, value.row_index)
        ):
            deadline.check()
            market, event, series, metadata_audits = self._materialize_candidate(
                candidate,
                evidence[candidate.page_index],
                cutoff,
                historical_cutoff,
                deadline,
            )
            self._remember_market(market)
            markets_by_page[candidate.page_index].append(market)
            events_by_page[candidate.page_index][event.key] = event
            series_by_page[candidate.page_index][series.key] = series
            metadata_audits_by_page[candidate.page_index].extend(metadata_audits)
        return tuple(
            CataloguePage(
                page.requested_cursor,
                page.next_cursor,
                page.observed_at,
                tuple(series_by_page[index].values()),
                tuple(events_by_page[index].values()),
                tuple(markets_by_page[index]),
                page.artifact,
                tuple(dict.fromkeys(metadata_audits_by_page[index])),
                page.record_count,
            )
            for index, page in enumerate(evidence)
        )

    def _materialize_candidate(
        self,
        candidate: _CatalogueCandidate,
        page: _CataloguePageEvidence,
        cutoff: datetime | None,
        historical_cutoff: HistoricalCutoff,
        deadline: _Deadline,
    ) -> tuple[BinaryMarket, BinaryEvent, Series, tuple[RawArtifact, ...]]:
        payload = candidate.payload
        archived = page.archived()
        event_metadata: Mapping[str, Any] | None = None
        event_archived: _ArchivedResponse | None = None
        series_ref = _first_string(
            payload, ("series_ticker", "series_ref"), label="series ticker", required=False
        )
        if series_ref is None:
            event_ref = _first_string(payload, ("event_ticker", "event_ref"), label="event ticker")
            assert event_ref is not None
            deadline.check()
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
            deadline.check()
            series_metadata, series_archived = self._fetch_series_metadata(
                series_ref, deadline=deadline
            )
        market = self._normalize_market(
            payload,
            archived,
            cutoff,
            historical_cutoff,
            series_ref=series_ref,
            resolution_source=(
                _resolution_source(event_metadata) if event_metadata is not None else None
            ),
        )
        event = self._event_from_market(
            payload,
            archived,
            market,
            metadata=event_metadata,
            metadata_archived=event_archived,
        )
        series = self._series_from_market(
            payload,
            archived,
            market,
            metadata=series_metadata,
            metadata_archived=series_archived,
        )
        metadata_audits = tuple(
            artifact
            for artifact in (
                event_archived.artifact if event_archived is not None else None,
                series_archived.artifact if series_archived is not None else None,
            )
            if artifact is not None
        )
        return market, event, series, metadata_audits

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
        event_metadata: Mapping[str, Any] | None = None
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
            resolution_source=(
                _resolution_source(event_metadata) if event_metadata is not None else None
            ),
        )
        if market.key != market_key:
            raise KalshiPayloadError("market response ticker does not match the requested key")
        self._remember_market(market)
        return market

    def get_context(
        self,
        market_key: MarketKey,
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> MarketContext:
        requested_cutoff = _aware(cutoff, "context cutoff")
        operation_deadline = self._deadline(deadline)
        historical_cutoff = self._last_historical_cutoff or self.get_historical_cutoff(
            deadline=operation_deadline
        )
        market = self._fetch_market(
            market_key,
            cutoff=requested_cutoff,
            historical_cutoff=historical_cutoff,
            deadline=operation_deadline,
        )
        operation_deadline.check()
        path = f"/markets/{market.key.market_ref}/orderbook"
        archived = self._request(path, {"depth": "0"}, deadline=operation_deadline)
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
        self,
        market_keys: Sequence[MarketKey],
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> tuple[MarketContext, ...]:
        unique = tuple(dict.fromkeys(market_keys))
        if len(unique) != len(market_keys):
            raise ValueError("context request contains duplicate market identities")
        if not unique:
            return ()
        operation_deadline = self._deadline(deadline)
        return _parallel_map(
            unique,
            lambda key: self.get_context(
                key,
                cutoff=cutoff,
                deadline=operation_deadline.monotonic_deadline,
            ),
            maximum_workers=min(self._maximum_parallel_requests, len(unique)),
            deadline=operation_deadline,
        )

    def get_resolutions(
        self,
        market_keys: Sequence[MarketKey],
        *,
        cutoff: datetime,
        deadline: float | None = None,
    ) -> tuple[ResolutionObservation, ...]:
        requested_cutoff = _aware(cutoff, "resolution cutoff")
        unique = tuple(dict.fromkeys(market_keys))
        if not unique:
            return ()
        operation_deadline = self._deadline(deadline)
        historical_cutoff = self._last_historical_cutoff or self.get_historical_cutoff(
            deadline=operation_deadline
        )

        def read(key: MarketKey) -> ResolutionObservation:
            market = self._market_cache.get(key)
            if market is None:
                market = self._fetch_market(
                    key,
                    cutoff=requested_cutoff,
                    historical_cutoff=historical_cutoff,
                    deadline=operation_deadline,
                )
            path = self._market_path(market, historical_cutoff)
            archived = self._request(path, deadline=operation_deadline)
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

        observations = _parallel_map(
            unique,
            read,
            maximum_workers=min(self._maximum_parallel_requests, len(unique)),
            deadline=operation_deadline,
        )
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
