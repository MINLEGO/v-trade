"""Validate the auditable evidence record for a Kalshi-only cutover.

The evidence record is deliberately separate from runtime configuration.  It records
what was observed during staging or cutover, while this module prevents a partial
record from being presented as a ready release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from vtrade.config import (
    ACTIVE_EXPERIMENT_CONFIG,
    ACTIVE_EXPERIMENT_VERSION,
    ACTIVE_FIXTURE_MANIFEST,
)
from vtrade.fixtures import validate_fixture_manifest
from vtrade.migrate import EXPECTED_MIGRATIONS, MigrationError, load_migration_sources

CUTOVER_EVIDENCE_SCHEMA_VERSION = "vtrade-kalshi-cutover-evidence-v1"
DEFAULT_EVIDENCE_PATH = Path("docs/evidence/kalshi-cutover-2026-08-27.json")
REQUIRED_GATES = (
    "offline",
    "postgresql",
    "built_image",
    "private_resources",
    "provider_egress",
    "french_host",
)
_GATE_STATUSES = frozenset({"passed", "failed", "not_recorded"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class CutoverEvidenceError(ValueError):
    """Raised when a cutover evidence record is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class MigrationEvidence:
    position: int
    name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SnapshotEvidence:
    status: str
    reference: str | None


@dataclass(frozen=True, slots=True)
class GateEvidence:
    status: str
    observed_at: datetime
    command: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CutoverEvidence:
    path: Path
    schema_version: str
    release: str
    target: str
    status: str
    recorded_at: datetime
    source_commit: str
    migration_chain: tuple[MigrationEvidence, ...]
    artifacts: Mapping[str, str]
    image_sha256: str | None
    snapshots: Mapping[str, SnapshotEvidence]
    gates: Mapping[str, GateEvidence]
    paper_only_verified: bool
    real_execution: str
    rollback_mode: str
    rollback_status: str

    @property
    def ready(self) -> bool:
        """Return whether every irreversible cutover gate is actually evidenced."""

        return (
            self.status == "ready"
            and self.paper_only_verified
            and self.real_execution == "unreachable"
            and bool(self.image_sha256 and _SHA256.fullmatch(self.image_sha256))
            and all(gate.status == "passed" for gate in self.gates.values())
            and all(
                snapshot.status == "passed" and snapshot.reference
                for snapshot in self.snapshots.values()
            )
            and self.rollback_mode == "infrastructure_only"
            and self.rollback_status == "passed"
        )


def validate_cutover_evidence(
    path: str | Path = DEFAULT_EVIDENCE_PATH,
    *,
    root: str | Path | None = None,
    require_ready: bool = False,
) -> CutoverEvidence:
    """Read and validate one evidence record.

    When ``root`` is provided, the record's migration and artifact digests are checked
    against the active checkout as well.  ``require_ready`` is the gate used before a
    real cutover and rejects partial or merely observational records.
    """

    source = Path(path)
    raw = _read_object(source)
    evidence = _parse_evidence(source, raw)
    if root is not None:
        _assert_matches_active_release(evidence, Path(root))
    if require_ready and not evidence.ready:
        raise CutoverEvidenceError(
            "cutover evidence is not ready: every gate, snapshot, image digest, "
            "paper-only check, and rollback record must pass"
        )
    return evidence


def active_release_fingerprint(root: str | Path = ".") -> dict[str, object]:
    """Return the hashes and ordered migrations bound to the active checkout."""

    base = Path(root)
    config_path = base / ACTIVE_EXPERIMENT_CONFIG
    try:
        config_bytes = config_path.read_bytes()
        config = json.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverEvidenceError("cannot read the active experiment configuration") from exc
    if (
        not isinstance(config, Mapping)
        or config.get("experiment_version") != ACTIVE_EXPERIMENT_VERSION
        or config.get("status") != "ready"
        or config.get("venue") != "kalshi"
        or config.get("execution_mode") != "paper_only"
    ):
        raise CutoverEvidenceError("active experiment configuration is not vtrade-kalshi-v1")
    artifact_definitions = config.get("artifacts")
    if not isinstance(artifact_definitions, Mapping):
        raise CutoverEvidenceError("active experiment artifacts are missing")

    fixture_definitions = config.get("fixtures")
    if not isinstance(fixture_definitions, Mapping):
        raise CutoverEvidenceError("active fixture definition is missing")
    fixture_expected = fixture_definitions.get("manifest_sha256")
    fixture_actual = _sha256_file(base / ACTIVE_FIXTURE_MANIFEST)
    if fixture_expected != fixture_actual:
        raise CutoverEvidenceError("active fixture manifest does not match its configured hash")
    artifacts: dict[str, str] = {
        "experiment_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "fixture_manifest_sha256": fixture_actual,
    }
    for name in ("prompt", "tool_schemas", "compatibility"):
        definition = artifact_definitions.get(name)
        if not isinstance(definition, Mapping) or not isinstance(definition.get("path"), str):
            raise CutoverEvidenceError(f"active artifact {name} is malformed")
        expected = definition.get("sha256")
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise CutoverEvidenceError(f"active artifact {name} has no valid configured hash")
        actual = _sha256_file(base / str(definition["path"]))
        if actual != expected:
            raise CutoverEvidenceError(f"active artifact {name} does not match its configured hash")
        artifacts[f"{name}_sha256"] = actual
    try:
        validate_fixture_manifest(base / ACTIVE_FIXTURE_MANIFEST, require_ready=True)
        sources = load_migration_sources(base / "migrations")
    except (MigrationError, OSError, ValueError) as exc:
        raise CutoverEvidenceError(f"active release fingerprint cannot be verified: {exc}") from exc
    return {
        "artifacts": artifacts,
        "migration_chain": tuple(
            MigrationEvidence(source.position, source.name, source.sha256) for source in sources
        ),
    }


def _assert_matches_active_release(evidence: CutoverEvidence, root: Path) -> None:
    fingerprint = active_release_fingerprint(root)
    if evidence.artifacts != fingerprint["artifacts"]:
        raise CutoverEvidenceError("evidence artifact hashes do not match the active checkout")
    if evidence.migration_chain != fingerprint["migration_chain"]:
        raise CutoverEvidenceError("evidence migration chain does not match the active checkout")


def _parse_evidence(path: Path, raw: Mapping[str, object]) -> CutoverEvidence:
    schema_version = _required_string(raw, "schema_version")
    if schema_version != CUTOVER_EVIDENCE_SCHEMA_VERSION:
        raise CutoverEvidenceError("unsupported cutover evidence schema version")
    release = _required_string(raw, "release")
    if release != ACTIVE_EXPERIMENT_VERSION:
        raise CutoverEvidenceError("cutover evidence release is not vtrade-kalshi-v1")
    target = _required_string(raw, "target")
    status = _required_string(raw, "status")
    if status not in {"blocked", "ready"}:
        raise CutoverEvidenceError("cutover evidence status must be blocked or ready")
    recorded_at = _timestamp(raw.get("recorded_at"), "recorded_at")
    source_commit = _required_string(raw, "source_commit")
    if not _COMMIT.fullmatch(source_commit):
        raise CutoverEvidenceError("source_commit must be a 40-character lowercase Git SHA")

    migrations_value = _required_array(raw, "migration_chain")
    migrations: list[MigrationEvidence] = []
    for position, value in enumerate(migrations_value, start=1):
        item = _as_mapping(value, f"migration_chain[{position - 1}]")
        observed_position = item.get("position")
        if isinstance(observed_position, bool) or not isinstance(observed_position, int):
            raise CutoverEvidenceError("migration chain positions must be integers")
        if observed_position != position:
            raise CutoverEvidenceError("migration chain positions must be consecutive")
        name = _required_string(item, "name")
        if name != EXPECTED_MIGRATIONS[position - 1]:
            raise CutoverEvidenceError(
                "evidence migration chain is not the canonical active chain"
            )
        migrations.append(
            MigrationEvidence(position, name, _digest(item.get("sha256"), f"migration {name}"))
        )
    if len(migrations) != len(EXPECTED_MIGRATIONS):
        raise CutoverEvidenceError(
            "evidence migration chain must contain exactly the active migration files"
        )

    artifact_values = _as_mapping(raw.get("artifacts"), "artifacts")
    expected_artifacts = (
        "experiment_config_sha256",
        "prompt_sha256",
        "tool_schemas_sha256",
        "compatibility_sha256",
        "fixture_manifest_sha256",
    )
    if set(artifact_values) != set(expected_artifacts):
        raise CutoverEvidenceError("evidence must contain the five active artifact hashes")
    artifacts = {
        name: _digest(artifact_values.get(name), f"artifacts.{name}")
        for name in expected_artifacts
    }

    image = _as_mapping(raw.get("image"), "image")
    image_sha256_value = image.get("sha256")
    image_sha256 = (
        None
        if image_sha256_value is None
        else _digest(image_sha256_value, "image.sha256")
    )
    _required_string(image, "reference")

    snapshots_value = _as_mapping(raw.get("snapshots"), "snapshots")
    if set(snapshots_value) != {"database", "object_storage"}:
        raise CutoverEvidenceError("evidence must contain database and object_storage snapshots")
    snapshots = {
        name: _parse_snapshot(snapshots_value.get(name), name)
        for name in ("database", "object_storage")
    }

    gates_value = _as_mapping(raw.get("gates"), "gates")
    if set(gates_value) != set(REQUIRED_GATES):
        raise CutoverEvidenceError("evidence must contain exactly the six cutover gates")
    gates = {name: _parse_gate(gates_value.get(name), name) for name in REQUIRED_GATES}

    paper_only = _as_mapping(raw.get("paper_only"), "paper_only")
    paper_only_verified = paper_only.get("verified")
    if not isinstance(paper_only_verified, bool):
        raise CutoverEvidenceError("paper_only.verified must be boolean")
    real_execution = _required_string(paper_only, "real_execution")
    if real_execution not in {"unreachable", "not_verified", "reachable"}:
        raise CutoverEvidenceError("paper_only.real_execution has an unsupported value")

    rollback = _as_mapping(raw.get("rollback"), "rollback")
    rollback_mode = _required_string(rollback, "mode")
    if rollback_mode != "infrastructure_only":
        raise CutoverEvidenceError("rollback mode must remain infrastructure_only")
    rollback_status = _required_status(rollback.get("status"), "rollback.status")
    _optional_reference(rollback, "pre_cutover_snapshot_reference")

    evidence = CutoverEvidence(
        path=path,
        schema_version=schema_version,
        release=release,
        target=target,
        status=status,
        recorded_at=recorded_at,
        source_commit=source_commit,
        migration_chain=tuple(migrations),
        artifacts=artifacts,
        image_sha256=image_sha256,
        snapshots=snapshots,
        gates=gates,
        paper_only_verified=paper_only_verified,
        real_execution=real_execution,
        rollback_mode=rollback_mode,
        rollback_status=rollback_status,
    )
    if evidence.status == "ready" and not evidence.ready:
        raise CutoverEvidenceError("evidence marked ready but its required records are incomplete")
    return evidence


def _parse_snapshot(value: object, name: str) -> SnapshotEvidence:
    item = _as_mapping(value, f"snapshots.{name}")
    status = _required_status(item.get("status"), f"snapshots.{name}.status")
    reference = _optional_reference(item, "reference")
    if status == "passed" and reference is None:
        raise CutoverEvidenceError(f"snapshots.{name} passed without a reference")
    return SnapshotEvidence(status, reference)


def _parse_gate(value: object, name: str) -> GateEvidence:
    item = _as_mapping(value, f"gates.{name}")
    return GateEvidence(
        status=_required_status(item.get("status"), f"gates.{name}.status"),
        observed_at=_timestamp(item.get("observed_at"), f"gates.{name}.observed_at"),
        command=_required_string(item, "command"),
        evidence=_required_string(item, "evidence"),
    )


def _read_object(path: Path) -> Mapping[str, object]:
    try:
        raw_bytes = path.read_bytes()
        if b"\r" in raw_bytes:
            raise CutoverEvidenceError("cutover evidence must use LF line endings")
        raw_value = json.loads(raw_bytes.decode("utf-8"))
    except CutoverEvidenceError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverEvidenceError(f"cannot read cutover evidence {path}") from exc
    return _as_mapping(raw_value, "cutover evidence")


def _sha256_file(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CutoverEvidenceError(f"cannot read active artifact {path}") from exc
    if b"\r" in content:
        raise CutoverEvidenceError(f"active artifact {path} must use LF line endings")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CutoverEvidenceError(f"active artifact {path} must be valid UTF-8") from exc
    return hashlib.sha256(content).hexdigest()


def _as_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CutoverEvidenceError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _required_array(raw: Mapping[str, object], field: str) -> list[object]:
    value = raw.get(field)
    if not isinstance(value, list):
        raise CutoverEvidenceError(f"{field} must be an array")
    return value


def _required_string(raw: Mapping[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CutoverEvidenceError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CutoverEvidenceError(f"{field} must be a lowercase SHA-256")
    return value


def _required_status(value: object, field: str) -> str:
    if not isinstance(value, str) or value not in _GATE_STATUSES:
        raise CutoverEvidenceError(f"{field} must be passed, failed, or not_recorded")
    return value


def _optional_reference(raw: Mapping[str, object], field: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CutoverEvidenceError(f"{field} must be null or a non-empty string")
    return value.strip()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CutoverEvidenceError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CutoverEvidenceError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CutoverEvidenceError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _payload(evidence: CutoverEvidence) -> dict[str, object]:
    return {
        "path": evidence.path.as_posix(),
        "release": evidence.release,
        "target": evidence.target,
        "status": evidence.status,
        "ready": evidence.ready,
        "source_commit": evidence.source_commit,
        "migration_count": len(evidence.migration_chain),
        "gate_statuses": {name: gate.status for name, gate in evidence.gates.items()},
        "snapshot_statuses": {
            name: snapshot.status for name, snapshot in evidence.snapshots.items()
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Kalshi cutover evidence")
    parser.add_argument("evidence", nargs="?", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        evidence = validate_cutover_evidence(
            args.evidence,
            root=args.root,
            require_ready=args.require_ready,
        )
    except CutoverEvidenceError as exc:
        parser.error(str(exc))
    if args.as_json:
        print(json.dumps(_payload(evidence), indent=2, sort_keys=True))
    else:
        print(
            f"{evidence.path}: status={evidence.status} ready={evidence.ready} "
            f"commit={evidence.source_commit} migrations={len(evidence.migration_chain)}"
        )
        for name, gate in evidence.gates.items():
            print(f"{gate.status.upper():12} gate={name}")
        for name, snapshot in evidence.snapshots.items():
            print(f"{snapshot.status.upper():12} snapshot={name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
