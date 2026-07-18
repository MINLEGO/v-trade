from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from vtrade.runtime import (
    AlertEvent,
    ArtifactRegistration,
    CycleClaim,
    CycleStage,
    ExpiredArtifact,
    LeaseLost,
    ProjectionInputs,
    RuntimeProjection,
    StageResult,
    restore_stage,
    six_month_retain_until,
    stage_checkpoint,
)


class _Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


class PostgresRuntimeRepository:
    """PostgreSQL scheduler, lease, checkpoint, alert and retention repository."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def register_agent_schedule(self, agent_id: uuid.UUID, *, starts_at: datetime) -> bool:
        """Register a new agent's independent anchor without changing existing agents."""
        starts_at = _aware(starts_at)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO agent_runtime_schedules "
                "(agent_id, next_scheduled_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (agent_id) DO NOTHING",
                (agent_id, starts_at, starts_at, starts_at),
            )
            return cursor.rowcount == 1

    def claim_due_cycles(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        missed_grace: timedelta,
        limit: int,
    ) -> tuple[CycleClaim, ...]:
        now = _aware(now)
        if not lease_owner or lease_duration <= timedelta(0) or missed_grace < timedelta(0):
            raise ValueError("invalid scheduler lease configuration")
        if limit <= 0:
            return ()
        claims: list[CycleClaim] = []
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                ("vtrade:hourly-scheduler",),
            )
            lock = cursor.fetchone()
            if lock is None or not bool(lock[0]):
                return ()
            cursor.execute(
                "SELECT schedules.agent_id, schedules.next_scheduled_at "
                "FROM agent_runtime_schedules schedules "
                "JOIN agents ON agents.id = schedules.agent_id "
                "JOIN experiment_runs runs ON runs.id = agents.run_id "
                "CROSS JOIN system_controls controls "
                "WHERE schedules.enabled = true AND schedules.next_scheduled_at <= %s "
                "AND controls.singleton = true AND controls.globally_paused = false "
                "AND agents.paused_at IS NULL AND runs.status = 'running' "
                "AND NOT EXISTS (SELECT 1 FROM agent_cycles active "
                "WHERE active.agent_id = schedules.agent_id AND active.status = 'running' "
                "AND (active.lease_expires_at IS NULL OR active.lease_expires_at > %s)) "
                "ORDER BY schedules.next_scheduled_at, schedules.agent_id "
                "FOR UPDATE OF schedules SKIP LOCKED LIMIT %s",
                (now, now, limit),
            )
            for row in cursor.fetchall():
                agent_id = uuid.UUID(str(row[0]))
                anchor = _aware(cast(datetime, row[1]))
                scheduled_at = _latest_hour_not_after(now, anchor)
                claim_current = now - scheduled_at <= missed_grace
                skipped_through = (
                    scheduled_at - timedelta(hours=1) if claim_current else scheduled_at
                )
                if skipped_through >= anchor:
                    self._record_skipped_range(
                        cursor, agent_id, anchor, skipped_through, now
                    )
                if not claim_current:
                    self._advance_schedule(cursor, agent_id, scheduled_at + timedelta(hours=1))
                    continue
                lease_expires = now + lease_duration
                cycle_id = self._insert_or_claim_cycle(
                    cursor, agent_id, scheduled_at, lease_owner, lease_expires, now
                )
                self._advance_schedule(cursor, agent_id, scheduled_at + timedelta(hours=1))
                if cycle_id is not None:
                    claims.append(
                        CycleClaim(
                            cycle_id,
                            agent_id,
                            scheduled_at,
                            None,
                            lease_owner,
                            lease_expires,
                        )
                    )
        return tuple(claims)

    def recover_expired_cycles(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[CycleClaim, ...]:
        now = _aware(now)
        if not lease_owner or lease_duration <= timedelta(0) or limit <= 0:
            return ()
        claims: list[CycleClaim] = []
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, agent_id, scheduled_at, data_cutoff FROM agent_cycles "
                "WHERE status IN ('running', 'interrupted') "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= %s) "
                "ORDER BY scheduled_at FOR UPDATE SKIP LOCKED LIMIT %s",
                (now, limit),
            )
            for row in cursor.fetchall():
                cycle_id = uuid.UUID(str(row[0]))
                cursor.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (str(cycle_id),),
                )
                locked = cursor.fetchone()
                if locked is None or not bool(locked[0]):
                    continue
                lease_expires = now + lease_duration
                cursor.execute(
                    "UPDATE agent_cycles SET status = 'running', lease_owner = %s, "
                    "lease_expires_at = %s, attempt_count = attempt_count + 1, "
                    "failure_reason = NULL WHERE id = %s",
                    (lease_owner, lease_expires, cycle_id),
                )
                cutoff = _aware(cast(datetime, row[3])) if row[3] is not None else None
                claims.append(
                    CycleClaim(
                        cycle_id,
                        uuid.UUID(str(row[1])),
                        _aware(cast(datetime, row[2])),
                        cutoff,
                        lease_owner,
                        lease_expires,
                        recovery=True,
                    )
                )
        return tuple(claims)

    def renew_lease(
        self, claim: CycleClaim, *, now: datetime, duration: timedelta
    ) -> None:
        now = _aware(now)
        if duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_cycles SET lease_expires_at = %s WHERE id = %s "
                "AND status = 'running' AND lease_owner = %s AND lease_expires_at > %s",
                (now + duration, claim.cycle_id, claim.lease_owner, now),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(f"cycle lease lost: {claim.cycle_id}")

    def load_stage(self, cycle_id: uuid.UUID, stage: CycleStage) -> StageResult | None:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT output FROM runtime_cycle_steps WHERE agent_cycle_id = %s "
                "AND stage = %s AND status = 'completed'",
                (cycle_id, stage.value),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        value = row[0]
        if not isinstance(value, dict):
            raise ValueError("runtime checkpoint output must be a JSON object")
        return restore_stage(stage, cast(dict[str, object], value))

    def begin_stage(
        self,
        claim: CycleClaim,
        stage: CycleStage,
        input_fingerprint: str,
        *,
        now: datetime,
    ) -> None:
        now = _aware(now)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _assert_lease(cursor, claim, now)
            cursor.execute(
                "SELECT status, input_fingerprint FROM runtime_cycle_steps "
                "WHERE agent_cycle_id = %s AND stage = %s FOR UPDATE",
                (claim.cycle_id, stage.value),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "INSERT INTO runtime_cycle_steps "
                    "(id, agent_cycle_id, stage, status, input_fingerprint, attempt_count, "
                    "started_at) VALUES (%s, %s, %s, 'running', %s, 1, %s)",
                    (uuid.uuid4(), claim.cycle_id, stage.value, input_fingerprint, now),
                )
                return
            if str(row[1]) != input_fingerprint:
                raise ValueError("runtime stage idempotency fingerprint conflict")
            if str(row[0]) == "completed":
                return
            cursor.execute(
                "UPDATE runtime_cycle_steps SET status = 'running', "
                "attempt_count = attempt_count + 1, started_at = %s, error = NULL "
                "WHERE agent_cycle_id = %s AND stage = %s",
                (now, claim.cycle_id, stage.value),
            )

    def complete_stage(
        self,
        claim: CycleClaim,
        stage: CycleStage,
        input_fingerprint: str,
        result: StageResult,
        *,
        now: datetime,
    ) -> None:
        now = _aware(now)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _assert_lease(cursor, claim, now)
            cursor.execute(
                "UPDATE runtime_cycle_steps SET status = 'completed', output = %s::jsonb, "
                "completed_at = %s WHERE agent_cycle_id = %s AND stage = %s "
                "AND status = 'running' AND input_fingerprint = %s",
                (
                    json.dumps(
                        stage_checkpoint(result),
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                    now,
                    claim.cycle_id,
                    stage.value,
                    input_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("runtime stage completion lost its checkpoint")
            if stage is CycleStage.MARKET_FREEZE:
                cursor.execute(
                    "UPDATE agent_cycles SET data_cutoff = %s WHERE id = %s "
                    "AND data_cutoff IS NULL AND lease_owner = %s",
                    (now, claim.cycle_id, claim.lease_owner),
                )
                if cursor.rowcount != 1:
                    raise ValueError("market freeze could not atomically finalize data cutoff")
            for artifact in result.artifacts:
                _register_artifact(cursor, claim, stage, artifact, now)

    def complete_cycle(
        self, claim: CycleClaim, *, now: datetime, summary: dict[str, object]
    ) -> None:
        now = _aware(now)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _assert_lease(cursor, claim, now)
            cursor.execute(
                "UPDATE agent_cycles SET status = 'completed', completed_at = %s, "
                "final_summary = %s::text, lease_owner = NULL, lease_expires_at = NULL "
                "WHERE id = %s AND lease_owner = %s",
                (
                    json.dumps(summary, sort_keys=True, default=str),
                    now,
                    claim.cycle_id,
                    claim.lease_owner,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(f"cycle completion lost lease: {claim.cycle_id}")

    def fail_cycle(self, claim: CycleClaim, *, now: datetime, reason: str) -> int:
        now = _aware(now)
        safe_reason = reason[:4000]
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE runtime_cycle_steps SET status = 'failed', error = %s, completed_at = %s "
                "WHERE agent_cycle_id = %s AND status = 'running'",
                (safe_reason, now, claim.cycle_id),
            )
            cursor.execute(
                "UPDATE agent_cycles SET status = 'failed', failure_reason = %s, "
                "completed_at = %s, lease_owner = NULL, lease_expires_at = NULL "
                "WHERE id = %s AND lease_owner = %s",
                (safe_reason, now, claim.cycle_id, claim.lease_owner),
            )
            cursor.execute(
                "SELECT count(*) FROM (SELECT status, sum(CASE WHEN status <> 'failed' "
                "THEN 1 ELSE 0 END) OVER (ORDER BY scheduled_at DESC ROWS BETWEEN "
                "UNBOUNDED PRECEDING AND CURRENT ROW) AS stop_count FROM agent_cycles "
                "WHERE agent_id = %s AND completed_at IS NOT NULL) recent "
                "WHERE status = 'failed' AND stop_count = 0",
                (claim.agent_id,),
            )
            row = cursor.fetchone()
        return int(str(row[0])) if row else 1

    def open_alert(self, alert: AlertEvent) -> None:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO alerts (id, run_id, agent_id, severity, code, details, opened_at, "
                "dedupe_key) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (dedupe_key) WHERE resolved_at IS NULL DO UPDATE SET "
                "details = EXCLUDED.details, severity = EXCLUDED.severity, "
                "opened_at = EXCLUDED.opened_at",
                (
                    uuid.uuid4(),
                    alert.run_id,
                    alert.agent_id,
                    alert.severity,
                    alert.code,
                    json.dumps(alert.details, sort_keys=True, default=str),
                    alert.opened_at,
                    alert.dedupe_key,
                ),
            )

    def projection_inputs(self, *, since: datetime, until: datetime) -> ProjectionInputs:
        since, until = _aware(since), _aware(until)
        if until <= since:
            raise ValueError("projection interval is empty")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(sum(byte_length), 0) FROM artifact_inventory "
                "WHERE created_at >= %s AND created_at < %s",
                (since, until),
            )
            artifacts = cursor.fetchone()
            cursor.execute(
                "SELECT COALESCE(sum(billed_cost_micros), 0), "
                "COALESCE(sum(nominal_cost_micros), 0) FROM provider_usage "
                "WHERE created_at >= %s AND created_at < %s",
                (since, until),
            )
            costs = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM agent_cycles WHERE status = 'completed' "
                "AND completed_at >= %s AND completed_at < %s",
                (since, until),
            )
            cycles = cursor.fetchone()
        return ProjectionInputs(
            int(str(artifacts[0])) if artifacts else 0,
            int(str(costs[0])) if costs else 0,
            int(str(costs[1])) if costs else 0,
            int(str(cycles[0])) if cycles else 0,
        )

    def record_projection(self, projection: RuntimeProjection) -> None:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runtime_projections "
                "(id, window_started_at, window_ended_at, projected_monthly_artifact_bytes, "
                "projected_monthly_billed_cost_micros, projected_monthly_nominal_cost_micros, "
                "observed_cycles, calculated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    projection.window_started_at,
                    projection.window_ended_at,
                    projection.projected_monthly_artifact_bytes,
                    projection.projected_monthly_billed_cost_micros,
                    projection.projected_monthly_nominal_cost_micros,
                    projection.observed_cycles,
                    projection.calculated_at,
                ),
            )

    def claim_expired_artifacts(
        self, *, now: datetime, lease_owner: str, limit: int
    ) -> tuple[ExpiredArtifact, ...]:
        now = _aware(now)
        if not lease_owner or limit <= 0:
            return ()
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, uri, sha256 FROM artifact_inventory WHERE "
                "(status = 'active' AND retain_until <= %s) OR "
                "(status = 'deleting' AND lease_expires_at <= %s) "
                "ORDER BY retain_until FOR UPDATE SKIP LOCKED LIMIT %s",
                (now, now, limit),
            )
            rows = cursor.fetchall()
            ids = [uuid.UUID(str(row[0])) for row in rows]
            for identifier in ids:
                cursor.execute(
                    "UPDATE artifact_inventory SET status = 'deleting', lease_owner = %s, "
                    "lease_expires_at = %s, deletion_attempts = deletion_attempts + 1 "
                    "WHERE id = %s",
                    (lease_owner, now + timedelta(minutes=10), identifier),
                )
        return tuple(
            ExpiredArtifact(uuid.UUID(str(row[0])), str(row[1]), str(row[2])) for row in rows
        )

    def purge_expired_payloads(self, *, now: datetime) -> int:
        now = _aware(now)
        statements = (
            "UPDATE cycle_contexts SET rendered_cycle_prompt = '[retention-expired]', "
            "context = '{}'::jsonb, artifact_uri = NULL, retention_purged_at = %s "
            "WHERE retain_until <= %s AND retention_purged_at IS NULL",
            "UPDATE model_turns SET request = '{}'::jsonb, response = NULL, "
            "raw_artifact_uri = NULL, retention_purged_at = %s WHERE retain_until <= %s "
            "AND retention_purged_at IS NULL",
            "UPDATE tool_calls SET arguments = '{}'::jsonb, output = NULL, "
            "error = '[retention-expired]', retention_purged_at = %s WHERE retain_until <= %s "
            "AND retention_purged_at IS NULL",
            "UPDATE provider_usage SET raw_artifact_uri = NULL, retention_purged_at = %s "
            "WHERE retain_until <= %s AND retention_purged_at IS NULL",
            "UPDATE harness_runs SET transcript_artifact_uri = '[retention-expired]', "
            "retention_purged_at = %s WHERE retain_until <= %s AND retention_purged_at IS NULL",
            "UPDATE harness_tool_records SET arguments = NULL, output = '{}'::jsonb, "
            "retention_purged_at = %s WHERE retain_until <= %s AND retention_purged_at IS NULL",
            "UPDATE model_replay_records SET response_artifact_uri = '[retention-expired]', "
            "retention_purged_at = %s WHERE retain_until <= %s AND retention_purged_at IS NULL",
        )
        changed = 0
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("vtrade:retention-purge",),
            )
            for statement in statements:
                cursor.execute(statement, (now, now))
                changed += cursor.rowcount
        return changed

    def complete_artifact_deletion(
        self, artifact: ExpiredArtifact, *, now: datetime, error: str | None
    ) -> None:
        now = _aware(now)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            if error is None:
                cursor.execute(
                    "UPDATE artifact_inventory SET status = 'deleted', deleted_at = %s, "
                    "lease_owner = NULL, lease_expires_at = NULL, deletion_error = NULL "
                    "WHERE id = %s AND status = 'deleting'",
                    (now, artifact.inventory_id),
                )
            else:
                cursor.execute(
                    "UPDATE artifact_inventory SET status = 'active', lease_owner = NULL, "
                    "lease_expires_at = NULL, deletion_error = %s WHERE id = %s "
                    "AND status = 'deleting'",
                    (error[:4000], artifact.inventory_id),
                )

    @staticmethod
    def _record_skipped_range(
        cursor: _Cursor,
        agent_id: uuid.UUID,
        first_scheduled_at: datetime,
        last_scheduled_at: datetime,
        now: datetime,
    ) -> None:
        cursor.execute(
            "INSERT INTO agent_cycles "
            "(agent_id, scheduled_at, data_cutoff, status, completed_at, failure_reason, "
            "idempotency_key) SELECT %s, slot, slot, 'skipped', %s, %s, "
            "%s || slot::text FROM generate_series(%s, %s, interval '1 hour') slot "
            "ON CONFLICT (agent_id, scheduled_at) DO NOTHING",
            (
                agent_id,
                now,
                "missed hourly slot; backfill disabled",
                f"agent-hour:{agent_id}:",
                first_scheduled_at,
                last_scheduled_at,
            ),
        )

    @staticmethod
    def _advance_schedule(cursor: _Cursor, agent_id: uuid.UUID, next_at: datetime) -> None:
        cursor.execute(
            "UPDATE agent_runtime_schedules SET next_scheduled_at = %s, updated_at = now() "
            "WHERE agent_id = %s",
            (next_at, agent_id),
        )

    @staticmethod
    def _insert_or_claim_cycle(
        cursor: _Cursor,
        agent_id: uuid.UUID,
        scheduled_at: datetime,
        lease_owner: str,
        lease_expires: datetime,
        now: datetime,
    ) -> uuid.UUID | None:
        cycle_id = uuid.uuid4()
        cursor.execute(
            "INSERT INTO agent_cycles "
            "(id, agent_id, scheduled_at, status, started_at, idempotency_key, "
            "lease_owner, lease_expires_at, attempt_count) "
            "VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, 1) "
            "ON CONFLICT (agent_id, scheduled_at) DO NOTHING RETURNING id",
            (
                cycle_id,
                agent_id,
                scheduled_at,
                now,
                f"agent-hour:{agent_id}:{scheduled_at.isoformat()}",
                lease_owner,
                lease_expires,
            ),
        )
        row = cursor.fetchone()
        return uuid.UUID(str(row[0])) if row else None


def _register_artifact(
    cursor: _Cursor,
    claim: CycleClaim,
    stage: CycleStage,
    artifact: ArtifactRegistration,
    created_at: datetime,
) -> None:
    # Artifact creation and stage registration are separate operations. Using the
    # later registration clock makes an exact six-calendar-month retention fail by
    # the few milliseconds spent completing the stage. The scheduled cycle instant
    # is the deterministic causal lower bound; artifacts calculate their expiry from
    # their later, actual creation time.
    minimum = six_month_retain_until(claim.scheduled_at)
    if artifact.retain_until < minimum:
        raise ValueError("runtime artifacts must be retained for at least six calendar months")
    cursor.execute(
        "INSERT INTO artifact_inventory "
        "(id, agent_cycle_id, stage, uri, sha256, byte_length, retain_until, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s) "
        "ON CONFLICT (uri) DO UPDATE SET retain_until = GREATEST("
        "artifact_inventory.retain_until, EXCLUDED.retain_until), status = 'active', "
        "lease_owner = NULL, lease_expires_at = NULL, deletion_error = NULL, deleted_at = NULL",
        (
            uuid.uuid4(),
            claim.cycle_id,
            stage.value,
            artifact.uri,
            artifact.sha256,
            artifact.byte_length,
            artifact.retain_until,
            created_at,
        ),
    )


def _assert_lease(cursor: _Cursor, claim: CycleClaim, now: datetime) -> None:
    cursor.execute(
        "SELECT 1 FROM agent_cycles WHERE id = %s AND status = 'running' "
        "AND lease_owner = %s AND lease_expires_at > %s FOR UPDATE",
        (claim.cycle_id, claim.lease_owner, now),
    )
    if cursor.fetchone() is None:
        raise LeaseLost(f"cycle lease lost: {claim.cycle_id}")


def _latest_hour_not_after(now: datetime, anchor: datetime) -> datetime:
    if anchor > now:
        raise ValueError("schedule anchor is not due")
    elapsed = now - anchor
    periods = int(elapsed.total_seconds() // 3600)
    return anchor + timedelta(hours=periods)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))
