from __future__ import annotations

from pathlib import Path

import pytest

from vtrade.release_verification import (
    ReleaseVerificationError,
    assert_archive_boundary,
    assert_zero_active_venue,
    external_validation_matrix,
    offline_validation_matrix,
    scan_active_surface,
    verify_release,
)


def test_repository_release_report_passes_local_gates() -> None:
    report = verify_release()

    assert report.ok
    assert not report.legacy_references
    assert {gate.scope for gate in report.external_gates} == {
        "real-postgresql",
        "real-storage",
        "french-production-host",
    }


def test_active_sweep_reports_file_line_and_identifier(tmp_path: Path) -> None:
    source = tmp_path / "src" / "legacy.py"
    source.parent.mkdir()
    legacy_key = "condition" + "_id"
    source.write_text(f"payload = {{'{legacy_key}': 'blocked'}}\n", encoding="utf-8")

    findings = scan_active_surface(tmp_path)

    assert len(findings) == 1
    assert findings[0].path == Path("src/legacy.py")
    assert findings[0].line == 1
    assert findings[0].identifier == legacy_key
    with pytest.raises(ReleaseVerificationError, match=r"src\\legacy\.py:1"):
        assert_zero_active_venue(tmp_path)


def test_archive_boundary_has_explicit_read_only_contract() -> None:
    assert_archive_boundary()


def test_validation_matrix_separates_local_and_external_evidence() -> None:
    offline = offline_validation_matrix()
    external = external_validation_matrix()

    assert all(gate.scope in {"offline", "offline-shape", "built-image"} for gate in offline)
    assert all(gate.scope not in {"offline", "offline-shape", "built-image"} for gate in external)
    assert any("pytest" in gate.command for gate in offline)
    assert any("migrate" in gate.command for gate in external)
