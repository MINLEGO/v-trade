from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def config_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    raw: dict[str, Any]
    sha256: str

    @property
    def version(self) -> str:
        return str(self.raw["experiment_version"])

    @property
    def pending_decisions(self) -> tuple[str, ...]:
        decisions = self.raw.get("owner_decisions", {})
        return tuple(sorted(k for k, v in decisions.items() if v.get("status") == "owner_pending"))

    def assert_runnable(self) -> None:
        if self.pending_decisions:
            joined = ", ".join(self.pending_decisions)
            raise ConfigurationError(f"experiment has REQUIRED owner_pending decisions: {joined}")


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot load experiment config {source}: {exc}") from exc
    required = {"experiment_version", "classifications", "limits", "owner_decisions"}
    missing = sorted(required - raw.keys())
    if missing:
        raise ConfigurationError(f"missing config fields: {', '.join(missing)}")
    return ExperimentConfig(raw=raw, sha256=config_hash(raw))


def required_environment(names: tuple[str, ...]) -> dict[str, str]:
    missing = [name for name in names if not os.getenv(name) or os.getenv(name) == "REQUIRED"]
    if missing:
        raise ConfigurationError(f"missing REQUIRED environment resources: {', '.join(missing)}")
    return {name: os.environ[name] for name in names}

