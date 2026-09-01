from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from vtrade.config import (
    ACTIVE_EXPERIMENT_CONFIG,
    ACTIVE_EXPERIMENT_VERSION,
    ACTIVE_FIXTURE_MANIFEST,
)
from vtrade.fixtures import fixture_manifest_sha256, validate_fixture_manifest

FROZEN_EXPERIMENT_CONFIGS = (Path("config/experiments/vtrade-kalshi-v1.json"),)
FROZEN_ARTIFACT_NAMES = ("prompt", "tool_schemas", "compatibility")
EXPECTED_TOOL_COUNT = 27
FORBIDDEN_ACTIVE_FIELDS = (
    "market_id",
    "outcome_id",
    "token_id",
    "venue_token_id",
    "condition_id",
    "negative_risk",
    "shares",
    "SHARES",
    "poly" + "market",
)


class FrozenArtifactError(ValueError):
    """Raised when active contract bytes or structure are not canonical."""


def canonical_artifact_sha256(content: bytes, *, label: str = "frozen artifact") -> str:
    if b"\r" in content:
        raise FrozenArtifactError(f"{label} must use LF line endings")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrozenArtifactError(f"{label} must be valid UTF-8") from exc
    return hashlib.sha256(content).hexdigest()


def canonical_artifact_file_sha256(path: str | Path, *, label: str = "frozen artifact") -> str:
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        raise FrozenArtifactError(f"cannot read {label} at {path}") from exc
    return canonical_artifact_sha256(content, label=label)


def verify_experiment_config(path: str | Path) -> None:
    source = Path(path)
    try:
        config_bytes = source.read_bytes()
        canonical_artifact_sha256(config_bytes, label=f"experiment config {source}")
        loaded = json.loads(config_bytes.decode("utf-8"))
    except FrozenArtifactError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenArtifactError(f"cannot read experiment config {source}") from exc
    if not isinstance(loaded, Mapping):
        raise FrozenArtifactError(f"experiment config {source} must be an object")
    if loaded.get("experiment_version") != ACTIVE_EXPERIMENT_VERSION:
        raise FrozenArtifactError(f"only {ACTIVE_EXPERIMENT_VERSION} may be active")
    if loaded.get("venue") != "kalshi" or loaded.get("execution_mode") != "paper_only":
        raise FrozenArtifactError("active experiment is not Kalshi paper-only")
    artifacts = loaded.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(FROZEN_ARTIFACT_NAMES):
        raise FrozenArtifactError("active experiment must declare exactly three frozen artifacts")
    for name in FROZEN_ARTIFACT_NAMES:
        definition = artifacts.get(name)
        if not isinstance(definition, Mapping):
            raise FrozenArtifactError(f"experiment config {source} is missing {name}")
        path_value = definition.get("path")
        expected = definition.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise FrozenArtifactError(f"experiment config {source} has malformed {name}")
        if len(expected) != 64 or expected.lower() != expected:
            raise FrozenArtifactError(f"experiment config {source}: {name} SHA-256 is malformed")
        actual = canonical_artifact_file_sha256(path_value, label=f"{source}: {name}")
        print(f"{source}: {name}: expected={expected} actual={actual}")
        if actual != expected:
            raise FrozenArtifactError(f"{source}: {name} hash mismatch")
        if name != "compatibility":
            _reject_forbidden_fields(Path(path_value).read_bytes(), label=name)
    _verify_tool_schema(Path(cast(str, artifacts["tool_schemas"]["path"])))
    _verify_fee_schedule_reference(loaded)
    _verify_fixture_reference(loaded)


def verify_active_artifacts(path: str | Path = ACTIVE_EXPERIMENT_CONFIG) -> None:
    verify_experiment_config(path)


def _verify_tool_schema(path: Path) -> None:
    try:
        raw_bytes = path.read_bytes()
        document = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenArtifactError(f"cannot read active tool schema {path}") from exc
    if not isinstance(document, Mapping):
        raise FrozenArtifactError("active tool schema must be an object")
    if document.get("schema_version") != "vtrade-kalshi-tools-v1":
        raise FrozenArtifactError("active tool schema version is unsupported")
    if document.get("venue") != "kalshi" or document.get("execution_mode") != "paper_only":
        raise FrozenArtifactError("active tool schema is not paper-only Kalshi")
    tools = document.get("tools")
    if not isinstance(tools, list) or len(tools) != EXPECTED_TOOL_COUNT:
        raise FrozenArtifactError("active tool schema must contain exactly 27 tools")
    names: list[str] = []
    for row in tools:
        if not isinstance(row, Mapping):
            raise FrozenArtifactError("active tool schema row is malformed")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise FrozenArtifactError("active tool schema tool name is missing")
        if name in names:
            raise FrozenArtifactError(f"duplicate active tool name {name}")
        names.append(name)
        if not isinstance(row.get("description"), str) or not str(row["description"]).strip():
            raise FrozenArtifactError(f"tool {name} lacks a description")
        if not isinstance(row.get("input_schema"), Mapping) or not isinstance(
            row.get("output_schema"), Mapping
        ):
            raise FrozenArtifactError(f"tool {name} lacks input/output schema")
    try:
        Draft202012Validator.check_schema(document)
    except Exception as exc:  # jsonschema has several SchemaError subclasses across versions
        raise FrozenArtifactError(f"active tool schema is invalid: {exc}") from exc
    _require_closed_objects(document)
    _reject_forbidden_fields(raw_bytes, label="tool schema")


def _require_closed_objects(value: object) -> None:
    if isinstance(value, Mapping):
        if (
            value.get("type") == "object"
            and "additionalProperties" in value
            and value.get("additionalProperties") is not False
        ):
            raise FrozenArtifactError(
                "active tool schema objects must reject unknown properties"
            )
        for child in value.values():
            _require_closed_objects(child)
    elif isinstance(value, list):
        for child in value:
            _require_closed_objects(child)


def _verify_fixture_reference(config: Mapping[str, object]) -> None:
    fixtures = config.get("fixtures")
    if not isinstance(fixtures, Mapping):
        raise FrozenArtifactError("active experiment fixture reference is missing")
    path = fixtures.get("manifest_path")
    expected = fixtures.get("manifest_sha256")
    if path != str(ACTIVE_FIXTURE_MANIFEST).replace("\\", "/"):
        raise FrozenArtifactError("active experiment must reference the Kalshi fixture manifest")
    if not isinstance(expected, str) or len(expected) != 64:
        raise FrozenArtifactError("fixture manifest hash is missing")
    try:
        validate_fixture_manifest(str(path))
        actual = fixture_manifest_sha256(str(path))
    except (OSError, ValueError) as exc:
        raise FrozenArtifactError(f"Kalshi fixture manifest is invalid: {exc}") from exc
    if actual != expected:
        raise FrozenArtifactError("Kalshi fixture manifest hash mismatch")


def _verify_fee_schedule_reference(config: Mapping[str, object]) -> None:
    fees = config.get("fees")
    if not isinstance(fees, Mapping):
        raise FrozenArtifactError("active fee schedule reference is missing")
    definition = fees.get("schedule_artifact")
    if not isinstance(definition, Mapping):
        raise FrozenArtifactError("active fee schedule artifact is missing")
    path_value = definition.get("path")
    expected_file = definition.get("sha256")
    if (
        not isinstance(path_value, str)
        or not isinstance(expected_file, str)
        or not _is_sha256(expected_file)
    ):
        raise FrozenArtifactError("active fee schedule JSON hash is malformed")
    path = Path(path_value)
    actual_file = canonical_artifact_file_sha256(path, label="active fee schedule")
    if actual_file != expected_file:
        raise FrozenArtifactError("active fee schedule artifact hash mismatch")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenArtifactError("active fee schedule artifact is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise FrozenArtifactError("active fee schedule artifact must be an object")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and value.lower() == value and all(
        character in "0123456789abcdef" for character in value
    )


def _reject_forbidden_fields(content: bytes, *, label: str) -> None:
    try:
        text = content.decode("utf-8").casefold()
    except UnicodeDecodeError as exc:
        raise FrozenArtifactError(f"{label} must be valid UTF-8") from exc
    for field in FORBIDDEN_ACTIVE_FIELDS:
        if field.casefold() in text:
            raise FrozenArtifactError(f"{label} contains forbidden active field {field}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify vtrade-kalshi-v1 frozen artifacts")
    parser.add_argument("configs", nargs="*", type=Path)
    args = parser.parse_args()
    configs = cast(tuple[Path, ...], tuple(args.configs)) or FROZEN_EXPERIMENT_CONFIGS
    try:
        for config in configs:
            verify_experiment_config(config)
    except (FrozenArtifactError, OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
