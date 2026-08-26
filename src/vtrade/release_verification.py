"""Release-time verification for the vtrade-kalshi-v1 paper cutover.

The checks in this module are deliberately bounded. They prove repository-local
invariants and report the external gates that still require a real database, image,
private storage, provider egress, or the intended French production host.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from vtrade.config import ACTIVE_EXPERIMENT_CONFIG, ACTIVE_FIXTURE_MANIFEST
from vtrade.fixtures import validate_fixture_manifest
from vtrade.frozen_artifacts import verify_active_artifacts
from vtrade.migrate import MigrationError, load_migration_sources

ARCHIVE_ROOT = Path("docs/archive/predictionarena")
ARCHIVE_README = ARCHIVE_ROOT / "README.md"
ARCHIVE_MARKER = ARCHIVE_ROOT / "ARCHIVE.md"
ACTIVE_SCAN_DIRECTORIES = (
    Path("src"),
    Path("config"),
    Path("migrations"),
    Path("spec"),
    Path("tests"),
    Path("scripts"),
    Path("docs"),
)
ACTIVE_SCAN_FILES = (
    Path("Dockerfile"),
    Path("compose.coolify.yaml"),
    Path("README.md"),
    Path("CONTEXT.md"),
)
ARCHIVE_PROVENANCE_PATH = Path("spec/vtrade-kalshi-v1-compatibility.md")
PROVENANCE_MARKER = "Historical provenance (controlled):"
EXTERNAL_EVIDENCE_ROOTS = (
    Path("spec/fixtures/kalshi/responses"),
    Path("scripts/probe_kalshi_result"),
)
GUARD_FILES = frozenset(
    {
        Path("src/vtrade/frozen_artifacts.py"),
        Path("src/vtrade/kalshi.py"),
        Path("src/vtrade/release_verification.py"),
        Path("src/vtrade/runtime.py"),
    }
)
FORBIDDEN_ACTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("polymarket", re.compile(r"polymarket", re.IGNORECASE)),
    ("predictionarena-polymarket", re.compile(r"predictionarena[-_]polymarket", re.IGNORECASE)),
    ("gamma-api", re.compile(r"gamma-api", re.IGNORECASE)),
    ("clob venue host", re.compile(r"clob\.polymarket", re.IGNORECASE)),
    ("condition_id", re.compile(r"(?<![A-Za-z0-9_])condition_id(?![A-Za-z0-9_])")),
    ("token_id", re.compile(r"(?<![A-Za-z0-9_])token_id(?![A-Za-z0-9_])")),
    ("venue_token_id", re.compile(r"(?<![A-Za-z0-9_])venue_token_id(?![A-Za-z0-9_])")),
    ("negative_risk", re.compile(r"(?<![A-Za-z0-9_])negative_risk(?![A-Za-z0-9_])")),
    ("shares", re.compile(r"(?<![A-Za-z0-9_])shares(?![A-Za-z0-9_])", re.IGNORECASE)),
)
FORBIDDEN_ACTIVE_PATHS = (
    Path("src/vtrade/polymarket.py"),
    Path("config/experiments/predictionarena-polymarket-v1.json"),
    Path("config/experiments/predictionarena-polymarket-v1-liquidity-aware.json"),
    Path("spec/prompt/predictionarena-polymarket-v1.md"),
    Path("spec/tool-schemas-v1.json"),
    Path("spec/tool-schemas-v1-legacy.json"),
    Path("spec/fixtures/polymarket"),
    Path("tests/test_polymarket.py"),
    Path("scripts/record_polymarket_contracts.py"),
    Path("docs/cycle_analysis"),
)
_SKIPPED_DIRECTORY_NAMES = frozenset({".git", ".venv", "__pycache__", ".uv-cache"})


class ReleaseVerificationError(RuntimeError):
    """Raised when a repository-local release gate fails."""


@dataclass(frozen=True, slots=True)
class LegacyReference:
    path: Path
    line: int
    identifier: str
    text: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    name: str
    status: str
    evidence: str


@dataclass(frozen=True, slots=True)
class ValidationGate:
    name: str
    scope: str
    command: str
    evidence: str


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    checks: tuple[ValidationResult, ...]
    legacy_references: tuple[LegacyReference, ...]
    external_gates: tuple[ValidationGate, ...]

    @property
    def ok(self) -> bool:
        return not self.legacy_references and all(
            result.status == "passed" for result in self.checks
        )


def iter_active_files(root: str | Path = ".") -> Iterator[Path]:
    """Yield only files that can participate in the active release surface."""

    base = Path(root)
    seen: set[Path] = set()
    for relative in (*ACTIVE_SCAN_DIRECTORIES, *ACTIVE_SCAN_FILES):
        candidate = base / relative
        if candidate.is_file():
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield candidate
            continue
        if not candidate.is_dir():
            continue
        for path in sorted(candidate.rglob("*")):
            if not path.is_file() or any(part in _SKIPPED_DIRECTORY_NAMES for part in path.parts):
                continue
            relative_path = path.relative_to(base).as_posix()
            if any(
                relative_path == evidence_root.as_posix()
                or relative_path.startswith(evidence_root.as_posix() + "/")
                for evidence_root in EXTERNAL_EVIDENCE_ROOTS
            ):
                continue
            if (
                relative_path == ARCHIVE_ROOT.as_posix()
                or relative_path.startswith(ARCHIVE_ROOT.as_posix() + "/")
                or relative_path == "docs/agents"
                or relative_path.startswith("docs/agents/")
            ):
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def scan_active_surface(root: str | Path = ".") -> tuple[LegacyReference, ...]:
    """Find forbidden legacy venue vocabulary in active files.

    The three guard files are intentionally allowed to name rejected fields: they
    implement fail-closed deny-lists and stage-payload rejection. The compatibility
    statement may contain one explicitly marked provenance paragraph; every other
    active occurrence is a release failure. Byte-exact provider responses and the
    probe staging directory are external evidence, not active source vocabulary.
    """

    base = Path(root)
    findings: list[LegacyReference] = []
    for path in iter_active_files(base):
        relative = path.relative_to(base)
        if relative in GUARD_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(LegacyReference(relative, 1, "invalid UTF-8", ""))
            continue
        in_controlled_provenance = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            if relative == ARCHIVE_PROVENANCE_PATH:
                if PROVENANCE_MARKER in line:
                    in_controlled_provenance = True
                    continue
                if in_controlled_provenance:
                    if not line.strip():
                        in_controlled_provenance = False
                    else:
                        continue
            for identifier, pattern in FORBIDDEN_ACTIVE_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        LegacyReference(relative, line_number, identifier, line.strip())
                    )
    return tuple(findings)


def assert_zero_active_venue(root: str | Path = ".") -> None:
    findings = scan_active_surface(root)
    if not findings:
        return
    details = "; ".join(
        f"{item.path}:{item.line} [{item.identifier}] {item.text}" for item in findings[:20]
    )
    suffix = "" if len(findings) <= 20 else f"; ... {len(findings) - 20} more"
    raise ReleaseVerificationError(f"active legacy venue references found: {details}{suffix}")


def assert_archive_boundary(root: str | Path = ".") -> None:
    base = Path(root)
    missing = [path for path in (ARCHIVE_README, ARCHIVE_MARKER) if not (base / path).is_file()]
    if missing:
        raise ReleaseVerificationError(
            "historical archive marker/readme is missing: "
            + ", ".join(str(path) for path in missing)
        )
    readme = (base / ARCHIVE_README).read_text(encoding="utf-8").casefold()
    required_phrases = (
        "historical evidence",
        "read-only",
        "not an active",
        "not establish",
        "not imported",
        "not loaded",
    )
    missing_phrases = [phrase for phrase in required_phrases if phrase not in readme]
    if missing_phrases:
        raise ReleaseVerificationError(
            f"archive README is missing boundary language: {', '.join(missing_phrases)}"
        )


def assert_active_paths_are_clean(root: str | Path = ".") -> None:
    base = Path(root)
    existing = [str(path) for path in FORBIDDEN_ACTIVE_PATHS if (base / path).exists()]
    if existing:
        raise ReleaseVerificationError("forbidden active paths remain: " + ", ".join(existing))


def offline_validation_matrix() -> tuple[ValidationGate, ...]:
    """Return the reproducible local gates and their evidence boundaries."""

    return (
        ValidationGate(
            "pytest",
            "offline",
            "uv run --extra dev python -m pytest",
            "unit and contract tests; no provider calls",
        ),
        ValidationGate(
            "ruff",
            "offline",
            "uv run --extra dev python -m ruff check src tests",
            "style, import, and static lint checks",
        ),
        ValidationGate(
            "mypy",
            "offline",
            "uv run --extra dev python -m mypy src/vtrade",
            "strict type checking of the active package",
        ),
        ValidationGate(
            "frozen artifacts and active-venue sweep",
            "offline",
            "uv run --extra dev python -m vtrade.release_verification",
            "canonical hashes, clean migrations, archive boundary, and zero active legacy venue",
        ),
        ValidationGate(
            "compose configuration",
            "offline-shape",
            "docker compose -f compose.coolify.yaml config --quiet",
            "rendered deployment shape only; does not prove resources or startup",
        ),
        ValidationGate(
            "built image imports",
            "built-image",
            "docker build --pull -t vtrade:kalshi-cutover .",
            "image build plus vtrade.api/worker import and frozen-artifact checks",
        ),
    )


def external_validation_matrix() -> tuple[ValidationGate, ...]:
    return (
        ValidationGate(
            "fresh PostgreSQL chain",
            "real-postgresql",
            "VTRADE_RUN_POSTGRES_INTEGRATION=1 uv run --extra dev python -m pytest "
            "tests/test_postgres_*.py",
            "real disposable database applies/reruns 0001-0005, rejects checksum drift, "
            "and exposes latest migration",
        ),
        ValidationGate(
            "private storage readiness",
            "real-storage",
            "uv run --extra dev python -m vtrade.migrate",
            "real private PostgreSQL/object storage resources; local substitutes are not evidence",
        ),
        ValidationGate(
            "French-host public REST probe",
            "french-production-host",
            "uv run --extra dev python scripts/probe_kalshi_public_rest.py",
            "real Kalshi REST reachability, pagination, cutoff, binary book, bounded "
            "concurrency, and raw hashes",
        ),
    )


def verify_release(
    root: str | Path = ".", *, require_ready_fixture: bool = False
) -> ReleaseReport:
    base = Path(root)
    checks: list[ValidationResult] = []
    try:
        assert_active_paths_are_clean(base)
        checks.append(ValidationResult("active paths", "passed", "legacy active files are absent"))
    except ReleaseVerificationError as exc:
        checks.append(ValidationResult("active paths", "failed", str(exc)))
    try:
        assert_archive_boundary(base)
        checks.append(ValidationResult("archive boundary", "passed", str(ARCHIVE_ROOT)))
    except (OSError, ReleaseVerificationError) as exc:
        checks.append(ValidationResult("archive boundary", "failed", str(exc)))
    try:
        sources = load_migration_sources(base / "migrations")
        checks.append(
            ValidationResult(
                "migration chain",
                "passed",
                "ordered canonical migration chain: " + ", ".join(source.name for source in sources),
            )
        )
    except (MigrationError, OSError, ReleaseVerificationError, ValueError) as exc:
        checks.append(ValidationResult("migration chain", "failed", str(exc)))
    findings = scan_active_surface(base)
    checks.append(
        ValidationResult(
            "active venue sweep",
            "passed" if not findings else "failed",
            "no forbidden active references"
            if not findings
            else f"{len(findings)} forbidden active references",
        )
    )
    try:
        manifest = validate_fixture_manifest(
            base / ACTIVE_FIXTURE_MANIFEST,
            require_ready=require_ready_fixture,
        )
        checks.append(
            ValidationResult(
                "fixture manifest",
                "passed",
                f"status={manifest.status}; captures={len(manifest.captures)}",
            )
        )
    except (OSError, ValueError) as exc:
        checks.append(ValidationResult("fixture manifest", "failed", str(exc)))
    if base.resolve() == Path.cwd().resolve():
        try:
            verify_active_artifacts(ACTIVE_EXPERIMENT_CONFIG)
            checks.append(
                ValidationResult("frozen artifacts", "passed", "config and hashes verify")
            )
        except (OSError, ValueError) as exc:
            checks.append(ValidationResult("frozen artifacts", "failed", str(exc)))
    return ReleaseReport(tuple(checks), findings, external_validation_matrix())


def _report_payload(report: ReleaseReport) -> dict[str, object]:
    return {
        "ok": report.ok,
        "checks": [asdict(result) for result in report.checks],
        "legacy_references": [
            {**asdict(item), "path": item.path.as_posix()} for item in report.legacy_references
        ],
        "external_gates": [asdict(gate) for gate in report.external_gates],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Kalshi-only paper release")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--require-ready-fixture", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = verify_release(args.root, require_ready_fixture=args.require_ready_fixture)
    if args.as_json:
        print(json.dumps(_report_payload(report), indent=2, sort_keys=True))
    else:
        for result in report.checks:
            print(f"{result.status.upper():7} {result.name}: {result.evidence}")
        print("External gates are evidence requirements, not claimed by this offline run.")
        for gate in report.external_gates:
            print(f"REQUIRED {gate.name}: {gate.scope}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
