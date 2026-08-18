from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

FROZEN_EXPERIMENT_CONFIGS = (
    Path("config/experiments/predictionarena-polymarket-v1.json"),
    Path("config/experiments/predictionarena-polymarket-v1-liquidity-aware.json"),
)
FROZEN_ARTIFACT_NAMES = ("prompt", "tool_schemas")


class FrozenArtifactError(ValueError):
    """Raised when frozen artifact bytes are not canonical UTF-8/LF bytes."""


def canonical_artifact_sha256(content: bytes, *, label: str = "frozen artifact") -> str:
    if b"\r" in content:
        raise FrozenArtifactError(f"{label} must use LF line endings")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrozenArtifactError(f"{label} must be valid UTF-8") from exc
    return hashlib.sha256(content).hexdigest()


def canonical_artifact_file_sha256(path: str | Path, *, label: str = "frozen artifact") -> str:
    return canonical_artifact_sha256(
        Path(path).read_bytes(),
        label=label,
    )


def verify_experiment_config(path: str | Path) -> None:
    source = Path(path)
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenArtifactError(f"cannot read experiment config {source}") from exc
    if not isinstance(loaded, Mapping):
        raise FrozenArtifactError(f"experiment config {source} must be an object")
    artifacts = loaded.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise FrozenArtifactError(f"experiment config {source} has no artifacts")

    for name in FROZEN_ARTIFACT_NAMES:
        definition = artifacts.get(name)
        if not isinstance(definition, Mapping):
            raise FrozenArtifactError(f"experiment config {source} is missing {name}")
        path_value = definition.get("path")
        expected = definition.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise FrozenArtifactError(f"experiment config {source} has malformed {name}")
        actual = canonical_artifact_file_sha256(
            path_value,
            label=f"{source}: {name}",
        )
        print(f"{source}: {name}: expected={expected} actual={actual}")
        if actual != expected:
            raise FrozenArtifactError(f"{source}: {name} hash mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify frozen experiment artifact hashes")
    parser.add_argument("configs", nargs="*", type=Path)
    args = parser.parse_args()
    configs = cast(tuple[Path, ...], tuple(args.configs)) or FROZEN_EXPERIMENT_CONFIGS
    try:
        for config in configs:
            verify_experiment_config(config)
    except (FrozenArtifactError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
