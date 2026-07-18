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
    EXA_MAX_CREDITS_PER_SEARCH,
    EXA_MAX_SEARCH_COST_MICROS,
    EXA_SEARCH_URL,
    OPENROUTER_URL,
    TAVILY_MAX_SEARCH_COST_MICROS,
    WEB_SEARCH_TOOL_SCHEMA,
    BudgetExceeded,
    BudgetReservation,
    ExaResearchProvider,
    OpenRouterModelGateway,
    ProviderDisabled,
    ProviderPayloadError,
    RecordedModelGateway,
    TavilyResearchProvider,
    canonical_redacted_json,
)

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)


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


def model_config(slug: str = "deepseek/deepseek-v4-flash") -> dict:
    return {
        "slug": slug,
        "allowed_quantizations": ["fp8"]
        if slug.startswith("deepseek/")
        else ["fp8", "unknown"],
        "provider_allowlist": None,
        "provider_selection": "all_compatible_sorted_by_price",
        "allow_provider_fallbacks": True,
        "cross_model_fallback": False,
        "reasoning_effort": "max",
        "reasoning_effort_policy": "owner_fixed",
        "estimated_max_cost_micros": 100_000,
        "maximum_context_tokens": 100_000,
        "maximum_prompt_tokens": 10_000,
        "maximum_output_tokens": 1_000,
        "provider_max_price": {"prompt": "0.1", "completion": "0.1", "request": "0"},
    }


def model_payload(*, model: str = "deepseek/deepseek-v4-flash") -> bytes:
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
        self.assertEqual(body["model"], "deepseek/deepseek-v4-flash")
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

    def test_openrouter_cross_model_response_fails_closed(self) -> None:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, content=model_payload(model="xiaomi/mimo-v2.5-pro")
                )
            )
        )
        gateway = OpenRouterModelGateway("key", self.store, self.budget, client=client)
        with self.assertRaises(ProviderPayloadError):
            gateway.complete([], [], model_config())

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
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Primary",
                            "url": "https://example.com",
                            "publishedDate": "2026-07-16T00:00:00Z",
                            "highlights": ["evidence"],
                        }
                    ],
                    "costDollars": {"total": 0},
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
        response = provider.search("query", {"num_results": 5})
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

    def test_exa_positive_billed_cost_is_reconciled_then_raises(self) -> None:
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
        with self.assertRaisesRegex(BudgetExceeded, "Exa reported a billed cost"):
            exa.search("query", {})
        self.assertEqual(budget.reconciliations, [(1_000, 20_000, 1, Decimal("1.5"))])

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
