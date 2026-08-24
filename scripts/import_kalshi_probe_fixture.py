"""Import a reviewed credential-free Kalshi probe into the active fixture surface.

The probe manifest is an operational audit record.  The active fixture manifest is
the smaller runtime contract consumed by ``vtrade.fixtures``.  This importer keeps
the response bytes unchanged and derives only the contract metadata from the probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

KALSHI_PUBLIC_ROOT = "https://external-api.kalshi.com/trade-api/v2"
PROBE_SCHEMA_VERSION = "vtrade-kalshi-probe-v1"
FIXTURE_SCHEMA_VERSION = "vtrade-kalshi-fixtures-v1"


class ProbeImportError(ValueError):
    """Raised when a probe result cannot become a reviewed fixture corpus."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeImportError(f"cannot read probe manifest {path}") from exc
    if not isinstance(raw, dict):
        raise ProbeImportError("probe manifest must be a JSON object")
    return raw


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProbeImportError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbeImportError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProbeImportError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbeImportError(f"{field} must be a non-empty string")
    return value.strip()


def _request_target(request_identity: str, path: str) -> str:
    prefix = "GET "
    if not request_identity.startswith(prefix):
        raise ProbeImportError(f"unsupported request identity {request_identity!r}")
    target = request_identity.removeprefix(prefix)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.path != path:
        raise ProbeImportError(
            f"request identity path does not match captured path: {request_identity!r}"
        )
    return target


def _copy_exact(
    source: Path, destination: Path, expected_sha256: str, expected_length: int
) -> None:
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise ProbeImportError(f"missing probe response {source}") from exc
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != expected_length or digest != expected_sha256:
        raise ProbeImportError(f"probe response integrity mismatch for {source}")
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError as exc:
            raise ProbeImportError(f"cannot read existing fixture response {destination}") from exc
        if existing != content:
            raise ProbeImportError(
                "fixture response already exists with different bytes: "
                f"{destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def import_probe(probe_directory: Path, destination: Path) -> Path:
    probe_directory = probe_directory.resolve()
    source_manifest_path = probe_directory / "manifest.json"
    source = _read_object(source_manifest_path)
    if source.get("schema_version") != PROBE_SCHEMA_VERSION:
        raise ProbeImportError("probe manifest schema version is unsupported")
    if source.get("root") != KALSHI_PUBLIC_ROOT:
        raise ProbeImportError("probe manifest root is not the public Kalshi root")
    policy = source.get("request_policy")
    if not isinstance(policy, dict) or any(
        policy.get(field) is not False
        for field in (
            "authenticated",
            "bulk_orderbook",
            "order_submission",
            "vpn_or_proxy",
            "websocket",
        )
    ):
        raise ProbeImportError("probe is not credential-free, public REST-only evidence")
    captured_cutoff = _timestamp(source.get("captured_at"), "captured_at")
    responses = source.get("responses")
    if not isinstance(responses, list) or not responses:
        raise ProbeImportError("probe manifest has no responses")

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    probe_copy = destination / "probe-manifest.json"
    source_bytes = source_manifest_path.read_bytes()
    if probe_copy.exists() and probe_copy.read_bytes() != source_bytes:
        raise ProbeImportError(f"existing probe manifest differs: {probe_copy}")
    if not probe_copy.exists():
        shutil.copyfile(source_manifest_path, probe_copy)

    captures: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(responses):
        if not isinstance(item, dict):
            raise ProbeImportError(f"response {index} must be an object")
        name = _required_string(item.get("label"), f"response {index}.label")
        if name in names:
            raise ProbeImportError(f"duplicate response label {name}")
        names.add(name)
        path = _required_string(item.get("path"), f"response {name}.path")
        request_identity = _required_string(
            item.get("request_identity"), f"response {name}.request_identity"
        )
        target = _request_target(request_identity, path)
        artifact_path = _required_string(
            item.get("artifact_path"), f"response {name}.artifact_path"
        )
        source_response = (probe_directory / artifact_path).resolve()
        if probe_directory not in source_response.parents:
            raise ProbeImportError(f"response {name} escapes the probe directory")
        filename = Path(artifact_path).name
        raw_path = Path("responses") / filename
        expected_sha256 = _required_string(item.get("sha256"), f"response {name}.sha256")
        expected_length = item.get("byte_length")
        if (
            not isinstance(expected_length, int)
            or isinstance(expected_length, bool)
            or expected_length < 0
        ):
            raise ProbeImportError(f"response {name}.byte_length is invalid")
        if item.get("status_code") != 200:
            raise ProbeImportError(f"response {name} is not HTTP 200")
        observed_at = _timestamp(item.get("observed_at"), f"response {name}.observed_at")
        if observed_at > captured_cutoff:
            raise ProbeImportError(f"response {name} was observed after captured_at")
        _copy_exact(
            source_response,
            destination / raw_path,
            expected_sha256,
            expected_length,
        )
        captures.append(
            {
                "name": name,
                "endpoint": KALSHI_PUBLIC_ROOT + target,
                "request_identity": request_identity,
                "raw_path": raw_path.as_posix(),
                "raw_sha256": expected_sha256,
                "raw_byte_length": expected_length,
                "response_status": 200,
                "observed_at": _format_timestamp(observed_at),
                "source_timestamp": None,
                "captured_cutoff": _format_timestamp(captured_cutoff),
            }
        )

    manifest = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "venue": "kalshi",
        "status": "ready",
        "source_root": KALSHI_PUBLIC_ROOT,
        "captures": captures,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe_directory", type=Path)
    parser.add_argument(
        "--destination", type=Path, default=Path("spec/fixtures/kalshi")
    )
    args = parser.parse_args()
    manifest = import_probe(args.probe_directory, args.destination)
    print(f"imported {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
