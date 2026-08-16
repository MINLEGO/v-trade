from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vtrade.api import AdminSettings, create_app
from vtrade.config import load_experiment_config
from vtrade.dashboard.policy import (
    freshness_max_age_seconds,
    position_valuation_max_age_seconds,
)
from vtrade.dashboard.repository import (
    _FRESHNESS,
    DashboardFilters,
    DashboardPage,
    DashboardWindow,
    PostgresDashboardRepository,
)
from vtrade.dashboard.service import build_cycle_diagnostics

SECRET = "a-strong-admin-secret-with-32-bytes"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000012")
CYCLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000013")


class _FakeStorage:
    def validate(self) -> None:
        return None


class _FakeAdminRepository:
    def probe(self) -> dict[str, object]:
        return {"status": "ok"}

    def overview(self) -> dict[str, object]:
        return {}

    def view(self, _name: str, **_kwargs: object) -> list[dict[str, object]]:
        return []

    def set_global_pause(self, **_kwargs: object) -> dict[str, object]:
        return {}

    def set_agent_pause(self, _agent_id: uuid.UUID, **_kwargs: object) -> dict[str, object]:
        return {}


class _FakeDashboardRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object | None]] = []

    def overview(self, filters: DashboardFilters) -> dict[str, object]:
        self.calls.append(("overview", filters, None))
        return {"filters": {"window": filters.window.value}, "performance": {}}

    def agents(self, filters: DashboardFilters, *, page: DashboardPage) -> list[dict[str, object]]:
        self.calls.append(("agents", filters, page))
        return [{"agent_id": str(AGENT_ID), "current_beliefs": [], "active_plans": []}]

    def cycles(self, filters: DashboardFilters, *, page: DashboardPage) -> list[dict[str, object]]:
        self.calls.append(("cycles", filters, page))
        return [{"cycle_id": str(CYCLE_ID), "status": "completed"}]

    def cycle_detail(self, cycle_id: uuid.UUID) -> dict[str, object] | None:
        self.calls.append(("cycle_detail", cycle_id, None))
        if cycle_id != CYCLE_ID:
            return None
        return {"metadata": {"cycle_id": str(cycle_id)}, "model_turns": []}


class _ScriptedCursor:
    def __init__(self) -> None:
        self.description: Sequence[Sequence[object]] | None = None
        self.rows: list[Sequence[object]] = []
        self.selected: Sequence[object] | None = None
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> _ScriptedCursor:
        self.queries.append((query, tuple(params)))
        self.description = None
        self.rows = []
        self.selected = None
        if "WHERE ac.id = %s::uuid" in query:
            self._row(
                (
                    "cycle_id",
                    "status",
                    "model_termination_status",
                    "account_value_micros",
                    "entry_fees_micros",
                ),
                (CYCLE_ID, "completed", "stop", 12_500_000, 120_000),
            )
        elif "FROM agent_cycles ac" in query and "LIMIT %s OFFSET %s" in query:
            self._rows(("cycle_id", "agent_id", "status"), [(CYCLE_ID, AGENT_ID, "completed")])
        elif "FROM model_turns mt" in query:
            self._rows(
                ("id", "turn_index", "reasoning", "retention_purged"),
                [(uuid.UUID(int=101), 0, "I should inspect the source.", False)],
            )
        elif "FROM tool_calls tc" in query:
            self._rows(
                ("id", "tool_name", "arguments", "success", "error"),
                [
                    (uuid.UUID(int=102), "web_search", {"query": "rates"}, True, None),
                    (uuid.UUID(int=103), "get_market", {"id": "x"}, False, "timeout"),
                ],
            )
        elif "FROM research_artifacts ra" in query:
            self._rows(
                ("id", "tool_call_id", "query", "canonical_url", "title"),
                [
                    (
                        uuid.UUID(int=104),
                        uuid.UUID(int=102),
                        "rates",
                        "https://example.test/rates",
                        "Rates report",
                    )
                ],
            )
        elif "FROM provider_usage pu" in query:
            self._rows(
                ("id", "reasoning_tokens", "billed_cost_micros"),
                [(uuid.UUID(int=105), 12, 44)],
            )
        elif "FROM belief_revisions br" in query:
            self._rows(
                ("revision_id", "belief_id", "content", "confidence"),
                [(uuid.UUID(int=106), uuid.UUID(int=107), "Rates may fall", 0.7)],
            )
        elif "FROM plan_revisions pr" in query:
            self._rows(
                ("revision_id", "plan_id", "plan_type", "content"),
                [(uuid.UUID(int=108), uuid.UUID(int=109), "next_cycle", "Check the next release")],
            )
        elif "FROM order_intents oi" in query:
            self._rows(
                ("intent_id", "thesis", "estimated_probability", "fill_id", "fill_price"),
                [(uuid.UUID(int=110), "Rates will fall", 0.67, uuid.UUID(int=111), 0.61)],
            )
        elif "FROM runtime_cycle_steps" in query:
            self._rows(("id", "stage", "status"), [(uuid.UUID(int=112), "harness", "completed")])
        elif "FROM agents a" in query and "current_beliefs" in query:
            self._rows(("agent_id", "current_beliefs", "active_plans"), [(AGENT_ID, [], [])])
        elif "AS performance_points" in query:
            self._row(("agents",), (1,))
        elif "AS total_cycles" in query:
            self._row(("total_cycles",), (1,))
        elif "AS prompt_tokens" in query:
            self._row(("prompt_tokens",), (2,))
        elif "AS open_alerts" in query:
            self._row(("open_alerts",), (0,))
        else:
            raise AssertionError(f"unexpected dashboard query: {query}")
        return self

    def fetchone(self) -> Sequence[object] | None:
        row = self.selected
        self.selected = None
        return row

    def fetchall(self) -> Sequence[Sequence[object]]:
        return self.rows

    def _row(self, names: Sequence[str], row: Sequence[object]) -> None:
        self.description = tuple((name,) for name in names)
        self.selected = row

    def _rows(self, names: Sequence[str], rows: list[Sequence[object]]) -> None:
        self.description = tuple((name,) for name in names)
        self.rows = rows


class _ScriptedConnection:
    def __init__(self) -> None:
        self.cursor_instance = _ScriptedCursor()

    def __enter__(self) -> _ScriptedConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_instance


class TestDashboardWindow:
    def test_window_and_since_are_bounded_and_timezone_aware(self) -> None:
        assert DashboardWindow("24h") is DashboardWindow.LAST_24_HOURS
        assert DashboardWindow("7d") is DashboardWindow.LAST_7_DAYS
        assert DashboardWindow("30d") is DashboardWindow.LAST_30_DAYS
        assert DashboardWindow("all") is DashboardWindow.ALL
        assert DashboardFilters(DashboardWindow.LAST_7_DAYS).since(NOW) == NOW - timedelta(days=7)
        assert DashboardFilters(DashboardWindow.ALL).since(NOW) is None

    def test_invalid_window_page_and_naive_clock_are_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            DashboardWindow("90d")
        with pytest.raises(ValueError, match="between 1 and 200"):
            DashboardPage(limit=201)
        with pytest.raises(ValueError, match="timezone-aware"):
            DashboardFilters().since(datetime(2026, 7, 30, 12, 0))

    def test_position_valuation_uses_the_experiment_definition_policy(self) -> None:
        active = load_experiment_config(
            "config/experiments/predictionarena-polymarket-v1-liquidity-aware.json"
        ).raw
        historical = load_experiment_config(
            "config/experiments/predictionarena-polymarket-v1.json"
        ).raw
        assert (
            active["execution"]["maximum_order_book_age_seconds"]
            == freshness_max_age_seconds(active)
        )
        assert position_valuation_max_age_seconds(active) == 1800
        assert position_valuation_max_age_seconds(historical) == 300
        assert position_valuation_max_age_seconds({}) == 300

    def test_freshness_query_reads_active_order_book_age_policy(self) -> None:
        assert "experiment_definitions" in _FRESHNESS
        assert "experiment_runs" in _FRESHNESS
        assert "maximum_order_book_age_seconds" in _FRESHNESS
        policy = {
            "execution": {"maximum_order_book_age_seconds": 42},
            "limits": {"maximum_archived_bid_age_seconds": 1800},
            "owner_decisions": {"no_bid_valuation": {"maximum_age_seconds": 1800}},
        }
        assert freshness_max_age_seconds(policy) == 42
        assert freshness_max_age_seconds({"limits": {"maximum_archived_bid_age_seconds": 17}}) == 17


class TestDashboardRepository:
    def _repository(self) -> tuple[PostgresDashboardRepository, _ScriptedConnection]:
        connection = _ScriptedConnection()
        repository = PostgresDashboardRepository(
            "postgresql://unused", connect=lambda _url: connection, clock=lambda: NOW
        )
        return repository, connection

    def test_agents_and_cycles_scope_queries_and_keep_pages_bounded(self) -> None:
        repository, connection = self._repository()
        filters = DashboardFilters(DashboardWindow.LAST_30_DAYS, RUN_ID, AGENT_ID)

        assert repository.agents(filters, page=DashboardPage(200, 3))[0]["agent_id"] == AGENT_ID
        assert repository.cycles(filters, page=DashboardPage(200, 4))[0]["cycle_id"] == CYCLE_ID

        agents_query, agents_params = connection.cursor_instance.queries[0]
        cycles_query, cycles_params = connection.cursor_instance.queries[1]
        since = NOW - timedelta(days=30)
        assert "LIMIT %s OFFSET %s" in agents_query
        assert "LIMIT %s OFFSET %s" in cycles_query
        assert "entry_fees_micros" in agents_query
        assert "no_bid_valuation" in agents_query
        assert agents_params[-2:] == (200, 3)
        assert cycles_params[-2:] == (200, 4)
        assert agents_params.count(RUN_ID) == 2
        assert cycles_params.count(RUN_ID) == 2
        assert agents_params.count(AGENT_ID) == 2
        assert cycles_params.count(AGENT_ID) == 2
        assert agents_params.count(since) == 6
        assert cycles_params.count(since) == 2

    def test_cycle_detail_maps_retained_reasoning_evidence_and_trade_context(self) -> None:
        repository, connection = self._repository()

        detail = repository.cycle_detail(CYCLE_ID)

        assert detail is not None
        assert detail["metadata"]["cycle_id"] == CYCLE_ID
        assert detail["performance"]["account_value_micros"] == 12_500_000
        assert detail["performance"]["entry_fees_micros"] == 120_000
        assert detail["model_turns"][0]["reasoning"] == "I should inspect the source."
        assert detail["tool_calls"][1]["error"] == "timeout"
        assert detail["research"][0]["canonical_url"] == "https://example.test/rates"
        assert detail["belief_revisions"][0]["confidence"] == 0.7
        assert detail["plan_revisions"][0]["plan_type"] == "next_cycle"
        assert detail["order_intents"][0]["fill_price"] == 0.61
        assert detail["runtime_steps"][0]["stage"] == "harness"
        assert {item["code"] for item in detail["diagnostics"]} == {"failed_tools"}
        assert len(connection.cursor_instance.queries) == 9
        assert all(params == (CYCLE_ID,) for _query, params in connection.cursor_instance.queries)


class TestDashboardDiagnostics:
    def test_diagnostics_are_deterministic_and_linked_to_evidence(self) -> None:
        detail = {
            "metadata": {"status": "completed", "harness_termination_status": "timeout"},
            "model_turns": [],
            "tool_calls": [
                {
                    "id": "failed-call",
                    "tool_name": "web_search",
                    "arguments": {"q": "x"},
                    "success": False,
                },
                {
                    "id": "repeat-a",
                    "tool_name": "web_search",
                    "arguments": {"q": "same"},
                    "success": True,
                },
                {
                    "id": "repeat-b",
                    "tool_name": "web_search",
                    "arguments": {"q": "same"},
                    "success": True,
                },
            ],
            "research": [{} for _ in range(9)],
            "order_intents": [],
        }

        diagnostics = build_cycle_diagnostics(detail)
        by_code = {item["code"]: item for item in diagnostics}
        assert by_code["failed_tools"]["evidence_ids"] == ["failed-call"]
        assert by_code["repeated_tool_calls"]["severity"] == "warning"
        assert by_code["high_search_count"]["severity"] == "info"
        assert by_code["no_action"]["severity"] == "info"
        assert by_code["termination_failure"]["severity"] == "error"
        assert by_code["missing_model_turns"]["severity"] == "warning"
        assert diagnostics == build_cycle_diagnostics(detail)


class TestDashboardApi:
    def test_dashboard_ui_assets_and_data_are_private_no_store_and_csp_protected(self) -> None:
        repository = _FakeDashboardRepository()
        app = create_app(
            settings=AdminSettings(
                "postgresql://unused",
                SECRET,
                __import__("pathlib").Path("config/experiments/predictionarena-polymarket-v1.json"),
            ),
            repository=_FakeAdminRepository(),
            dashboard_repository=repository,
            storage=_FakeStorage(),
        )
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {SECRET}"}

        for path in (
            "/",
            "/admin",
            "/admin/assets/dashboard.css",
            "/admin/assets/dashboard.js",
            "/admin/dashboard-data/overview?window=7d&run_id=" + str(RUN_ID),
            "/admin/dashboard-data/agents?agent_id=" + str(AGENT_ID) + "&limit=200&offset=2",
            "/admin/dashboard-data/cycles?window=24h&limit=200&offset=4",
            "/admin/dashboard-data/cycles/" + str(CYCLE_ID),
        ):
            unauthenticated = client.get(path)
            assert unauthenticated.status_code == 401
            assert unauthenticated.headers["cache-control"] == "no-store"

            response = client.get(path, headers=auth)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "default-src 'none'" in response.headers["content-security-policy"]

        html = client.get("/admin", headers=auth).text
        assert "legacy-views" not in html
        javascript = client.get("/admin/assets/dashboard.js", headers=auth).text
        for label in (
            "Long-term plan",
            "Next-cycle plan",
            "Open positions",
            "Research detail",
            "Tool input",
        ):
            assert label in javascript

        overview = client.get(
            "/admin/dashboard-data/overview?window=7d&run_id=" + str(RUN_ID), headers=auth
        ).json()
        assert overview["filters"]["window"] == "7d"
        agents_call = next(call for call in repository.calls if call[0] == "agents")
        assert agents_call[1].agent_id == AGENT_ID
        assert agents_call[2] == DashboardPage(200, 2)
        cycles_call = next(call for call in repository.calls if call[0] == "cycles")
        assert cycles_call[1].window is DashboardWindow.LAST_24_HOURS
        assert cycles_call[2] == DashboardPage(200, 4)

    def test_dashboard_api_rejects_invalid_window_uuid_and_unbounded_page(self) -> None:
        app = create_app(
            settings=AdminSettings(
                "postgresql://unused",
                SECRET,
                __import__("pathlib").Path("config/experiments/predictionarena-polymarket-v1.json"),
            ),
            repository=_FakeAdminRepository(),
            dashboard_repository=_FakeDashboardRepository(),
            storage=_FakeStorage(),
        )
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {SECRET}"}

        assert (
            client.get("/admin/dashboard-data/overview?window=90d", headers=auth).status_code == 422
        )
        assert (
            client.get("/admin/dashboard-data/agents?agent_id=nope", headers=auth).status_code
            == 422
        )
        assert client.get("/admin/dashboard-data/cycles?limit=201", headers=auth).status_code == 422
        assert (
            client.get("/admin/dashboard-data/cycles/not-a-uuid", headers=auth).status_code == 422
        )
