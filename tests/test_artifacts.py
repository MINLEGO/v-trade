from __future__ import annotations

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from vtrade.artifacts import (
    ArtifactStorageConfigurationError,
    ContentAddressedArtifactStore,
    SupabaseArtifactStore,
)


class ArtifactTests(unittest.TestCase):
    def test_content_addressing_is_deterministic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ContentAddressedArtifactStore(Path(directory))
            first = store.put(b'{"cycles": []}')
            second = store.put(b'{"cycles": []}')
            self.assertEqual(first, second)
            self.assertEqual(store.get(first), b'{"cycles": []}')
            self.assertEqual(len(list(Path(directory).rglob("*.gz"))), 1)

    def test_put_file_streams_to_the_same_content_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cycles.json"
            raw = b'{"cycles":[{"id":"a"}]}'
            source.write_bytes(raw)
            store = ContentAddressedArtifactStore(root / "artifacts")

            from_bytes = store.put(raw)
            from_file = store.put_file(source)

            self.assertEqual(from_file, from_bytes)
            self.assertEqual(store.get(from_file), raw)

    def test_supabase_store_requires_private_bucket_and_archives_exact_raw_content(self) -> None:
        uploaded: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"public": False}, request=request)
            uploaded.append(request.content)
            return httpx.Response(200, json={"Key": "ok"}, request=request)

        store = SupabaseArtifactStore(
            "https://project.test",
            "private-artifacts",
            "service-role-placeholder",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        raw = b'{"public":"market-data"}'
        reference = store.put(raw)

        self.assertEqual(reference.byte_length, len(raw))
        self.assertEqual(gzip.decompress(uploaded[0]), raw)
        self.assertTrue(reference.uri.startswith("supabase://private-artifacts/"))

    def test_supabase_store_fails_clearly_when_required_bucket_is_absent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "not found"}, request=request)

        store = SupabaseArtifactStore(
            "https://project.test",
            "missing-private-bucket",
            "service-role-placeholder",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaisesRegex(ArtifactStorageConfigurationError, "was not found"):
            store.put(b"payload")

    def test_supabase_delete_validates_content_address_and_uses_storage_delete(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json={"public": False}, request=request)
            return httpx.Response(200, json={}, request=request)

        store = SupabaseArtifactStore(
            "https://project.test",
            "private-artifacts",
            "service-role-placeholder",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        digest = hashlib.sha256(b"expired").hexdigest()
        uri = f"supabase://private-artifacts/{digest[:2]}/{digest}.json.gz"
        store.delete(uri, digest)
        self.assertEqual(methods, ["GET", "DELETE"])
        with self.assertRaises(ValueError):
            store.delete(f"supabase://other/{digest[:2]}/{digest}.json.gz", digest)
        with self.assertRaises(ValueError):
            store.delete(uri, "A" * 64)


if __name__ == "__main__":
    unittest.main()
