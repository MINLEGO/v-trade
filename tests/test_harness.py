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
    PlanRecord,
    PlanType,
    PrivateAgentMemory,
    PromptBuilder,
    RecentActivityEvent,
    ToolExecution,
    ToolHandlerError,
    ToolOutputContractError,
    ToolSpec,
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

    def test_schema_valid_input_and_output_are_recorded_unchanged(self) -> None:
        called: list[dict[str, object]] = []
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value"],
                    "properties": {"value": {"type": "integer", "minimum": 1}},
                },
            },
        }
        output_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }

        def handler(arguments: dict[str, object]) -> dict[str, object]:
            called.append(arguments)
            return {"ok": True}

        call = {
            "id": "inspect-valid",
            "function": {"name": "inspect", "arguments": '{"value": 2}'},
        }
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "tool_calls": [call]}),
             response({"role": "assistant", "content": "done"})),
            self.store,
        )
        harness = BoundedToolHarness(
            gateway,
            (ToolSpec(schema, handler, "market", output_schema=output_schema),),
            limits(),
            monotonic=lambda: 0,
        )

        result = harness.run([], model_config=config())

        self.assertEqual(called, [{"value": 2}])
        self.assertTrue(result.tool_calls[0].success)
        self.assertEqual(result.tool_calls[0].output, {"ok": True})

    def test_invalid_output_is_fatal_and_does_not_continue_the_cycle(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {"type": "object", "additionalProperties": False},
            },
        }
        output_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        call = {
            "id": "inspect-invalid-output",
            "function": {"name": "inspect", "arguments": "{}"},
        }
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "tool_calls": [call]}),
             response({"role": "assistant", "content": "must not run"})),
            self.store,
        )
        harness = BoundedToolHarness(
            gateway,
            (
                ToolSpec(
                    schema,
                    lambda _arguments: {"ok": "wrong"},
                    "market",
                    output_schema=output_schema,
                ),
            ),
            limits(),
            monotonic=lambda: 0,
        )

        with self.assertRaisesRegex(ToolOutputContractError, "inspect returned invalid output: ok"):
            harness.run([], model_config=config())
        self.assertEqual(gateway.remaining, 1)

    def test_tool_execution_output_uses_the_same_contract_validation(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {"type": "object", "additionalProperties": False},
            },
        }
        output_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        call = {
            "id": "inspect-tool-execution",
            "function": {"name": "inspect", "arguments": "{}"},
        }
        harness = BoundedToolHarness(
            RecordedModelGateway(
                (response({"role": "assistant", "tool_calls": [call]}),), self.store
            ),
            (
                ToolSpec(
                    schema,
                    lambda _arguments: ToolExecution({"ok": "wrong"}),
                    "market",
                    output_schema=output_schema,
                ),
            ),
            limits(),
            monotonic=lambda: 0,
        )

        with self.assertRaises(ToolOutputContractError):
            harness.run([], model_config=config())

    def test_invalid_schema_fails_before_gateway_use(self) -> None:
        schema = {
            "type": "function",
            "function": {
                "name": "inspect",
                "parameters": {"type": "not-a-json-schema-type"},
            },
        }
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "content": "not reached"}),), self.store
        )

        with self.assertRaisesRegex(ValueError, "invalid schema for tool inspect"):
            BoundedToolHarness(
                gateway,
                (ToolSpec(schema, lambda _arguments: {}, "market"),),
                limits(),
                monotonic=lambda: 0,
            )
        self.assertEqual(gateway.remaining, 1)

    def test_format_checker_rejects_invalid_dates_datetimes_and_uuids(self) -> None:
        seen: list[dict[str, object]] = []
        schema = {
            "type": "function",
            "function": {
                "name": "dated",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["date", "timestamp", "identifier"],
                    "properties": {
                        "date": {"type": "string", "format": "date"},
                        "timestamp": {"type": "string", "format": "date-time"},
                        "identifier": {"type": "string", "format": "uuid"},
                    },
                },
            },
        }
        valid = {
            "date": "2026-07-16",
            "timestamp": "2026-07-16T15:00:00Z",
            "identifier": str(uuid.uuid4()),
        }
        invalid = {
            "date": "2026-02-30",
            "timestamp": "not-a-date-time",
            "identifier": "not-a-uuid",
        }
        calls = [
            {"id": "dated-valid", "function": {"name": "dated", "arguments": json.dumps(valid)}},
            {
                "id": "dated-invalid",
                "function": {"name": "dated", "arguments": json.dumps(invalid)},
            },
        ]
        harness = BoundedToolHarness(
            RecordedModelGateway(
                (
                    response({"role": "assistant", "tool_calls": calls}),
                    response({"role": "assistant", "content": "done"}),
                ),
                self.store,
            ),
            (ToolSpec(schema, lambda arguments: seen.append(arguments) or {"ok": True}, "market"),),
            limits(),
            monotonic=lambda: 0,
        )

        result = harness.run([], model_config=config())

        self.assertEqual(seen, [valid])
        self.assertTrue(result.tool_calls[0].success)
        self.assertFalse(result.tool_calls[1].success)

    def test_union_type_argument_schema_accepts_string_and_array(self) -> None:
        seen: list[dict[str, object]] = []

        def handler(arguments: dict[str, object]) -> dict[str, object]:
            seen.append(arguments)
            return {"ok": True}

        schema = {
            "type": "function",
            "function": {
                "name": "search",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "keyword": {
                            "type": ["string", "array"],
                            "items": {"type": "string"},
                        }
                    },
                },
            },
        }
        calls = [
            {
                "id": "string-keyword",
                "function": {"name": "search", "arguments": json.dumps({"keyword": "alpha"})},
            },
            {
                "id": "array-keyword",
                "function": {
                    "name": "search",
                    "arguments": json.dumps({"keyword": ["alpha", "beta"]}),
                },
            },
        ]
        harness = BoundedToolHarness(
            RecordedModelGateway(
                (
                    response({"role": "assistant", "tool_calls": calls}),
                    response({"role": "assistant", "content": "done"}),
                ),
                self.store,
            ),
            (ToolSpec(schema, handler, "market"),),
            limits(),
            monotonic=lambda: 0,
        )

        result = harness.run([], model_config=config())

        self.assertTrue(all(record.success for record in result.tool_calls))
        self.assertEqual(seen, [{"keyword": "alpha"}, {"keyword": ["alpha", "beta"]}])

    def test_null_union_argument_is_accepted_for_optional_fetch_query(self) -> None:
        seen: list[dict[str, object]] = []

        def handler(arguments: dict[str, object]) -> dict[str, object]:
            seen.append(arguments)
            return {"ok": True}

        schema = {
            "type": "function",
            "function": {
                "name": "fetch_webpage",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string"},
                        "result_type": {"type": "string", "enum": ["full_text", "highlights"]},
                        "highlight_query": {"type": ["string", "null"]},
                    },
                },
            },
        }
        call = {
            "id": "fetch-null-query",
            "function": {
                "name": "fetch_webpage",
                "arguments": json.dumps(
                    {
                        "url": "https://example.com",
                        "highlight_query": None,
                    }
                ),
            },
        }
        harness = BoundedToolHarness(
            RecordedModelGateway(
                (
                    response({"role": "assistant", "tool_calls": [call]}),
                    response({"role": "assistant", "content": "done"}),
                ),
                self.store,
            ),
            (ToolSpec(schema, handler, "research"),),
            limits(),
            monotonic=lambda: 0,
        )

        result = harness.run([], model_config=config())

        self.assertTrue(result.tool_calls[0].success)
        self.assertEqual(seen[0]["highlight_query"], None)

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

    def test_exa_research_batch_above_strict_fifty_is_rejected_before_execution(self) -> None:
        called = 0

        def search(_arguments):
            nonlocal called
            called += 1
            return {"results": []}

        web_schema = {
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
        fetch_schema = {
            "type": "function",
            "function": {
                "name": "fetch_webpage",
                "parameters": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string"},
                        "result_type": {"type": "string"},
                    },
                },
            },
        }
        calls = [
            {
                "id": str(index),
                "function": {
                    "name": "web_search" if index < 26 else "fetch_webpage",
                    "arguments": (
                        '{"query":"x"}'
                        if index < 26
                        else '{"url":"https://example.com","result_type":"highlights"}'
                    ),
                },
            }
            for index in range(51)
        ]
        gateway = RecordedModelGateway(
            (response({"role": "assistant", "tool_calls": calls}),), self.store
        )
        harness = BoundedToolHarness(
            gateway,
            (
                ToolSpec(web_schema, search, "research"),
                ToolSpec(fetch_schema, search, "research"),
            ),
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
        plan = PlanRecord(
            str(uuid.uuid4()), "bob", PlanType.NEXT_CYCLE, "x", None, NOW
        )
        with self.assertRaises(PermissionError):
            PromptBuilder("system").build(
                agent_id="alice",
                cycle_context={},
                plans=(plan,),
                recent_activity={"events": [], "truncated": False},
            )

    def test_prompt_builder_uses_named_plans_and_excludes_beliefs_and_audit_ids(self) -> None:
        plans = (
            PlanRecord(str(uuid.uuid4()), "alice", PlanType.LONG_TERM, "durable", None, NOW),
            PlanRecord(
                str(uuid.uuid4()),
                "alice",
                PlanType.NEXT_CYCLE,
                "follow up",
                NOW,
                NOW,
            ),
        )
        _system, user = PromptBuilder("system").build(
            agent_id="alice",
            cycle_context={
                "scheduled_at": NOW.isoformat(),
                "data_cutoff": NOW.isoformat(),
                "account": {"cash_micros": 10},
            },
            plans=plans,
            recent_activity={"events": [], "truncated": False},
        )
        payload = json.loads(str(user["content"]))
        self.assertEqual(payload["long_term_plan"]["content"], "durable")
        self.assertEqual(payload["long_term_plan"]["created_at"], NOW.isoformat())
        self.assertEqual(payload["next_cycle_plan"]["content"], "follow up")
        self.assertNotIn("agent_id", payload)
        self.assertNotIn("beliefs", payload)
        self.assertNotIn("plans", payload)
        self.assertNotIn("critical_learning", payload)

    def test_recent_activity_event_keeps_created_at_and_optional_outcome(self) -> None:
        event = RecentActivityEvent("rejection", "market", NOW, "outcome", None, "stale")
        self.assertEqual(event.created_at, NOW)
        self.assertEqual(event.outcome_id, "outcome")

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
