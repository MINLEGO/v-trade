from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from vtrade.domain.ports import ArtifactStore, JsonObject

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
EXA_SEARCH_URL = "https://api.exa.ai/search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
EXA_MAX_SEARCH_COST_MICROS = 20_000
EXA_MAX_CREDITS_PER_SEARCH = Decimal(10)
TAVILY_MAX_SEARCH_COST_MICROS = 8_000

WEB_SEARCH_TOOL_SCHEMA: JsonObject = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the public web and return provider-neutral ranked sources.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "num_results": {"type": "integer", "minimum": 1, "maximum": 10},
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
            },
        },
    },
}


class ProviderConfigurationError(ValueError):
    pass


class ProviderPayloadError(RuntimeError):
    pass


class ProviderDisabled(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    id: str
    estimated_cost_micros: int
    provider: str = ""
    reserved_request_count: int = 0
    reserved_credit_count: Decimal = Decimal(0)


class BudgetGuard(Protocol):
    def reserve(
        self,
        provider: str,
        estimated_cost_micros: int,
        *,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> BudgetReservation: ...

    def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        billed_cost_micros: int,
        nominal_cost_micros: int,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderTelemetry:
    provider: str
    usage_kind: str
    route: str | None
    request_count: int
    credit_count: Decimal
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    billed_cost_micros: int
    nominal_cost_micros: int
    latency_ms: int
    artifact_uri: str
    raw_sha256: str
    artifact_byte_length: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    response: JsonObject
    telemetry: ProviderTelemetry


@dataclass(frozen=True, slots=True)
class SearchResponse:
    output: JsonObject
    telemetry: ProviderTelemetry


@dataclass(frozen=True, slots=True)
class OpenRouterRoute:
    model: str
    quantizations: tuple[str, ...]
    reasoning_effort: str
    allow_provider_fallbacks: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> OpenRouterRoute:
        slug = config.get("slug")
        quantizations = config.get("allowed_quantizations")
        if slug not in {
            "deepseek/deepseek-v4-flash",
            "xiaomi/mimo-v2.5-pro",
        }:
            raise ProviderConfigurationError("model slug is outside the frozen baseline")
        if not isinstance(quantizations, list) or not all(
            isinstance(item, str) for item in quantizations
        ):
            raise ProviderConfigurationError("allowed quantizations are required")
        expected = {
            "deepseek/deepseek-v4-flash": ("fp8",),
            "xiaomi/mimo-v2.5-pro": ("fp8", "unknown"),
        }[slug]
        if tuple(quantizations) != expected:
            raise ProviderConfigurationError("model quantizations differ from frozen baseline")
        if config.get("provider_allowlist") is not None:
            raise ProviderConfigurationError("baseline must allow all compatible providers")
        if config.get("provider_selection") != "all_compatible_sorted_by_price":
            raise ProviderConfigurationError("baseline providers must be sorted by price")
        if config.get("allow_provider_fallbacks") is not True:
            raise ProviderConfigurationError("same-model provider fallback must be enabled")
        if config.get("cross_model_fallback") is not False:
            raise ProviderConfigurationError("cross-model fallback is forbidden")
        if config.get("reasoning_effort") != "max":
            raise ProviderConfigurationError("baseline reasoning effort must be owner-fixed max")
        if config.get("reasoning_effort_policy") != "owner_fixed":
            raise ProviderConfigurationError("baseline reasoning effort policy must be owner-fixed")
        return cls(slug, expected, "max")


class OpenRouterModelGateway:
    def __init__(
        self,
        api_key: str,
        artifact_store: ArtifactStore,
        budget: BudgetGuard,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not api_key:
            raise ProviderConfigurationError("OpenRouter API key is required")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._store = artifact_store
        self._budget = budget
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
        self._clock = clock
        self._sleep = sleep or time.sleep

    def complete(
        self,
        messages: Sequence[JsonObject],
        tools: Sequence[JsonObject],
        model_config: JsonObject,
    ) -> ModelResponse:
        route = OpenRouterRoute.from_config(model_config)
        estimate = _required_nonnegative_int(model_config, "estimated_max_cost_micros")
        maximum_output_tokens = _required_positive_int(model_config, "maximum_output_tokens")
        maximum_prompt_tokens = _required_positive_int(model_config, "maximum_prompt_tokens")
        maximum_context_tokens = _required_positive_int(model_config, "maximum_context_tokens")
        if maximum_prompt_tokens + maximum_output_tokens > maximum_context_tokens:
            raise ProviderConfigurationError(
                "maximum prompt plus reserved output exceeds model context"
            )
        maximum_prices = _maximum_prices(model_config.get("provider_max_price"))
        request_upper_bound = _request_upper_bound_micros(
            maximum_prompt_tokens, maximum_output_tokens, maximum_prices
        )
        if estimate < request_upper_bound:
            raise ProviderConfigurationError(
                "estimated_max_cost_micros is below the enforceable provider price bound"
            )
        assembled_tokens = _rough_tokens([*messages, *tools])
        if assembled_tokens > maximum_prompt_tokens:
            raise ProviderConfigurationError("request exceeds configured maximum prompt tokens")
        if assembled_tokens + maximum_output_tokens > maximum_context_tokens:
            raise ProviderConfigurationError("request plus reserved output exceeds model context")
        reservation = self._budget.reserve("openrouter", estimate)
        payload: JsonObject = {
            "model": route.model,
            "messages": list(messages),
            "tools": list(tools),
            # OpenRouter Chat Completions and its provider capability metadata use
            # max_tokens. With require_parameters enabled, max_completion_tokens
            # excludes every otherwise-compatible tool-capable endpoint.
            "max_tokens": maximum_output_tokens,
            "reasoning": {"effort": route.reasoning_effort},
            "stream": False,
            "provider": {
                "quantizations": list(route.quantizations),
                "sort": "price",
                "allow_fallbacks": route.allow_provider_fallbacks,
                "require_parameters": bool(tools),
                "max_price": maximum_prices,
            },
        }
        if "models" in payload or "models" in model_config:
            raise ProviderConfigurationError("models[] cross-model fallback is forbidden")
        started = _clock_ms(self._clock)
        response: httpx.Response | None = None
        for attempt in range(3):
            response = self._client.post(OPENROUTER_URL, headers=self._headers, json=payload)
            if response.status_code not in {429, 503} or attempt == 2:
                break
            delay = _openrouter_retry_delay(response, attempt)
            if delay is None:
                break
            self._sleep(delay)
        if response is None:  # pragma: no cover - range(3) is statically non-empty
            raise RuntimeError("OpenRouter retry loop did not execute")
        latency = _clock_ms(self._clock) - started
        if response.status_code in {429, 503}:
            # Both statuses are explicit pre-inference routing failures. Release the
            # one request reservation after all bounded attempts are exhausted.
            self._budget.reconcile(
                reservation,
                billed_cost_micros=0,
                nominal_cost_micros=0,
            )
        response.raise_for_status()
        raw = response.content
        parsed = _json_object(raw, "OpenRouter response")
        _validate_openrouter_response(parsed, route.model)
        usage = _object(parsed.get("usage", {}), "OpenRouter usage")
        billed = _dollars_to_micros(usage.get("cost", 0))
        cost_details = _object(usage.get("cost_details", {}), "OpenRouter cost_details")
        nominal = _dollars_to_micros(cost_details.get("upstream_inference_cost", billed / 1e6))
        redacted = canonical_redacted_json(parsed)
        artifact = self._store.put(redacted)
        telemetry = ProviderTelemetry(
            provider="openrouter",
            usage_kind="model",
            route=_optional_string(parsed.get("provider")),
            request_count=1,
            credit_count=Decimal(0),
            prompt_tokens=_nonnegative_int(usage.get("prompt_tokens", 0)),
            completion_tokens=_nonnegative_int(usage.get("completion_tokens", 0)),
            reasoning_tokens=_nested_nonnegative_int(
                usage, "completion_tokens_details", "reasoning_tokens"
            ),
            cached_tokens=_nested_nonnegative_int(usage, "prompt_tokens_details", "cached_tokens"),
            billed_cost_micros=billed,
            nominal_cost_micros=nominal,
            latency_ms=max(latency, 0),
            artifact_uri=artifact.uri,
            raw_sha256=artifact.sha256,
            artifact_byte_length=artifact.byte_length,
        )
        self._budget.reconcile(
            reservation,
            billed_cost_micros=billed,
            nominal_cost_micros=nominal,
        )
        return ModelResponse(parsed, telemetry)


class RecordedModelGateway:
    """Deterministic response replay; deliberately has no HTTP client or API key."""

    def __init__(self, responses: Sequence[bytes], artifact_store: ArtifactStore) -> None:
        if not responses:
            raise ProviderConfigurationError("at least one recorded response is required")
        self._responses = list(responses)
        self._index = 0
        self._store = artifact_store

    @property
    def remaining(self) -> int:
        return len(self._responses) - self._index

    def complete(
        self,
        messages: Sequence[JsonObject],
        tools: Sequence[JsonObject],
        model_config: JsonObject,
    ) -> ModelResponse:
        del messages, tools
        route = OpenRouterRoute.from_config(model_config)
        if self._index >= len(self._responses):
            raise ProviderPayloadError("recorded model responses exhausted")
        raw = self._responses[self._index]
        self._index += 1
        parsed = _json_object(raw, "recorded model response")
        _validate_openrouter_response(parsed, route.model)
        artifact = self._store.put(canonical_redacted_json(parsed))
        usage = _object(parsed.get("usage", {}), "recorded usage")
        return ModelResponse(
            parsed,
            ProviderTelemetry(
                provider="recorded",
                usage_kind="model_replay",
                route=_optional_string(parsed.get("provider")),
                request_count=0,
                credit_count=Decimal(0),
                prompt_tokens=_nonnegative_int(usage.get("prompt_tokens", 0)),
                completion_tokens=_nonnegative_int(usage.get("completion_tokens", 0)),
                reasoning_tokens=_nested_nonnegative_int(
                    usage, "completion_tokens_details", "reasoning_tokens"
                ),
                cached_tokens=_nested_nonnegative_int(
                    usage, "prompt_tokens_details", "cached_tokens"
                ),
                billed_cost_micros=0,
                nominal_cost_micros=_dollars_to_micros(usage.get("cost", 0)),
                latency_ms=0,
                artifact_uri=artifact.uri,
                raw_sha256=artifact.sha256,
                artifact_byte_length=artifact.byte_length,
            ),
        )


class ExaResearchProvider:
    def __init__(
        self,
        api_key: str,
        artifact_store: ArtifactStore,
        budget: BudgetGuard,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key:
            raise ProviderConfigurationError("Exa API key is required")
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        self._store = artifact_store
        self._budget = budget
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._clock = clock

    def search(self, query: str, options: JsonObject) -> SearchResponse:
        normalized = _search_options(query, options)
        payload: JsonObject = {
            "query": query,
            "type": "auto",
            "numResults": normalized["num_results"],
            "contents": {"highlights": True},
        }
        if normalized["include_domains"]:
            payload["includeDomains"] = normalized["include_domains"]
        if normalized["exclude_domains"]:
            payload["excludeDomains"] = normalized["exclude_domains"]
        return self._request(payload, EXA_MAX_SEARCH_COST_MICROS)

    def _request(self, payload: JsonObject, estimate: int) -> SearchResponse:
        reservation = self._budget.reserve(
            "exa",
            estimate,
            request_count=1,
            credit_count=EXA_MAX_CREDITS_PER_SEARCH,
        )
        started = _clock_ms(self._clock)
        response = self._client.post(EXA_SEARCH_URL, headers=self._headers, json=payload)
        latency = _clock_ms(self._clock) - started
        response.raise_for_status()
        parsed = _json_object(response.content, "Exa response")
        output = _normalize_search_output(str(payload["query"]), parsed, provider="exa")
        cost = _object(parsed.get("costDollars", {}), "Exa costDollars")
        # Exa defines costDollars as an endpoint-dependent estimated cost and states
        # that billing is computed separately. The strict 18,000-request ceiling is
        # below the owner's 20,000-request free allowance, so this response field is
        # nominal telemetry and must not trip the billed-dollar circuit.
        _dollars_to_micros(cost.get("total", 0))
        credits = _nonnegative_decimal(parsed.get("requestCredits", 1), "requestCredits")
        artifact = self._store.put(canonical_redacted_json(parsed))
        telemetry = ProviderTelemetry(
            provider="exa",
            usage_kind="web_search",
            route=None,
            request_count=1,
            credit_count=credits,
            prompt_tokens=0,
            completion_tokens=0,
            reasoning_tokens=0,
            cached_tokens=0,
            billed_cost_micros=0,
            nominal_cost_micros=estimate,
            latency_ms=max(latency, 0),
            artifact_uri=artifact.uri,
            raw_sha256=artifact.sha256,
            artifact_byte_length=artifact.byte_length,
        )
        self._budget.reconcile(
            reservation,
            billed_cost_micros=0,
            nominal_cost_micros=estimate,
            request_count=1,
            credit_count=credits,
        )
        return SearchResponse(output, telemetry)


class TavilyResearchProvider:
    """Contract-compatible alternate; disabled unless a future experiment enables it."""

    def __init__(
        self,
        api_key: str | None,
        artifact_store: ArtifactStore,
        budget: BudgetGuard,
        *,
        enabled: bool = False,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if enabled and not api_key:
            raise ProviderConfigurationError("Tavily key is required only when enabled")
        self._enabled = enabled
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._store = artifact_store
        self._budget = budget
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._clock = clock

    def search(self, query: str, options: JsonObject) -> SearchResponse:
        if not self._enabled:
            raise ProviderDisabled("Tavily is disabled in predictionarena-polymarket-v1")
        normalized = _search_options(query, options)
        payload: JsonObject = {
            "query": query,
            "search_depth": "basic",
            "max_results": normalized["num_results"],
            "include_answer": False,
            "include_raw_content": False,
        }
        if normalized["include_domains"]:
            payload["include_domains"] = normalized["include_domains"]
        if normalized["exclude_domains"]:
            payload["exclude_domains"] = normalized["exclude_domains"]
        estimate = TAVILY_MAX_SEARCH_COST_MICROS
        reservation = self._budget.reserve("tavily", estimate)
        started = _clock_ms(self._clock)
        response = self._client.post(TAVILY_SEARCH_URL, headers=self._headers, json=payload)
        latency = _clock_ms(self._clock) - started
        response.raise_for_status()
        parsed = _json_object(response.content, "Tavily response")
        output = _normalize_search_output(query, parsed, provider="tavily")
        usage = _object(parsed.get("usage", {}), "Tavily usage")
        credits = Decimal(str(usage.get("credits", 1)))
        artifact = self._store.put(canonical_redacted_json(parsed))
        telemetry = ProviderTelemetry(
            provider="tavily",
            usage_kind="web_search",
            route=None,
            request_count=1,
            credit_count=credits,
            prompt_tokens=0,
            completion_tokens=0,
            reasoning_tokens=0,
            cached_tokens=0,
            billed_cost_micros=estimate,
            nominal_cost_micros=estimate,
            latency_ms=max(latency, 0),
            artifact_uri=artifact.uri,
            raw_sha256=artifact.sha256,
            artifact_byte_length=artifact.byte_length,
        )
        self._budget.reconcile(
            reservation,
            billed_cost_micros=estimate,
            nominal_cost_micros=estimate,
        )
        return SearchResponse(output, telemetry)


_SECRET_KEYS = re.compile(
    r"(^|_)(authorization|api_?key|token|secret|password|cookie)(_|$)", re.IGNORECASE
)
_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _SECRET_KEYS.search(str(key)) else redact_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _BEARER.sub("Bearer [REDACTED]", value)
    return value


def canonical_redacted_json(value: Any) -> bytes:
    return json.dumps(
        redact_secrets(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _search_options(query: str, options: JsonObject) -> JsonObject:
    if not isinstance(query, str) or not query.strip():
        raise ProviderConfigurationError("search query must be non-empty")
    allowed = {
        "num_results",
        "include_domains",
        "exclude_domains",
    }
    unknown = set(options) - allowed
    if unknown:
        raise ProviderConfigurationError(f"unsupported search options: {sorted(unknown)}")
    num_results = int(options.get("num_results", 5))
    if not 1 <= num_results <= 10:
        raise ProviderConfigurationError("num_results must be between 1 and 10")
    return {
        "num_results": num_results,
        "include_domains": _string_list(options.get("include_domains", [])),
        "exclude_domains": _string_list(options.get("exclude_domains", [])),
    }


def _normalize_search_output(query: str, payload: JsonObject, *, provider: str) -> JsonObject:
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise ProviderPayloadError(f"{provider} response lacks results")
    results: list[JsonObject] = []
    for row in rows:
        item = _object(row, f"{provider} result")
        url = item.get("url")
        if not isinstance(url, str) or not url:
            raise ProviderPayloadError(f"{provider} result URL is required")
        content = item.get("content", item.get("text"))
        if not isinstance(content, str):
            highlights = item.get("highlights", [])
            content = "\n".join(str(value) for value in highlights)
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": url,
                "published_at": item.get("publishedDate"),
                "content": content,
            }
        )
    return {"query": query, "results": results}


def _validate_openrouter_response(payload: JsonObject, expected_model: str) -> None:
    model = payload.get("model")
    if model != expected_model:
        raise ProviderPayloadError("OpenRouter returned a different model; cross-model blocked")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderPayloadError("OpenRouter response requires exactly one choice")
    choice = _object(choices[0], "OpenRouter choice")
    _object(choice.get("message"), "OpenRouter message")


def _json_object(raw: bytes, label: str) -> JsonObject:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderPayloadError(f"{label} is not JSON") from exc
    return _object(value, label)


def _object(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProviderPayloadError(f"{label} must be an object")
    return value


def _required_positive_int(values: Mapping[str, Any], key: str) -> int:
    value = _required_nonnegative_int(values, key)
    if value == 0:
        raise ProviderConfigurationError(f"{key} must be positive")
    return value


def _required_nonnegative_int(values: Mapping[str, Any], key: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderConfigurationError(f"{key} is required and must be non-negative")
    return value


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderPayloadError("usage counter must be a non-negative integer")
    return value


def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderPayloadError(f"{label} must be a non-negative decimal") from exc
    if not number.is_finite() or number < 0:
        raise ProviderPayloadError(f"{label} must be a non-negative decimal")
    return number


def _nested_nonnegative_int(values: JsonObject, parent: str, child: str) -> int:
    nested = values.get(parent)
    if nested is None:
        return 0
    return _nonnegative_int(_object(nested, parent).get(child, 0))


def _dollars_to_micros(value: Any) -> int:
    try:
        dollars = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderPayloadError("provider cost must be decimal-compatible") from exc
    if not dollars.is_finite() or dollars < 0:
        raise ProviderPayloadError("provider cost must be finite and non-negative")
    return int((dollars * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProviderConfigurationError("domain filters must be string arrays")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _clock_ms(clock: Callable[[], datetime] | None) -> int:
    if clock is None:
        return 0
    return int(clock().timestamp() * 1000)


def _maximum_prices(value: Any) -> JsonObject:
    prices = _object(value, "provider_max_price")
    required = {"prompt", "completion", "request"}
    if set(prices) != required:
        raise ProviderConfigurationError(
            "provider_max_price requires prompt, completion, and request bounds"
        )
    normalized: JsonObject = {}
    for key in sorted(required):
        try:
            price = Decimal(str(prices[key]))
        except (InvalidOperation, ValueError) as exc:
            raise ProviderConfigurationError("provider max prices must be decimals") from exc
        if not price.is_finite() or price < 0:
            raise ProviderConfigurationError("provider max prices must be non-negative")
        normalized[key] = str(price)
    return normalized


def _openrouter_retry_delay(response: httpx.Response, attempt: int) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            delay = float(raw)
        except ValueError:
            delay = float(2**attempt)
    else:
        delay = float(2**attempt)
    if delay > 60:
        return None
    return max(delay, 0.0)


def _request_upper_bound_micros(
    maximum_prompt_tokens: int,
    maximum_output_tokens: int,
    prices: JsonObject,
) -> int:
    prompt = Decimal(str(prices["prompt"])) * maximum_prompt_tokens / 1_000_000
    completion = Decimal(str(prices["completion"])) * maximum_output_tokens / 1_000_000
    request = Decimal(str(prices["request"]))
    return int(
        ((prompt + completion + request) * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP)
    )


def _rough_tokens(values: Sequence[JsonObject]) -> int:
    raw = json.dumps(values, separators=(",", ":"), ensure_ascii=False)
    # Provider-neutral preflight estimate; exact per-turn prompt usage returned by
    # OpenRouter governs later harness turns. Do not equate every UTF-8 byte to a token.
    return max(1, (len(raw.encode("utf-8")) + 3) // 4)
