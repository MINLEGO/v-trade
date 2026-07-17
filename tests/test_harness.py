from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from vtrade.artifacts import ContentAddressedArtifactStore
from vtrade.harness import (
    BeliefRecord,
    BoundedToolHarness,
    HarnessLimitExceeded,
    HarnessLimits,
    LearningEvent,
    PlanRecord,
    PlanType,
    PrivateAgentMemory,
    PromptBuilder,
    ToolSpec,
    deterministic_critical_learning,
)
from vtrade.providers import RecordedModelGateway

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)


def config() -> dict:
    return {
        "slug": "deepseek/deepseek-v4-flash",
        "allowed_quantizations": ["fp8"],
        "provider_allowlist": None,
        "provider_selection": "all_compatible_sorted_by_price",
        "allow_provider_fallbacks": True,
        "cross_model_fallback": False,
        "estimated_max_cost_micros": 1,
        "maximum_context_tokens": 100_000,
        "maximum_prompt_tokens": 10_000,
        "maximum_output_tokens": 100,
        "provider_max_price": {"prompt": "0", "completion": "0", "request": "0"},
    }


def response(message: dict, *, completion_tokens: int = 1) -> bytes:
    return json.dumps(
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"message": message}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": completion_tokens,
                "cost": 0,
            },
        }
    ).encode()


def limits(**overrides) -> HarnessLimits:
    values = {
        "maximum_model_turns": 32,
        "maximum_total_tool_calls": 100,
        "maximum_web_searches": 50,
        "maximum_wall_clock_seconds": 30,
        "maximum_context_tokens": 100_000,
        "maximum_assembled_input_tokens": 88_000,
        "maximum_model_output_tokens": 12_000,
        "maximum_tool_call_arguments_tokens": 4_000,
        "maximum_default_tool_result_tokens": 4_000,
        "maximum_get_portfolio_result_tokens": 24_000,
    }
    values.update(overrides)
    return HarnessLimits(**values)


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ContentAddressedArtifactStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_malformed_financial_call_cannot_reach_handler(self) -> None:
        called = 0

        def financial(_arguments):
            nonlocal called
            called += 1
            return {"placed": True}

        schema = {
            "type": "function",
            "function": {
                "name": "place_market_order",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["shares"],
                    "properties": {
                        "shares": {"type": "integer", "minimum": 1, "maximum": 100}
                    },
                },
            },
        }
        first = response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "bad",
                        "function": {
                            "name": "place_market_order",
                            "arguments": json.dumps({"shares": "all"}),
                        },
                    }
                ],
            }
        )
        second = response({"role": "assistant", "content": "done"})
        harness = BoundedToolHarness(
            RecordedModelGateway((first, second), self.store),
            (ToolSpec(schema, financial, "trading", mutates_financial_state=True),),
            limits(),
            monotonic=lambda: 0,
        )
        result = harness.run([{"role": "user", "content": "go"}], model_config=config())
        self.assertEqual(called, 0)
        self.assertFalse(result.tool_calls[0].success)

    def test_web_search_batch_above_strict_fifty_is_rejected_before_execution(self) -> None:
        called = 0

        def search(_arguments):
            nonlocal called
            called += 1
            return {"results": []}

        schema = {
            "type": "function",
            "function": {
                "name": "web_search",
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
        calls = [
            {
                "id": str(index),
                "function": {"name": "web_search", "arguments": '{"query":"x"}'},
            }
            for index in range(51)
        ]
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "tool_calls": calls}),), self.store
        )
        harness = BoundedToolHarness(
            gateway,
            (ToolSpec(schema, search, "research"),),
            limits(),
            monotonic=lambda: 0,
        )
        with self.assertRaises(HarnessLimitExceeded):
            harness.run([], model_config=config())
        self.assertEqual(called, 0)

    def test_agent_bound_memory_view_cannot_write_cross_agent_records(self) -> None:
        memory = PrivateAgentMemory()
        alice = memory.for_agent("alice")
        belief = BeliefRecord(
            str(uuid.uuid4()),
            "bob",
            Decimal("0.5"),
            "private",
            "test",
            (),
            NOW,
        )
        with self.assertRaises(PermissionError):
            alice.add_belief(belief)
        self.assertEqual(alice.beliefs(), ())
        self.assertEqual(memory.for_agent("bob").beliefs(), ())

    def test_prompt_builder_rejects_cross_agent_memory(self) -> None:
        belief = BeliefRecord(
            str(uuid.uuid4()), "bob", Decimal("0.5"), "x", "test", (), NOW
        )
        with self.assertRaises(PermissionError):
            PromptBuilder("system").build(
                agent_id="alice",
                cycle_context={},
                beliefs=(belief,),
                plans=(),
                critical_learning="none",
            )

    def test_critical_learning_is_deterministic_across_input_order(self) -> None:
        events = (
            LearningEvent("rejection", "m2"),
            LearningEvent("settlement", "m1", 100),
            LearningEvent("drawdown", "portfolio", -50),
        )
        self.assertEqual(
            deterministic_critical_learning(events),
            deterministic_critical_learning(tuple(reversed(events))),
        )

    def test_plan_records_are_private_in_bound_view(self) -> None:
        memory = PrivateAgentMemory()
        alice = memory.for_agent("alice")
        plan = PlanRecord(str(uuid.uuid4()), "alice", PlanType.NEXT_CYCLE, "research", None, NOW)
        alice.add_plan(plan)
        self.assertEqual(alice.plans(), (plan,))
        self.assertEqual(memory.for_agent("bob").plans(), ())

    def test_assembled_input_and_reserved_output_are_checked_before_gateway_call(self) -> None:
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "content": "not reached"}),), self.store
        )
        harness = BoundedToolHarness(
            gateway,
            (),
            limits(maximum_assembled_input_tokens=10),
            monotonic=lambda: 0,
            token_counter=len,
        )
        with self.assertRaisesRegex(HarnessLimitExceeded, "assembled input"):
            harness.run([{"role": "user", "content": "too large"}], model_config=config())
        self.assertEqual(gateway.remaining, 1)

        oversized_output = config()
        oversized_output["maximum_output_tokens"] = 12_001
        output_harness = BoundedToolHarness(
            gateway,
            (),
            limits(),
            monotonic=lambda: 0,
            token_counter=lambda _raw: 1,
        )
        with self.assertRaisesRegex(HarnessLimitExceeded, "reserved output"):
            output_harness.run([], model_config=oversized_output)
        self.assertEqual(gateway.remaining, 1)

    def test_tool_arguments_over_four_thousand_tokens_do_not_reach_handler(self) -> None:
        called = 0

        def handler(_arguments):
            nonlocal called
            called += 1
            return {"ok": True}

        schema = {
            "type": "function",
            "function": {
                "name": "web_search",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }
        first = response(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "large-arguments",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": "x" * 4_001}),
                        },
                    }
                ],
            }
        )
        gateway = RecordedModelGateway(
            (first, response({"role": "assistant", "content": "done"})), self.store
        )
        harness = BoundedToolHarness(
            gateway,
            (ToolSpec(schema, handler, "research"),),
            limits(),
            monotonic=lambda: 0,
            token_counter=len,
        )
        result = harness.run([], model_config=config())
        self.assertEqual(called, 0)
        self.assertFalse(result.tool_calls[0].success)
        self.assertIn("arguments", str(result.tool_calls[0].output))

    def test_default_and_portfolio_result_limits_are_distinct_and_paginated(self) -> None:
        def oversized(_arguments):
            return {"data": "x" * 4_001}

        def oversized_portfolio(_arguments):
            return {"positions": "x" * 24_001}

        def schema(name):
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }

        calls = [
            {
                "id": "default",
                "function": {"name": "get_balance", "arguments": "{}"},
            },
            {
                "id": "portfolio",
                "function": {"name": "get_portfolio", "arguments": "{}"},
            },
        ]
        gateway = RecordedModelGateway(
            (
                response({"role": "assistant", "tool_calls": calls}),
                response({"role": "assistant", "content": "done"}),
            ),
            self.store,
        )
        harness = BoundedToolHarness(
            gateway,
            (
                ToolSpec(schema("get_balance"), oversized, "account"),
                ToolSpec(schema("get_portfolio"), oversized_portfolio, "account"),
            ),
            limits(),
            monotonic=lambda: 0,
            token_counter=len,
        )
        result = harness.run([], model_config=config())
        self.assertEqual([call.success for call in result.tool_calls], [False, False])
        self.assertIn("token ceiling", str(result.tool_calls[0].output))
        self.assertIn("must paginate", str(result.tool_calls[1].output))


if __name__ == "__main__":
    unittest.main()
