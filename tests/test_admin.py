from __future__ import annotations

import base64
import json
import tempfile
import unittest
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from vtrade.admin import InvalidOperatorAction, Page, PostgresAdminRepository
from vtrade.api import AdminSettings, create_app

SECRET = "a-strong-admin-secret-with-32-bytes"
AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)


class FakeStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def validate(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("storage unavailable with secret detail")


class FakeAdminRepository:
    def __init__(self, *, fail_probe: bool = False) -> None:
        self.fail_probe = fail_probe
        self.controls: list[tuple[str, str, bool, str, str]] = []

    def probe(self) -> dict[str, object]:
        if self.fail_probe:
            raise RuntimeError("database unavailable with secret detail")
        return {"database": "vtrade", "latest_migration": "0006_private_admin.sql"}

    def overview(self) -> dict[str, object]:
        return {"runs": {"running_runs": 1}, "controls": {"globally_paused": False}}

    def view(
        self,
        name: str,
        *,
        page: Page | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> list[dict[str, object]]:
        return [{"view": name, "agent_id": str(agent_id) if agent_id else None}]

    def set_global_pause(
        self,
        *,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, object]:
        self.controls.append(("system", "", paused, actor_id, idempotency_key))
        return {"globally_paused": paused}

    def set_agent_pause(
        self,
        agent_id: uuid.UUID,
        *,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, object]:
        self.controls.append(("agent", str(agent_id), paused, actor_id, idempotency_key))
        return {"agent_id": str(agent_id), "paused_at": NOW if paused else None}


def _settings(
    config: Path = Path("config/experiments/predictionarena-polymarket-v1.json"),
) -> AdminSettings:
    return AdminSettings("postgresql://unused", SECRET, config)


class PrivateAdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeAdminRepository()
        self.storage = FakeStorage()
        self.client = TestClient(
            create_app(
                settings=_settings(), repository=self.repository, storage=self.storage
            )
        )
        self.auth = {"Authorization": f"Bearer {SECRET}"}

    def test_every_registered_route_requires_auth_and_schema_routes_are_disabled(self) -> None:
        for path in ("/", "/admin", "/health/live", "/health/ready", "/admin/positions"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"detail": "unauthorized"})
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/redoc").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        self.assertEqual(
            self.client.get("/health/live", headers={"Authorization": "Bearer wrong"}).status_code,
            401,
        )

    def test_bearer_and_basic_authentication_and_security_headers(self) -> None:
        bearer = self.client.get("/health/live", headers=self.auth)
        self.assertEqual(bearer.status_code, 200)
        token = base64.b64encode(f"operator:{SECRET}".encode()).decode()
        basic = self.client.get("/", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(basic.status_code, 200)
        self.assertIn("V-Trade private admin", basic.text)
        for response in (bearer, basic):
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("default-src 'none'", response.headers["content-security-policy"])

    def test_dashboard_contains_every_required_operator_view(self) -> None:
        response = self.client.get("/admin", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        for label in (
            "Leaderboard and PnL",
            "Positions and executable bid valuation",
            "Trades",
            "Settlements",
            "Rejections",
            "Cycles and decision versions",
            "Model and search usage and cost",
            "Data freshness",
            "Configuration, prompt, model and code versions",
            "Alerts",
        ):
            self.assertIn(label, response.text)

    def test_views_are_bounded_and_can_filter_by_agent(self) -> None:
        response = self.client.get(
            f"/admin/trades?agent_id={AGENT_ID}&limit=500&offset=2", headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["agent_id"], str(AGENT_ID))
        self.assertEqual(
            self.client.get("/admin/trades?limit=501", headers=self.auth).status_code,
            422,
        )

    def test_pause_resume_require_operator_and_idempotency_headers(self) -> None:
        path = f"/admin/agents/{AGENT_ID}/pause"
        self.assertEqual(self.client.post(path, headers=self.auth).status_code, 422)
        headers = {
            **self.auth,
            "X-Operator-Id": "ops@example.test",
            "Idempotency-Key": "phase6-agent-pause-1",
        }
        response = self.client.post(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.repository.controls[-1],
            ("agent", str(AGENT_ID), True, "ops@example.test", "phase6-agent-pause-1"),
        )

    def test_readiness_probes_real_components_with_runnable_configuration(self) -> None:
        response = self.client.get("/health/ready", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["checks"]["configuration"]["status"], "ok")
        self.assertEqual(payload["checks"]["database"]["database"], "vtrade")
        self.assertEqual(payload["checks"]["supabase_storage"]["status"], "ok")
        self.assertEqual(self.storage.calls, 1)

    def test_readiness_sanitizes_component_failures(self) -> None:
        app = create_app(
            settings=_settings(),
            repository=FakeAdminRepository(fail_probe=True),
            storage=FakeStorage(fail=True),
        )
        response = TestClient(app).get("/health/ready", headers=self.auth)
        encoded = response.text
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret detail", encoded)
        self.assertEqual(response.json()["checks"]["database"], {"status": "failed"})

    def test_readiness_can_succeed_with_resolved_configuration(self) -> None:
        source = json.loads(
            Path("config/experiments/predictionarena-polymarket-v1.json").read_text(
                encoding="utf-8"
            )
        )
        for decision in source["owner_decisions"].values():
            decision["status"] = "resolved"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            app = create_app(
                settings=_settings(path),
                repository=FakeAdminRepository(),
                storage=FakeStorage(),
            )
            response = TestClient(app).get("/health/ready", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_short_admin_secret_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "at least 32 bytes"):
            AdminSettings("postgresql://unused", "short", Path("config.json"))


class ScriptedCursor:
    def __init__(self) -> None:
        self.description: Sequence[Sequence[object]] | None = None
        self.selected: Sequence[object] | None = None
        self.rows: list[Sequence[object]] = []
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.globally_paused = False
        self.version = 1
        self.actions: dict[str, tuple[object, ...]] = {}
        self.agent_paused: dict[uuid.UUID, datetime | None] = {AGENT_ID: None}

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: Sequence[object] = ()) -> ScriptedCursor:
        values = tuple(params)
        self.queries.append((query, values))
        self.description = None
        self.selected = None
        self.rows = []
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT actor_id, action, target_type"):
            self.selected = self.actions.get(str(values[0]))
        elif "FROM system_controls WHERE singleton = true FOR UPDATE" in normalized:
            self.selected = (self.globally_paused, self.version, NOW, "migration")
        elif normalized.startswith("UPDATE system_controls"):
            self.globally_paused = bool(values[0])
            self.version += 1
            self.selected = (self.globally_paused, self.version, values[1], values[2])
        elif normalized.startswith("SELECT paused_at FROM agents"):
            agent = uuid.UUID(str(values[0]))
            if agent in self.agent_paused:
                self.selected = (self.agent_paused[agent],)
        elif normalized.startswith("UPDATE agents SET paused_at"):
            agent = uuid.UUID(str(values[1]))
            self.agent_paused[agent] = values[0] if isinstance(values[0], datetime) else None
            self.selected = (self.agent_paused[agent],)
        elif normalized.startswith("INSERT INTO operator_actions"):
            self.actions[str(values[8])] = (
                values[1],
                values[2],
                values[3],
                values[4],
                values[6],
            )
        elif "FROM alerts al" in normalized:
            self.description = (("id",), ("severity",))
            self.rows = [(uuid.UUID(int=1), "warning")]
        elif "FROM positions p" in normalized:
            self.description = (("id",),)
            self.rows = []
        return self

    def fetchone(self) -> Sequence[object] | None:
        selected = self.selected
        self.selected = None
        return selected

    def fetchall(self) -> Sequence[Sequence[object]]:
        return self.rows


class ScriptedConnection:
    def __init__(self) -> None:
        self.cursor_instance = ScriptedCursor()
        self.transactions = 0

    def __enter__(self) -> ScriptedConnection:
        self.transactions += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> ScriptedCursor:
        return self.cursor_instance


class AdminRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ScriptedConnection()

        def connect(_url: str) -> AbstractContextManager[Any]:
            return self.connection

        self.repository = PostgresAdminRepository("postgresql://unused", connect=connect)

    def test_fixed_view_maps_columns_and_rejects_unregistered_query(self) -> None:
        self.assertEqual(
            self.repository.view("alerts", page=Page(5, 2)),
            [{"id": uuid.UUID(int=1), "severity": "warning"}],
        )
        query, params = self.connection.cursor_instance.queries[-1]
        self.assertIn("FROM alerts al", query)
        self.assertEqual(params, (5, 2))
        with self.assertRaises(ValueError):
            self.repository.view("arbitrary_sql")

    def test_pause_is_atomic_audited_and_idempotent(self) -> None:
        first = self.repository.set_global_pause(
            paused=True,
            actor_id="ops@example.test",
            idempotency_key="pause-system-0001",
            occurred_at=NOW,
        )
        second = self.repository.set_global_pause(
            paused=True,
            actor_id="ops@example.test",
            idempotency_key="pause-system-0001",
            occurred_at=NOW,
        )
        self.assertEqual(first, second)
        inserts = [
            query
            for query, _params in self.connection.cursor_instance.queries
            if query.startswith("INSERT INTO operator_actions")
        ]
        self.assertEqual(len(inserts), 1)
        self.assertTrue(first["globally_paused"])

    def test_idempotency_conflict_fails_without_second_mutation(self) -> None:
        self.repository.set_agent_pause(
            AGENT_ID,
            paused=True,
            actor_id="operator-1",
            idempotency_key="agent-control-0001",
            occurred_at=NOW,
        )
        with self.assertRaises(InvalidOperatorAction):
            self.repository.set_agent_pause(
                AGENT_ID,
                paused=False,
                actor_id="operator-1",
                idempotency_key="agent-control-0001",
                occurred_at=NOW,
            )

    def test_position_query_blocks_stale_and_missing_bid_valuation(self) -> None:
        self.repository.view("positions", agent_id=AGENT_ID)
        query = self.connection.cursor_instance.queries[-1][0]
        self.assertIn("interval '300 seconds'", query)
        self.assertIn("THEN 'stale'", query)
        self.assertIn("liquidation_value_micros", query)

    def test_phase_six_migration_has_singleton_control_and_audit_indexes(self) -> None:
        migration = Path("migrations/0006_private_admin.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE system_controls", migration)
        self.assertIn("globally_paused", migration)
        self.assertIn("operator_actions_occurred_idx", migration)
        foundation = Path("migrations/0001_foundation.sql").read_text(encoding="utf-8")
        self.assertIn("operator_actions_append_only", foundation)


if __name__ == "__main__":
    unittest.main()
