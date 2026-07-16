from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vtrade.artifacts import ArtifactRef, ContentAddressedArtifactStore
from vtrade.fixtures import (
    FixtureValidationError,
    extract_cycles,
    ingest_file,
    should_request_next_page,
)


class FixtureTests(unittest.TestCase):
    def test_count_and_has_more_are_not_pagination_truth(self) -> None:
        payload = {"count": 999, "hasMore": False, "cycles": [{"id": "a"}, {"id": "b"}]}
        self.assertTrue(should_request_next_page(payload, requested_limit=2))
        self.assertFalse(should_request_next_page(payload, requested_limit=3))

    def test_duplicate_stable_ids_are_deduplicated(self) -> None:
        cycles = extract_cycles({"cycles": [{"id": "a"}, {"id": "a"}, {"cycle_id": "b"}]})
        self.assertEqual([item.get("id") or item.get("cycle_id") for item in cycles], ["a", "b"])

    def test_missing_stable_id_is_rejected(self) -> None:
        with self.assertRaises(FixtureValidationError):
            extract_cycles({"cycles": [{}]})

    def test_ingestion_preserves_raw_bytes_and_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cycles.json"
            payload = {
                "count": 1,
                "hasMore": False,
                "cycles": [{"id": "a", "completed_at": "2026-07-13T10:00:00Z"}],
            }
            raw = json.dumps(
                payload,
                separators=(",", ":"),
            ).encode()
            source.write_bytes(raw)
            store = ContentAddressedArtifactStore(root / "artifacts")
            record = ingest_file(
                source,
                "https://example.test/cycles",
                store,
                "2026-07-13T11:00:00Z",
            )
            self.assertEqual(record.source_cutoff, "2026-07-13T10:00:00Z")
            reference = ArtifactRef(record.raw_sha256, len(raw), record.artifact_path)
            self.assertEqual(store.get(reference), raw)


if __name__ == "__main__":
    unittest.main()
