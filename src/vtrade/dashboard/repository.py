"""Fixed, bounded, read-only PostgreSQL queries for the audit dashboard."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast

from vtrade.dashboard.policy import (
    ACTIVE_EXPERIMENT_DEFINITION_SQL,
    FRESHNESS_MAX_AGE_SQL,
    POSITION_VALUATION_MAX_AGE_SQL,
)
from vtrade.dashboard.service import build_cycle_diagnostics


class DashboardRepositoryError(RuntimeError):
    """Raised when a dashboard query does not produce its expected shape."""


class _Cursor(Protocol):
    description: Sequence[Sequence[object]] | None

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


class DashboardWindow(StrEnum):
    LAST_24_HOURS = "24h"
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    ALL = "all"

    @property
    def duration(self) -> timedelta | None:
        return {
            DashboardWindow.LAST_24_HOURS: timedelta(hours=24),
            DashboardWindow.LAST_7_DAYS: timedelta(days=7),
            DashboardWindow.LAST_30_DAYS: timedelta(days=30),
            DashboardWindow.ALL: None,
        }[self]


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    window: DashboardWindow = DashboardWindow.LAST_30_DAYS
    run_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None

    def since(self, now: datetime | None = None) -> datetime | None:
        if self.window.duration is None:
            return None
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None or reference.utcoffset() is None:
            raise ValueError("dashboard timestamps must be timezone-aware")
        return reference.astimezone(UTC) - self.window.duration


@dataclass(frozen=True, slots=True)
class DashboardPage:
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if self.offset < 0:
            raise ValueError("offset cannot be negative")


class PostgresDashboardRepository:
    """Private read model for dashboard routes; it never mutates agent state."""

    def __init__(
        self,
        database_url: str,
        *,
        connect: _Connect | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def overview(self, filters: DashboardFilters | None = None) -> dict[str, object]:
        """Return aggregate performance, cycle, cost, and alert measures."""

        selected = filters or DashboardFilters()
        scope = _scope_params(selected, self._clock())
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                _OVERVIEW_PERFORMANCE,
                (*scope, scope[4], scope[5], scope[4], scope[5]),
            )
            performance = _mapped_row(cursor)
            cursor.execute(_OVERVIEW_CYCLES, scope)
            cycles = _mapped_row(cursor)
            cursor.execute(_OVERVIEW_USAGE, scope)
            usage = _mapped_row(cursor)
            cursor.execute(_OVERVIEW_ALERTS, scope)
            alerts = _mapped_row(cursor)
            cursor.execute(_PERFORMANCE_HISTORY, (*scope, 10_000))
            performance_history = _mapped_rows(cursor)
            cursor.execute(_USAGE_BREAKDOWN, (*scope, 50))
            usage_by_provider = _mapped_rows(cursor)
            cursor.execute(_OPEN_ALERTS, (*scope, 100))
            open_alerts = _mapped_rows(cursor)
            cursor.execute(_FRESHNESS)
            freshness = _mapped_rows(cursor)
            cursor.execute(_SYSTEM_CONTROLS)
            controls = _mapped_row(cursor)
        return {
            "filters": _filter_payload(selected),
            "performance": performance,
            "performance_history": performance_history,
            "cycles": cycles,
            "usage": usage,
            "usage_by_provider": usage_by_provider,
            "alerts": alerts,
            "open_alerts": open_alerts,
            "freshness": freshness,
            "controls": controls,
        }

    def agents(
        self,
        filters: DashboardFilters | None = None,
        *,
        page: DashboardPage | None = None,
    ) -> list[dict[str, object]]:
        """Return performance plus current beliefs and both current plan types per agent."""

        selected = filters or DashboardFilters()
        selected_page = page or DashboardPage()
        scope = _scope_params(selected, self._clock())
        params = (
            scope[4],
            scope[5],
            scope[4],
            scope[5],
            *scope,
            selected_page.limit,
            selected_page.offset,
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(_AGENTS, params)
            return _mapped_rows(cursor)

    def cycles(
        self,
        filters: DashboardFilters | None = None,
        *,
        page: DashboardPage | None = None,
    ) -> list[dict[str, object]]:
        """Return bounded cycle summaries suitable for the audit timeline."""

        selected = filters or DashboardFilters()
        selected_page = page or DashboardPage()
        params = (
            *_scope_params(selected, self._clock()),
            selected_page.limit,
            selected_page.offset,
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(_CYCLES, params)
            return _mapped_rows(cursor)

    def cycle_detail(self, cycle_id: uuid.UUID) -> dict[str, object] | None:
        """Return retained cycle evidence and deterministic diagnostics for one UUID."""

        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(_CYCLE_METADATA, (cycle_id,))
            metadata = _mapped_optional_row(cursor)
            if metadata is None:
                return None
            cursor.execute(_CYCLE_MODEL_TURNS, (cycle_id,))
            model_turns = _mapped_rows(cursor)
            cursor.execute(_CYCLE_TOOL_CALLS, (cycle_id,))
            tool_calls = _mapped_rows(cursor)
            cursor.execute(_CYCLE_RESEARCH, (cycle_id,))
            research = _mapped_rows(cursor)
            cursor.execute(_CYCLE_PROVIDER_USAGE, (cycle_id,))
            provider_usage = _mapped_rows(cursor)
            cursor.execute(_CYCLE_BELIEFS, (cycle_id,))
            beliefs = _mapped_rows(cursor)
            cursor.execute(_CYCLE_PLANS, (cycle_id,))
            plans = _mapped_rows(cursor)
            cursor.execute(_CYCLE_ORDERS, (cycle_id,))
            operations = _mapped_rows(cursor)
            cursor.execute(_CYCLE_RUNTIME_STEPS, (cycle_id,))
            runtime_steps = _mapped_rows(cursor)
        detail: dict[str, object] = {
            "metadata": metadata,
            "performance": _performance_from_metadata(metadata),
            "model_turns": model_turns,
            "tool_calls": tool_calls,
            "research": research,
            "provider_usage": provider_usage,
            "belief_revisions": beliefs,
            "plan_revisions": plans,
            "operations": operations,
            "runtime_steps": runtime_steps,
        }
        detail["diagnostics"] = build_cycle_diagnostics(detail)
        return detail


def _scope_params(filters: DashboardFilters, now: datetime) -> tuple[object, ...]:
    since = filters.since(now)
    return (filters.run_id, filters.run_id, filters.agent_id, filters.agent_id, since, since)


def _filter_payload(filters: DashboardFilters) -> dict[str, object]:
    return {
        "window": filters.window.value,
        "run_id": str(filters.run_id) if filters.run_id is not None else None,
        "agent_id": str(filters.agent_id) if filters.agent_id is not None else None,
    }


def _performance_from_metadata(metadata: dict[str, object]) -> dict[str, object]:
    keys = (
        "cash_micros",
        "position_liquidation_micros",
        "account_value_micros",
        "realized_pnl_micros",
        "unrealized_pnl_micros",
        "entry_fees_micros",
        "performance_calculated_at",
    )
    return {key: metadata.get(key) for key in keys}


def _mapped_rows(cursor: _Cursor) -> list[dict[str, object]]:
    description = cursor.description
    if description is None:
        raise DashboardRepositoryError("dashboard query returned no columns")
    names = [str(column[0]) for column in description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _mapped_row(cursor: _Cursor) -> dict[str, object]:
    row = _mapped_optional_row(cursor)
    if row is None:
        raise DashboardRepositoryError("dashboard summary returned no row")
    return row


def _mapped_optional_row(cursor: _Cursor) -> dict[str, object] | None:
    description = cursor.description
    row = cursor.fetchone()
    if description is None:
        raise DashboardRepositoryError("dashboard query returned no columns")
    if row is None:
        return None
    names = [str(column[0]) for column in description]
    return dict(zip(names, row, strict=True))


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


_SCOPE = """
  (%s::uuid IS NULL OR a.run_id = %s::uuid)
  AND (%s::uuid IS NULL OR a.id = %s::uuid)
  AND (%s::timestamptz IS NULL OR ac.scheduled_at >= %s::timestamptz)
"""

_OVERVIEW_PERFORMANCE = f"""
WITH scoped_agents AS (
    SELECT DISTINCT a.id, a.initial_cash_micros
      FROM agents a
      JOIN agent_cycles ac ON ac.agent_id = a.id
     WHERE {_SCOPE}
)
SELECT count(*) AS agents,
       count(latest.account_value_micros) AS performance_points,
       COALESCE(sum(a.initial_cash_micros), 0)::bigint AS initial_cash_micros,
       COALESCE(sum(latest.account_value_micros), 0)::bigint AS account_value_micros,
       COALESCE(sum(latest.account_value_micros - a.initial_cash_micros), 0)::bigint
           AS total_pnl_micros,
       COALESCE(sum(latest.realized_pnl_micros), 0)::bigint AS realized_pnl_micros,
       COALESCE(sum(latest.unrealized_pnl_micros), 0)::bigint AS unrealized_pnl_micros,
       COALESCE(sum(latest.entry_fees_micros), 0)::bigint AS entry_fees_micros,
       CASE WHEN sum(a.initial_cash_micros) = 0 THEN NULL
            ELSE sum(latest.account_value_micros - a.initial_cash_micros)::numeric
                 / sum(a.initial_cash_micros) END AS return_fraction,
       max(CASE WHEN peak.account_value_micros IS NULL OR peak.account_value_micros = 0
                THEN NULL
                ELSE (peak.account_value_micros - latest.account_value_micros)::numeric
                     / peak.account_value_micros END) AS drawdown_fraction
  FROM scoped_agents a
  LEFT JOIN LATERAL (
      SELECT current.account_value_micros, current.realized_pnl_micros,
             current.unrealized_pnl_micros,
             (current.calculation ->> 'entry_fees_micros')::bigint AS entry_fees_micros
        FROM performance_snapshots current
        JOIN agent_cycles current_cycle ON current_cycle.id = current.agent_cycle_id
       WHERE current_cycle.agent_id = a.id
         AND (%s::timestamptz IS NULL OR current_cycle.scheduled_at >= %s::timestamptz)
       ORDER BY current.calculated_at DESC, current.id DESC
       LIMIT 1
  ) latest ON true
  LEFT JOIN LATERAL (
      SELECT max(snapshot.account_value_micros) AS account_value_micros
        FROM performance_snapshots snapshot
        JOIN agent_cycles peak_cycle ON peak_cycle.id = snapshot.agent_cycle_id
       WHERE peak_cycle.agent_id = a.id
         AND (%s::timestamptz IS NULL OR peak_cycle.scheduled_at >= %s::timestamptz)
  ) peak ON true
"""

_OVERVIEW_CYCLES = f"""
SELECT count(*) AS total_cycles,
       count(*) FILTER (WHERE ac.status = 'completed') AS completed_cycles,
       count(*) FILTER (WHERE ac.status = 'failed') AS failed_cycles,
       count(*) FILTER (WHERE ac.status = 'running') AS running_cycles,
       count(*) FILTER (WHERE ac.status = 'skipped') AS skipped_cycles,
       max(ac.completed_at) FILTER (WHERE ac.status = 'completed') AS last_success_at,
       max(ac.completed_at) FILTER (WHERE ac.status = 'failed') AS last_failure_at,
       avg(extract(epoch FROM (ac.completed_at - ac.started_at)))
           FILTER (WHERE ac.completed_at IS NOT NULL AND ac.started_at IS NOT NULL)
           AS average_duration_seconds
  FROM agent_cycles ac
  JOIN agents a ON a.id = ac.agent_id
 WHERE {_SCOPE}
"""

_OVERVIEW_USAGE = f"""
SELECT COALESCE(sum(pu.prompt_tokens), 0)::bigint AS prompt_tokens,
       COALESCE(sum(pu.completion_tokens), 0)::bigint AS completion_tokens,
       COALESCE(sum(pu.reasoning_tokens), 0)::bigint AS reasoning_tokens,
       COALESCE(sum(pu.cached_tokens), 0)::bigint AS cached_tokens,
       COALESCE(sum(pu.request_count), 0)::bigint AS request_count,
       COALESCE(sum(pu.billed_cost_micros), 0)::bigint AS billed_cost_micros,
       COALESCE(sum(pu.nominal_cost_micros), 0)::bigint AS nominal_cost_micros,
       avg(pu.latency_ms) AS average_latency_ms
  FROM provider_usage pu
  JOIN agent_cycles ac ON ac.id = pu.agent_cycle_id
  JOIN agents a ON a.id = ac.agent_id
 WHERE {_SCOPE}
"""

_OVERVIEW_ALERTS = """
SELECT count(*) FILTER (WHERE al.resolved_at IS NULL) AS open_alerts,
       count(*) FILTER (WHERE al.resolved_at IS NULL AND al.severity = 'critical')
           AS critical_alerts,
       max(al.opened_at) FILTER (WHERE al.resolved_at IS NULL) AS latest_opened_at
 FROM alerts al
 WHERE (%s::uuid IS NULL OR al.run_id = %s::uuid)
   AND (%s::uuid IS NULL OR al.agent_id = %s::uuid)
   AND (%s::timestamptz IS NULL OR al.opened_at >= %s::timestamptz)
"""

_PERFORMANCE_HISTORY = f"""
SELECT ac.agent_id, a.name AS agent_name, ps.calculated_at,
       ps.cash_micros, ps.position_liquidation_micros, ps.account_value_micros,
       ps.realized_pnl_micros, ps.unrealized_pnl_micros,
       (ps.calculation ->> 'entry_fees_micros')::bigint AS entry_fees_micros
  FROM performance_snapshots ps
  JOIN agent_cycles ac ON ac.id = ps.agent_cycle_id
  JOIN agents a ON a.id = ac.agent_id
 WHERE {_SCOPE}
 ORDER BY ps.calculated_at ASC, ps.id ASC
 LIMIT %s
"""

_USAGE_BREAKDOWN = f"""
SELECT pu.provider, pu.route, pu.usage_kind,
       COALESCE(sum(pu.request_count), 0)::bigint AS request_count,
       COALESCE(sum(pu.prompt_tokens), 0)::bigint AS prompt_tokens,
       COALESCE(sum(pu.completion_tokens), 0)::bigint AS completion_tokens,
       COALESCE(sum(pu.reasoning_tokens), 0)::bigint AS reasoning_tokens,
       COALESCE(sum(pu.cached_tokens), 0)::bigint AS cached_tokens,
       COALESCE(sum(pu.billed_cost_micros), 0)::bigint AS billed_cost_micros,
       COALESCE(sum(pu.nominal_cost_micros), 0)::bigint AS nominal_cost_micros,
       avg(pu.latency_ms) AS average_latency_ms
  FROM provider_usage pu
  JOIN agent_cycles ac ON ac.id = pu.agent_cycle_id
  JOIN agents a ON a.id = ac.agent_id
 WHERE {_SCOPE}
 GROUP BY pu.provider, pu.route, pu.usage_kind
 ORDER BY billed_cost_micros DESC, pu.provider, pu.route
 LIMIT %s
"""

_OPEN_ALERTS = """
SELECT al.id, al.run_id, al.agent_id, al.severity, al.code, al.details,
       al.opened_at, al.acknowledged_at
  FROM alerts al
 WHERE al.resolved_at IS NULL
   AND (%s::uuid IS NULL OR al.run_id = %s::uuid)
   AND (%s::uuid IS NULL OR al.agent_id = %s::uuid)
   AND (%s::timestamptz IS NULL OR al.opened_at >= %s::timestamptz)
 ORDER BY al.opened_at DESC, al.id DESC
 LIMIT %s
"""

_FRESHNESS = f"""
WITH active_definition AS (
    {ACTIVE_EXPERIMENT_DEFINITION_SQL}
), observations AS (
      SELECT 'market' AS source, max(observed_at) AS last_observed_at,
             count(*) AS record_count FROM markets
      UNION ALL
      SELECT 'catalogue', max(observed_at), count(*)
        FROM catalogue_page_observations
      UNION ALL
      SELECT 'order_book', max(cutoff), count(*) FROM order_book_snapshots
      UNION ALL
      SELECT 'resolution', max(observed_at), count(*)
        FROM resolution_observations
)
SELECT source, last_observed_at,
       CASE WHEN last_observed_at IS NULL THEN NULL
            ELSE extract(epoch FROM (now() - last_observed_at))::bigint END AS age_seconds,
       record_count,
       CASE WHEN last_observed_at IS NULL THEN 'missing'
            WHEN last_observed_at < now()
                 - make_interval(secs => freshness_policy.max_age_seconds)
                THEN 'stale'
            ELSE 'fresh' END AS status,
       freshness_policy.max_age_seconds AS freshness_max_age_seconds
  FROM observations
  LEFT JOIN active_definition ON true
 CROSS JOIN LATERAL (
      SELECT {FRESHNESS_MAX_AGE_SQL} AS max_age_seconds
  ) freshness_policy
 ORDER BY source
"""

_SYSTEM_CONTROLS = """
SELECT globally_paused, version, updated_at, updated_by
  FROM system_controls
 WHERE singleton = true
"""

_AGENTS = f"""
SELECT a.id AS agent_id, a.run_id, a.name AS agent_name, a.initial_cash_micros, a.paused_at,
       mc.label AS model_label, mc.model_slug,
       latest.account_value_micros, latest.realized_pnl_micros, latest.unrealized_pnl_micros,
       latest.entry_fees_micros,
       latest.calculated_at AS performance_calculated_at,
       latest.account_value_micros - a.initial_cash_micros AS total_pnl_micros,
       CASE WHEN a.initial_cash_micros = 0 OR latest.account_value_micros IS NULL THEN NULL
            ELSE (latest.account_value_micros - a.initial_cash_micros)::numeric
                 / a.initial_cash_micros END AS return_fraction,
       CASE WHEN peak.account_value_micros IS NULL OR peak.account_value_micros = 0 THEN NULL
            ELSE (peak.account_value_micros - latest.account_value_micros)::numeric
                 / peak.account_value_micros END AS drawdown_fraction,
       COALESCE(belief_snapshot.beliefs, '[]'::jsonb) AS current_beliefs,
       COALESCE(plan_snapshot.plans, '[]'::jsonb) AS active_plans,
       COALESCE(position_snapshot.positions, '[]'::jsonb) AS open_positions
  FROM agents a
  JOIN model_configs mc ON mc.id = a.model_config_id
  JOIN experiment_runs er ON er.id = a.run_id
  JOIN experiment_definitions ed ON ed.id = er.definition_id
  JOIN agent_cycles ac ON ac.agent_id = a.id
  LEFT JOIN LATERAL (
      SELECT ps.account_value_micros, ps.realized_pnl_micros, ps.unrealized_pnl_micros,
             (ps.calculation ->> 'entry_fees_micros')::bigint AS entry_fees_micros,
             ps.calculated_at
        FROM performance_snapshots ps
        JOIN agent_cycles latest_cycle ON latest_cycle.id = ps.agent_cycle_id
       WHERE latest_cycle.agent_id = a.id
         AND (%s::timestamptz IS NULL OR latest_cycle.scheduled_at >= %s::timestamptz)
       ORDER BY ps.calculated_at DESC, ps.id DESC LIMIT 1
  ) latest ON true
  LEFT JOIN LATERAL (
      SELECT max(ps.account_value_micros) AS account_value_micros
        FROM performance_snapshots ps
        JOIN agent_cycles peak_cycle ON peak_cycle.id = ps.agent_cycle_id
       WHERE peak_cycle.agent_id = a.id
         AND (%s::timestamptz IS NULL OR peak_cycle.scheduled_at >= %s::timestamptz)
  ) peak ON true
  LEFT JOIN LATERAL (
      SELECT jsonb_agg(jsonb_build_object(
          'belief_id', b.id, 'revision_id', br.id, 'revision', br.revision,
          'content', br.content, 'category', br.category, 'confidence', br.confidence,
          'evidence', br.evidence, 'created_by_cycle_id', br.created_by_cycle_id,
          'created_at', br.created_at
      ) ORDER BY br.created_at DESC, br.id DESC) AS beliefs
        FROM beliefs b
        JOIN LATERAL (
            SELECT revision.id, revision.revision, revision.content, revision.category,
                   revision.confidence, revision.evidence, revision.created_by_cycle_id,
                   revision.created_at
              FROM belief_revisions revision
             WHERE revision.belief_id = b.id
             ORDER BY revision.revision DESC LIMIT 1
        ) br ON true
       WHERE b.agent_id = a.id AND b.active
  ) belief_snapshot ON true
  LEFT JOIN LATERAL (
      SELECT jsonb_agg(jsonb_build_object(
          'plan_id', p.id, 'plan_type', p.plan_type, 'status', p.status, 'due_at', p.due_at,
          'revision_id', pr.id, 'revision', pr.revision, 'content', pr.content,
          'created_by_cycle_id', pr.created_by_cycle_id, 'created_at', pr.created_at
      ) ORDER BY p.plan_type, pr.revision DESC) AS plans
        FROM plans p
        JOIN LATERAL (
            SELECT revision.id, revision.revision, revision.content,
                   revision.created_by_cycle_id, revision.created_at
              FROM plan_revisions revision
             WHERE revision.plan_id = p.id
             ORDER BY revision.revision DESC LIMIT 1
        ) pr ON true
       WHERE p.agent_id = a.id AND p.status = 'active'
  ) plan_snapshot ON true
  LEFT JOIN LATERAL (
      SELECT {POSITION_VALUATION_MAX_AGE_SQL} AS max_age_seconds
  ) valuation_policy ON true
  LEFT JOIN LATERAL (
      SELECT jsonb_agg(jsonb_build_object(
          'position_id', p.id, 'market_ref', m.market_ref, 'market_question', m.question,
          'outcome', p.outcome_side, 'contract_units', p.contract_units,
          'gross_cost_basis_micros', p.gross_cost_basis_micros,
          'entry_fees_micros', p.entry_fees_micros,
          'realized_pnl_micros', p.realized_pnl_micros,
          'bid_price_micros', quote.price_micros, 'quote_cutoff', quote.cutoff,
          'bid_snapshot_id', quote.snapshot_id,
          'bid_artifact_id', quote.raw_artifact_id,
          'bid_artifact_sha256', quote.raw_sha256,
          'bid_artifact_uri', quote.raw_uri,
          'bid_artifact_observed_at', quote.raw_observed_at,
          'liquidation_value_micros',
              CASE WHEN quote.price_micros IS NULL OR quote.cutoff IS NULL
                         OR quote.cutoff < now()
                            - make_interval(secs => valuation_policy.max_age_seconds)
                   THEN NULL
                   ELSE round(p.contract_units::numeric * quote.price_micros / 100)::bigint END,
          'unrealized_pnl_micros',
              CASE WHEN quote.price_micros IS NULL OR quote.cutoff IS NULL
                         OR quote.cutoff < now()
                            - make_interval(secs => valuation_policy.max_age_seconds)
                   THEN NULL
                   ELSE round(p.contract_units::numeric * quote.price_micros / 100)::bigint
                        - p.gross_cost_basis_micros - p.entry_fees_micros END,
          'valuation_max_age_seconds', valuation_policy.max_age_seconds,
          'valuation_status',
              CASE WHEN quote.price_micros IS NULL OR quote.cutoff IS NULL THEN 'missing'
                   WHEN quote.cutoff < now()
                        - make_interval(secs => valuation_policy.max_age_seconds)
                        THEN 'stale'
                   ELSE 'fresh' END,
          'updated_at', p.updated_at
      ) ORDER BY p.gross_cost_basis_micros DESC, p.id) AS positions
        FROM positions p
        JOIN markets m ON m.id = p.market_id
        LEFT JOIN LATERAL (
            SELECT obl.price_micros, obs.cutoff, obs.id AS snapshot_id,
                   obs.raw_artifact_id, ra.sha256 AS raw_sha256,
                   ra.uri AS raw_uri, ra.observed_at AS raw_observed_at
              FROM order_book_snapshots obs
              JOIN order_book_levels obl ON obl.snapshot_id = obs.id
              JOIN raw_artifacts ra ON ra.id = obs.raw_artifact_id
             WHERE obs.market_id = p.market_id
               AND obl.outcome_side = p.outcome_side
               AND obl.book_side = 'bid'
             ORDER BY obs.cutoff DESC, obs.id DESC, obl.price_micros DESC LIMIT 1
        ) quote ON true
       WHERE p.agent_id = a.id AND p.contract_units > 0
  ) position_snapshot ON true
 WHERE {_SCOPE}
 GROUP BY a.id, mc.id, latest.account_value_micros, latest.realized_pnl_micros,
          latest.unrealized_pnl_micros, latest.entry_fees_micros, latest.calculated_at,
          peak.account_value_micros,
          belief_snapshot.beliefs, plan_snapshot.plans, position_snapshot.positions
 ORDER BY latest.account_value_micros DESC NULLS LAST, a.name, a.id
 LIMIT %s OFFSET %s
"""

_CYCLES = f"""
SELECT ac.id AS cycle_id, ac.agent_id, a.run_id, a.name AS agent_name, mc.label AS model_label,
       ac.scheduled_at, ac.data_cutoff, ac.status, ac.started_at, ac.completed_at,
       ac.model_termination_status, ac.failure_reason, ac.final_summary, ac.attempt_count,
       hr.termination_status AS harness_termination_status, hr.total_model_turns,
       hr.total_tool_calls, hr.total_web_searches, hr.total_completion_tokens,
       ps.account_value_micros, ps.realized_pnl_micros, ps.unrealized_pnl_micros,
       (ps.calculation ->> 'entry_fees_micros')::bigint AS entry_fees_micros,
       COALESCE(tool_summary.failed_tools, 0)::bigint AS failed_tools,
       COALESCE(order_summary.operations, 0)::bigint AS operations
  FROM agent_cycles ac
  JOIN agents a ON a.id = ac.agent_id
  JOIN model_configs mc ON mc.id = a.model_config_id
  LEFT JOIN harness_runs hr ON hr.agent_cycle_id = ac.id
  LEFT JOIN performance_snapshots ps ON ps.agent_cycle_id = ac.id
  LEFT JOIN LATERAL (
      SELECT count(*) FILTER (WHERE tc.success IS FALSE) AS failed_tools
        FROM model_turns mt JOIN tool_calls tc ON tc.model_turn_id = mt.id
       WHERE mt.agent_cycle_id = ac.id
  ) tool_summary ON true
  LEFT JOIN LATERAL (
       SELECT count(*) AS operations FROM order_operations oo WHERE oo.agent_cycle_id = ac.id
  ) order_summary ON true
 WHERE {_SCOPE}
 ORDER BY ac.scheduled_at DESC, ac.id DESC
 LIMIT %s OFFSET %s
"""

_CYCLE_METADATA = """
SELECT ac.id AS cycle_id, ac.agent_id, a.run_id, a.name AS agent_name, mc.label AS model_label,
       mc.model_slug, ac.scheduled_at, ac.data_cutoff, ac.status, ac.started_at, ac.completed_at,
       ac.model_termination_status, ac.failure_reason, ac.final_summary, ac.attempt_count,
       hr.termination_status AS harness_termination_status, hr.total_model_turns,
       hr.total_tool_calls, hr.total_web_searches, hr.total_completion_tokens,
       cc.rendered_cycle_prompt, cc.context AS prompt_context, cc.rendered_prompt_sha256,
       cc.artifact_uri AS prompt_artifact_uri, cc.artifact_sha256 AS prompt_artifact_sha256,
       cc.retain_until AS prompt_retain_until,
       (cc.retention_purged_at IS NOT NULL) AS prompt_retention_purged,
       pv.name AS prompt_version, pv.body_sha256 AS prompt_sha256,
       ed.experiment_version, ed.version_number AS config_version, ed.config_sha256,
       ed.code_version, ps.cash_micros, ps.position_liquidation_micros,
       ps.account_value_micros, ps.realized_pnl_micros, ps.unrealized_pnl_micros,
       (ps.calculation ->> 'entry_fees_micros')::bigint AS entry_fees_micros,
       ps.calculated_at AS performance_calculated_at
  FROM agent_cycles ac
  JOIN agents a ON a.id = ac.agent_id
  JOIN model_configs mc ON mc.id = a.model_config_id
  JOIN experiment_runs er ON er.id = a.run_id
  JOIN experiment_definitions ed ON ed.id = er.definition_id
  LEFT JOIN cycle_contexts cc ON cc.agent_cycle_id = ac.id
  LEFT JOIN prompt_versions pv ON pv.id = cc.prompt_version_id
  LEFT JOIN harness_runs hr ON hr.agent_cycle_id = ac.id
  LEFT JOIN performance_snapshots ps ON ps.agent_cycle_id = ac.id
 WHERE ac.id = %s::uuid
"""

_CYCLE_MODEL_TURNS = """
SELECT mt.id, mt.turn_index, mt.request, mt.response,
       COALESCE(
           mt.response -> 'reasoning_content',
           mt.response -> 'reasoning',
           mt.response -> 'analysis',
           mt.response -> 'reasoning_details'
       ) AS reasoning,
       mt.provider_response_id, mt.termination_status, mt.started_at, mt.completed_at,
       mt.raw_artifact_uri, mt.raw_sha256, mt.retain_until,
       (mt.retention_purged_at IS NOT NULL) AS retention_purged
  FROM model_turns mt
 WHERE mt.agent_cycle_id = %s::uuid
 ORDER BY mt.turn_index ASC, mt.id ASC
 LIMIT 200
"""

_CYCLE_TOOL_CALLS = """
SELECT tc.id, mt.id AS model_turn_id, mt.turn_index, tc.call_index, tc.provider_call_id,
       tc.category, tc.tool_name, tc.display_name, tc.arguments, tc.output, tc.success,
       tc.validation_status, tc.error, tc.called_at, tc.completed_at, tc.retain_until,
       (tc.retention_purged_at IS NOT NULL) AS retention_purged
  FROM tool_calls tc
  JOIN model_turns mt ON mt.id = tc.model_turn_id
 WHERE mt.agent_cycle_id = %s::uuid
 ORDER BY mt.turn_index ASC, tc.call_index ASC, tc.id ASC
 LIMIT 500
"""

_CYCLE_RESEARCH = """
SELECT ra.id, tc.id AS tool_call_id, mt.turn_index, tc.call_index, ra.provider, ra.query,
       rd.canonical_url, rd.title, rd.source_published_at, rd.fetched_at,
       ra.source_cutoff, ra.artifact_uri, ra.raw_sha256, ra.created_at
  FROM research_artifacts ra
  JOIN tool_calls tc ON tc.id = ra.tool_call_id
  JOIN model_turns mt ON mt.id = tc.model_turn_id
  LEFT JOIN research_documents rd ON rd.id = ra.document_id
 WHERE mt.agent_cycle_id = %s::uuid
 ORDER BY mt.turn_index ASC, tc.call_index ASC, ra.created_at ASC, ra.id ASC
 LIMIT 500
"""

_CYCLE_PROVIDER_USAGE = """
SELECT pu.id, pu.model_turn_id, pu.tool_call_id, pu.provider, pu.route, pu.usage_kind,
       pu.prompt_tokens, pu.completion_tokens, pu.reasoning_tokens, pu.cached_tokens,
       pu.request_count, pu.credit_count, pu.billed_cost_micros, pu.nominal_cost_micros,
       pu.estimated_cost_micros, pu.latency_ms, pu.cache_hit, pu.raw_sha256,
       pu.raw_artifact_uri, pu.retain_until, pu.created_at,
       (pu.retention_purged_at IS NOT NULL) AS retention_purged
  FROM provider_usage pu
 WHERE pu.agent_cycle_id = %s::uuid
 ORDER BY pu.created_at ASC, pu.id ASC
 LIMIT 500
"""

_CYCLE_BELIEFS = """
SELECT br.id AS revision_id, b.id AS belief_id, br.revision, br.content, br.category,
       br.confidence, br.evidence, br.created_at, b.active
  FROM belief_revisions br
  JOIN beliefs b ON b.id = br.belief_id
 WHERE br.created_by_cycle_id = %s::uuid
 ORDER BY br.created_at ASC, br.id ASC
 LIMIT 200
"""

_CYCLE_PLANS = """
SELECT pr.id AS revision_id, p.id AS plan_id, p.plan_type, p.status, p.due_at,
       pr.revision, pr.content, pr.created_at
  FROM plan_revisions pr
  JOIN plans p ON p.id = pr.plan_id
 WHERE pr.created_by_cycle_id = %s::uuid
 ORDER BY pr.created_at ASC, pr.id ASC
 LIMIT 100
"""

_CYCLE_ORDERS = """
SELECT oo.id AS operation_id, oo.created_at, oo.outcome_side, oo.order_side,
       oo.amount_kind, oo.cash_amount_micros, oo.contract_units,
       oo.limit_price_micros, oo.time_in_force, oo.frozen_cutoff,
       oo.execution_cutoff, m.market_ref, m.question AS market_question,
       current_state.state AS lifecycle_state,
       current_state.reconciliation_state, lifecycle.reason AS lifecycle_reason,
       f.id AS fill_id, f.fill_id AS venue_fill_ref, f.contract_units AS filled_contract_units,
       f.price_micros AS fill_price_micros, f.gross_cash_micros,
       f.authoritative_fee_micros, f.net_cash_delta_micros, f.filled_at,
       f.frozen_context_id, f.execution_context_id,
       execution_book.raw_artifact_id AS execution_artifact_id,
       execution_artifact.sha256 AS execution_artifact_sha256,
       execution_artifact.uri AS execution_artifact_uri,
       execution_artifact.observed_at AS execution_artifact_observed_at
  FROM order_operations oo
  JOIN markets m ON m.id = oo.market_id
  LEFT JOIN order_operation_current current_state
    ON current_state.operation_id = oo.id
  LEFT JOIN LATERAL (
      SELECT reason
        FROM order_lifecycle_events event
       WHERE event.operation_id = oo.id
       ORDER BY event.sequence_number DESC, event.id DESC
       LIMIT 1
  ) lifecycle ON true
  LEFT JOIN fills f ON f.operation_id = oo.id
  LEFT JOIN order_book_snapshots execution_book
    ON execution_book.id = f.execution_context_id
  LEFT JOIN raw_artifacts execution_artifact
    ON execution_artifact.id = execution_book.raw_artifact_id
 WHERE oo.agent_cycle_id = %s::uuid
 ORDER BY oo.created_at ASC, oo.id ASC, f.filled_at ASC NULLS LAST, f.id ASC
 LIMIT 500
"""

_CYCLE_RUNTIME_STEPS = """
SELECT id, stage, status, input_fingerprint, output, attempt_count, started_at, completed_at, error
  FROM runtime_cycle_steps
 WHERE agent_cycle_id = %s::uuid
 ORDER BY started_at ASC, id ASC
 LIMIT 20
"""
