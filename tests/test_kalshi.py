from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import httpx

from vtrade.artifacts import ContentAddressedArtifactStore
from vtrade.domain.types import (
    CatalogueScanRequest,
    MarketKey,
    OutcomeSide,
    PriceGrid,
    to_contract_quantity,
    to_price_micros,
)
from vtrade.kalshi import (
    ORDINARY_BINARY_FORBIDDEN_FIELDS,
    KalshiLookAheadError,
    KalshiPayloadError,
    KalshiPublicRestAdapter,
)
from vtrade.kalshi_freeze import KalshiFreezeRequest, KalshiMarketFreezeService

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def market_payload(
    ticker: str = "KXTEST-1",
    *,
    status: str = "active",
    latest_expiration_time: str = "2026-08-30T00:00:00Z",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "event_ticker": "KXTEST",
        "series_ticker": "KX",
        "title": f"Question {ticker}",
        "rules_primary": "Resolve from the official source.",
        "settlement_sources": ["https://source.example.test"],
        "status": status,
        "open_time": "2026-08-01T00:00:00Z",
        "close_time": "2026-08-30T00:00:00Z",
        "expected_expiration_time": "2026-08-30T00:00:00Z",
        "latest_expiration_time": latest_expiration_time,
        "updated_time": "2026-08-21T09:00:00Z",
        "price_ranges": [
            {"start": "0.00", "end": "0.50", "step": "0.01"},
            {"start": "0.50", "end": "1.00", "step": "0.005"},
        ],
        "yes_sub_title": "YES",
        "no_sub_title": "NO",
        "volume_fp": "12.34",
        "liquidity_dollars": "12.50",
        "result": None,
    }


class Replay:
    def __init__(self, *, repeated_cursor: bool = False, retry_markets: bool = False) -> None:
        self.repeated_cursor = repeated_cursor
        self.retry_markets = retry_markets
        self.market_attempts = 0
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        self.calls.append(str(request.url))
        if path == "/historical/cutoff":
            return self.response(request, {"market_settled_ts": "2026-08-01T00:00:00Z"})
        if path == "/markets" and request.url.params.get("cursor") is None:
            self.market_attempts += 1
            if self.retry_markets and self.market_attempts == 1:
                return httpx.Response(503, request=request)
            return self.response(
                request,
                {
                    "markets": [market_payload()],
                    "cursor": "same" if self.repeated_cursor else "next",
                },
            )
        if path == "/markets" and request.url.params.get("cursor") == "next":
            return self.response(request, {"markets": [market_payload("KXTEST-2")], "cursor": None})
        if path == "/markets" and request.url.params.get("cursor") == "same":
            return self.response(request, {"markets": [market_payload()], "cursor": "same"})
        if path == "/markets/KXTEST-1/orderbook":
            return self.response(
                request,
                {
                    "orderbook_fp": {
                        "yes_dollars": [["0.4200", "1.55"]],
                        "no_dollars": [["0.5600", "2.00"]],
                    }
                },
            )
        if path == "/markets/KXTEST-2/orderbook":
            return self.response(
                request,
                {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
            )
        if path == "/markets/KXTEST-1":
            return self.response(request, market_payload())
        if path == "/markets/KXTEST-2":
            return self.response(request, market_payload("KXTEST-2"))
        return httpx.Response(404, request=request)

    @staticmethod
    def response(request: httpx.Request, payload: object) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload, separators=(",", ":")).encode(),
            request=request,
        )


class LargeCatalogueReplay:
    total_rows = 95_366
    page_size = 1_000

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        self.calls.append(str(request.url))
        if path == "/historical/cutoff":
            return Replay.response(request, {"market_settled_ts": "2026-08-01T00:00:00Z"})
        if path == "/events/KXLARGE":
            return Replay.response(
                request,
                {
                    "event": {
                        "event_ticker": "KXLARGE",
                        "series_ticker": "KXLARGE-SERIES",
                        "title": "Large event",
                    }
                },
            )
        if path == "/series/KXLARGE-SERIES":
            return Replay.response(
                request,
                {
                    "series": {
                        "ticker": "KXLARGE-SERIES",
                        "title": "Large series",
                        "rules_primary": "Large series rules",
                    }
                },
            )
        if path != "/markets":
            return httpx.Response(404, request=request)
        cursor = request.url.params.get("cursor")
        page_index = 0 if cursor is None else int(cursor.removeprefix("page-"))
        start = page_index * self.page_size
        count = min(self.page_size, self.total_rows - start)
        rows: list[dict[str, object]] = []
        for offset in range(count):
            index = start + offset
            row = market_payload(f"KXLARGE-{index}")
            row.pop("series_ticker")
            row["event_ticker"] = "KXLARGE"
            row["volume_fp"] = f"{index}.00"
            row["liquidity_dollars"] = f"{index}.00"
            rows.append(row)
        next_cursor = None if start + count >= self.total_rows else f"page-{page_index + 1}"
        return Replay.response(request, {"markets": rows, "cursor": next_cursor})


class KalshiDomainTests(unittest.TestCase):
    def test_exact_values_and_deterministic_opaque_keys(self) -> None:
        self.assertEqual(int(to_price_micros("0.4200")), 420_000)
        self.assertEqual(int(to_contract_quantity("1.55")), 155)
        self.assertEqual(MarketKey("A").stable_id, MarketKey("A").stable_id)
        self.assertNotEqual(MarketKey("A").stable_id, MarketKey("B").stable_id)
        with self.assertRaises(ValueError):
            to_contract_quantity("1.005")
        with self.assertRaises(ValueError):
            to_price_micros("0.0000001")

    def test_grid_accepts_tapered_ticks_and_rejects_off_grid_values(self) -> None:
        grid = PriceGrid.from_ranges(market_payload()["price_ranges"])
        self.assertTrue(grid.contains(500_000))
        self.assertTrue(grid.contains(560_000))
        self.assertFalse(grid.contains(561_000))
        with self.assertRaises(ValueError):
            grid.require(561_000)

    def test_catalogue_follows_opaque_cursors_and_builds_reciprocal_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay()
            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(replay)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            snapshot = adapter.sync_catalogue(cutoff=NOW)
            self.assertEqual(len(snapshot.pages), 2)
            context = adapter.get_context(MarketKey("KXTEST-1"), cutoff=NOW)
            self.assertEqual(context.order_book.best_bid(OutcomeSide.YES).price, 420_000)
            self.assertEqual(context.order_book.best_ask(OutcomeSide.YES).price, 440_000)
            self.assertEqual(context.order_book.best_ask(OutcomeSide.YES).quantity, 200)
            self.assertEqual(context.order_book.best_ask(OutcomeSide.NO).price, 580_000)
            self.assertEqual(
                context.order_book.artifact.source_endpoint,
                "GET /trade-api/v2/markets/KXTEST-1/orderbook",
            )
            self.assertEqual(context.market.volume, 1234)
            self.assertEqual(context.market.liquidity_micros, 12_500_000)

    def test_repeated_cursor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay(repeated_cursor=True)
            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(replay)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            with self.assertRaises(KalshiPayloadError):
                adapter.sync_catalogue(cutoff=NOW)

    def test_catalogue_resolves_series_through_event_metadata_when_market_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay()
            original = replay.__call__

            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path.removeprefix("/trade-api/v2")
                if path == "/markets" and request.url.params.get("cursor") is None:
                    response = original(request)
                    payload = json.loads(response.content)
                    payload["markets"][0].pop("series_ticker")
                    return Replay.response(request, payload)
                if path == "/events/KXTEST":
                    return Replay.response(
                        request,
                        {
                            "event": {
                                "event_ticker": "KXTEST",
                                "series_ticker": "KX",
                                "title": "Event title",
                            }
                        },
                    )
                if path == "/series/KX":
                    return Replay.response(
                        request,
                        {
                            "series": {
                                "ticker": "KX",
                                "title": "Series title",
                                "rules_primary": "Series rules",
                            }
                        },
                    )
                return original(request)

            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(handler)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            snapshot = adapter.sync_catalogue(cutoff=NOW)
            self.assertEqual(snapshot.pages[0].events[0].title, "Event title")
            self.assertEqual(snapshot.pages[0].series[0].title, "Series title")
            self.assertEqual(len(snapshot.pages[0].metadata_audits), 2)

    def test_retry_policy_retries_only_transient_http_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay(retry_markets=True)
            delays: list[float] = []
            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(replay)),
                clock=lambda: NOW,
                sleep=delays.append,
            )
            adapter.sync_catalogue(cutoff=NOW)
            self.assertEqual(replay.market_attempts, 2)
            self.assertEqual(delays, [0.25])

    def test_token_shaped_and_lookahead_payloads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay()
            original = replay.__call__

            def token_handler(request: httpx.Request) -> httpx.Response:
                response = original(request)
                path = request.url.path.removeprefix("/trade-api/v2")
                if path == "/markets" and request.url.params.get("cursor") is None:
                    payload = json.loads(response.content)
                    forbidden_field = next(iter(ORDINARY_BINARY_FORBIDDEN_FIELDS))
                    payload["markets"][0][forbidden_field] = "forbidden"
                    return Replay.response(request, payload)
                return response

            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(token_handler)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            with self.assertRaises(KalshiPayloadError):
                adapter.sync_catalogue(cutoff=NOW)

            late = market_payload()
            late["updated_time"] = "2026-08-21T11:00:00Z"
            replay_late = Replay()

            def late_handler(request: httpx.Request) -> httpx.Response:
                response = replay_late(request)
                path = request.url.path.removeprefix("/trade-api/v2")
                if path == "/markets" and request.url.params.get("cursor") is None:
                    payload = json.loads(response.content)
                    payload["markets"][0] = late
                    return Replay.response(request, payload)
                return response

            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory) / "late"),
                client=httpx.Client(transport=httpx.MockTransport(late_handler)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            with self.assertRaises(KalshiLookAheadError):
                adapter.sync_catalogue(cutoff=NOW)

    def test_only_complete_finalized_binary_resolution_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay()
            original = replay.__call__

            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path.removeprefix("/trade-api/v2")
                if path == "/markets/KXTEST-1":
                    payload = market_payload(status="finalized")
                    payload["result"] = "yes"
                    payload["settlement_ts"] = "2026-08-20T00:00:00Z"
                    return Replay.response(request, payload)
                return original(request)

            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(handler)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            adapter.sync_catalogue(cutoff=NOW)
            resolution = adapter.get_resolutions((MarketKey("KXTEST-1"),), cutoff=NOW)[0]
            self.assertTrue(resolution.terminal)
            self.assertEqual(resolution.result, OutcomeSide.YES)

            def incomplete_handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path.removeprefix("/trade-api/v2")
                if path == "/markets/KXTEST-1":
                    return Replay.response(request, market_payload(status="finalized"))
                return original(request)

            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory) / "incomplete"),
                client=httpx.Client(transport=httpx.MockTransport(incomplete_handler)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            adapter.sync_catalogue(cutoff=NOW)
            incomplete = adapter.get_resolutions((MarketKey("KXTEST-1"),), cutoff=NOW)[0]
            self.assertTrue(incomplete.blocked)
            self.assertFalse(incomplete.terminal)

    def test_freeze_bounds_discovery_but_keeps_resolution_universe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = Replay()
            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(replay)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            service = KalshiMarketFreezeService(adapter, clock=lambda: NOW)
            freeze = service.freeze(
                KalshiFreezeRequest(
                    historical_markets=(MarketKey("KXTEST-1"), MarketKey("KXTEST-2")),
                    cutoff=NOW,
                    maximum_historical_markets=1,
                    maximum_additional_markets=0,
                )
            )
            self.assertEqual(freeze.discovery_market_keys, (MarketKey("KXTEST-1"),))
            self.assertEqual(
                freeze.resolution_market_keys,
                (MarketKey("KXTEST-1"), MarketKey("KXTEST-2")),
            )
            self.assertEqual(
                tuple(context.market.key for context in freeze.contexts),
                freeze.discovery_market_keys,
            )
            self.assertTrue(freeze.artifacts)

    def test_large_catalogue_scan_keeps_only_global_top_k_and_selected_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            replay = LargeCatalogueReplay()
            adapter = KalshiPublicRestAdapter(
                ContentAddressedArtifactStore(Path(directory)),
                client=httpx.Client(transport=httpx.MockTransport(replay)),
                clock=lambda: NOW,
                sleep=lambda _delay: None,
            )
            result = adapter.scan_catalogue(
                CatalogueScanRequest(
                    cutoff=NOW,
                    maximum_historical_markets=0,
                    maximum_additional_markets=2,
                )
            )

            self.assertEqual(len(result.pages), 96)
            self.assertEqual(result.scanned_market_count, 95_366)
            self.assertEqual(sum(page.record_count for page in result.pages), 95_366)
            self.assertEqual(len(result.markets), 2)
            self.assertEqual(
                result.discovery_market_keys,
                (MarketKey("KXLARGE-95365"), MarketKey("KXLARGE-95364")),
            )
            self.assertEqual(sum(len(page.markets) for page in result.pages), 2)
            self.assertEqual(
                len([call for call in replay.calls if "/events/" in call]),
                1,
            )
            self.assertEqual(
                len([call for call in replay.calls if "/series/" in call]),
                1,
            )
            self.assertFalse(
                any("KXLARGE-0" in call for call in replay.calls if "/events/" in call)
            )


if __name__ == "__main__":
    unittest.main()
