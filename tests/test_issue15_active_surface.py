from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from vtrade.config import ConfigurationError, load_experiment_config
from vtrade.fixtures import FixtureValidationError, validate_fixture_manifest
from vtrade.frozen_artifacts import verify_active_artifacts
from vtrade.production_tools import ProductionToolRegistry

ACTIVE_CONFIG = Path("config/experiments/vtrade-kalshi-v1.json")
ACTIVE_SCHEMA = Path("spec/tool-schemas-vtrade-kalshi-v1.json")


def test_active_artifacts_are_frozen_and_schema_is_strict() -> None:
    verify_active_artifacts()
    document = json.loads(ACTIVE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(document)
    assert document["schema_version"] == "vtrade-kalshi-tools-v1"
    assert len(document["tools"]) == 27
    assert len({tool["name"] for tool in document["tools"]}) == 27
    active_text = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "spec/prompt/vtrade-kalshi-v1.md",
            "spec/tool-schemas-vtrade-kalshi-v1.json",
        )
    ).casefold()
    for forbidden in (
        "market_id",
        "outcome_id",
        "token_id",
        "venue_token_id",
        "condition_id",
        "negative_risk",
        "shares",
        "polymarket",
    ):
        assert forbidden not in active_text


def test_only_the_kalshi_experiment_can_load_and_external_gate_fails_closed() -> None:
    config = load_experiment_config(ACTIVE_CONFIG)
    assert config.version == "vtrade-kalshi-v1"
    assert config.raw["execution_mode"] == "paper_only"
    with pytest.raises(ConfigurationError, match="reviewed Kalshi fixture capture"):
        config.assert_runnable()

    assert not Path("config/experiments/predictionarena-polymarket-v1.json").exists()
    assert not Path("spec/tool-schemas-v1.json").exists()


def test_fixture_manifest_accepts_pending_repository_state_but_not_composition(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "vtrade-kalshi-fixtures-v1",
                "venue": "kalshi",
                "status": "owner_pending",
                "source_root": "https://external-api.kalshi.com/trade-api/v2",
                "captures": [],
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    assert validate_fixture_manifest(manifest).status == "owner_pending"
    with pytest.raises(FixtureValidationError, match="reviewed Kalshi fixture capture"):
        validate_fixture_manifest(manifest, require_ready=True)


def test_registry_exposes_exactly_the_frozen_27_tools() -> None:
    context = SimpleNamespace(
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {"items": [], "next_cursor": None, "has_more": False},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]
    specs = registry.tool_specs()
    assert len(specs) == 27
    assert {spec.name for spec in specs} == {
        tool["name"] for tool in json.loads(ACTIVE_SCHEMA.read_text(encoding="utf-8"))["tools"]
    }


def test_ready_fixture_manifest_validates_exact_raw_bytes(tmp_path: Path) -> None:
    raw = b'{"markets":[]}'
    response = tmp_path / "responses" / "markets.json"
    response.parent.mkdir()
    response.write_bytes(raw)
    manifest = tmp_path / "manifest.json"
    import hashlib

    manifest.write_text(
        json.dumps(
            {
                "schema_version": "vtrade-kalshi-fixtures-v1",
                "venue": "kalshi",
                "status": "ready",
                "source_root": "https://external-api.kalshi.com/trade-api/v2",
                "captures": [
                    {
                        "name": "markets",
                        "endpoint": "https://external-api.kalshi.com/trade-api/v2/markets",
                        "request_identity": "GET /markets?status=open",
                        "raw_path": "responses/markets.json",
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "raw_byte_length": len(raw),
                        "response_status": 200,
                        "observed_at": "2026-08-21T10:00:00Z",
                        "source_timestamp": None,
                        "captured_cutoff": "2026-08-21T10:00:00Z",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    result = validate_fixture_manifest(manifest, require_ready=True)
    assert result.ready
    assert result.captures[0].raw_byte_length == len(raw)
