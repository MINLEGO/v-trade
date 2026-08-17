from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


class SpecificationTests(unittest.TestCase):
    def test_all_tool_schemas_compile_and_have_output_contracts(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        shared_defs = document["$defs"]
        self.assertEqual(document["schema_version"], "predictionarena-tools-v1")
        self.assertEqual(len(document["tools"]), 27)
        for tool in document["tools"]:
            with self.subTest(name=tool["name"]):
                self.assertIn("input_schema", tool)
                self.assertIn("output_schema", tool)
                for schema_name in ("input_schema", "output_schema"):
                    schema = dict(tool[schema_name])
                    schema["$defs"] = {
                        **shared_defs,
                        **schema.get("$defs", {}),
                    }
                    Draft202012Validator.check_schema(schema)
                self.assertNotEqual(
                    tool["output_schema"],
                    {"type": "object", "additionalProperties": True},
                )

    def test_exactly_27_unique_tool_schemas(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        names = [tool["name"] for tool in document["tools"]]
        self.assertEqual(len(names), 27)
        self.assertEqual(len(set(names)), 27)

    def test_plan_tools_limit_content_to_4000_characters(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tools = {tool["name"]: tool for tool in document["tools"]}
        for name in ("create_long_term_plan", "create_next_cycle_plan"):
            with self.subTest(name=name):
                content = tools[name]["input_schema"]["properties"]["plan_content"]
                self.assertEqual(content["minLength"], 1)
                self.assertEqual(content["maxLength"], 4_000)

    def test_active_order_output_keeps_private_liquidity_internal(self) -> None:
        active = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        legacy = json.loads(
            Path("spec/tool-schemas-v1-legacy.json").read_text(encoding="utf-8")
        )
        active_order = next(row for row in active["tools"] if row["name"] == "place_market_order")
        legacy_order = next(row for row in legacy["tools"] if row["name"] == "place_market_order")

        self.assertNotIn("virtual_liquidity", json.dumps(active_order))
        self.assertIn("virtual_liquidity", json.dumps(legacy_order))

    def test_paginated_tools_expose_their_cursor_contract(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tools = {tool["name"]: tool for tool in document["tools"]}
        self.assertEqual(tools["get_balance"]["input_schema"]["properties"], {})
        portfolio = tools["get_portfolio"]
        self.assertEqual(set(portfolio["input_schema"]["properties"]), {"cursor", "limit"})
        self.assertEqual(portfolio["input_schema"]["properties"]["limit"]["maximum"], 200)
        self.assertEqual(
            set(portfolio["output_schema"]["required"]),
            {"items", "next_cursor", "has_more"},
        )
        for name in ("get_general_beliefs", "search_general_beliefs"):
            properties = tools[name]["input_schema"]["properties"]
            self.assertIn("cursor", properties)
            self.assertEqual(properties["include_inactive"]["default"], False)
            self.assertEqual(properties["limit"]["maximum"], 100)
            self.assertEqual(properties["limit"]["default"], 100)

    def test_position_outputs_expose_buy_fees_without_redefining_gross_costs(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tools = {tool["name"]: tool for tool in document["tools"]}
        portfolio_item = tools["get_portfolio"]["output_schema"]["properties"]["items"][
            "items"
        ]
        self.assertEqual(
            portfolio_item["properties"]["entry_fees_micros"], {"type": "integer"}
        )
        self.assertIn("entry_fees_micros", portfolio_item["required"])
        affected = document["$defs"]["portfolio_after"]["properties"]["affected_position"]
        self.assertIn("entry_fees_micros", affected["required"])

    def test_orderbook_exposes_nullable_fee_policy_contract(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        orderbook = next(row for row in document["tools"] if row["name"] == "get_orderbook")
        output = orderbook["output_schema"]
        self.assertIn("fee_policy", output["properties"])
        self.assertIn("fee_policy", output["required"])
        self.assertEqual(
            output["properties"]["fee_policy"]["anyOf"][1], {"type": "null"}
        )
        self.assertEqual(
            document["$defs"]["fee_policy"]["properties"]["formula_version"]["const"],
            "polymarket-v2-p-one-minus-p",
        )

    def test_keyword_searches_accept_strings_or_keyword_arrays(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tools = {tool["name"]: tool for tool in document["tools"]}
        for name, key in (
            ("discover_events", "keyword"),
            ("search_tags", "query"),
            ("search_general_beliefs", "keyword"),
        ):
            with self.subTest(name=name):
                property_schema = tools[name]["input_schema"]["properties"][key]
                self.assertEqual(property_schema["type"], ["string", "array"])
                self.assertEqual(property_schema["items"]["type"], "string")
                self.assertEqual(property_schema["items"]["minLength"], 1)
                self.assertEqual(property_schema["minItems"], 1)

    def test_web_search_exposes_bounded_highlights_and_published_date_options(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tool = next(row for row in document["tools"] if row["name"] == "web_search")
        properties = tool["input_schema"]["properties"]
        self.assertEqual(
            set(properties),
            {
                "query",
                "max_highlight_length",
                "num_results",
                "start_published_date",
                "end_published_date",
            },
        )
        self.assertEqual(properties["max_highlight_length"]["default"], 1500)
        self.assertEqual(properties["num_results"]["default"], 10)
        self.assertEqual(properties["num_results"]["maximum"], 10)
        self.assertEqual(properties["start_published_date"]["default"], 30)
        self.assertEqual(properties["end_published_date"]["default"], 0)
        output = tool["output_schema"]
        self.assertFalse(output["additionalProperties"])
        self.assertFalse(output["properties"]["results"]["items"]["additionalProperties"])

    def test_fetch_webpage_exposes_bounded_content_modes(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tool = next(row for row in document["tools"] if row["name"] == "fetch_webpage")
        properties = tool["input_schema"]["properties"]
        self.assertEqual(
            set(properties), {"url", "result_type", "highlight_query", "max_length"}
        )
        self.assertEqual(tool["input_schema"]["required"], ["url"])
        self.assertEqual(properties["result_type"]["enum"], ["full_text", "highlights"])
        self.assertEqual(properties["result_type"]["default"], "highlights")
        self.assertEqual(properties["highlight_query"]["default"], None)
        self.assertEqual(properties["max_length"]["default"], 4000)
        self.assertEqual(properties["max_length"]["maximum"], 12000)
        output = tool["output_schema"]
        self.assertEqual(len(output["oneOf"]), 2)
        self.assertTrue(all(not branch["additionalProperties"] for branch in output["oneOf"]))
        self.assertIn("full_text", output["oneOf"][0]["required"])
        self.assertIn("highlights", output["oneOf"][1]["required"])

    def test_prompt_has_no_unresolved_placeholder(self) -> None:
        body = Path("spec/prompt/predictionarena-polymarket-v1.md").read_text(encoding="utf-8")
        placeholders = re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", body)
        self.assertEqual(placeholders, [])
        stages = (
            "data_cutoff",
            "recent activity",
            "since_last_cycle",
            "since_last_cycle_truncated",
            "summary_24h",
            "YES/NO",
            "expected value",
            "execution constraints",
            "unrelated theses",
        )
        for stage in stages:
            self.assertIn(stage, body)

    def test_belief_schema_uses_confidence_and_fixed_categories(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        tools = {tool["name"]: tool for tool in document["tools"]}
        expected = [
            "event_analysis",
            "trading_strategy",
            "market_sentiment",
            "market_structure",
            "risk_assessment",
        ]
        belief = tools["create_general_belief"]["input_schema"]
        self.assertIn("confidence", belief["properties"])
        self.assertNotIn("probability", belief["properties"])
        self.assertEqual(belief["properties"]["confidence"]["minimum"], 0)
        self.assertEqual(belief["properties"]["confidence"]["maximum"], 1)
        self.assertEqual(belief["properties"]["category"]["enum"], expected)
        self.assertEqual(belief["properties"]["evidence"]["type"], "array")
        self.assertEqual(belief["properties"]["evidence"]["items"]["type"], "string")
        self.assertNotIn("evidence", belief["required"])

    def test_fixture_manifest_records_owner_approved_raw_capture(self) -> None:
        manifest = json.loads(Path("spec/fixtures/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), 1)
        fixture = manifest[0]
        self.assertEqual(fixture["cycle_count"], 200)
        self.assertEqual(fixture["raw_byte_length"], 20_313_102)
        self.assertEqual(
            fixture["raw_sha256"],
            "2362521d0597263e882c397ab8ef456f64af2cb373ed1888319d157d3b18f2f2",
        )
        self.assertEqual(fixture["completeness"], "page_complete")

    def test_agent_cycles_support_independent_schedule_and_retention(self) -> None:
        migration = Path("migrations/0001_foundation.sql").read_text(encoding="utf-8")
        self.assertIn("cohort_cycle_id uuid REFERENCES cohort_cycles(id)", migration)
        self.assertIn("UNIQUE (agent_id, scheduled_at)", migration)
        self.assertGreaterEqual(migration.count("retain_until timestamptz NOT NULL"), 3)


if __name__ == "__main__":
    unittest.main()
