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

    def test_prompt_has_no_unresolved_placeholder(self) -> None:
        body = Path("spec/prompt/predictionarena-polymarket-v1.md").read_text(encoding="utf-8")
        placeholders = re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", body)
        self.assertEqual(placeholders, [])
        stages = ("fundamental outcome", "Research efficiently", "YES", "expected profit", "size")
        for stage in stages:
            self.assertIn(stage, body)

    def test_fixture_manifest_is_explicitly_empty_until_raw_capture(self) -> None:
        manifest = json.loads(Path("spec/fixtures/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest, [])

    def test_agent_cycles_support_independent_schedule_and_retention(self) -> None:
        migration = Path("migrations/0001_foundation.sql").read_text(encoding="utf-8")
        self.assertIn("cohort_cycle_id uuid REFERENCES cohort_cycles(id)", migration)
        self.assertIn("UNIQUE (agent_id, scheduled_at)", migration)
        self.assertGreaterEqual(migration.count("retain_until timestamptz NOT NULL"), 3)


if __name__ == "__main__":
    unittest.main()
