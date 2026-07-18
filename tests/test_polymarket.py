from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from vtrade.artifacts import ContentAddressedArtifactStore
from vtrade.domain.types import MarketStatus
from vtrade.polymarket import (
    LookAheadError,
    MarketDiscoveryCache,
    MarketDiscoveryTools,
    PolymarketVenue,
    RequestRateLimiter,
    RetryPolicy,
)

FIXTURES = Path("spec/fixtures/polymarket")


class PolymarketReplay:
    def __init__(self) -> None:
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        self.captured_at = datetime.fromisoformat(manifest["captured_at"]) + timedelta(
            minutes=5
        )
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.path == "/events/keyset":
            fixture = "events-keyset-limit-1.json"
        elif request.url.path == "/markets/keyset" and request.url.params.get("closed") == "true":
            fixture = "resolved-markets-keyset-limit-1.json"
        elif request.url.path == "/markets/keyset":
            fixture = "markets-keyset-limit-1.json"
        elif request.url.path == "/book":
            fixture = "clob-book.json"
        elif request.url.path.startswith("/fee-rate/"):
            return httpx.Response(200, json={"base_fee": 30}, request=request)
        else:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=(FIXTURES / fixture).read_bytes(), request=request)


class PolymarketContractReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.replay = PolymarketReplay()
        client = httpx.Client(transport=httpx.MockTransport(self.replay))
        self.venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name)),
            client=client,
            clock=lambda: self.replay.captured_at,
            rate_limiter=RequestRateLimiter({}),
            sleep=lambda _: None,
        )

    def test_gamma_and_clob_contracts_normalize_and_archive_raw_bytes(self) -> None:
        events = self.venue.sync_events(limit=1)
        markets = self.venue.sync_markets(limit=1)
        market = markets.markets[0]
        book = self.venue.get_order_book([market.outcomes[0].venue_token_id])[0]

        self.assertEqual(len(events.events), 1)
        self.assertGreaterEqual(len(events.markets), 1)
        self.assertEqual(len(markets.events), 1)
        self.assertEqual(len(market.outcomes), 2)
        self.assertTrue(market.tradeable)
        self.assertIsNone(market.outcomes[0].best_bid_micros)
        self.assertGreater(len(book.bids), 0)
        self.assertGreater(len(book.asks), 0)
        self.assertEqual(book.token_id, market.outcomes[0].venue_token_id)
        self.assertLessEqual(book.source_created_at, book.observed_at)
        raw = (FIXTURES / "markets-keyset-limit-1.json").read_bytes()
        self.assertEqual(markets.artifact.sha256, hashlib.sha256(raw).hexdigest())
        self.assertNotIn("$schema", market.venue_metadata)

    def test_official_fee_rate_is_archived_before_normalization(self) -> None:
        token_id = "123456789"
        rate = self.venue.get_fee_rates([token_id])[0]
        self.assertEqual(rate.token_id, token_id)
        self.assertEqual(rate.base_fee_bps, 30)
        self.assertEqual(rate.observed_at, self.replay.captured_at)
        self.assertIsNone(rate.source_created_at)
        self.assertTrue(rate.artifact.uri.endswith(".json.gz"))

    def test_resolution_requires_resolved_singular_status_and_exact_final_prices(self) -> None:
        payload = json.loads(
            (FIXTURES / "resolved-markets-keyset-limit-1.json").read_text(encoding="utf-8")
        )
        venue_id = payload["markets"][0]["id"]
        resolutions = self.venue.sync_resolutions([venue_id])

        self.assertEqual(len(resolutions), 1)
        resolution = resolutions[0]
        self.assertEqual(resolution.market_id, f"polymarket:market:{venue_id}")
        self.assertLessEqual(resolution.source_created_at, resolution.observed_at)
        self.assertEqual(
            self.venue.get_resolutions([venue_id], resolution.observed_at), resolutions
        )
        self.assertEqual(
            self.venue.get_resolutions(
                [venue_id], resolution.observed_at - timedelta(microseconds=1)
            ),
            (),
        )

    def test_resolved_fifty_fifty_payload_has_no_singular_winner(self) -> None:
        payload = json.loads(
            (FIXTURES / "resolved-markets-keyset-limit-1.json").read_text(
                encoding="utf-8"
            )
        )
        market_payload = payload["markets"][0]
        market_payload["outcomePrices"] = json.dumps(["0.5", "0.5"])
        raw = json.dumps(payload).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=raw, request=request)

        venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name) / "fifty-fifty"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=RequestRateLimiter({}),
            clock=lambda: self.replay.captured_at,
        )

        resolutions = venue.sync_resolutions([market_payload["id"]])

        self.assertEqual(len(resolutions), 1)
        self.assertIsNone(resolutions[0].winning_outcome_id)
        self.assertEqual(resolutions[0].result, "50/50")

    def test_versioned_discovery_cache_never_serves_future_delta(self) -> None:
        delta = self.venue.sync_markets(limit=1)
        cache = MarketDiscoveryCache()
        cache.ingest(delta)

        self.assertEqual(
            cache.markets_as_of(delta.observed_at - timedelta(microseconds=1)), ()
        )
        self.assertEqual(cache.markets_as_of(delta.observed_at), delta.markets)
        with self.assertRaises(ValueError):
            cache.markets_as_of(delta.observed_at.replace(tzinfo=None))

    def test_orderbook_tool_reads_frozen_cache_without_network(self) -> None:
        delta = self.venue.sync_markets(limit=1)
        token_id = delta.markets[0].outcomes[0].venue_token_id
        books = self.venue.get_order_book([token_id])
        cache = MarketDiscoveryCache()
        cache.ingest(delta)
        cache.ingest_order_books(books)
        calls_after_freeze = len(self.replay.calls)

        tools = MarketDiscoveryTools(cache, as_of=books[0].observed_at)
        result = tools.get_orderbook([token_id])

        self.assertEqual(result["books"][0]["token_id"], token_id)
        self.assertEqual(len(self.replay.calls), calls_after_freeze)

    def test_exact_prices_do_not_resolve_proposed_or_disputed_market(self) -> None:
        payload = json.loads(
            (FIXTURES / "resolved-markets-keyset-limit-1.json").read_text(encoding="utf-8")
        )
        payload["markets"][0]["umaResolutionStatus"] = "disputed"
        raw = json.dumps(payload).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=raw, request=request)

        venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name) / "disputed"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=RequestRateLimiter({}),
            clock=lambda: self.replay.captured_at,
        )
        market = venue.sync_markets(limit=1).markets[0]
        self.assertEqual(market.status, MarketStatus.CLOSED)

    def test_bounded_clob_clock_skew_advances_effective_cutoff(self) -> None:
        payload = json.loads((FIXTURES / "clob-book.json").read_text(encoding="utf-8"))
        source_milliseconds = int(
            (self.replay.captured_at + timedelta(milliseconds=149)).timestamp() * 1000
        )
        source_time = datetime.fromtimestamp(source_milliseconds / 1000, tz=UTC)
        payload["timestamp"] = str(source_milliseconds)
        token_id = payload["asset_id"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name) / "bounded-skew"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=RequestRateLimiter({}),
            clock=lambda: self.replay.captured_at,
            maximum_source_clock_skew_seconds=5,
        )
        book = venue.get_order_book([token_id])[0]
        self.assertEqual(book.source_created_at, source_time)
        self.assertEqual(book.observed_at, source_time)

    def test_excessive_clob_clock_skew_is_rejected(self) -> None:
        payload = json.loads((FIXTURES / "clob-book.json").read_text(encoding="utf-8"))
        payload["timestamp"] = str(
            int((self.replay.captured_at + timedelta(seconds=6)).timestamp() * 1000)
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name) / "excessive-skew"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=RequestRateLimiter({}),
            clock=lambda: self.replay.captured_at,
            maximum_source_clock_skew_seconds=5,
        )
        with self.assertRaisesRegex(LookAheadError, "above the 5.000000s clock-skew bound"):
            venue.get_order_book([payload["asset_id"]])

    def test_bounded_gamma_updated_at_advances_page_cutoff(self) -> None:
        payload = json.loads(
            (FIXTURES / "markets-keyset-limit-1.json").read_text(encoding="utf-8")
        )
        source_time = self.replay.captured_at + timedelta(seconds=1)
        payload["markets"][0]["updatedAt"] = source_time.isoformat()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name) / "gamma-skew"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=RequestRateLimiter({}),
            clock=lambda: self.replay.captured_at,
            maximum_source_clock_skew_seconds=5,
        )
        page = venue.sync_markets(limit=1)
        self.assertEqual(page.markets[0].source_updated_at, source_time)
        self.assertEqual(page.markets[0].observed_at, source_time)
        self.assertEqual(page.observed_at, source_time)

    def test_retry_count_is_bounded(self) -> None:
        attempts = 0
        recorded = (FIXTURES / "markets-keyset-limit-1.json").read_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, content=recorded, request=request)

        venue = PolymarketVenue(
            ContentAddressedArtifactStore(Path(self.directory.name) / "retry"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            retry_policy=RetryPolicy(maximum_attempts=2, initial_backoff_seconds=0),
            rate_limiter=RequestRateLimiter({}),
            clock=lambda: self.replay.captured_at,
            sleep=lambda _: None,
        )
        self.assertEqual(len(venue.sync_markets(limit=1).markets), 1)
        self.assertEqual(attempts, 2)

    def test_naive_as_of_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.venue.get_resolutions([], self.replay.captured_at.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
