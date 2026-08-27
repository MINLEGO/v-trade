"""Capture credential-free Kalshi public REST evidence for the French-host gate.

The probe intentionally uses only GET requests.  It does not accept or read
Kalshi credentials, configure a proxy/VPN, call WebSockets, call the
authenticated bulk-book endpoint, or submit orders.  The response files are
written byte-for-byte; the JSON manifest is separate audit metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

import httpx

ROOT = "https://external-api.kalshi.com/trade-api/v2"
RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})
SAFE_HEADERS = frozenset(
    {"cache-control", "content-length", "content-type", "date", "etag", "last-modified"}
)


class ProbeError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() in SAFE_HEADERS}


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    label: str
    path: str
    request_identity: str
    status_code: int
    observed_at: str
    elapsed_ms: float
    artifact_path: str
    sha256: str
    byte_length: int
    headers: dict[str, str]
    retries: int


class Probe:
    def __init__(
        self,
        root: str,
        output: Path,
        *,
        timeout_seconds: float,
        deadline_seconds: float,
        maximum_attempts: int = 3,
    ) -> None:
        if not root.startswith("https://"):
            raise ValueError("probe root must use HTTPS")
        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        self.root = root.rstrip("/")
        self.output = output
        self.responses = output / "responses"
        self.responses.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            trust_env=False,
        )
        self.deadline = time.monotonic() + deadline_seconds
        self.maximum_attempts = maximum_attempts
        self.captures: list[CapturedResponse] = []
        self._capture_lock = Lock()

    def close(self) -> None:
        self.client.close()

    def get(
        self,
        label: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], CapturedResponse]:
        endpoint = self.root + path
        retries = 0
        for attempt in range(1, self.maximum_attempts + 1):
            if time.monotonic() >= self.deadline:
                raise ProbeError("probe deadline exceeded")
            started = time.perf_counter()
            try:
                response = self.client.get(endpoint, params=params)
            except httpx.HTTPError as exc:
                if attempt == self.maximum_attempts:
                    raise ProbeError(f"request failed for {path}") from exc
                time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                retries += 1
                continue
            if response.status_code in RETRYABLE:
                if attempt == self.maximum_attempts:
                    raise ProbeError(f"HTTP {response.status_code} for {path}")
                time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                retries += 1
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise ProbeError(f"HTTP {response.status_code} for {path}")
            content = bytes(response.content)
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProbeError(f"{path} did not return UTF-8 JSON") from exc
            if not isinstance(payload, dict):
                raise ProbeError(f"{path} did not return a JSON object")
            digest = _digest(content)
            query = response.request.url.query.decode("ascii") if response.request.url.query else ""
            request_identity = f"GET {path}" + (f"?{query}" if query else "")
            with self._capture_lock:
                artifact_name = f"{len(self.captures):04d}-{label}.json"
                artifact_path = self.responses / artifact_name
                artifact_path.write_bytes(content)
                captured = CapturedResponse(
                    label,
                    path,
                    request_identity,
                    response.status_code,
                    _now().isoformat(),
                    (time.perf_counter() - started) * 1000,
                    str(artifact_path.relative_to(self.output).as_posix()),
                    digest,
                    len(content),
                    _safe_headers(response.headers),
                    retries,
                )
                self.captures.append(captured)
            return payload, captured
        raise AssertionError("probe retry loop must return or raise")


def _next_cursor(payload: dict[str, Any]) -> str | None:
    values = [payload[name] for name in ("cursor", "next_cursor") if name in payload]
    if (
        len(values) > 1
        and values[0] not in (None, "")
        and values[1] not in (None, "")
        and values[0] != values[1]
    ):
        raise ProbeError("catalogue response has conflicting cursor values")
    value = next((item for item in values if item not in (None, "")), None)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProbeError("catalogue cursor is not an opaque string")
    return value


def _ordinary_market(rows: list[Any]) -> dict[str, Any]:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if any(
            row.get(name) not in (None, False, "", [], {})
            for name in ("mve_collection_ticker", "mve_selected_legs")
        ):
            continue
        if row.get("result") == "scalar" or row.get("market_type") not in (
            None,
            "binary",
            "Binary",
        ):
            continue
        if isinstance(row.get("ticker"), str) and row["ticker"]:
            return row
    raise ProbeError("complete catalogue contains no ordinary binary market")


def _series_ref_from_event(payload: dict[str, Any]) -> str:
    event = payload.get("event")
    if not isinstance(event, dict):
        raise ProbeError("event response lacks an event object")
    series_ref = event.get("series_ticker")
    if not isinstance(series_ref, str) or not series_ref:
        raise ProbeError("event lacks opaque series_ticker")
    return series_ref


def run(args: argparse.Namespace) -> Path:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    probe = Probe(
        args.root,
        output,
        timeout_seconds=args.timeout_seconds,
        deadline_seconds=args.deadline_seconds,
    )
    try:
        cutoff, _ = probe.get("historical-cutoff", "/historical/cutoff")
        pages: list[dict[str, Any]] = []
        rows: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"status": "open", "mve_filter": "exclude", "limit": "1000"}
            if cursor is not None:
                params["cursor"] = cursor
            page, _ = probe.get(
                f"markets-page-{len(pages):04d}",
                "/markets",
                params,
            )
            page_rows = page.get("markets")
            if not isinstance(page_rows, list):
                raise ProbeError("markets response lacks a markets array")
            pages.append(page)
            rows.extend(page_rows)
            next_cursor = _next_cursor(page)
            if next_cursor is None:
                break
            if next_cursor in seen_cursors or next_cursor == cursor:
                raise ProbeError("catalogue cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        market = _ordinary_market(rows)
        market_ref = args.market_ref or market["ticker"]
        event_ref = market.get("event_ticker")
        if not isinstance(event_ref, str) or not event_ref:
            raise ProbeError("selected market lacks opaque event_ticker")
        event_payload, _ = probe.get(
            "event-metadata", f"/events/{quote(event_ref, safe='')}"
        )
        series_ref = _series_ref_from_event(event_payload)
        probe.get("series-metadata", f"/series/{quote(series_ref, safe='')}")
        book_path = f"/markets/{quote(market_ref, safe='')}/orderbook"
        probe.get("ordinary-binary-orderbook", book_path, {"depth": "0"})
        sequential = [
            probe.get(
                f"orderbook-sequential-{index:02d}",
                book_path,
                {"depth": "0"},
            )[1]
            for index in range(10)
        ]
        concurrency: dict[str, dict[str, object]] = {}
        for level in (1, 2, 4, 8):
            with ThreadPoolExecutor(max_workers=level) as executor:
                results = tuple(
                    executor.map(
                        lambda index, worker_level=level: probe.get(
                            f"orderbook-concurrency-{worker_level}-{index:02d}",
                            book_path,
                            {"depth": "0"},
                        )[1],
                        range(level),
                    )
                )
        concurrency[str(level)] = {
            "request_count": len(results),
            "status_codes": [capture.status_code for capture in results],
            "retry_count": sum(capture.retries for capture in results),
            "maximum_workers": level,
        }
        candle_end_ts = int(time.time())
        probe.get(
            "market-candlesticks",
            "/markets/candlesticks",
            {
                "market_tickers": market_ref,
                "start_ts": str(candle_end_ts - 48 * 60 * 60),
                "end_ts": str(candle_end_ts),
                "period_interval": "60",
                "include_latest_before_start": "false",
            },
        )
        manifest = {
            "schema_version": "vtrade-kalshi-probe-v1",
            "captured_at": _now().isoformat(),
            "root": args.root,
            "request_policy": {
                "authenticated": False,
                "websocket": False,
                "bulk_orderbook": False,
                "order_submission": False,
                "vpn_or_proxy": False,
                "maximum_attempts": 3,
                "deadline_seconds": args.deadline_seconds,
            },
            "selected_market_ref": market_ref,
            "selected_event_ref": event_ref,
            "selected_series_ref": series_ref,
            "historical_cutoff": cutoff,
            "sequential_orderbooks": {
                "request_count": len(sequential),
                "status_codes": [capture.status_code for capture in sequential],
                "retry_count": sum(capture.retries for capture in sequential),
            },
            "bounded_concurrency": concurrency,
            "responses": [asdict(capture) for capture in probe.captures],
            "sha256": {capture.artifact_path: capture.sha256 for capture in probe.captures},
        }
        manifest_path = output / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return manifest_path
    finally:
        probe.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--market-ref")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--deadline-seconds", type=float, default=300.0)
    args = parser.parse_args()
    print(run(args))


if __name__ == "__main__":
    main()
