from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTIVE_EXPERIMENT_VERSION = "vtrade-kalshi-v1"
ACTIVE_EXPERIMENT_CONFIG = Path("config/experiments/vtrade-kalshi-v1.json")
ACTIVE_FIXTURE_MANIFEST = Path("spec/fixtures/kalshi/manifest.json")
ACTIVE_TOOL_SCHEMA = Path("spec/tool-schemas-vtrade-kalshi-v1.json")


class ConfigurationError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def config_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    raw: dict[str, Any]
    sha256: str
    source: Path | None = None

    @property
    def version(self) -> str:
        return str(self.raw["experiment_version"])

    @property
    def pending_decisions(self) -> tuple[str, ...]:
        decisions = self.raw.get("owner_decisions", {})
        if not isinstance(decisions, Mapping):
            return ("owner_decisions",)
        return tuple(
            sorted(
                str(key)
                for key, value in decisions.items()
                if isinstance(value, Mapping) and value.get("status") == "owner_pending"
            )
        )

    def assert_runnable(self) -> None:
        _validate_active_shape(self.raw)
        if self.pending_decisions:
            joined = ", ".join(self.pending_decisions)
            raise ConfigurationError(f"experiment has REQUIRED owner_pending decisions: {joined}")
        if self.source is None:
            return
        try:
            from vtrade.fixtures import validate_fixture_manifest
            from vtrade.frozen_artifacts import verify_experiment_config

            verify_experiment_config(self.source)
            manifest = self.raw["fixtures"]["manifest_path"]
            validate_fixture_manifest(manifest, require_ready=True)
        except ConfigurationError:
            raise
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                f"active experiment artifacts are not runnable: {exc}"
            ) from exc


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    source = Path(path)
    try:
        raw_value = json.loads(source.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot load experiment config {source}: {exc}") from exc
    if not isinstance(raw_value, dict):
        raise ConfigurationError(f"experiment config {source} must be an object")
    raw = dict(raw_value)
    _validate_active_shape(raw)
    return ExperimentConfig(raw=raw, sha256=config_hash(raw), source=source)


def required_environment(names: tuple[str, ...]) -> dict[str, str]:
    missing = [name for name in names if not os.getenv(name) or os.getenv(name) == "REQUIRED"]
    if missing:
        raise ConfigurationError(f"missing REQUIRED environment resources: {', '.join(missing)}")
    return {name: os.environ[name] for name in names}


def _validate_active_shape(raw: Mapping[str, object]) -> None:
    version = raw.get("experiment_version")
    if version != ACTIVE_EXPERIMENT_VERSION:
        raise ConfigurationError(
            f"only {ACTIVE_EXPERIMENT_VERSION} is runnable; received {version!r}"
        )
    if raw.get("status") != "ready":
        raise ConfigurationError("active experiment must have status ready")
    if raw.get("venue") != "kalshi":
        raise ConfigurationError("active experiment must use the kalshi venue")
    if raw.get("execution_mode") != "paper_only":
        raise ConfigurationError("active experiment must be paper-only")
    required = {
        "experiment_version",
        "status",
        "venue",
        "execution_mode",
        "artifacts",
        "fixtures",
        "fees",
        "classifications",
        "limits",
        "owner_decisions",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ConfigurationError(f"missing config fields: {', '.join(missing)}")
    artifacts = raw["artifacts"]
    if not isinstance(artifacts, Mapping):
        raise ConfigurationError("active experiment artifacts must be an object")
    if set(artifacts) != {"prompt", "tool_schemas", "compatibility"}:
        raise ConfigurationError(
            "active experiment must declare exactly prompt, tool_schemas, compatibility"
        )
    expected_paths = {
        "prompt": "spec/prompt/vtrade-kalshi-v1.md",
        "tool_schemas": "spec/tool-schemas-vtrade-kalshi-v1.json",
        "compatibility": "spec/vtrade-kalshi-v1-compatibility.md",
    }
    for name, expected_path in expected_paths.items():
        definition = artifacts.get(name)
        if not isinstance(definition, Mapping):
            raise ConfigurationError(f"artifact {name} is malformed")
        if definition.get("path") != expected_path:
            raise ConfigurationError(f"artifact {name} must use {expected_path}")
        digest = definition.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or digest.lower() != digest:
            raise ConfigurationError(f"artifact {name} must carry a lowercase SHA-256")
    fees = raw["fees"]
    if not isinstance(fees, Mapping):
        raise ConfigurationError("active experiment fee policy configuration is missing")
    if fees.get("policy_required") is not True:
        raise ConfigurationError("active experiment must require a fee policy")
    schedule_artifact = fees.get("schedule_artifact")
    if not isinstance(schedule_artifact, Mapping):
        raise ConfigurationError("active experiment fee schedule artifact is missing")
    if schedule_artifact.get("path") != "spec/fee-schedules/kalshi-predictions-v1.json":
        raise ConfigurationError("active experiment must use the canonical Kalshi fee schedule")
    schedule_sha256 = schedule_artifact.get("sha256")
    pdf_sha256 = schedule_artifact.get("pdf_sha256")
    for name, digest in (("schedule", schedule_sha256), ("official PDF", pdf_sha256)):
        if not isinstance(digest, str) or len(digest) != 64 or digest.lower() != digest:
            raise ConfigurationError(f"fee {name} hash must be a lowercase SHA-256")
    fixtures = raw["fixtures"]
    if not isinstance(fixtures, Mapping):
        raise ConfigurationError("active experiment fixture contract is missing")
    if fixtures.get("manifest_path") != str(ACTIVE_FIXTURE_MANIFEST).replace("\\", "/"):
        raise ConfigurationError("active experiment must use the Kalshi fixture manifest")
    manifest_sha256 = fixtures.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
        raise ConfigurationError("fixture manifest SHA-256 is missing or malformed")
