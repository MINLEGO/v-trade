from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    byte_length: int
    relative_path: str
    compression: str = "gzip"


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

    def get(self, reference: ArtifactRef) -> bytes:
        compressed = self.root / reference.relative_path
        with gzip.open(compressed, "rb") as stream:
            content = stream.read()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("artifact content hash mismatch")
        return content
