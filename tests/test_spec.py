from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from vtrade.frozen_artifacts import FORBIDDEN_ACTIVE_FIELDS

SCHEMA = Path("spec/tool-schemas-vtrade-kalshi-v1.json")


def test_all_active_tool_schemas_compile_and_are_unique() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(document)
    assert document["schema_version"] == "vtrade-kalshi-tools-v1"
    names = [tool["name"] for tool in document["tools"]]
    assert len(names) == 27
    assert len(set(names)) == 27
    for tool in document["tools"]:
        assert (
            tool["input_schema"].get("additionalProperties") is False
            or "$ref" in tool["input_schema"]
        )
        assert "output_schema" in tool


def test_active_order_contract_is_exact_and_venue_neutral() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    order = next(tool for tool in document["tools"] if tool["name"] == "place_market_order")
    properties = order["input_schema"]["properties"]
    assert set(properties) == {
        "market_ref",
        "outcome",
        "action",
        "amount",
        "amount_type",
        "limit_price_micros",
        "time_in_force",
        "idempotency_key",
    }
    assert properties["outcome"]["enum"] == ["YES", "NO"]
    assert properties["amount_type"]["enum"] == ["CASH", "CONTRACTS"]
    assert properties["time_in_force"]["enum"] == ["IOC", "FOK"]
    assert properties["amount"]["type"] == "string"


def test_portfolio_position_schema_exposes_non_empty_market_question() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    position = document["$defs"]["position"]

    assert position["properties"]["market_question"] == {"type": "string", "minLength": 1}
    assert "market_question" in position["required"]


def test_active_prompt_has_no_unresolved_template_or_legacy_surface() -> None:
    prompt = Path("spec/prompt/vtrade-kalshi-v1.md").read_text(encoding="utf-8").casefold()
    assert "{" not in prompt and "}" not in prompt
    for forbidden in FORBIDDEN_ACTIVE_FIELDS:
        assert forbidden not in prompt


def test_orderbook_and_settlement_schemas_expose_audit_and_finalization_data() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    tools = {tool["name"]: tool for tool in document["tools"]}
    orderbook = tools["get_orderbook"]["output_schema"]["properties"]
    settlement = tools["get_settlements"]["output_schema"]["properties"]["settlements"]
    assert "fee_policy" in orderbook
    assert "audit" in document["$defs"]["book"]["properties"]
    assert settlement["items"]["$ref"] == "#/$defs/settlement"


def test_settlement_schema_exposes_nullable_market_question() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    settlement = document["$defs"]["settlement"]

    assert settlement["properties"]["market_question"] == {
        "type": ["string", "null"],
        "minLength": 1,
    }
    assert settlement["required"][-1] == "market_question"


def test_market_card_schema_exposes_freeze_metrics_and_nullable_missing_data() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    card = document["$defs"]["market_card"]
    properties = card["properties"]

    assert properties["volume_24h_units"]["type"] == ["integer", "null"]
    assert properties["volatility_micros"]["type"] == ["integer", "null"]
    assert properties["volume_trend"]["enum"] == [
        "increasing",
        "decreasing",
        "flat",
        "insufficient_data",
        None,
    ]
    assert properties["competitive_score"]["type"] == ["string", "null"]
    assert "volume_trend_delta" in card["required"]
    assert "indicative_price_micros" in document["$defs"]["outcome_summary"]["required"]
