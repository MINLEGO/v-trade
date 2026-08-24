from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.probe_kalshi_public_rest import ProbeError, _series_ref_from_event, run


class ProbeReplay:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/historical/cutoff":
            return self.response(request, {"market_settled_ts": "2026-08-01T00:00:00Z"})
        if path == "/markets":
            return self.response(
                request,
                {
                    "markets": [
                        {
                            "ticker": "KXTEST-1",
                            "event_ticker": "KXTEST",
                            "market_type": "binary",
                            "result": None,
                        }
                    ],
                    "cursor": None,
                },
            )
        if path == "/events/KXTEST":
            return self.response(
                request,
                {"event": {"event_ticker": "KXTEST", "series_ticker": "KX"}},
            )
        if path == "/series/KX":
            return self.response(request, {"series": {"ticker": "KX"}})
        if path == "/markets/KXTEST-1/orderbook":
            return self.response(
                request,
                {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
            )
        return httpx.Response(404, request=request)

    @staticmethod
    def response(request: httpx.Request, payload: object) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload, separators=(",", ":")).encode(),
            request=request,
        )


class KalshiPublicRestProbeTests(unittest.TestCase):
    def test_series_reference_is_resolved_from_event_metadata(self) -> None:
        market = {
            "ticker": "KXTEST-1",
            "event_ticker": "KXTEST",
            "market_type": "binary",
        }
        event_payload = {
            "event": {
                "event_ticker": "KXTEST",
                "series_ticker": "KX",
            }
        }

        self.assertNotIn("series_ticker", market)
        self.assertEqual(_series_ref_from_event(event_payload), "KX")

    def test_event_without_series_reference_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProbeError, "opaque series_ticker"):
            _series_ref_from_event({"event": {"event_ticker": "KXTEST"}})

    def test_run_resolves_series_before_orderbook_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            replay = ProbeReplay()
            original_client = httpx.Client

            def make_client(**_kwargs: object) -> httpx.Client:
                return original_client(transport=httpx.MockTransport(replay))

            args = argparse.Namespace(
                output=output,
                root="https://external-api.kalshi.com/trade-api/v2",
                market_ref=None,
                timeout_seconds=15.0,
                deadline_seconds=300.0,
            )
            with patch(
                "scripts.probe_kalshi_public_rest.httpx.Client",
                side_effect=make_client,
            ):
                manifest_path = run(args)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["selected_event_ref"], "KXTEST")
            self.assertEqual(manifest["selected_series_ref"], "KX")
            self.assertEqual(
                {capture["status_code"] for capture in manifest["responses"]},
                {200},
            )
