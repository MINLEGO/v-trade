from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


class SpecificationTests(unittest.TestCase):
    def test_exactly_29_unique_tool_schemas(self) -> None:
        document = json.loads(Path("spec/tool-schemas-v1.json").read_text(encoding="utf-8"))
        names = [tool["name"] for tool in document["tools"]]
        self.assertEqual(len(names), 29)
        self.assertEqual(len(set(names)), 29)

    def test_only_portfolio_owns_the_frozen_pagination_contract(self) -> None:
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

    def test_prompt_has_no_unresolved_placeholder(self) -> None:
        body = Path("spec/prompt/predictionarena-polymarket-v1.md").read_text(encoding="utf-8")
        placeholders = re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", body)
        self.assertEqual(placeholders, [])
        stages = ("fundamental outcome", "Research efficiently", "YES", "expected profit", "size")
        for stage in stages:
            self.assertIn(stage, body)

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
