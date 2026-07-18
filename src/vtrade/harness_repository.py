from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, cast

from vtrade.harness import BeliefRecord, HarnessResult, PlanRecord
from vtrade.providers import (
    EXA_MAX_CREDITS_PER_SEARCH,
    BudgetExceeded,
    BudgetReservation,
    ProviderTelemetry,
)
from vtrade.runtime import ArtifactRegistration


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


class PostgresBudgetGuard:
    def __init__(
        self,
        database_url: str,
        *,
        limit_micros: int = 40_000_000,
        thresholds: tuple[int, int, int] = (20_000_000, 32_000_000, 40_000_000),
        connect: _Connect | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        if limit_micros <= 0 or thresholds != tuple(sorted(thresholds)):
            raise ValueError("budget limit and sorted thresholds are required")
        self._database_url = database_url
        self._limit = limit_micros
        self._thresholds = thresholds
        self._connect = connect or _default_connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def reserve(
        self,
        provider: str,
        estimated_cost_micros: int,
        *,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> BudgetReservation:
        if not provider or estimated_cost_micros < 0:
            raise ValueError("provider and non-negative estimate are required")
        if request_count < 0 or not credit_count.is_finite() or credit_count < 0:
            raise ValueError("provider usage reservations cannot be negative")
        now = _aware(self._clock())
        month = date(now.year, now.month, 1)
        reservation = BudgetReservation(
            str(uuid.uuid4()),
            estimated_cost_micros,
            provider,
            request_count,
            credit_count,
        )
        if provider == "exa":
            return self._reserve_exa(reservation, month, now)
        if request_count != 0 or credit_count != 0:
            raise ValueError("only Exa has request and credit reservations")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_month(cursor, month)
            _ensure_budget_row(cursor, month, self._limit, now)
            cursor.execute(
                "SELECT billed_cost_micros, halted, alerted_20, alerted_32, alerted_40, "
                "limit_micros "
                "FROM monthly_provider_budgets WHERE month_start = %s FOR UPDATE",
                (month,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("monthly budget row disappeared")
            billed = int(str(row[0]))
            halted = bool(row[1])
            if int(str(row[5])) != self._limit:
                raise ValueError("monthly budget limit differs from frozen configuration")
            cursor.execute(
                "SELECT COALESCE(sum(estimated_cost_micros), 0) "
                "FROM provider_budget_reservations "
                "WHERE month_start = %s AND status = 'reserved'",
                (month,),
            )
            pending_row = cursor.fetchone()
            pending = int(str(pending_row[0])) if pending_row else 0
            projected = billed + pending + estimated_cost_micros
            if halted or projected > self._limit:
                raise BudgetExceeded("pre-request estimate exceeds the monthly circuit breaker")
            cursor.execute(
                "INSERT INTO provider_budget_reservations "
                "(id, month_start, provider, estimated_cost_micros, status, reserved_at) "
                "VALUES (%s, %s, %s, %s, 'reserved', %s)",
                (uuid.UUID(reservation.id), month, provider, estimated_cost_micros, now),
            )
            _open_budget_alerts(cursor, month, projected, row[2:5], self._thresholds, now)
        return reservation

    def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        billed_cost_micros: int,
        nominal_cost_micros: int,
        request_count: int = 0,
        credit_count: Decimal = Decimal(0),
    ) -> None:
        if billed_cost_micros < 0 or nominal_cost_micros < 0:
            raise ValueError("provider costs cannot be negative")
        if request_count < 0 or not credit_count.is_finite() or credit_count < 0:
            raise ValueError("provider usage cannot be negative")
        now = _aware(self._clock())
        month = date(now.year, now.month, 1)
        if reservation.provider == "exa":
            self._reconcile_exa(
                reservation,
                month,
                now,
                billed_cost_micros=billed_cost_micros,
                nominal_cost_micros=nominal_cost_micros,
                request_count=request_count,
                credit_count=credit_count,
            )
            return
        if request_count != 0 or credit_count != 0:
            raise ValueError("only Exa has request and credit reconciliation")
        exceeded = False
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_month(cursor, month)
            cursor.execute(
                "SELECT estimated_cost_micros, status FROM provider_budget_reservations "
                "WHERE id = %s AND month_start = %s FOR UPDATE",
                (uuid.UUID(reservation.id), month),
            )
            row = cursor.fetchone()
            if row is None or str(row[1]) != "reserved":
                raise ValueError("unknown or already reconciled budget reservation")
            if int(str(row[0])) != reservation.estimated_cost_micros:
                raise ValueError("budget reservation estimate mismatch")
            cursor.execute(
                "UPDATE provider_budget_reservations SET status = 'reconciled', "
                "billed_cost_micros = %s, nominal_cost_micros = %s, reconciled_at = %s "
                "WHERE id = %s",
                (
                    billed_cost_micros,
                    nominal_cost_micros,
                    now,
                    uuid.UUID(reservation.id),
                ),
            )
            cursor.execute(
                "SELECT COALESCE(sum(estimated_cost_micros), 0) "
                "FROM provider_budget_reservations "
                "WHERE month_start = %s AND status = 'reserved'",
                (month,),
            )
            pending_row = cursor.fetchone()
            pending = int(str(pending_row[0])) if pending_row else 0
            cursor.execute(
                "UPDATE monthly_provider_budgets SET "
                "billed_cost_micros = billed_cost_micros + %s, "
                "nominal_cost_micros = nominal_cost_micros + %s, "
                "halted = halted OR billed_cost_micros + %s + %s > limit_micros, "
                "updated_at = %s "
                "WHERE month_start = %s RETURNING billed_cost_micros, halted, "
                "alerted_20, alerted_32, alerted_40",
                (
                    billed_cost_micros,
                    nominal_cost_micros,
                    billed_cost_micros,
                    pending,
                    now,
                    month,
                ),
            )
            budget = cursor.fetchone()
            if budget is None:
                raise RuntimeError("monthly budget row missing during reconciliation")
            exceeded = bool(budget[1])
            _open_budget_alerts(
                cursor,
                month,
                int(str(budget[0])),
                budget[2:5],
                self._thresholds,
                now,
            )
        if exceeded:
            raise BudgetExceeded("actual cost recorded; monthly circuit is now halted")

    def _reserve_exa(
        self, reservation: BudgetReservation, month: date, now: datetime
    ) -> BudgetReservation:
        if (
            reservation.reserved_request_count != 1
            or reservation.reserved_credit_count != EXA_MAX_CREDITS_PER_SEARCH
        ):
            raise ValueError("each Exa search must reserve one request and ten worst-case credits")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_exa_month(cursor, month)
            _ensure_exa_quota_row(cursor, month, now)
            cursor.execute(
                "SELECT request_limit, credit_limit, request_count, credit_count, halted "
                "FROM monthly_exa_quotas WHERE month_start = %s FOR UPDATE",
                (month,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("monthly Exa quota row disappeared")
            if int(str(row[0])) != 18_000 or Decimal(str(row[1])) != Decimal(18_000):
                raise ValueError("monthly Exa limits differ from frozen configuration")
            cursor.execute(
                "SELECT COALESCE(sum(reserved_request_count), 0), "
                "COALESCE(sum(reserved_credit_count), 0) "
                "FROM exa_quota_reservations "
                "WHERE month_start = %s AND status = 'reserved'",
                (month,),
            )
            pending_row = cursor.fetchone()
            pending_requests = int(str(pending_row[0])) if pending_row else 0
            pending_credits = Decimal(str(pending_row[1])) if pending_row else Decimal(0)
            projected_requests = int(str(row[2])) + pending_requests + 1
            projected_credits = (
                Decimal(str(row[3])) + pending_credits + reservation.reserved_credit_count
            )
            if bool(row[4]) or projected_requests > 18_000 or projected_credits > Decimal(18_000):
                raise BudgetExceeded("pre-request Exa monthly request/credit cap reached")
            cursor.execute(
                "INSERT INTO exa_quota_reservations "
                "(id, month_start, reserved_request_count, reserved_credit_count, "
                "nominal_cost_micros, status, reserved_at) "
                "VALUES (%s, %s, 1, %s, %s, 'reserved', %s)",
                (
                    uuid.UUID(reservation.id),
                    month,
                    EXA_MAX_CREDITS_PER_SEARCH,
                    reservation.estimated_cost_micros,
                    now,
                ),
            )
        return reservation

    def _reconcile_exa(
        self,
        reservation: BudgetReservation,
        month: date,
        now: datetime,
        *,
        billed_cost_micros: int,
        nominal_cost_micros: int,
        request_count: int,
        credit_count: Decimal,
    ) -> None:
        if request_count != 1:
            raise ValueError("an Exa search must reconcile exactly one request")
        halted = False
        credit_overrun = credit_count > reservation.reserved_credit_count
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_exa_month(cursor, month)
            cursor.execute(
                "SELECT reserved_request_count, reserved_credit_count, nominal_cost_micros, "
                "status FROM exa_quota_reservations "
                "WHERE id = %s AND month_start = %s FOR UPDATE",
                (uuid.UUID(reservation.id), month),
            )
            row = cursor.fetchone()
            if row is None or str(row[3]) != "reserved":
                raise ValueError("unknown or already reconciled Exa quota reservation")
            if (
                int(str(row[0])) != reservation.reserved_request_count
                or Decimal(str(row[1])) != reservation.reserved_credit_count
                or int(str(row[2])) != reservation.estimated_cost_micros
                or nominal_cost_micros != reservation.estimated_cost_micros
            ):
                raise ValueError("Exa quota reservation mismatch")
            cursor.execute(
                "UPDATE exa_quota_reservations SET status = 'reconciled', "
                "actual_request_count = %s, actual_credit_count = %s, "
                "billed_cost_micros = %s, reconciled_at = %s WHERE id = %s",
                (
                    request_count,
                    credit_count,
                    billed_cost_micros,
                    now,
                    uuid.UUID(reservation.id),
                ),
            )
            cursor.execute(
                "SELECT COALESCE(sum(reserved_request_count), 0), "
                "COALESCE(sum(reserved_credit_count), 0) "
                "FROM exa_quota_reservations "
                "WHERE month_start = %s AND status = 'reserved'",
                (month,),
            )
            pending_row = cursor.fetchone()
            pending_requests = int(str(pending_row[0])) if pending_row else 0
            pending_credits = Decimal(str(pending_row[1])) if pending_row else Decimal(0)
            cursor.execute(
                "UPDATE monthly_exa_quotas SET "
                "request_count = request_count + %s, credit_count = credit_count + %s, "
                "nominal_cost_micros = nominal_cost_micros + %s, "
                "unexpected_billed_cost_micros = unexpected_billed_cost_micros + %s, "
                "halted = halted OR %s > 0 "
                "OR %s "
                "OR request_count + %s + %s > request_limit "
                "OR credit_count + %s + %s > credit_limit, updated_at = %s "
                "WHERE month_start = %s RETURNING halted, request_count, credit_count",
                (
                    request_count,
                    credit_count,
                    nominal_cost_micros,
                    billed_cost_micros,
                    billed_cost_micros,
                    credit_overrun,
                    request_count,
                    pending_requests,
                    credit_count,
                    pending_credits,
                    now,
                    month,
                ),
            )
            quota = cursor.fetchone()
            if quota is None:
                raise RuntimeError("monthly Exa quota row missing during reconciliation")
            halted = bool(quota[0])
            if billed_cost_micros > 0:
                cursor.execute(
                    "INSERT INTO alerts (severity, code, details, opened_at) "
                    "VALUES ('critical', 'exa_unexpected_billed_cost', %s, %s)",
                    (
                        json.dumps(
                            {
                                "month": month.isoformat(),
                                "billed_cost_micros": billed_cost_micros,
                                "reservation_id": reservation.id,
                            }
                        ),
                        now,
                    ),
                )
            if credit_overrun:
                cursor.execute(
                    "INSERT INTO alerts (severity, code, details, opened_at) "
                    "VALUES ('critical', 'exa_credit_reservation_exceeded', %s, %s)",
                    (
                        json.dumps(
                            {
                                "month": month.isoformat(),
                                "actual_credit_count": str(credit_count),
                                "reserved_credit_count": str(reservation.reserved_credit_count),
                                "reservation_id": reservation.id,
                            }
                        ),
                        now,
                    ),
                )
            over_quota = int(str(quota[1])) + pending_requests > 18_000 or Decimal(
                str(quota[2])
            ) + pending_credits > Decimal(18_000)
            if over_quota:
                cursor.execute(
                    "INSERT INTO alerts (severity, code, details, opened_at) "
                    "VALUES ('critical', 'exa_monthly_quota_exceeded', %s, %s)",
                    (
                        json.dumps(
                            {
                                "month": month.isoformat(),
                                "request_count": int(str(quota[1])),
                                "credit_count": str(quota[2]),
                            }
                        ),
                        now,
                    ),
                )
        if halted:
            raise BudgetExceeded("Exa usage recorded; the monthly Exa circuit is halted")


class PostgresHarnessRepository:
    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def persist_run(
        self,
        *,
        agent_cycle_id: uuid.UUID,
        result: HarnessResult,
        transcript_uri: str,
        transcript_sha256: str,
        completed_at: datetime,
        retain_until: datetime,
        artifacts: Sequence[ArtifactRegistration] = (),
    ) -> uuid.UUID:
        completed = _aware(completed_at)
        retained = _aware(retain_until)
        if retained <= completed:
            raise ValueError("harness artifacts must be retained beyond completion")
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:harness:{agent_cycle_id}")
        key = f"harness:{agent_cycle_id}"
        web_searches = sum(1 for call in result.tool_calls if call.name == "web_search")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, transcript_sha256 FROM harness_runs WHERE idempotency_key = %s",
                (key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[1]) != transcript_sha256:
                    raise ValueError("harness idempotency key reused with another transcript")
                return uuid.UUID(str(existing[0]))
            cursor.execute(
                "INSERT INTO harness_runs "
                "(id, agent_cycle_id, termination_status, total_model_turns, "
                "total_tool_calls, total_web_searches, total_completion_tokens, "
                "transcript_artifact_uri, transcript_sha256, idempotency_key, retain_until, "
                "completed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    agent_cycle_id,
                    result.termination_status,
                    sum(
                        1
                        for usage in result.telemetry
                        if usage.usage_kind in {"model", "model_replay"}
                    ),
                    len(result.tool_calls),
                    web_searches,
                    result.total_completion_tokens,
                    transcript_uri,
                    transcript_sha256,
                    key,
                    retained,
                    completed,
                ),
            )
            for index, call in enumerate(result.tool_calls):
                cursor.execute(
                    "INSERT INTO harness_tool_records "
                    "(id, harness_run_id, call_index, provider_call_id, tool_name, "
                    "category, arguments, output, success, retain_until) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:harness-tool:{run_id}:{index}"),
                        run_id,
                        index,
                        call.id,
                        call.name,
                        call.category,
                        json.dumps(call.arguments) if call.arguments is not None else None,
                        json.dumps(call.output),
                        call.success,
                        retained,
                    ),
                )
            for telemetry in result.telemetry:
                _insert_usage(cursor, agent_cycle_id, telemetry, completed, retained)
            for artifact in artifacts:
                cursor.execute(
                    "INSERT INTO artifact_inventory "
                    "(id, agent_cycle_id, stage, uri, sha256, byte_length, retain_until, "
                    "status, created_at) VALUES "
                    "(%s, %s, 'harness', %s, %s, %s, %s, 'active', %s) "
                    "ON CONFLICT (uri) DO UPDATE SET retain_until = GREATEST("
                    "artifact_inventory.retain_until, EXCLUDED.retain_until), "
                    "byte_length = EXCLUDED.byte_length, status = 'active', "
                    "lease_owner = NULL, lease_expires_at = NULL, deletion_error = NULL, "
                    "deleted_at = NULL",
                    (
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vtrade:artifact-inventory:{artifact.uri}",
                        ),
                        agent_cycle_id,
                        artifact.uri,
                        artifact.sha256,
                        artifact.byte_length,
                        artifact.retain_until,
                        completed,
                    ),
                )
        return run_id

    def append_belief(
        self, belief: BeliefRecord, *, actor_id: uuid.UUID, cycle_id: uuid.UUID
    ) -> None:
        if str(actor_id) != belief.agent_id:
            raise PermissionError("agent cannot write another agent's belief")
        fingerprint = _memory_fingerprint(
            {
                "probability": str(belief.probability),
                "content": belief.content,
                "category": belief.category,
                "evidence": belief.evidence,
                "created_at": belief.created_at.isoformat(),
            }
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, memory_fingerprint FROM beliefs WHERE idempotency_key = %s",
                (f"belief:{belief.id}",),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[1]) != fingerprint:
                    raise ValueError("belief idempotency key reused with different content")
                return
            cursor.execute(
                "INSERT INTO beliefs (id, agent_id, idempotency_key, memory_fingerprint) "
                "VALUES (%s, %s, %s, %s)",
                (
                    uuid.UUID(belief.id),
                    uuid.UUID(belief.agent_id),
                    f"belief:{belief.id}",
                    fingerprint,
                ),
            )
            cursor.execute(
                "INSERT INTO belief_revisions "
                "(belief_id, revision, probability, content, category, confidence, evidence, "
                "created_by_cycle_id, created_at) VALUES (%s, 1, %s, %s, %s, NULL, %s, %s, %s)",
                (
                    uuid.UUID(belief.id),
                    belief.probability,
                    belief.content,
                    belief.category,
                    json.dumps(belief.evidence),
                    cycle_id,
                    _aware(belief.created_at),
                ),
            )

    def append_plan(self, plan: PlanRecord, *, actor_id: uuid.UUID, cycle_id: uuid.UUID) -> None:
        if str(actor_id) != plan.agent_id:
            raise PermissionError("agent cannot write another agent's plan")
        fingerprint = _memory_fingerprint(
            {
                "type": plan.plan_type.value,
                "content": plan.content,
                "due_at": plan.due_at.isoformat() if plan.due_at else None,
                "created_at": plan.created_at.isoformat(),
            }
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, memory_fingerprint FROM plans WHERE idempotency_key = %s",
                (f"plan:{plan.id}",),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[1]) != fingerprint:
                    raise ValueError("plan idempotency key reused with different content")
                return
            cursor.execute(
                "INSERT INTO plans "
                "(id, agent_id, plan_type, status, due_at, idempotency_key, "
                "memory_fingerprint) VALUES (%s, %s, %s, 'active', %s, %s, %s)",
                (
                    uuid.UUID(plan.id),
                    uuid.UUID(plan.agent_id),
                    plan.plan_type.value,
                    plan.due_at,
                    f"plan:{plan.id}",
                    fingerprint,
                ),
            )
            cursor.execute(
                "INSERT INTO plan_revisions "
                "(plan_id, revision, content, created_by_cycle_id, created_at) "
                "VALUES (%s, 1, %s, %s, %s)",
                (uuid.UUID(plan.id), plan.content, cycle_id, _aware(plan.created_at)),
            )

    def read_beliefs(
        self, *, actor_id: uuid.UUID, target_agent_id: uuid.UUID
    ) -> list[dict[str, object]]:
        if actor_id != target_agent_id:
            raise PermissionError("agent cannot read another agent's beliefs")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.id, r.probability, r.content, r.category, r.evidence, r.created_at "
                "FROM beliefs b JOIN LATERAL (SELECT * FROM belief_revisions "
                "WHERE belief_id = b.id ORDER BY revision DESC LIMIT 1) r ON true "
                "WHERE b.agent_id = %s AND b.active = true ORDER BY r.created_at, b.id",
                (actor_id,),
            )
            rows: list[dict[str, object]] = []
            while row := cursor.fetchone():
                rows.append(
                    {
                        "id": str(row[0]),
                        "probability": str(row[1]),
                        "content": str(row[2]),
                        "category": str(row[3]),
                        "evidence": row[4],
                        "created_at": str(row[5]),
                    }
                )
            return rows

    def read_plans(
        self, *, actor_id: uuid.UUID, target_agent_id: uuid.UUID
    ) -> list[dict[str, object]]:
        if actor_id != target_agent_id:
            raise PermissionError("agent cannot read another agent's plans")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.id, p.plan_type, p.status, p.due_at, r.content, r.created_at "
                "FROM plans p JOIN LATERAL (SELECT * FROM plan_revisions "
                "WHERE plan_id = p.id ORDER BY revision DESC LIMIT 1) r ON true "
                "WHERE p.agent_id = %s ORDER BY r.created_at, p.id",
                (actor_id,),
            )
            rows: list[dict[str, object]] = []
            while row := cursor.fetchone():
                rows.append(
                    {
                        "id": str(row[0]),
                        "plan_type": str(row[1]),
                        "status": str(row[2]),
                        "due_at": str(row[3]) if row[3] is not None else None,
                        "content": str(row[4]),
                        "created_at": str(row[5]),
                    }
                )
            return rows


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _lock_month(cursor: _Cursor, month: date) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"provider-budget:{month.isoformat()}",),
    )


def _lock_exa_month(cursor: _Cursor, month: date) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"exa-quota:{month.isoformat()}",),
    )


def _ensure_budget_row(cursor: _Cursor, month: date, limit: int, now: datetime) -> None:
    cursor.execute(
        "INSERT INTO monthly_provider_budgets (month_start, limit_micros, updated_at) "
        "VALUES (%s, %s, %s) ON CONFLICT (month_start) DO NOTHING",
        (month, limit, now),
    )


def _ensure_exa_quota_row(cursor: _Cursor, month: date, now: datetime) -> None:
    cursor.execute(
        "INSERT INTO monthly_exa_quotas "
        "(month_start, request_limit, credit_limit, updated_at) "
        "VALUES (%s, 18000, 18000, %s) ON CONFLICT (month_start) DO NOTHING",
        (month, now),
    )


def _open_budget_alerts(
    cursor: _Cursor,
    month: date,
    projected: int,
    flags: Sequence[object],
    thresholds: tuple[int, int, int],
    now: datetime,
) -> None:
    columns = ("alerted_20", "alerted_32", "alerted_40")
    for column, threshold, flagged in zip(columns, thresholds, flags, strict=True):
        if projected >= threshold and not bool(flagged):
            cursor.execute(
                f"UPDATE monthly_provider_budgets SET {column} = true WHERE month_start = %s",
                (month,),
            )
            cursor.execute(
                "INSERT INTO alerts (severity, code, details, opened_at) VALUES (%s, %s, %s, %s)",
                (
                    "critical" if threshold == thresholds[-1] else "warning",
                    f"monthly_api_budget_{threshold}",
                    json.dumps({"month": month.isoformat(), "projected_micros": projected}),
                    now,
                ),
            )


def _insert_usage(
    cursor: _Cursor,
    agent_cycle_id: uuid.UUID,
    usage: ProviderTelemetry,
    created_at: datetime,
    retain_until: datetime,
) -> None:
    cursor.execute(
        "INSERT INTO provider_usage "
        "(agent_cycle_id, provider, route, usage_kind, prompt_tokens, completion_tokens, "
        "reasoning_tokens, cached_tokens, request_count, credit_count, billed_cost_micros, "
        "nominal_cost_micros, latency_ms, raw_sha256, raw_artifact_uri, retain_until, "
        "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s)",
        (
            agent_cycle_id,
            usage.provider,
            usage.route,
            usage.usage_kind,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.reasoning_tokens,
            usage.cached_tokens,
            usage.request_count,
            usage.credit_count,
            usage.billed_cost_micros,
            usage.nominal_cost_micros,
            usage.latency_ms,
            usage.raw_sha256,
            usage.artifact_uri,
            retain_until,
            created_at,
        ),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _memory_fingerprint(value: dict[str, object]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()
