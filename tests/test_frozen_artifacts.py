from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vtrade.frozen_artifacts import (
    FROZEN_EXPERIMENT_CONFIGS,
    FrozenArtifactError,
    canonical_artifact_sha256,
    verify_experiment_config,
)


def test_active_artifact_smoke_check_prints_three_hash_pairs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for config in FROZEN_EXPERIMENT_CONFIGS:
        verify_experiment_config(config)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert all("expected=" in line and "actual=" in line for line in lines)


def test_hash_contract_rejects_non_lf_bytes() -> None:
    with pytest.raises(FrozenArtifactError, match="LF line endings"):
        canonical_artifact_sha256(b"first\r\nsecond\n", label="test artifact")


def test_hash_drift_fails_before_composition(tmp_path: Path) -> None:
    raw = json.loads(Path(FROZEN_EXPERIMENT_CONFIGS[0]).read_text(encoding="utf-8"))
    raw["artifacts"]["prompt"]["sha256"] = hashlib.sha256(b"changed").hexdigest()
    candidate = tmp_path / "experiment.json"
    candidate.write_text(json.dumps(raw), encoding="utf-8", newline="\n")
    with pytest.raises(FrozenArtifactError, match="hash mismatch"):
        verify_experiment_config(candidate)
