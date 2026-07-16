from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vtrade.artifacts import ContentAddressedArtifactStore


class ArtifactTests(unittest.TestCase):
    def test_content_addressing_is_deterministic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ContentAddressedArtifactStore(Path(directory))
            first = store.put(b'{"cycles": []}')
            second = store.put(b'{"cycles": []}')
            self.assertEqual(first, second)
            self.assertEqual(store.get(first), b'{"cycles": []}')
            self.assertEqual(len(list(Path(directory).rglob("*.gz"))), 1)


if __name__ == "__main__":
    unittest.main()

