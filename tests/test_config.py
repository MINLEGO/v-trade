from __future__ import annotations

import json
from pathlib import Path

import pytest

from vtrade.config import ConfigurationError, config_hash, load_experiment_config

ACTIVE_CONFIG = Path("config/experiments/vtrade-kalshi-v1.json")


def test_hash_is_stable_across_key_order() -> None:
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_active_configuration_is_kalshi_paper_only() -> None:
    config = load_experiment_config(ACTIVE_CONFIG)
    assert config.version == "vtrade-kalshi-v1"
    assert config.raw["venue"] == "kalshi"
    assert config.raw["execution_mode"] == "paper_only"
    assert config.pending_decisions == ()
    assert set(config.raw["artifacts"]) == {"prompt", "tool_schemas", "compatibility"}
    assert config.raw["fixtures"]["manifest_path"] == "spec/fixtures/kalshi/manifest.json"


def test_active_configuration_exposes_all_three_model_routes() -> None:
    config = load_experiment_config(ACTIVE_CONFIG)
    models = {model["slug"]: model for model in config.raw["models"]}

    assert set(models) == {
        "deepseek/deepseek-v4-flash-0731",
        "openai/gpt-5.6-luna",
        "z-ai/glm-5.3-flash",
    }
    glm = models["z-ai/glm-5.3-flash"]
    assert glm["label"] == "GLM 5.3 Flash"
    assert glm["maximum_quantization_bits"] == 8
    assert glm["allowed_quantizations"] == ["fp8"]
    assert glm["provider_max_price"] == {
        "prompt": "0.15",
        "completion": "0.5",
        "request": "0",
    }
    assert glm["estimated_max_cost_micros"] == 19_200
    assert glm["provider_order"] == ["z-ai"]


def test_active_configuration_accepts_the_reviewed_fixture_gate() -> None:
    config = load_experiment_config(ACTIVE_CONFIG)
    config.assert_runnable()


def test_non_active_versions_are_rejected(tmp_path: Path) -> None:
    candidate = json.loads(ACTIVE_CONFIG.read_text(encoding="utf-8"))
    candidate["experiment_version"] = "vtrade-kalshi-v0"
    source = tmp_path / "experiment.json"
    source.write_text(json.dumps(candidate), encoding="utf-8", newline="\n")
    with pytest.raises(ConfigurationError, match="only vtrade-kalshi-v1"):
        load_experiment_config(source)


def test_artifact_paths_are_fixed_to_the_active_contract() -> None:
    config = load_experiment_config(ACTIVE_CONFIG)
    assert config.raw["artifacts"]["prompt"]["path"] == "spec/prompt/vtrade-kalshi-v1.md"
    assert (
        config.raw["artifacts"]["tool_schemas"]["path"] == "spec/tool-schemas-vtrade-kalshi-v1.json"
    )
    assert (
        config.raw["artifacts"]["compatibility"]["path"] == "spec/vtrade-kalshi-v1-compatibility.md"
    )
