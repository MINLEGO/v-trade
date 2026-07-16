from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vtrade.config import ConfigurationError, config_hash, load_experiment_config


class ConfigTests(unittest.TestCase):
    def test_hash_is_stable_across_key_order(self) -> None:
        self.assertEqual(config_hash({"a": 1, "b": 2}), config_hash({"b": 2, "a": 1}))

    def test_baseline_cannot_run_while_decisions_are_pending(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        self.assertIn("paper_fill_rule", config.pending_decisions)
        self.assertIn("exa_web_search_burst_ceiling", config.pending_decisions)
        self.assertNotIn("experiment_comparison", config.pending_decisions)
        with self.assertRaisesRegex(ConfigurationError, "REQUIRED owner_pending"):
            config.assert_runnable()

    def test_resolved_model_and_scheduling_decisions_are_frozen(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        self.assertEqual(config.raw["schedule"]["mode"], "independent_per_agent")
        self.assertFalse(config.raw["schedule"]["simultaneous_start_required"])
        self.assertEqual(config.raw["retention"]["prompts_transcripts_reasoning_months"], 6)
        self.assertEqual(
            {model["slug"] for model in config.raw["models"]},
            {"deepseek/deepseek-v4-flash", "xiaomi/mimo-v2.5-pro"},
        )
        for model in config.raw["models"]:
            self.assertEqual(model["maximum_quantization_bits"], 8)
            self.assertFalse(model["cross_model_fallback"])
            self.assertIsNone(model["reasoning_effort"])
            self.assertIsNone(model["provider_allowlist"])

    def test_versioned_artifact_hashes_match_files(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        for artifact in config.raw["artifacts"].values():
            body = Path(artifact["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), artifact["sha256"])

    def test_missing_required_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.json"
            source.write_text(json.dumps({"experiment_version": "x"}), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "missing config fields"):
                load_experiment_config(source)


if __name__ == "__main__":
    unittest.main()
