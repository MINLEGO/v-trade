from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vtrade.artifacts import ContentAddressedArtifactStore


class FixtureValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    stable_id: str
    endpoint: str
    checked_at: str
    source_cutoff: str | None
    raw_sha256: str
    raw_byte_length: int
    artifact_path: str
    cycle_count: int
    completeness: str


def extract_cycles(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("cycles"), list):
        raise FixtureValidationError("cycles payload must be an object with a cycles array")
    cycles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["cycles"]:
        if not isinstance(item, dict):
            raise FixtureValidationError("cycle entries must be objects")
        stable_id = item.get("id") or item.get("cycle_id")
        if not isinstance(stable_id, str) or not stable_id:
            raise FixtureValidationError("cycle lacks stable id/id cycle_id")
        if stable_id in seen:
            continue
        seen.add(stable_id)
        cycles.append(item)
    return tuple(cycles)


def should_request_next_page(payload: Any, requested_limit: int) -> bool:
    """Do not trust count or hasMore; a full page is the only continuation signal."""
    return len(extract_cycles(payload)) == requested_limit


def _latest_timestamp(cycles: Iterable[dict[str, Any]]) -> str | None:
    values = [
        value
        for cycle in cycles
        for key in ("created_at", "updated_at", "completed_at")
        if isinstance((value := cycle.get(key)), str)
    ]
    return max(values, default=None)


def ingest_file(
    source: Path, endpoint: str, store: ContentAddressedArtifactStore, checked_at: str
) -> FixtureRecord:
    try:
        with source.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except json.JSONDecodeError as exc:
        raise FixtureValidationError(f"invalid JSON: {exc}") from exc
    cycles = extract_cycles(payload)
    reference = store.put_file(source)
    stable_id = hashlib.sha256((endpoint + ":" + reference.sha256).encode()).hexdigest()
    return FixtureRecord(
        stable_id=stable_id,
        endpoint=endpoint,
        checked_at=checked_at,
        source_cutoff=_latest_timestamp(cycles),
        raw_sha256=reference.sha256,
        raw_byte_length=reference.byte_length,
        artifact_path=reference.relative_path,
        cycle_count=len(cycles),
        completeness="page_complete" if cycles else "empty",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a raw PredictionArena cycles fixture")
    parser.add_argument("source", type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    checked_at = datetime.now(UTC).isoformat()
    record = ingest_file(
        args.source, args.endpoint, ContentAddressedArtifactStore(args.artifact_root), checked_at
    )
    existing: list[dict[str, Any]] = []
    if args.manifest.exists():
        existing = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not any(item.get("stable_id") == record.stable_id for item in existing):
        existing.append(asdict(record))
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
