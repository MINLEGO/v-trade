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

    def test_external_policy_decisions_are_resolved(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        self.assertEqual(config.raw["status"], "ready")
        self.assertEqual(config.pending_decisions, ())
        config.assert_runnable()
        self.assertEqual(
            config.raw["owner_decisions"]["no_bid_valuation"],
            {
                "status": "resolved",
                "required": True,
                "policy": "last_known_bid",
                "maximum_age_seconds": 300,
                "on_missing_or_stale": "block_snapshot_and_scoring",
            },
        )

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
        models = {model["slug"]: model for model in config.raw["models"]}
        for model in models.values():
            self.assertEqual(model["maximum_quantization_bits"], 8)
            self.assertFalse(model["cross_model_fallback"])
            self.assertEqual(model["reasoning_effort"], "max")
            self.assertEqual(model["reasoning_effort_policy"], "owner_fixed")
            self.assertIsNone(model["provider_allowlist"])
            self.assertEqual(model["provider_selection"], "all_compatible_sorted_by_price")
            self.assertTrue(model["allow_provider_fallbacks"])
            self.assertEqual(model["status"], "ready")
        self.assertEqual(
            models["deepseek/deepseek-v4-flash"]["allowed_quantizations"], ["fp8"]
        )
        self.assertEqual(
            models["xiaomi/mimo-v2.5-pro"]["allowed_quantizations"], ["fp8", "unknown"]
        )

    def test_execution_audit_and_exa_limits_are_frozen(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        self.assertEqual(
            config.raw["execution"],
            {
                "paper_policy": "predictionarena_unconditional",
                "buy_fill_price": "best_ask",
                "sell_fill_price": "best_bid",
                "counterparty_required": False,
                "absent_required_quote": "reject",
            },
        )
        self.assertEqual(config.raw["retention"]["visibility"], "operator_only")
        self.assertEqual(
            config.raw["retention"]["redaction_policy"]["always_redact"],
            ["secrets", "tokens", "authorization_headers"],
        )
        self.assertEqual(config.raw["limits"]["maximum_web_searches_per_cycle"], 50)
        self.assertEqual(config.raw["research"]["maximum_searches_per_agent_cycle"], 50)
        self.assertEqual(config.raw["research"]["ceiling_enforcement"], "strict")
        self.assertEqual(config.raw["research"]["maximum_results_per_search"], 10)
        self.assertEqual(
            config.raw["research"]["tavily"],
            {"enabled": False, "policy": "future_only_no_runtime_calls"},
        )
        self.assertEqual(config.raw["research"]["monthly_request_cap"], 18_000)
        self.assertEqual(config.raw["research"]["monthly_credit_cap"], 18_000)
        self.assertTrue(config.raw["research"]["excluded_from_dollar_circuit_breaker"])
        self.assertEqual(
            config.raw["research"]["cost_dollars_semantics"],
            "provider_estimated_nominal_not_actual_billing",
        )
        self.assertEqual(config.raw["research"]["maximum_exa_cost_per_search_micros"], 20_000)
        self.assertEqual(
            config.raw["research"]["maximum_tavily_basic_cost_per_search_micros"], 8_000
        )
        self.assertEqual(
            config.raw["research"]["expected_searches_conditional_on_any_search"], 8
        )
        self.assertEqual(config.raw["research"]["expected_searches_across_all_cycles"], 3.5)
        self.assertEqual(
            config.raw["research"]["expected_searches_classification"],
            "owner_provided_empirical_expectation_not_independently_verified",
        )
        self.assertEqual(config.raw["limits"]["maximum_source_clock_skew_seconds"], 5)
        self.assertEqual(config.raw["limits"]["maximum_archived_bid_age_seconds"], 300)
        self.assertFalse(
            config.raw["owner_decisions"]["tavily_runtime_policy"]["enabled"]
        )

    def test_owner_token_limits_are_frozen(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        limits = config.raw["limits"]
        self.assertEqual(limits["maximum_model_context_tokens"], 100_000)
        self.assertEqual(limits["reserved_model_output_tokens"], 12_000)
        self.assertEqual(limits["maximum_assembled_input_tokens"], 88_000)
        self.assertEqual(limits["maximum_tool_call_argument_tokens"], 4_000)
        self.assertEqual(limits["default_maximum_tool_result_tokens"], 4_000)
        self.assertEqual(limits["get_portfolio_maximum_tool_result_tokens"], 24_000)
        self.assertTrue(limits["get_portfolio_pagination_required_beyond_limit"])
        for model in config.raw["models"]:
            self.assertEqual(model["maximum_context_tokens"], 100_000)
            self.assertEqual(model["maximum_prompt_tokens"], 88_000)
            self.assertEqual(model["maximum_output_tokens"], 12_000)

    def test_openrouter_owner_price_bounds_are_frozen(self) -> None:
        config = load_experiment_config(
            Path("config/experiments/predictionarena-polymarket-v1.json")
        )
        models = {model["slug"]: model for model in config.raw["models"]}
        deepseek = models["deepseek/deepseek-v4-flash"]
        self.assertEqual(deepseek["estimated_max_cost_micros"], 14_000)
        self.assertEqual(
            deepseek["provider_max_price"],
            {"prompt": "0.12", "completion": "0.24", "request": "0"},
        )
        mimo = models["xiaomi/mimo-v2.5-pro"]
        self.assertEqual(mimo["estimated_max_cost_micros"], 50_000)
        self.assertEqual(
            mimo["provider_max_price"],
            {"prompt": "0.44", "completion": "0.88", "request": "0"},
        )
        prices = config.raw["owner_decisions"]["provider_request_cost_estimates"]
        self.assertEqual(prices["status"], "resolved")
        self.assertEqual(
            prices["resolved_research"],
            {
                "exa_maximum_cost_per_search_micros": 20_000,
                "tavily_basic_maximum_cost_per_search_micros": 8_000,
            },
        )

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
