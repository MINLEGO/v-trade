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
    ToolHandlerError,
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
        "reasoning_effort": "max",
        "reasoning_effort_policy": "owner_fixed",
        "estimated_max_cost_micros": 1,
        "maximum_context_tokens": 100_000,
        "maximum_prompt_tokens": 10_000,
        "maximum_output_tokens": 100,
        "provider_max_price": {"prompt": "0", "completion": "0", "request": "0"},
    }


def response(
    message: dict, *, completion_tokens: int = 1, prompt_tokens: int = 1
) -> bytes:
    return json.dumps(
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"message": message}],
            "usage": {
                "prompt_tokens": prompt_tokens,
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
                "name": "submit_market_order_intent",
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
                            "name": "submit_market_order_intent",
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

    def test_expected_handler_error_is_recorded_but_system_error_propagates(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "get_market_details",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        call = {
            "id": "details-1",
            "function": {"name": "get_market_details", "arguments": "{}"},
        }

        def expected(_arguments):
            raise ToolHandlerError("market is absent from the frozen snapshot")

        expected_harness = BoundedToolHarness(
            RecordedModelGateway(
                (
                    response({"role": "assistant", "tool_calls": [call]}),
                    response({"role": "assistant", "content": "done"}),
                ),
                self.store,
            ),
            (ToolSpec(schema, expected, "market"),),
            limits(),
            monotonic=lambda: 0,
        )
        recorded = expected_harness.run([], model_config=config())
        self.assertFalse(recorded.tool_calls[0].success)
        self.assertEqual(recorded.tool_calls[0].output["error"], "ToolHandlerError")

        def system_failure(_arguments):
            raise RuntimeError("database connection was lost")

        fatal_harness = BoundedToolHarness(
            RecordedModelGateway(
                (response({"role": "assistant", "tool_calls": [call]}),), self.store
            ),
            (ToolSpec(schema, system_failure, "market"),),
            limits(),
            monotonic=lambda: 0,
        )
        with self.assertRaisesRegex(RuntimeError, "database connection"):
            fatal_harness.run([], model_config=config())

    def test_duplicate_tool_call_ids_never_reach_any_handler(self) -> None:
        called = 0

        def handler(_arguments):
            nonlocal called
            called += 1
            return {"ok": True}

        schema = {
            "type": "function",
            "function": {
                "name": "get_balance",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        duplicate = {
            "id": "same-provider-id",
            "function": {"name": "get_balance", "arguments": "{}"},
        }
        harness = BoundedToolHarness(
            RecordedModelGateway(
                (
                    response({"role": "assistant", "tool_calls": [duplicate, duplicate]}),
                    response({"role": "assistant", "content": "done"}),
                ),
                self.store,
            ),
            (ToolSpec(schema, handler, "account"),),
            limits(),
            monotonic=lambda: 0,
        )
        result = harness.run([], model_config=config())
        self.assertEqual(called, 0)
        self.assertEqual(len(result.tool_calls), 2)
        self.assertTrue(all(not record.success for record in result.tool_calls))
        self.assertTrue(
            all("duplicate" in str(record.output) for record in result.tool_calls)
        )

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
            "event_analysis",
            (),
            NOW,
        )
        with self.assertRaises(PermissionError):
            alice.add_belief(belief)
        self.assertEqual(alice.beliefs(), ())
        self.assertEqual(memory.for_agent("bob").beliefs(), ())

    def test_prompt_builder_rejects_cross_agent_memory(self) -> None:
        belief = BeliefRecord(
            str(uuid.uuid4()), "bob", Decimal("0.5"), "x", "event_analysis", (), NOW
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

    def test_bound_memory_replaces_plan_of_same_type(self) -> None:
        memory = PrivateAgentMemory()
        alice = memory.for_agent("alice")
        first = PlanRecord(str(uuid.uuid4()), "alice", PlanType.NEXT_CYCLE, "first", None, NOW)
        second = PlanRecord(str(uuid.uuid4()), "alice", PlanType.NEXT_CYCLE, "second", None, NOW)
        alice.add_plan(first)
        alice.add_plan(second)
        self.assertEqual(alice.plans(), (second,))

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

    def test_assembled_input_limit_after_tool_turn_terminates_cleanly(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        gateway = RecordedModelGateway(
            (
                response(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "inspect-1",
                                "type": "function",
                                "function": {"name": "inspect", "arguments": "{}"},
                            }
                        ],
                    }
                ),
                response({"role": "assistant", "content": "not reached"}),
            ),
            self.store,
        )
        harness = BoundedToolHarness(
            gateway,
            (ToolSpec(schema, lambda _arguments: {"large": "result"}, "market"),),
            limits(maximum_assembled_input_tokens=10),
            monotonic=lambda: 0,
            token_counter=lambda raw: 11 if '"role":"tool"' in raw else 1,
        )

        result = harness.run([], model_config=config())

        self.assertEqual(result.termination_status, "assembled_input_limit")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(len(result.telemetry), 1)
        self.assertEqual(gateway.remaining, 1)

    def test_context_limit_uses_previous_request_not_cumulative_prompt_usage(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        gateway = RecordedModelGateway(
            (
                response(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "inspect-context",
                                "type": "function",
                                "function": {"name": "inspect", "arguments": "{}"},
                            }
                        ],
                    },
                    prompt_tokens=30,
                ),
                response(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "inspect-context-again",
                                "type": "function",
                                "function": {"name": "inspect", "arguments": "{}"},
                            }
                        ],
                    },
                    prompt_tokens=40,
                ),
                response({"role": "assistant", "content": "done"}, prompt_tokens=45),
            ),
            self.store,
        )
        harness = BoundedToolHarness(
            gateway,
            (ToolSpec(schema, lambda _arguments: {"result": "new"}, "market"),),
            limits(maximum_assembled_input_tokens=50),
            monotonic=lambda: 0,
            token_counter=lambda raw: (
                100 if '"initial"' in raw and '"role":"tool"' in raw else 10
            ),
        )

        result = harness.run(
            [{"role": "user", "content": "initial"}], model_config=config()
        )

        self.assertEqual(result.termination_status, "stop")
        self.assertEqual(
            [item.prompt_tokens for item in result.telemetry], [30, 40, 45]
        )
        self.assertEqual(gateway.remaining, 0)

    def test_zero_prompt_usage_falls_back_to_full_context_estimate(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        gateway = RecordedModelGateway(
            (
                response(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "inspect-without-usage",
                                "type": "function",
                                "function": {"name": "inspect", "arguments": "{}"},
                            }
                        ],
                    },
                    prompt_tokens=0,
                ),
                response({"role": "assistant", "content": "must not run"}),
            ),
            self.store,
        )
        harness = BoundedToolHarness(
            gateway,
            (ToolSpec(schema, lambda _arguments: {"result": "new"}, "market"),),
            limits(maximum_assembled_input_tokens=50),
            monotonic=lambda: 0,
            token_counter=lambda raw: (
                100 if '"initial"' in raw and '"role":"tool"' in raw else 10
            ),
        )

        result = harness.run(
            [{"role": "user", "content": "initial"}], model_config=config()
        )

        self.assertEqual(result.termination_status, "assembled_input_limit")
        self.assertEqual(len(result.telemetry), 1)
        self.assertEqual(gateway.remaining, 1)

    def test_default_token_estimate_uses_four_utf8_bytes_per_token(self) -> None:
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "content": "done"}),), self.store
        )
        harness = BoundedToolHarness(
            gateway,
            (),
            limits(maximum_assembled_input_tokens=100),
            monotonic=lambda: 0,
        )

        result = harness.run(
            [{"role": "user", "content": "x" * 100}], model_config=config()
        )

        self.assertEqual(result.termination_status, "stop")
        self.assertEqual(gateway.remaining, 0)

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
