from __future__ import annotations

import hashlib
import threading
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx

from vtrade.artifacts import ArtifactRef
from vtrade.deadline import DeadlineExceeded, run_with_deadline
from vtrade.domain.types import CatalogueScanRequest, MarketKey, RawArtifact
from vtrade.kalshi import KalshiDeadlineExceeded, KalshiPublicRestAdapter
from vtrade.kalshi_freeze import KalshiMarketFreezeService
from vtrade.kalshi_persistence import PostgresKalshiFreezeRepository
from vtrade.postgres_runtime import PostgresRuntimeRepository
from vtrade.runtime import (
    BrokerExecutionResult,
    CycleClaim,
    CycleOrchestrator,
    HarnessExecutionResult,
    MarketFreezeResult,
    PreSettlementResult,
    PromptResult,
    SettlementValuationResult,
)

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


class _RecordingRepository:
    def __init__(self) -> None:
        self.failure: str | None = None

    def load_stage(self, *_args: object, **_kwargs: object) -> None:
        return None

    def renew_lease(self, *_args: object, **_kwargs: object) -> None:
        return None

    def begin_stage(self, *_args: object, **_kwargs: object) -> None:
        return None

    def complete_stage(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("a timed-out freeze must not be checkpointed as completed")

    def fail_cycle(self, *_args: object, **kwargs: object) -> int:
        self.failure = str(kwargs["reason"])
        return 1

    def open_alert(self, *_args: object, **_kwargs: object) -> None:
        return None


class _BlockedFreeze:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def freeze(self, _claim: CycleClaim, *, deadline: float | None = None) -> MarketFreezeResult:
        del deadline
        self.started.set()
        self.release.wait()
        return MarketFreezeResult({"snapshot": "unexpected"}, (), NOW)


class _BlockingArtifactStore:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def put(self, content: bytes) -> ArtifactRef:
        self.started.set()
        try:
            self.release.wait()
            digest = hashlib.sha256(content).hexdigest()
            return ArtifactRef(digest, len(content), digest)
        finally:
            self.finished.set()


class _CutoffReplay:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"market_settled_ts":"2026-08-01T00:00:00Z"}',
            request=request,
        )


class _BlockedVenue:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def get_context(self, *_args: object, **_kwargs: object) -> object:
        self.started.set()
        self.release.wait()
        return None


class _BlockingCursor:
    def __init__(self) -> None:
        self.rowcount = 1
        self.queries: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._blocked = False

    def __enter__(self) -> _BlockingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.finished.set()

    def execute(self, query: str, _params: tuple[object, ...] = ()) -> _BlockingCursor:
        self.queries.append(query)
        if not self._blocked:
            self._blocked = True
            self.started.set()
            self.release.wait()
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _BlockingConnection:
    def __init__(self, cursor: _BlockingCursor) -> None:
        self.cursor_instance = cursor
        self.committed = False

    def __enter__(self) -> _BlockingConnection:
        return self

    def __exit__(self, exc_type: object, *_args: object) -> None:
        self.committed = exc_type is None

    def cursor(self) -> _BlockingCursor:
        return self.cursor_instance


class _RecoveryCursor:
    def __init__(self) -> None:
        self.rowcount = 1
        self.queries: list[str] = []
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _RecoveryCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _params: tuple[object, ...] = ()) -> _RecoveryCursor:
        self.queries.append(query)
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT pg_try_advisory"):
            self.rows = [(True,)]
        elif normalized.startswith("SELECT cycles.id"):
            self.rows = [
                (CYCLE_ID, AGENT_ID, NOW - timedelta(minutes=10), None, 3)
            ]
        else:
            self.rows = []
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self.rows = self.rows, []
        return rows


class _SingleCursorConnection:
    def __init__(self, cursor: _RecoveryCursor) -> None:
        self.cursor_instance = cursor

    def __enter__(self) -> _SingleCursorConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _RecoveryCursor:
        return self.cursor_instance


AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000071")
CYCLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")


class _UnusedPorts:
    def settle_before_prompt(
        self, _claim: CycleClaim, _frozen: dict[str, object]
    ) -> PreSettlementResult:
        return PreSettlementResult({}, (), 0)

    def render(self, _claim: CycleClaim, _frozen: dict[str, object]) -> PromptResult:
        return PromptResult({}, (), 1)

    def run(
        self,
        _claim: CycleClaim,
        _frozen: dict[str, object],
        _prompt: dict[str, object],
    ) -> HarnessExecutionResult:
        return HarnessExecutionResult({}, (), 0, 0)

    def execute(
        self,
        _claim: CycleClaim,
        _frozen: dict[str, object],
        _harness: dict[str, object],
    ) -> BrokerExecutionResult:
        return BrokerExecutionResult({}, (), 0)

    def settle_and_value(
        self,
        _claim: CycleClaim,
        _frozen: dict[str, object],
        _broker: dict[str, object],
    ) -> SettlementValuationResult:
        return SettlementValuationResult({}, (), 0, 0, 0)


def _claim() -> CycleClaim:
    return CycleClaim(
        uuid.uuid4(),
        uuid.uuid4(),
        NOW,
        None,
        "worker-1",
        NOW + timedelta(minutes=70),
    )


class Issue21MarketFreezeTests(unittest.TestCase):
    def test_blocked_market_freeze_is_failed_at_stage_deadline(self) -> None:
        repository = _RecordingRepository()
        blocked = _BlockedFreeze()
        ports = _UnusedPorts()
        orchestrator = CycleOrchestrator(
            repository=repository,
            market_freezer=blocked,
            pre_settlement=ports,
            prompt=ports,
            harness=ports,
            broker=ports,
            settlement_valuation=ports,
            clock=lambda: NOW,
            market_freeze_deadline_seconds=0.05,
        )
        errors: list[BaseException] = []

        def run() -> None:
            try:
                orchestrator.run(_claim())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(
            target=run, daemon=True
        )

        thread.start()
        self.assertTrue(blocked.started.wait(1.0))
        thread.join(1.0)
        try:
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(repository.failure)
            self.assertIn("deadline", repository.failure.lower())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DeadlineExceeded)
        finally:
            blocked.release.set()
            thread.join(1.0)

    def test_blocked_artifact_upload_obeys_the_shared_deadline(self) -> None:
        store = _BlockingArtifactStore()
        client = httpx.Client(transport=httpx.MockTransport(_CutoffReplay()))
        adapter = KalshiPublicRestAdapter(store, client=client, clock=lambda: NOW)
        started_at = time.monotonic()
        try:
            with self.assertRaises(KalshiDeadlineExceeded):
                adapter.scan_catalogue(
                    CatalogueScanRequest(cutoff=NOW),
                    deadline=started_at + 0.05,
                )
            self.assertLess(time.monotonic() - started_at, 1.0)
            self.assertTrue(store.started.is_set())
        finally:
            store.release.set()
            self.assertTrue(store.finished.wait(1.0))
            client.close()

    def test_order_book_executor_does_not_wait_after_deadline(self) -> None:
        venue = _BlockedVenue()
        service = KalshiMarketFreezeService(venue)
        started_at = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "deadline exceeded"):
                service._read_contexts((MarketKey("KX-1"),), NOW, started_at + 0.05)
            self.assertLess(time.monotonic() - started_at, 1.0)
        finally:
            venue.release.set()

    def test_blocked_postgres_persistence_cannot_publish_a_freeze(self) -> None:
        artifact = RawArtifact("a" * 64, 1, "memory://catalogue", observed_at=NOW)
        page = SimpleNamespace(
            requested_cursor=None,
            next_cursor=None,
            observed_at=NOW,
            series=(),
            events=(),
            markets=(),
            audit=artifact,
            record_count=0,
        )
        freeze = SimpleNamespace(
            catalogue=SimpleNamespace(pages=(page,), historical_cutoff=NOW),
            data_cutoff=NOW,
            discovery_market_keys=(),
            resolution_market_keys=(),
            contexts=(),
            resolutions=(),
        )
        cursor = _BlockingCursor()
        connection = _BlockingConnection(cursor)
        repository = PostgresKalshiFreezeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        deadline = time.monotonic() + 0.05
        with self.assertRaises(DeadlineExceeded):
            run_with_deadline(
                lambda: repository.persist(
                    freeze,
                    agent_cycle_id=CYCLE_ID,
                    raw_artifact_ids={artifact.sha256: uuid.uuid4()},
                    published_at=NOW,
                    deadline=deadline,
                ),
                deadline=deadline,
                label="test persistence",
            )
        time.sleep(0.05)
        cursor.release.set()
        self.assertTrue(cursor.finished.wait(1.0))
        self.assertFalse(connection.committed)

    def test_recovery_fences_the_old_generation_and_records_stage_failure(self) -> None:
        cursor = _RecoveryCursor()
        connection = _SingleCursorConnection(cursor)
        repository = PostgresRuntimeRepository(
            "postgresql://unused", connect=lambda _url: connection
        )
        claims = repository.recover_expired_cycles(
            now=NOW,
            lease_owner="worker-1",
            lease_duration=timedelta(minutes=10),
            stale_after=timedelta(seconds=5),
            limit=1,
        )
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0].recovery)
        self.assertEqual(claims[0].attempt, 4)
        self.assertNotEqual(claims[0].lease_owner, "worker-1")
        stage_failure = next(
            index
            for index, query in enumerate(cursor.queries)
            if "UPDATE runtime_cycle_steps SET status = 'failed'" in query
        )
        owner_update = next(
            index
            for index, query in enumerate(cursor.queries)
            if query.startswith("UPDATE agent_cycles SET status = 'running'")
        )
        self.assertLess(stage_failure, owner_update)
        recovery_query = next(
            query for query in cursor.queries if query.startswith("SELECT cycles.id")
        )
        self.assertIn("cycles.started_at <=", recovery_query)
        self.assertIn("freeze_step.status <> 'completed'", recovery_query)

    def test_old_generation_cannot_fail_a_reclaimed_cycle(self) -> None:
        class StaleCursor(_RecoveryCursor):
            def execute(self, query: str, params: tuple[object, ...] = ()) -> StaleCursor:
                super().execute(query, params)
                if query.startswith("UPDATE agent_cycles SET status = 'failed'"):
                    self.rowcount = 0
                return self

        cursor = StaleCursor()
        repository = PostgresRuntimeRepository(
            "postgresql://unused",
            connect=lambda _url: _SingleCursorConnection(cursor),
        )
        claim = _claim()
        self.assertEqual(repository.fail_cycle(claim, now=NOW, reason="stale worker"), 0)
        self.assertFalse(
            any(
                "UPDATE runtime_cycle_steps SET status = 'failed'" in query
                for query in cursor.queries
            )
        )


if __name__ == "__main__":
    unittest.main()
