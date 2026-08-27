from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from vtrade.artifacts import ContentAddressedArtifactStore
from vtrade.budget import MonthlyBudgetCircuitBreaker
from vtrade.providers import (
    EXA_CONTENTS_URL,
    EXA_MAX_CREDITS_PER_SEARCH,
    EXA_MAX_SEARCH_COST_MICROS,
    EXA_SEARCH_URL,
    OPENROUTER_URL,
    TAVILY_MAX_SEARCH_COST_MICROS,
    WEB_SEARCH_TOOL_SCHEMA,
    BudgetReservation,
    ExaResearchProvider,
    OpenRouterModelGateway,
    OpenRouterRoute,
    ProviderConfigurationError,
    ProviderDisabled,
    ProviderPayloadError,
    RecordedModelGateway,
    TavilyResearchProvider,
    canonical_redacted_json,
)

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash-0731"
LUNA_MODEL = "openai/gpt-5.6-luna"
GLM_FLASH_MODEL = "z-ai/glm-5.3-flash"


class CapturingBudget:
    def __init__(self) -> None:
        self.estimates: list[tuple[str, int]] = []
        self.reservations: list[tuple[str, int, Decimal]] = []
        self.reconciliations: list[tuple[int, int, int, Decimal]] = []

    def reserve(
        self,
        provider: str,
        estimated_cost_micros: int,
        *,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> BudgetReservation:
        self.estimates.append((provider, estimated_cost_micros))
        self.reservations.append((provider, request_count, credit_count))
        return BudgetReservation(
            "reservation", estimated_cost_micros, provider, request_count, credit_count
        )

    def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        billed_cost_micros: int,
        nominal_cost_micros: int,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> None:
        del reservation
        self.reconciliations.append(
            (billed_cost_micros, nominal_cost_micros, request_count, credit_count)
        )


def model_config(slug: str = DEEPSEEK_MODEL) -> dict:
    config = {
        "slug": slug,
        "provider_allowlist": None,
        "provider_selection": "all_compatible_sorted_by_price",
        "allow_provider_fallbacks": True,
        "cross_model_fallback": False,
        "reasoning_effort": "xhigh" if slug == LUNA_MODEL else "max",
        "reasoning_effort_policy": "owner_fixed",
        "estimated_max_cost_micros": 100_000,
        "maximum_context_tokens": 100_000,
        "maximum_prompt_tokens": 10_000,
        "maximum_output_tokens": 1_000,
        "provider_max_price": (
            {"prompt": "0.15", "completion": "0.5", "request": "0"}
            if slug == GLM_FLASH_MODEL
            else {"prompt": "0.1", "completion": "0.1", "request": "0"}
        ),
    }
    if slug in {DEEPSEEK_MODEL, GLM_FLASH_MODEL}:
        config["allowed_quantizations"] = ["fp8"]
    if slug == GLM_FLASH_MODEL:
        config["provider_order"] = ["z-ai"]
    return config


def model_payload(*, model: str = DEEPSEEK_MODEL) -> bytes:
    return json.dumps(
        {
            "id": "generation-1",
            "model": model,
            "provider": "provider-a",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "cost": 0.01,
                "completion_tokens_details": {"reasoning_tokens": 2},
                "prompt_tokens_details": {"cached_tokens": 1},
                "cost_details": {"upstream_inference_cost": 0.02},
            },
        }
    ).encode()


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ContentAddressedArtifactStore(Path(self.temp.name))
        self.budget = MonthlyBudgetCircuitBreaker(clock=lambda: NOW)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_openrouter_payload_enforces_same_model_price_routing_and_tools(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=model_payload())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        gateway = OpenRouterModelGateway(
            "secret-key", self.store, self.budget, client=client
        )
        response = gateway.complete(
            [{"role": "user", "content": "hello"}],
            [WEB_SEARCH_TOOL_SCHEMA],
            model_config(),
        )
        self.assertEqual(requests[0].url, OPENROUTER_URL)
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], DEEPSEEK_MODEL)
        self.assertNotIn("models", body)
        self.assertNotIn("max_completion_tokens", body)
        self.assertEqual(body["max_tokens"], 1_000)
        self.assertEqual(body["reasoning"], {"effort": "max"})
        self.assertEqual(
            body["provider"],
            {
                "quantizations": ["fp8"],
                "sort": "price",
                "allow_fallbacks": True,
                "require_parameters": True,
                "max_price": {"completion": "0.1", "prompt": "0.1", "request": "0"},
            },
        )
        self.assertEqual(response.telemetry.billed_cost_micros, 10_000)
        self.assertEqual(response.telemetry.nominal_cost_micros, 20_000)

    def test_openrouter_luna_uses_xhigh_without_quantization_filter(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=model_payload(model=LUNA_MODEL))

        gateway = OpenRouterModelGateway(
            "secret-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        gateway.complete([], [], model_config(LUNA_MODEL))

        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], LUNA_MODEL)
        self.assertEqual(body["reasoning"], {"effort": "xhigh"})
        self.assertNotIn("quantizations", body["provider"])

    def test_openrouter_glm_flash_uses_fp8_and_prefers_z_ai(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=model_payload(model=GLM_FLASH_MODEL))

        gateway = OpenRouterModelGateway(
            "secret-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        gateway.complete([], [WEB_SEARCH_TOOL_SCHEMA], model_config(GLM_FLASH_MODEL))

        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], GLM_FLASH_MODEL)
        self.assertEqual(body["reasoning"], {"effort": "max"})
        self.assertEqual(
            body["provider"],
            {
                "quantizations": ["fp8"],
                "order": ["z-ai"],
                "sort": "price",
                "allow_fallbacks": True,
                "require_parameters": True,
                "max_price": {"completion": "0.5", "prompt": "0.15", "request": "0"},
            },
        )

    def test_openrouter_rejects_glm_flash_policy_drift(self) -> None:
        for key, value, message in (
            ("allowed_quantizations", ["fp16"], "model quantizations differ"),
            ("provider_order", ["novita"], "provider order differs"),
        ):
            with self.subTest(key=key):
                config = model_config(GLM_FLASH_MODEL)
                config[key] = value
                with self.assertRaisesRegex(ProviderConfigurationError, message):
                    OpenRouterRoute.from_config(config)

    def test_openrouter_rejects_models_outside_active_set(self) -> None:
        with self.assertRaisesRegex(ProviderConfigurationError, "outside the active model set"):
            OpenRouterRoute.from_config(model_config("deepseek/deepseek-v4-flash"))

    def test_openrouter_accepts_legacy_implicit_owner_fixed_policy(self) -> None:
        config = model_config()
        del config["reasoning_effort_policy"]

        route = OpenRouterRoute.from_config(config)

        self.assertEqual(route.reasoning_effort, "max")

    def test_openrouter_cross_model_response_fails_closed(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, content=model_payload(model=LUNA_MODEL)
                )
            )
        )
        gateway = OpenRouterModelGateway("key", self.store, self.budget, client=client)
        with self.assertRaises(ProviderPayloadError):
            gateway.complete([], [], model_config())

    def test_openrouter_retries_503_with_retry_after_then_succeeds(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, headers={"Retry-After": "0.25"})
            return httpx.Response(200, content=model_payload())

        gateway = OpenRouterModelGateway(
            "key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=sleeps.append,
        )

        response = gateway.complete([], [], model_config())

        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(response.response["id"], "generation-1")

    def test_openrouter_exhausted_503_releases_reservation(self) -> None:
        budget = CapturingBudget()
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        gateway = OpenRouterModelGateway(
            "key",
            self.store,
            budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(httpx.HTTPStatusError):
            gateway.complete([], [], model_config())

        self.assertEqual(attempts, 3)
        self.assertEqual(budget.reconciliations, [(0, 0, 0, Decimal(0))])

    def test_openrouter_does_not_retry_before_long_retry_after(self) -> None:
        budget = CapturingBudget()
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, headers={"Retry-After": "120"})

        gateway = OpenRouterModelGateway(
            "key",
            self.store,
            budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _seconds: self.fail("long Retry-After must not be shortened"),
        )

        with self.assertRaises(httpx.HTTPStatusError):
            gateway.complete([], [], model_config())

        self.assertEqual(attempts, 1)
        self.assertEqual(budget.reconciliations, [(0, 0, 0, Decimal(0))])

    def test_recorded_response_replays_without_any_network_transport(self) -> None:
        gateway = RecordedModelGateway((model_payload(),), self.store)
        response = gateway.complete([], [WEB_SEARCH_TOOL_SCHEMA], model_config())
        self.assertEqual(response.telemetry.provider, "recorded")
        self.assertEqual(response.telemetry.request_count, 0)
        self.assertEqual(response.response["id"], "generation-1")

    def test_exa_normalizes_to_stable_provider_neutral_search_shape(self) -> None:
        clocks = iter((NOW, NOW + timedelta(milliseconds=125)))

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url, EXA_SEARCH_URL)
            self.assertEqual(
                json.loads(request.content),
                {
                    "query": "query",
                    "type": "auto",
                    "numResults": 10,
                    "contents": {"highlights": {"maxCharacters": 1500}},
                    "startPublishedDate": "2026-06-16",
                    "endPublishedDate": "2026-07-16",
                },
            )
            return httpx.Response(
                200,
                json={
                    "requestId": "request-id",
                    "searchType": "auto",
                    "results": [
                        {
                            "title": "Primary",
                            "url": "https://example.com",
                            "id": "https://example.com",
                            "publishedDate": "2026-07-16T00:00:00Z",
                            "author": "Author Name",
                            "image": "https://example.com/image.png",
                            "favicon": "https://example.com/favicon.ico",
                            "highlights": ["evidence"],
                        }
                    ],
                    "searchTime": 1026.9,
                    "costDollars": {"total": 0},
                    "requestCredits": 1,
                    "resolvedSearchType": "",
                },
            )

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: next(clocks),
        )
        response = provider.search("query", {}, now=NOW)
        self.assertEqual(
            response.output,
            {
                "query": "query",
                "results": [
                    {
                        "title": "Primary",
                        "url": "https://example.com",
                        "published_at": "2026-07-16T00:00:00Z",
                        "content": "evidence",
                    }
                ],
            },
        )
        self.assertEqual(response.telemetry.latency_ms, 125)
        self.assertEqual(response.telemetry.billed_cost_micros, 0)
        self.assertEqual(
            response.telemetry.nominal_cost_micros, EXA_MAX_SEARCH_COST_MICROS
        )

    def test_exa_search_enforces_highlight_budget_and_date_options(self) -> None:
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [], "costDollars": {"total": 0}})

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        provider.search(
            "query",
            {
                "max_highlight_length": 1500,
                "num_results": 10,
                "start_published_date": 30,
                "end_published_date": 0,
            },
            now=NOW,
        )
        self.assertEqual(
            requests,
            [
                {
                    "query": "query",
                    "type": "auto",
                    "numResults": 10,
                    "contents": {"highlights": {"maxCharacters": 1500}},
                    "startPublishedDate": "2026-06-16",
                    "endPublishedDate": "2026-07-16",
                }
            ],
        )

        with self.assertRaisesRegex(ProviderConfigurationError, "must not exceed 15000"):
            provider.search(
                "query",
                {"max_highlight_length": 1501, "num_results": 10},
                now=NOW,
            )
        self.assertEqual(len(requests), 1)

        provider.search(
            "query",
            {
                "max_highlight_length": 1500,
                "num_results": 10,
                "start_published_date": "2026-07-01",
                "end_published_date": "2026-07-24",
            },
            now=NOW,
        )
        self.assertEqual(requests[-1]["startPublishedDate"], "2026-07-01")
        self.assertEqual(requests[-1]["endPublishedDate"], "2026-07-24")

    def test_exa_fetch_full_text_uses_contents_and_filters_metadata(self) -> None:
        clocks = iter((NOW, NOW + timedelta(milliseconds=125)))

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url, EXA_CONTENTS_URL)
            self.assertEqual(request.headers["Authorization"], "Bearer exa-key")
            self.assertEqual(
                json.loads(request.content),
                {
                    "urls": ["https://example.com/page"],
                    "text": {"maxCharacters": 4000},
                },
            )
            return httpx.Response(
                200,
                json={
                    "requestId": "request-id",
                    "results": [
                        {
                            "title": "Page Title",
                            "url": "https://example.com/page",
                            "id": "https://example.com/page",
                            "publishedDate": "2024-01-15T00:00:00.000Z",
                            "author": "Author Name",
                            "image": "https://example.com/image.png",
                            "favicon": "https://example.com/favicon.ico",
                            "text": "Full page content",
                            "highlights": ["ignored in full text mode"],
                            "highlightScores": [0.46],
                        }
                    ],
                    "statuses": [
                        {"id": "https://example.com/page", "status": "success"}
                    ],
                    "costDollars": {"total": 0.003},
                    "requestCredits": 1,
                },
            )

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: next(clocks),
        )
        response = provider.fetch(
            "https://example.com/page",
            {"result_type": "full_text"},
        )

        self.assertEqual(
            response.output,
            {
                "title": "Page Title",
                "url": "https://example.com/page",
                "published_at": "2024-01-15T00:00:00.000Z",
                "author": "Author Name",
                "full_text": "Full page content",
            },
        )
        self.assertEqual(response.telemetry.usage_kind, "web_search")
        self.assertEqual(response.telemetry.latency_ms, 125)

    def test_exa_fetch_highlights_forwards_guiding_query_and_length(self) -> None:
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Page Title",
                            "url": "https://example.com/page",
                            "publishedDate": None,
                            "author": None,
                            "highlights": ["Relevant excerpt"],
                        }
                    ],
                    "statuses": [
                        {"id": "https://example.com/page", "status": "success"}
                    ],
                },
            )

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        response = provider.fetch(
            "https://example.com/page",
            {"highlight_query": "relevant evidence", "max_length": 12_000},
        )

        self.assertEqual(
            requests,
            [
                {
                    "urls": ["https://example.com/page"],
                    "highlights": {
                        "query": "relevant evidence",
                        "maxCharacters": 12_000,
                    },
                }
            ],
        )
        self.assertEqual(response.output["highlights"], ["Relevant excerpt"])

    def test_exa_fetch_rejects_invalid_mode_options_and_content_status(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "statuses": [
                        {
                            "id": "https://example.com/page",
                            "status": "error",
                            "error": {"tag": "SOURCE_NOT_AVAILABLE"},
                        }
                    ],
                },
            )

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaisesRegex(ProviderConfigurationError, "invalid"):
            provider.fetch(
                "https://example.com/page",
                {"result_type": "full_text", "highlight_query": "not allowed"},
            )
        with self.assertRaisesRegex(ProviderConfigurationError, "12000"):
            provider.fetch(
                "https://example.com/page",
                {"result_type": "highlights", "max_length": 12_001},
            )
        with self.assertRaisesRegex(ProviderPayloadError, "SOURCE_NOT_AVAILABLE"):
            provider.fetch(
                "https://example.com/page",
                {"result_type": "highlights"},
            )
        self.assertEqual(requests, 1)

    def test_exa_search_rejects_invalid_published_date_ranges_before_request(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(500)

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaisesRegex(ProviderConfigurationError, "must not be later"):
            provider.search(
                "query",
                {"start_published_date": 0, "end_published_date": 30},
                now=NOW,
            )
        with self.assertRaisesRegex(ProviderConfigurationError, "YYYY-MM-DD"):
            provider.search(
                "query",
                {"start_published_date": "not-a-date"},
                now=NOW,
            )
        self.assertEqual(requests, 0)

    def test_search_result_count_above_ten_fails_before_provider_request(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(500)

        provider = ExaResearchProvider(
            "exa-key",
            self.store,
            self.budget,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaisesRegex(ValueError, "between 1 and 10"):
            provider.search("query", {"num_results": 11})
        self.assertEqual(requests, 0)

    def test_search_providers_reserve_owner_confirmed_worst_case_costs(self) -> None:
        budget = CapturingBudget()
        exa = ExaResearchProvider(
            "exa-key",
            self.store,
            budget,
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        json={"results": [], "costDollars": {"total": 0}},
                    )
                )
            ),
        )
        exa.search("query", {})
        self.assertEqual(budget.estimates, [("exa", 20_000)])
        self.assertEqual(
            budget.reservations,
            [("exa", 1, EXA_MAX_CREDITS_PER_SEARCH)],
        )
        self.assertEqual(budget.reconciliations, [(0, 20_000, 1, Decimal(1))])
        self.assertEqual(EXA_MAX_CREDITS_PER_SEARCH, Decimal(10))
        self.assertEqual(EXA_MAX_SEARCH_COST_MICROS, 20_000)
        self.assertEqual(TAVILY_MAX_SEARCH_COST_MICROS, 8_000)
        with self.assertRaisesRegex(ValueError, "unsupported search options"):
            exa.search("query", {"estimated_cost_micros": 1})

    def test_exa_cost_dollars_is_nominal_and_does_not_halt_free_route(self) -> None:
        budget = CapturingBudget()
        exa = ExaResearchProvider(
            "exa-key",
            self.store,
            budget,
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        json={
                            "results": [],
                            "costDollars": {"total": 0.001},
                            "requestCredits": "1.5",
                        },
                    )
                )
            ),
        )
        response = exa.search("query", {})
        self.assertEqual(response.telemetry.billed_cost_micros, 0)
        self.assertEqual(response.telemetry.nominal_cost_micros, 20_000)
        self.assertEqual(budget.reconciliations, [(0, 20_000, 1, Decimal("1.5"))])

    def test_tavily_is_disabled_without_inventing_or_requiring_a_key(self) -> None:
        provider = TavilyResearchProvider(None, self.store, self.budget)
        with self.assertRaises(ProviderDisabled):
            provider.search("query", {})

    def test_secret_redaction_covers_keys_and_bearer_values(self) -> None:
        redacted = canonical_redacted_json(
            {"api_key": "secret", "nested": {"Authorization": "Bearer abc.def"}}
        )
        self.assertNotIn(b"secret", redacted)
        self.assertNotIn(b"abc.def", redacted)
        self.assertIn(b"[REDACTED]", redacted)


if __name__ == "__main__":
    unittest.main()
