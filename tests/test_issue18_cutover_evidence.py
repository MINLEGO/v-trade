from __future__ import annotations

import json
from pathlib import Path

import pytest

from vtrade.cutover_evidence import (
    REQUIRED_GATES,
    CutoverEvidenceError,
    validate_cutover_evidence,
)

EVIDENCE_PATH = Path("docs/evidence/kalshi-cutover-2026-08-27.json")


def test_recorded_issue18_evidence_is_structurally_valid_and_bound_to_release() -> None:
    evidence = validate_cutover_evidence(EVIDENCE_PATH, root=".")

    assert evidence.release == "vtrade-kalshi-v1"
    assert len(evidence.migration_chain) == 7
    assert tuple(evidence.gates) == REQUIRED_GATES
    assert evidence.status == "blocked"
    assert not evidence.ready


def test_ready_status_cannot_hide_missing_external_evidence(tmp_path: Path) -> None:
    raw = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    raw["status"] = "ready"
    destination = tmp_path / "evidence.json"
    destination.write_text(json.dumps(raw) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(CutoverEvidenceError, match="marked ready"):
        validate_cutover_evidence(destination)


def test_require_ready_rejects_the_current_observation_until_gates_are_complete() -> None:
    with pytest.raises(CutoverEvidenceError, match="not ready"):
        validate_cutover_evidence(EVIDENCE_PATH, root=".", require_ready=True)


def test_evidence_rejects_a_noncanonical_migration_chain(tmp_path: Path) -> None:
    raw = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    raw["migration_chain"][0]["name"] = "0001_legacy.sql"
    destination = tmp_path / "evidence.json"
    destination.write_text(json.dumps(raw) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(CutoverEvidenceError, match="canonical seven-file chain"):
        validate_cutover_evidence(destination)
