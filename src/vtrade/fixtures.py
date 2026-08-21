from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KALSHI_PUBLIC_ROOT = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_FIXTURE_SCHEMA_VERSION = "vtrade-kalshi-fixtures-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FixtureValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KalshiFixtureCapture:
    name: str
    endpoint: str
    request_identity: str
    raw_path: Path
    raw_sha256: str
    raw_byte_length: int
    response_status: int
    observed_at: datetime
    source_timestamp: datetime | None
    captured_cutoff: datetime


@dataclass(frozen=True, slots=True)
class KalshiFixtureManifest:
    path: Path
    schema_version: str
    venue: str
    status: str
    source_root: str
    captures: tuple[KalshiFixtureCapture, ...]

    @property
    def ready(self) -> bool:
        return self.status == "ready" and bool(self.captures)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def validate_fixture_manifest(
    path: str | Path = "spec/fixtures/kalshi/manifest.json", *, require_ready: bool = False
) -> KalshiFixtureManifest:
    source = Path(path)
    try:
        raw_bytes = source.read_bytes()
        if b"\r" in raw_bytes:
            raise FixtureValidationError("fixture manifest must use LF line endings")
        raw = json.loads(raw_bytes.decode("utf-8"))
    except FixtureValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureValidationError(f"cannot read Kalshi fixture manifest {source}") from exc
    if not isinstance(raw, Mapping):
        raise FixtureValidationError("fixture manifest must be an object")
    if raw.get("schema_version") != KALSHI_FIXTURE_SCHEMA_VERSION:
        raise FixtureValidationError("fixture manifest schema version is unsupported")
    if raw.get("venue") != "kalshi":
        raise FixtureValidationError("fixture manifest venue must be kalshi")
    status = raw.get("status")
    if status not in {"owner_pending", "ready"}:
        raise FixtureValidationError("fixture manifest status must be owner_pending or ready")
    source_root = raw.get("source_root")
    if source_root != KALSHI_PUBLIC_ROOT:
        raise FixtureValidationError("fixture manifest source root is not the public Kalshi root")
    captures_value = raw.get("captures")
    if not isinstance(captures_value, list):
        raise FixtureValidationError("fixture manifest captures must be an array")
    captures: list[KalshiFixtureCapture] = []
    seen_names: set[str] = set()
    for index, value in enumerate(captures_value):
        captures.append(_capture(source, value, index, seen_names))
    if status == "ready" and not captures:
        raise FixtureValidationError("ready fixture manifest has no reviewed captures")
    if require_ready and status != "ready":
        raise FixtureValidationError(
            "reviewed Kalshi fixture capture is required before composition"
        )
    if require_ready and not captures:
        raise FixtureValidationError("Kalshi fixture manifest has no external capture")
    return KalshiFixtureManifest(
        path=source,
        schema_version=str(raw["schema_version"]),
        venue=str(raw["venue"]),
        status=str(status),
        source_root=str(source_root),
        captures=tuple(captures),
    )


def require_kalshi_fixture_manifest(
    path: str | Path = "spec/fixtures/kalshi/manifest.json",
) -> KalshiFixtureManifest:
    return validate_fixture_manifest(path, require_ready=True)


def fixture_manifest_sha256(path: str | Path = "spec/fixtures/kalshi/manifest.json") -> str:
    manifest = Path(path)
    raw = manifest.read_bytes()
    if b"\r" in raw:
        raise FixtureValidationError("fixture manifest must use LF line endings")
    raw.decode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _capture(
    manifest_path: Path,
    value: object,
    index: int,
    seen_names: set[str],
) -> KalshiFixtureCapture:
    if not isinstance(value, Mapping):
        raise FixtureValidationError(f"capture {index} must be an object")
    name = _required_string(value, "name", index)
    if name in seen_names:
        raise FixtureValidationError(f"duplicate fixture capture name {name}")
    seen_names.add(name)
    endpoint = _required_string(value, "endpoint", index)
    if not endpoint.startswith(KALSHI_PUBLIC_ROOT):
        raise FixtureValidationError(f"capture {name} endpoint is outside the public root")
    request_identity = _required_string(value, "request_identity", index)
    raw_path_value = _required_string(value, "raw_path", index)
    raw_path = (manifest_path.parent / raw_path_value).resolve()
    parent = manifest_path.parent.resolve()
    if parent not in raw_path.parents:
        raise FixtureValidationError(f"capture {name} raw_path escapes the fixture directory")
    digest = _required_string(value, "raw_sha256", index)
    if not _SHA256.fullmatch(digest):
        raise FixtureValidationError(f"capture {name} raw_sha256 is malformed")
    byte_length = value.get("raw_byte_length")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
        raise FixtureValidationError(f"capture {name} raw_byte_length is invalid")
    response_status = value.get("response_status")
    if response_status != 200:
        raise FixtureValidationError(f"capture {name} must record an HTTP 200 response")
    observed_at = _timestamp(value.get("observed_at"), f"capture {name}.observed_at")
    cutoff = _timestamp(value.get("captured_cutoff"), f"capture {name}.captured_cutoff")
    source_timestamp_value = value.get("source_timestamp")
    source_timestamp = (
        None
        if source_timestamp_value is None
        else _timestamp(source_timestamp_value, f"capture {name}.source_timestamp")
    )
    if observed_at > cutoff:
        raise FixtureValidationError(f"capture {name} was observed after its cutoff")
    if source_timestamp is not None and source_timestamp > cutoff:
        raise FixtureValidationError(f"capture {name} has source data newer than its cutoff")
    try:
        actual_bytes = raw_path.read_bytes()
    except OSError as exc:
        raise FixtureValidationError(f"capture {name} raw response is missing") from exc
    if len(actual_bytes) != byte_length:
        raise FixtureValidationError(f"capture {name} raw byte length does not match the manifest")
    if hashlib.sha256(actual_bytes).hexdigest() != digest:
        raise FixtureValidationError(f"capture {name} raw SHA-256 does not match the manifest")
    return KalshiFixtureCapture(
        name=name,
        endpoint=endpoint,
        request_identity=request_identity,
        raw_path=raw_path,
        raw_sha256=digest,
        raw_byte_length=byte_length,
        response_status=response_status,
        observed_at=observed_at,
        source_timestamp=source_timestamp,
        captured_cutoff=cutoff,
    )


def _required_string(value: Mapping[str, object], key: str, index: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise FixtureValidationError(f"capture {index} requires a non-empty {key}")
    return item.strip()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise FixtureValidationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FixtureValidationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FixtureValidationError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the reviewed Kalshi fixture manifest")
    parser.add_argument("manifest", nargs="?", default="spec/fixtures/kalshi/manifest.json")
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    try:
        manifest = validate_fixture_manifest(args.manifest, require_ready=args.require_ready)
    except FixtureValidationError as exc:
        parser.error(str(exc))
    print(
        f"{manifest.path}: status={manifest.status} "
        f"captures={len(manifest.captures)} sha256={manifest.sha256}"
    )


if __name__ == "__main__":
    main()
