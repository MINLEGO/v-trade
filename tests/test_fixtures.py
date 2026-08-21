from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vtrade.fixtures import FixtureValidationError, validate_fixture_manifest


class FixtureTests(unittest.TestCase):
    def test_owner_pending_manifest_is_structurally_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "vtrade-kalshi-fixtures-v1",
                        "venue": "kalshi",
                        "status": "owner_pending",
                        "source_root": "https://external-api.kalshi.com/trade-api/v2",
                        "captures": [],
                    }
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(validate_fixture_manifest(manifest).status, "owner_pending")
            with self.assertRaises(FixtureValidationError):
                validate_fixture_manifest(manifest, require_ready=True)

    def test_ready_manifest_hashes_exact_raw_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = root / "responses" / "markets.json"
            response.parent.mkdir()
            raw = b'{"markets":[]}'
            response.write_bytes(raw)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "vtrade-kalshi-fixtures-v1",
                        "venue": "kalshi",
                        "status": "ready",
                        "source_root": "https://external-api.kalshi.com/trade-api/v2",
                        "captures": [
                            {
                                "name": "markets",
                                "endpoint": "https://external-api.kalshi.com/trade-api/v2/markets",
                                "request_identity": "GET /markets?status=open",
                                "raw_path": "responses/markets.json",
                                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                                "raw_byte_length": len(raw),
                                "response_status": 200,
                                "observed_at": "2026-08-21T10:00:00Z",
                                "source_timestamp": None,
                                "captured_cutoff": "2026-08-21T10:00:00Z",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            result = validate_fixture_manifest(manifest, require_ready=True)
            self.assertTrue(result.ready)
            self.assertEqual(result.captures[0].raw_byte_length, len(raw))


if __name__ == "__main__":
    unittest.main()
