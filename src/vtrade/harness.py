from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from vtrade.domain.ports import JsonObject
from vtrade.providers import ModelResponse, ProviderTelemetry


class HarnessLimitExceeded(RuntimeError):
    pass


class ToolValidationError(ValueError):
    pass


class ToolHandlerError(RuntimeError):
    """Expected, agent-visible tool failure that is safe to record and continue."""


class ModelGateway(Protocol):
    def complete(
        self,
        messages: Sequence[JsonObject],
        tools: Sequence[JsonObject],
        model_config: JsonObject,
    ) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class HarnessLimits:
    maximum_model_turns: int
    maximum_total_tool_calls: int
    maximum_web_searches: int
    maximum_wall_clock_seconds: float
    maximum_context_tokens: int
    maximum_assembled_input_tokens: int
    maximum_model_output_tokens: int
    maximum_tool_call_arguments_tokens: int
    maximum_default_tool_result_tokens: int
    maximum_get_portfolio_result_tokens: int

    def __post_init__(self) -> None:
        if min(
            self.maximum_model_turns,
            self.maximum_total_tool_calls,
            self.maximum_web_searches,
            self.maximum_context_tokens,
            self.maximum_assembled_input_tokens,
            self.maximum_model_output_tokens,
            self.maximum_tool_call_arguments_tokens,
            self.maximum_default_tool_result_tokens,
            self.maximum_get_portfolio_result_tokens,
        ) <= 0:
            raise ValueError("all harness count/token limits must be positive")
        if self.maximum_wall_clock_seconds <= 0:
            raise ValueError("wall-clock limit must be positive")
        if self.maximum_total_tool_calls < 92:
            raise ValueError("tool ceiling must remain compatible with observed 92-call traces")
        if (
            self.maximum_assembled_input_tokens + self.maximum_model_output_tokens
            > self.maximum_context_tokens
        ):
            raise ValueError("assembled input plus reserved output exceeds model context")
        if self.maximum_get_portfolio_result_tokens < self.maximum_default_tool_result_tokens:
            raise ValueError("get_portfolio result limit cannot be below the default")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    schema: JsonObject
    handler: Callable[[JsonObject], JsonObject | ToolExecution]
    category: str
    mutates_financial_state: bool = False

    @property
    def name(self) -> str:
        function = self.schema.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("tool schema requires function.name")
        return str(function["name"])


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    id: str
    name: str
    arguments: JsonObject | None
    success: bool
    output: JsonObject
    category: str


@dataclass(frozen=True, slots=True)
class ToolExecution:
    output: JsonObject
    telemetry: tuple[ProviderTelemetry, ...] = ()


@dataclass(frozen=True, slots=True)
class HarnessResult:
    messages: tuple[JsonObject, ...]
    tool_calls: tuple[ToolCallRecord, ...]
    telemetry: tuple[ProviderTelemetry, ...]
    termination_status: str
    total_completion_tokens: int


class BoundedToolHarness:
    def __init__(
        self,
        gateway: ModelGateway,
        tools: Sequence[ToolSpec],
        limits: HarnessLimits,
        *,
        monotonic: Callable[[], float],
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")
        self._limits = limits
        self._monotonic = monotonic
        self._token_counter = token_counter or _conservative_token_upper_bound

    def run(
        self, initial_messages: Sequence[JsonObject], *, model_config: JsonObject
    ) -> HarnessResult:
        messages = list(initial_messages)
        records: list[ToolCallRecord] = []
        telemetry: list[ProviderTelemetry] = []
        web_searches = 0
        completion_tokens = 0
        seen_tool_call_ids: set[str] = set()
        started = self._monotonic()
        schemas = [tool.schema for tool in self._tools.values()]
        for _turn in range(self._limits.maximum_model_turns):
            self._check_wall_clock(started)
            assembled_tokens = self._count_tokens({"messages": messages, "tools": schemas})
            if assembled_tokens > self._limits.maximum_assembled_input_tokens:
                raise HarnessLimitExceeded(
                    "assembled input token ceiling reached before model request"
                )
            if (
                assembled_tokens + self._limits.maximum_model_output_tokens
                > self._limits.maximum_context_tokens
            ):
                raise HarnessLimitExceeded("reserved model output would exceed context")
            configured_output = model_config.get("maximum_output_tokens")
            if not isinstance(configured_output, int) or isinstance(configured_output, bool):
                raise HarnessLimitExceeded("model output ceiling must be configured")
            if not 0 < configured_output <= self._limits.maximum_model_output_tokens:
                raise HarnessLimitExceeded("configured model output exceeds reserved output")
            response = self._gateway.complete(messages, schemas, model_config)
            telemetry.append(response.telemetry)
            completion_tokens += response.telemetry.completion_tokens
            if response.telemetry.completion_tokens > self._limits.maximum_model_output_tokens:
                raise HarnessLimitExceeded("model response exceeded reserved output")
            message = _assistant_message(response.response)
            messages.append(message)
            calls = _tool_calls(message)
            if not calls:
                return HarnessResult(
                    tuple(messages),
                    tuple(records),
                    tuple(telemetry),
                    "stop",
                    completion_tokens,
                )
            if len(records) + len(calls) > self._limits.maximum_total_tool_calls:
                raise HarnessLimitExceeded("total tool-call ceiling would be exceeded")
            proposed_web = sum(1 for call in calls if _tool_name(call) == "web_search")
            if web_searches + proposed_web > self._limits.maximum_web_searches:
                raise HarnessLimitExceeded("strict web-search ceiling would be exceeded")
            web_searches += proposed_web
            call_ids = [call.get("id") for call in calls]
            duplicate_ids = {
                call_id
                for call_id in call_ids
                if isinstance(call_id, str)
                and call_id
                and (call_ids.count(call_id) > 1 or call_id in seen_tool_call_ids)
            }
            for call in calls:
                self._check_wall_clock(started)
                raw_call_id = call.get("id")
                tool_telemetry: tuple[ProviderTelemetry, ...]
                if isinstance(raw_call_id, str) and raw_call_id in duplicate_ids:
                    record = self._failed_tool_call(
                        call,
                        raw_call_id,
                        ToolValidationError("duplicate tool-call ID; call was not executed"),
                    )
                    tool_telemetry = ()
                else:
                    record, tool_telemetry = self._execute_tool(call)
                if isinstance(raw_call_id, str) and raw_call_id:
                    seen_tool_call_ids.add(raw_call_id)
                records.append(record)
                telemetry.extend(tool_telemetry)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": record.id,
                        "name": record.name,
                        "content": json.dumps(record.output, sort_keys=True),
                    }
                )
        raise HarnessLimitExceeded("model-turn ceiling reached")

    def _execute_tool(
        self, call: JsonObject
    ) -> tuple[ToolCallRecord, tuple[ProviderTelemetry, ...]]:
        raw_call_id = call.get("id")
        call_id = (
            raw_call_id
            if isinstance(raw_call_id, str) and raw_call_id
            else str(uuid.uuid4())
        )
        try:
            if not isinstance(raw_call_id, str) or not raw_call_id:
                raise ToolValidationError("tool call requires a non-empty provider call ID")
            name = _tool_name(call)
            tool = self._tools.get(name)
            if tool is None:
                raise ToolValidationError("tool is not authorized")
            arguments = _tool_arguments(
                call,
                maximum_tokens=self._limits.maximum_tool_call_arguments_tokens,
                token_counter=self._token_counter,
            )
            _validate_arguments(tool.schema, arguments)
            executed = tool.handler(arguments)
            if isinstance(executed, ToolExecution):
                output = executed.output
                tool_telemetry = executed.telemetry
            else:
                output = executed
                tool_telemetry = ()
            if not isinstance(output, dict):
                raise ToolValidationError("tool output must be an object")
            maximum_result_tokens = (
                self._limits.maximum_get_portfolio_result_tokens
                if name == "get_portfolio"
                else self._limits.maximum_default_tool_result_tokens
            )
            if self._count_tokens(output) > maximum_result_tokens:
                if name == "get_portfolio":
                    raise ToolValidationError(
                        "get_portfolio result exceeds 24K-token ceiling and must paginate"
                    )
                raise ToolValidationError("tool result exceeds its token ceiling")
            return (
                ToolCallRecord(call_id, name, arguments, True, output, tool.category),
                tool_telemetry,
            )
        except (ToolValidationError, ToolHandlerError, ValueError, json.JSONDecodeError) as exc:
            return self._failed_tool_call(call, call_id, exc), ()

    def _failed_tool_call(
        self, call: JsonObject, call_id: str, error: Exception
    ) -> ToolCallRecord:
        name = _safe_tool_name(call)
        category = self._tools[name].category if name in self._tools else "invalid"
        return ToolCallRecord(
            call_id,
            name,
            None,
            False,
            {"error": type(error).__name__, "message": str(error)},
            category,
        )

    def _check_wall_clock(self, started: float) -> None:
        if self._monotonic() - started > self._limits.maximum_wall_clock_seconds:
            raise HarnessLimitExceeded("cycle wall-clock ceiling exceeded")

    def _count_tokens(self, value: object) -> int:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        count = self._token_counter(raw)
        if count < 0:
            raise ValueError("token counter cannot return a negative value")
        return count


class PlanType(StrEnum):
    LONG_TERM = "long_term"
    NEXT_CYCLE = "next_cycle"


@dataclass(frozen=True, slots=True)
class BeliefRecord:
    id: str
    agent_id: str
    probability: Decimal
    content: str
    category: str
    evidence: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.probability <= Decimal(1):
            raise ValueError("belief probability must be between zero and one")


@dataclass(frozen=True, slots=True)
class PlanRecord:
    id: str
    agent_id: str
    plan_type: PlanType
    content: str
    due_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class PrivateAgentMemory:
    _beliefs: dict[str, list[BeliefRecord]] = field(default_factory=dict)
    _plans: dict[str, list[PlanRecord]] = field(default_factory=dict)

    def for_agent(self, actor_id: str) -> AgentMemoryView:
        if not actor_id:
            raise ValueError("authenticated agent ID is required")
        return AgentMemoryView(self, actor_id)

    def _add_belief(self, agent_id: str, belief: BeliefRecord) -> None:
        if belief.agent_id != agent_id:
            raise PermissionError("agent cannot write another agent's belief")
        self._beliefs.setdefault(agent_id, []).append(belief)

    def _beliefs_for(self, agent_id: str) -> tuple[BeliefRecord, ...]:
        return tuple(self._beliefs.get(agent_id, ()))

    def _add_plan(self, agent_id: str, plan: PlanRecord) -> None:
        if plan.agent_id != agent_id:
            raise PermissionError("agent cannot write another agent's plan")
        self._plans.setdefault(agent_id, []).append(plan)

    def _plans_for(self, agent_id: str) -> tuple[PlanRecord, ...]:
        return tuple(self._plans.get(agent_id, ()))


@dataclass(frozen=True, slots=True)
class AgentMemoryView:
    _store: PrivateAgentMemory
    actor_id: str

    def add_belief(self, belief: BeliefRecord) -> None:
        self._store._add_belief(self.actor_id, belief)

    def beliefs(self) -> tuple[BeliefRecord, ...]:
        return self._store._beliefs_for(self.actor_id)

    def add_plan(self, plan: PlanRecord) -> None:
        self._store._add_plan(self.actor_id, plan)

    def plans(self) -> tuple[PlanRecord, ...]:
        return self._store._plans_for(self.actor_id)


@dataclass(frozen=True, slots=True)
class PromptBuilder:
    system_prompt: str

    def __post_init__(self) -> None:
        if not self.system_prompt.strip():
            raise ValueError("system prompt cannot be empty")
        if "{trading_tool_ref}" in self.system_prompt:
            raise ValueError("system prompt contains unresolved trading tool placeholder")

    def build(
        self,
        *,
        agent_id: str,
        cycle_context: JsonObject,
        beliefs: Sequence[BeliefRecord],
        plans: Sequence[PlanRecord],
        critical_learning: str,
    ) -> tuple[JsonObject, JsonObject]:
        if any(item.agent_id != agent_id for item in beliefs) or any(
            item.agent_id != agent_id for item in plans
        ):
            raise PermissionError("prompt context cannot include another agent's memory")
        payload = {
            "agent_id": agent_id,
            "cycle_context": cycle_context,
            "beliefs": [
                {
                    "probability": str(item.probability),
                    "content": item.content,
                    "category": item.category,
                    "evidence": list(item.evidence),
                }
                for item in beliefs
            ],
            "plans": [
                {
                    "type": item.plan_type.value,
                    "content": item.content,
                    "due_at": item.due_at.isoformat() if item.due_at else None,
                }
                for item in plans
            ],
            "critical_learning": critical_learning,
        }
        return (
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            },
        )


@dataclass(frozen=True, slots=True)
class LearningEvent:
    kind: str
    market_id: str
    pnl_micros: int = 0
    detail: str = ""


def deterministic_critical_learning(events: Sequence[LearningEvent]) -> str:
    ordered = sorted(events, key=lambda item: (item.kind, item.market_id, item.detail))
    wins = sum(1 for item in ordered if item.kind == "settlement" and item.pnl_micros > 0)
    losses = sum(1 for item in ordered if item.kind == "settlement" and item.pnl_micros < 0)
    rejected = sum(1 for item in ordered if item.kind == "rejection")
    concentration = sum(1 for item in ordered if item.kind == "concentration")
    drawdowns = [item.pnl_micros for item in ordered if item.kind == "drawdown"]
    worst_drawdown = min(drawdowns, default=0)
    return (
        f"Settled wins/losses: {wins}/{losses}. Rejections: {rejected}. "
        f"Concentration events: {concentration}. Worst drawdown: {worst_drawdown} micros."
    )


def _assistant_message(response: JsonObject) -> JsonObject:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ToolValidationError("model response requires one choice")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ToolValidationError("model response lacks assistant message")
    return dict(choice["message"])


def _tool_calls(message: JsonObject) -> list[JsonObject]:
    calls = message.get("tool_calls", [])
    if calls is None:
        return []
    if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
        raise ToolValidationError("assistant tool_calls must be an object array")
    return calls


def _tool_name(call: JsonObject) -> str:
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ToolValidationError("tool call lacks function name")
    return str(function["name"])


def _safe_tool_name(call: JsonObject) -> str:
    try:
        return _tool_name(call)
    except ToolValidationError:
        return "invalid"


def _tool_arguments(
    call: JsonObject,
    *,
    maximum_tokens: int,
    token_counter: Callable[[str], int],
) -> JsonObject:
    function = call.get("function")
    if not isinstance(function, dict):
        raise ToolValidationError("tool call lacks function object")
    raw = function.get("arguments")
    if not isinstance(raw, str):
        raise ToolValidationError("tool arguments must be a JSON string")
    if token_counter(raw) > maximum_tokens:
        raise ToolValidationError("tool-call arguments exceed their token ceiling")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ToolValidationError("tool arguments must decode to an object")
    return value


def _validate_arguments(schema: JsonObject, arguments: JsonObject) -> None:
    function = schema.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("parameters"), dict):
        raise ToolValidationError("tool schema lacks parameters")
    parameters = function["parameters"]
    required = parameters.get("required", [])
    properties = parameters.get("properties", {})
    if not isinstance(required, list) or not isinstance(properties, dict):
        raise ToolValidationError("tool schema required/properties are malformed")
    missing = [key for key in required if key not in arguments]
    if missing:
        raise ToolValidationError(f"missing required arguments: {missing}")
    if parameters.get("additionalProperties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ToolValidationError(f"unknown arguments: {sorted(unknown)}")
    for key, value in arguments.items():
        definition = properties.get(key, {})
        if not isinstance(definition, dict):
            raise ToolValidationError(f"schema for {key} must be an object")
        _validate_schema_value(value, definition, path=key)


def _validate_schema_value(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or value not in enum:
            raise ToolValidationError(f"argument {path} is outside its enum")
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        None: True,
    }
    if expected not in valid or not valid[expected]:
        raise ToolValidationError(f"argument {path} must be {expected}")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ToolValidationError(f"argument {path} is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ToolValidationError(f"argument {path} is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolValidationError(f"argument {path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolValidationError(f"argument {path} is above maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ToolValidationError(f"argument {path} has too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ToolValidationError(f"argument {path} has too many items")
        items = schema.get("items", {})
        if not isinstance(items, dict):
            raise ToolValidationError(f"argument {path} items schema is malformed")
        for index, item in enumerate(value):
            _validate_schema_value(item, items, path=f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ToolValidationError(f"argument {path} object schema is malformed")
        missing = [key for key in required if key not in value]
        if missing:
            raise ToolValidationError(f"argument {path} misses {missing}")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ToolValidationError(f"argument {path} has unknown keys {sorted(unknown)}")
        for key, item in value.items():
            child = properties.get(key, {})
            if not isinstance(child, dict):
                raise ToolValidationError(f"argument {path}.{key} schema is malformed")
            _validate_schema_value(item, child, path=f"{path}.{key}")


def _conservative_token_upper_bound(raw: str) -> int:
    """Return a provider-neutral strict upper bound until exact tokenizers are wired."""
    return max(1, len(raw.encode("utf-8")))
