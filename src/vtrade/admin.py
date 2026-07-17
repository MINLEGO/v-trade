from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast


class AdminRepositoryError(RuntimeError):
    pass


class InvalidOperatorAction(ValueError):
    pass


class _Cursor(Protocol):
    description: Sequence[Sequence[object]] | None

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class Page:
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if self.offset < 0:
            raise ValueError("offset cannot be negative")


_VIEWS: dict[str, str] = {
    "leaderboard": """
        SELECT a.id AS agent_id, a.name AS agent_name, er.id AS run_id,
               mc.label AS model_label, latest.account_value_micros,
               latest.realized_pnl_micros, latest.unrealized_pnl_micros,
               latest.account_value_micros - a.initial_cash_micros AS total_pnl_micros,
               CASE WHEN peak.peak_value_micros IS NULL OR peak.peak_value_micros = 0 THEN NULL
                    ELSE (peak.peak_value_micros - latest.account_value_micros)::numeric
                         / peak.peak_value_micros END AS drawdown_fraction,
               latest.calculated_at, a.paused_at
          FROM agents a
          JOIN experiment_runs er ON er.id = a.run_id
          JOIN model_configs mc ON mc.id = a.model_config_id
          LEFT JOIN LATERAL (
              SELECT ps.account_value_micros, ps.realized_pnl_micros,
                     ps.unrealized_pnl_micros, ps.calculated_at
                FROM performance_snapshots ps
                JOIN agent_cycles ac ON ac.id = ps.agent_cycle_id
               WHERE ac.agent_id = a.id
               ORDER BY ps.calculated_at DESC, ps.id DESC LIMIT 1
          ) latest ON true
          LEFT JOIN LATERAL (
              SELECT max(ps.account_value_micros) AS peak_value_micros
                FROM performance_snapshots ps
                JOIN agent_cycles ac ON ac.id = ps.agent_cycle_id
               WHERE ac.agent_id = a.id
          ) peak ON true
         ORDER BY latest.account_value_micros DESC NULLS LAST, a.id
         LIMIT %s OFFSET %s
    """,
    "positions": """
        SELECT p.id, p.agent_id, a.name AS agent_name, m.id AS market_id,
               m.question, o.id AS outcome_id, o.name AS outcome, p.shares,
               p.average_cost, p.cost_basis_micros, p.realized_pnl_micros,
               quote.best_bid, quote.cutoff AS quote_cutoff,
               CASE WHEN quote.best_bid IS NULL THEN NULL
                    WHEN quote.cutoff < now() - interval '300 seconds' THEN NULL
                    ELSE round(p.shares * quote.best_bid * 1000000)::bigint END
                    AS liquidation_value_micros,
               CASE WHEN quote.best_bid IS NULL THEN 'missing'
                    WHEN quote.cutoff < now() - interval '300 seconds' THEN 'stale'
                    ELSE 'fresh' END AS valuation_status,
               CASE WHEN quote.cutoff IS NULL THEN NULL
                    ELSE extract(epoch FROM (now() - quote.cutoff))::bigint END
                    AS quote_age_seconds,
               p.updated_at
          FROM positions p
          JOIN agents a ON a.id = p.agent_id
          JOIN outcomes o ON o.id = p.outcome_id
          JOIN markets m ON m.id = o.market_id
          LEFT JOIN LATERAL (
              SELECT obs.best_bid, obs.cutoff
                FROM order_book_snapshots obs
               WHERE obs.outcome_id = p.outcome_id AND obs.best_bid IS NOT NULL
               ORDER BY obs.cutoff DESC, obs.id DESC LIMIT 1
          ) quote ON true
         WHERE (%s::uuid IS NULL OR p.agent_id = %s::uuid)
         ORDER BY p.updated_at DESC, p.id
         LIMIT %s OFFSET %s
    """,
    "trades": """
        SELECT f.id AS fill_id, f.filled_at, a.id AS agent_id, a.name AS agent_name,
               m.id AS market_id, m.question, oc.id AS outcome_id, oc.name AS outcome,
               oi.side, f.shares, f.price, f.gross_micros, f.fee_micros,
               ord.policy, ord.liquidity_time_in_force, ac.id AS agent_cycle_id,
               ac.data_cutoff
          FROM fills f
          JOIN orders ord ON ord.id = f.order_id
          JOIN order_intents oi ON oi.id = ord.intent_id
          JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id
          JOIN agents a ON a.id = ac.agent_id
          JOIN outcomes oc ON oc.id = oi.outcome_id
          JOIN markets m ON m.id = oi.market_id
         WHERE (%s::uuid IS NULL OR a.id = %s::uuid)
         ORDER BY f.filled_at DESC, f.id DESC
         LIMIT %s OFFSET %s
    """,
    "settlements": """
        SELECT s.id, s.settled_at, a.id AS agent_id, a.name AS agent_name,
               m.id AS market_id, m.question, o.id AS outcome_id, o.name AS outcome,
               s.shares, s.payout_micros, s.realized_pnl_micros,
               r.result, r.source_created_at, r.observed_at, s.as_of_cutoff
          FROM settlements s
          JOIN agents a ON a.id = s.agent_id
          JOIN positions p ON p.id = s.position_id
          JOIN outcomes o ON o.id = p.outcome_id
          JOIN markets m ON m.id = o.market_id
          JOIN resolutions r ON r.id = s.resolution_id
         WHERE (%s::uuid IS NULL OR a.id = %s::uuid)
         ORDER BY s.settled_at DESC, s.id DESC
         LIMIT %s OFFSET %s
    """,
    "rejections": """
        SELECT oi.id AS intent_id, ord.id AS order_id, oi.created_at,
               a.id AS agent_id, a.name AS agent_name, m.id AS market_id, m.question,
               o.id AS outcome_id, o.name AS outcome, oi.side,
               oi.validation_status, COALESCE(ord.rejection_code, oi.rejection_code)
                   AS rejection_code,
               ord.status AS order_status, ac.id AS agent_cycle_id
          FROM order_intents oi
          JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id
          JOIN agents a ON a.id = ac.agent_id
          JOIN markets m ON m.id = oi.market_id
          JOIN outcomes o ON o.id = oi.outcome_id
          LEFT JOIN orders ord ON ord.intent_id = oi.id
         WHERE COALESCE(ord.rejection_code, oi.rejection_code) IS NOT NULL
           AND (%s::uuid IS NULL OR a.id = %s::uuid)
         ORDER BY oi.created_at DESC, oi.id DESC
         LIMIT %s OFFSET %s
    """,
    "cycles": """
        SELECT ac.id, ac.agent_id, a.name AS agent_name, ac.scheduled_at,
               ac.data_cutoff, ac.status, ac.started_at, ac.completed_at,
               ac.model_termination_status, ac.failure_reason,
               cc.rendered_prompt_sha256, cc.artifact_sha256,
               pv.name AS prompt_version, pv.body_sha256 AS prompt_sha256,
               ed.experiment_version, ed.version_number AS config_version,
               ed.config_sha256, ed.code_version
          FROM agent_cycles ac
          JOIN agents a ON a.id = ac.agent_id
          JOIN experiment_runs er ON er.id = a.run_id
          JOIN experiment_definitions ed ON ed.id = er.definition_id
          LEFT JOIN cycle_contexts cc ON cc.agent_cycle_id = ac.id
          LEFT JOIN prompt_versions pv ON pv.id = cc.prompt_version_id
         WHERE (%s::uuid IS NULL OR ac.agent_id = %s::uuid)
         ORDER BY ac.scheduled_at DESC, ac.id DESC
         LIMIT %s OFFSET %s
    """,
    "usage": """
        SELECT pu.provider, pu.route, pu.usage_kind,
               count(*) AS usage_records, sum(pu.request_count) AS request_count,
               sum(pu.credit_count) AS credit_count,
               sum(pu.prompt_tokens) AS prompt_tokens,
               sum(pu.completion_tokens) AS completion_tokens,
               sum(pu.reasoning_tokens) AS reasoning_tokens,
               sum(pu.cached_tokens) AS cached_tokens,
               sum(pu.billed_cost_micros) AS billed_cost_micros,
               sum(pu.nominal_cost_micros) AS nominal_cost_micros,
               avg(pu.latency_ms) AS average_latency_ms,
               max(pu.created_at) AS last_used_at
          FROM provider_usage pu
          LEFT JOIN agent_cycles ac ON ac.id = pu.agent_cycle_id
         WHERE (%s::uuid IS NULL OR ac.agent_id = %s::uuid)
         GROUP BY pu.provider, pu.route, pu.usage_kind
         ORDER BY billed_cost_micros DESC NULLS LAST, pu.provider, pu.route
         LIMIT %s OFFSET %s
    """,
    "freshness": """
        SELECT source, last_observed_at,
               CASE WHEN last_observed_at IS NULL THEN NULL
                    ELSE extract(epoch FROM (now() - last_observed_at))::bigint END
                    AS age_seconds,
               record_count
          FROM (
              SELECT 'market' AS source, max(observed_at) AS last_observed_at,
                     count(*) AS record_count FROM markets
              UNION ALL
              SELECT 'order_book', max(cutoff), count(*) FROM order_book_snapshots
              UNION ALL
              SELECT 'resolution', max(observed_at), count(*) FROM resolutions
              UNION ALL
              SELECT 'venue_sync', max(observed_at), count(*) FROM venue_sync_pages
          ) observations
         ORDER BY source
    """,
    "config_versions": """
        SELECT ed.id, ed.experiment_version, ed.version_number, ed.status,
               ed.definition, ed.config_sha256, ed.code_version, ed.created_at,
               ed.supersedes_id,
               jsonb_agg(DISTINCT jsonb_build_object(
                   'model_config_id', mc.id, 'label', mc.label,
                   'model_slug', mc.model_slug, 'provider_policy', mc.provider_policy,
                   'parameters', mc.parameters, 'config_sha256', mc.config_sha256
               )) FILTER (WHERE mc.id IS NOT NULL) AS models,
               jsonb_agg(DISTINCT jsonb_build_object(
                   'prompt_version_id', pv.id, 'name', pv.name,
                   'body', pv.body, 'body_sha256', pv.body_sha256,
                   'classification', pv.classification
               )) FILTER (WHERE pv.id IS NOT NULL) AS prompts
          FROM experiment_definitions ed
          LEFT JOIN model_configs mc ON mc.definition_id = ed.id
          LEFT JOIN prompt_versions pv ON pv.definition_id = ed.id
         GROUP BY ed.id
         ORDER BY ed.created_at DESC, ed.id DESC
         LIMIT %s OFFSET %s
    """,
    "alerts": """
        SELECT al.id, al.run_id, al.agent_id, al.severity, al.code, al.details,
               al.opened_at, al.acknowledged_at, al.resolved_at
          FROM alerts al
         ORDER BY (al.resolved_at IS NULL) DESC, al.opened_at DESC, al.id DESC
         LIMIT %s OFFSET %s
    """,
    "operator_actions": """
        SELECT id, actor_id, action, target_type, target_id, before_state,
               after_state, occurred_at, idempotency_key
          FROM operator_actions
         ORDER BY occurred_at DESC, id DESC
         LIMIT %s OFFSET %s
    """,
}


class PostgresAdminRepository:
    """Read-only operator views plus the four explicitly allowed control mutations."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def probe(self) -> dict[str, object]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), now()")
            row = cursor.fetchone()
            if row is None:
                raise AdminRepositoryError("database probe returned no result")
            cursor.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            )
            migration = cursor.fetchone()
        return {
            "database": str(row[0]),
            "database_time": row[1],
            "latest_migration": str(migration[0]) if migration else None,
        }

    def view(
        self,
        name: str,
        *,
        page: Page | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> list[dict[str, object]]:
        try:
            query = _VIEWS[name]
        except KeyError as exc:
            raise ValueError(f"unknown admin view: {name}") from exc
        selected_page = page or Page()
        if name in {"positions", "trades", "settlements", "rejections", "cycles", "usage"}:
            params: tuple[object, ...] = (
                agent_id,
                agent_id,
                selected_page.limit,
                selected_page.offset,
            )
        elif name == "freshness":
            params = ()
        else:
            params = (selected_page.limit, selected_page.offset)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(query, params)
            return _mapped_rows(cursor)

    def overview(self) -> dict[str, object]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE status = 'running') AS running_runs, "
                "count(*) FILTER (WHERE status = 'paused') AS paused_runs, count(*) AS runs "
                "FROM experiment_runs"
            )
            runs = _mapped_row(cursor)
            cursor.execute(
                "SELECT count(*) AS agents, count(*) FILTER (WHERE paused_at IS NOT NULL) "
                "AS paused_agents FROM agents"
            )
            agents = _mapped_row(cursor)
            cursor.execute(
                "SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS open_alerts, "
                "max(opened_at) FILTER (WHERE resolved_at IS NULL) AS latest_alert_at "
                "FROM alerts"
            )
            alerts = _mapped_row(cursor)
            cursor.execute(
                "SELECT max(completed_at) FILTER (WHERE status = 'completed') "
                "AS last_success_at, "
                "max(completed_at) FILTER (WHERE status = 'failed') AS last_failure_at, "
                "count(*) FILTER (WHERE status = 'running') AS running_cycles, "
                "count(*) FILTER (WHERE status = 'failed') AS failed_cycles "
                "FROM agent_cycles"
            )
            cycles = _mapped_row(cursor)
            cursor.execute(
                "SELECT globally_paused, version, updated_at, updated_by "
                "FROM system_controls WHERE singleton = true"
            )
            controls = _mapped_row(cursor)
        return {
            "runs": runs,
            "agents": agents,
            "alerts": alerts,
            "cycles": cycles,
            "controls": controls,
        }

    def set_global_pause(
        self,
        *,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, object]:
        return self._control(
            target_type="system",
            target_id=None,
            paused=paused,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )

    def set_agent_pause(
        self,
        agent_id: uuid.UUID,
        *,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, object]:
        return self._control(
            target_type="agent",
            target_id=agent_id,
            paused=paused,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )

    def _control(
        self,
        *,
        target_type: str,
        target_id: uuid.UUID | None,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None,
    ) -> dict[str, object]:
        _validate_operator_fields(actor_id, idempotency_key)
        when = _aware(occurred_at or datetime.now(UTC))
        action = "pause" if paused else "resume"
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT actor_id, action, target_type, target_id, after_state "
                "FROM operator_actions WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != actor_id
                    or str(existing[1]) != action
                    or str(existing[2]) != target_type
                    or _optional_uuid(existing[3]) != target_id
                ):
                    raise InvalidOperatorAction(
                        "operator idempotency key was already used for another action"
                    )
                return _json_object(existing[4])

            if target_type == "system":
                cursor.execute(
                    "SELECT globally_paused, version, updated_at, updated_by "
                    "FROM system_controls WHERE singleton = true FOR UPDATE"
                )
                row = cursor.fetchone()
                if row is None:
                    raise AdminRepositoryError("system control row is missing")
                before = {
                    "globally_paused": bool(row[0]),
                    "version": int(str(row[1])),
                    "updated_at": str(row[2]),
                    "updated_by": str(row[3]),
                }
                cursor.execute(
                    "UPDATE system_controls SET globally_paused = %s, version = version + 1, "
                    "updated_at = %s, updated_by = %s WHERE singleton = true "
                    "RETURNING globally_paused, version, updated_at, updated_by",
                    (paused, when, actor_id),
                )
                changed = cursor.fetchone()
                if changed is None:
                    raise AdminRepositoryError("system pause update returned no result")
                after = {
                    "globally_paused": bool(changed[0]),
                    "version": int(str(changed[1])),
                    "updated_at": str(changed[2]),
                    "updated_by": str(changed[3]),
                }
            else:
                if target_id is None:
                    raise InvalidOperatorAction("agent target is required")
                cursor.execute(
                    "SELECT paused_at FROM agents WHERE id = %s FOR UPDATE", (target_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError("agent not found")
                before = {"agent_id": str(target_id), "paused_at": _optional_text(row[0])}
                cursor.execute(
                    "UPDATE agents SET paused_at = %s WHERE id = %s RETURNING paused_at",
                    (when if paused else None, target_id),
                )
                changed = cursor.fetchone()
                if changed is None:
                    raise AdminRepositoryError("agent pause update returned no result")
                after = {"agent_id": str(target_id), "paused_at": _optional_text(changed[0])}

            cursor.execute(
                "INSERT INTO operator_actions "
                "(id, actor_id, action, target_type, target_id, before_state, after_state, "
                "occurred_at, idempotency_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    actor_id,
                    action,
                    target_type,
                    target_id,
                    json.dumps(before),
                    json.dumps(after),
                    when,
                    idempotency_key,
                ),
            )
            return after


def _mapped_rows(cursor: _Cursor) -> list[dict[str, object]]:
    description = cursor.description
    if description is None:
        raise AdminRepositoryError("view query returned no columns")
    names = [str(column[0]) for column in description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _mapped_row(cursor: _Cursor) -> dict[str, object]:
    description = cursor.description
    row = cursor.fetchone()
    if description is None or row is None:
        raise AdminRepositoryError("summary query returned no row")
    names = [str(column[0]) for column in description]
    return dict(zip(names, row, strict=True))


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _validate_operator_fields(actor_id: str, idempotency_key: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", actor_id):
        raise InvalidOperatorAction("invalid operator identity")
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{8,200}", idempotency_key):
        raise InvalidOperatorAction("invalid operator idempotency key")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(str(value))


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise AdminRepositoryError("audited action state is not an object")
    return {str(key): item for key, item in parsed.items()}
