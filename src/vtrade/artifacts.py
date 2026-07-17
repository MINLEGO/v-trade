from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from vtrade.config import required_environment


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    byte_length: int
    relative_path: str
    compression: str = "gzip"

    @property
    def uri(self) -> str:
        return self.relative_path


class ContentAddressedArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, content: bytes) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        relative = Path(digest[:2]) / f"{digest}.json.gz"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with (
                    os.fdopen(fd, "wb") as raw,
                    gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
                ):
                    compressed.write(content)
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ArtifactRef(digest, len(content), relative.as_posix())

    def put_file(self, source: str | Path) -> ArtifactRef:
        """Archive a file without materializing its raw bytes a second time in memory."""
        source_path = Path(source)
        digest = hashlib.sha256()
        byte_length = 0
        with source_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                byte_length += len(chunk)

        sha256 = digest.hexdigest()
        relative = Path(sha256[:2]) / f"{sha256}.json.gz"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with (
                    source_path.open("rb") as source_stream,
                    os.fdopen(fd, "wb") as raw,
                    gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
                ):
                    shutil.copyfileobj(source_stream, compressed, length=1024 * 1024)
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ArtifactRef(sha256, byte_length, relative.as_posix())

    def get(self, reference: ArtifactRef) -> bytes:
        compressed = self.root / reference.relative_path
        with gzip.open(compressed, "rb") as stream:
            content = stream.read()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("artifact content hash mismatch")
        return content


class ArtifactStorageConfigurationError(RuntimeError):
    pass


class SupabaseArtifactStore:
    """Required durable runtime store; there is deliberately no local fallback."""

    def __init__(
        self,
        project_url: str,
        bucket: str,
        service_role_key: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not project_url or not bucket or not service_role_key:
            raise ArtifactStorageConfigurationError(
                "Supabase URL, private bucket, and service-role key are REQUIRED"
            )
        self.project_url = project_url.rstrip("/")
        self.bucket = bucket
        self._headers = {
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
        }
        self._client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        self._validated = False

    @classmethod
    def from_environment(cls) -> SupabaseArtifactStore:
        values = required_environment(
            (
                "VTRADE_SUPABASE_URL",
                "VTRADE_SUPABASE_BUCKET",
                "VTRADE_SUPABASE_SERVICE_ROLE_KEY",
            )
        )
        return cls(
            values["VTRADE_SUPABASE_URL"],
            values["VTRADE_SUPABASE_BUCKET"],
            values["VTRADE_SUPABASE_SERVICE_ROLE_KEY"],
        )

    def validate(self) -> None:
        endpoint = f"{self.project_url}/storage/v1/bucket/{quote(self.bucket, safe='')}"
        response = self._client.get(endpoint, headers=self._headers)
        if response.status_code == 404:
            raise ArtifactStorageConfigurationError(
                f"required private Supabase bucket {self.bucket!r} was not found"
            )
        try:
            response.raise_for_status()
            metadata = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ArtifactStorageConfigurationError(
                "cannot validate required Supabase artifact bucket"
            ) from exc
        if not isinstance(metadata, dict) or metadata.get("public") is not False:
            raise ArtifactStorageConfigurationError(
                f"Supabase bucket {self.bucket!r} must exist and be private"
            )
        self._validated = True

    def put(self, content: bytes) -> ArtifactRef:
        if not self._validated:
            self.validate()
        digest = hashlib.sha256(content).hexdigest()
        relative = f"{digest[:2]}/{digest}.json.gz"
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
            compressed.write(content)
        endpoint = (
            f"{self.project_url}/storage/v1/object/"
            f"{quote(self.bucket, safe='')}/{quote(relative, safe='/')}"
        )
        response = self._client.post(
            endpoint,
            headers={
                **self._headers,
                "Content-Type": "application/gzip",
                "x-upsert": "true",
            },
            content=buffer.getvalue(),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError("failed to persist required Supabase artifact") from exc
        return ArtifactRef(digest, len(content), f"supabase://{self.bucket}/{relative}")

    def delete(self, uri: str, sha256: str) -> None:
        """Delete one content-addressed object after strict bucket/hash validation."""
        if not self._validated:
            self.validate()
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("artifact deletion requires a lowercase SHA-256 digest")
        parsed = urlparse(uri)
        expected_path = f"/{sha256[:2]}/{sha256}.json.gz"
        if (
            parsed.scheme != "supabase"
            or parsed.netloc != self.bucket
            or parsed.path != expected_path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact URI does not match the configured bucket and SHA path")
        relative = expected_path.removeprefix("/")
        endpoint = (
            f"{self.project_url}/storage/v1/object/"
            f"{quote(self.bucket, safe='')}/{quote(relative, safe='/')}"
        )
        response = self._client.delete(endpoint, headers=self._headers)
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError("failed to delete expired Supabase artifact") from exc
