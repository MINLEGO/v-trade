"""Shared dashboard freshness and position-valuation policy definitions."""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_FRESHNESS_MAX_AGE_SECONDS = 300
DEFAULT_POSITION_VALUATION_MAX_AGE_SECONDS = 300

# The dashboard's operational freshness threshold belongs to the active run's
# immutable experiment definition.  Runs in ``paused`` state remain active for
# read-only operations.  The ordering makes the result deterministic if an
# operator has temporarily left more than one run active.
ACTIVE_EXPERIMENT_DEFINITION_SQL = """
SELECT ed.definition
  FROM experiment_definitions ed
  JOIN experiment_runs er ON er.definition_id = ed.id
 WHERE er.status IN ('running', 'paused')
 ORDER BY er.starts_at DESC, er.created_at DESC, er.id DESC
 LIMIT 1
"""

FRESHNESS_MAX_AGE_SQL = f"""
COALESCE(
    NULLIF(
        active_definition.definition #>> '{{execution,maximum_order_book_age_seconds}}', ''
    )::integer,
    NULLIF(
        active_definition.definition #>> '{{limits,maximum_archived_bid_age_seconds}}', ''
    )::integer,
    {DEFAULT_FRESHNESS_MAX_AGE_SECONDS}
)
"""

# ``ed`` is the experiment_definitions alias used by both the canonical
# dashboard read model and the compatibility operator positions view. The
# persisted experiment definition remains the source of truth, with the
# historical 300-second policy as a safe fallback for pre-policy rows.
POSITION_VALUATION_MAX_AGE_SQL = f"""
COALESCE(
    NULLIF(
        ed.definition #>> '{{owner_decisions,no_bid_valuation,maximum_age_seconds}}', ''
    )::integer,
    NULLIF(
        ed.definition #>> '{{limits,maximum_archived_bid_age_seconds}}', ''
    )::integer,
    {DEFAULT_POSITION_VALUATION_MAX_AGE_SECONDS}
)
"""


def position_valuation_max_age_seconds(definition: Mapping[str, object]) -> int:
    """Return the persisted experiment's last-known-bid valuation age."""

    owner_decisions = definition.get("owner_decisions")
    if isinstance(owner_decisions, Mapping):
        no_bid_valuation = owner_decisions.get("no_bid_valuation")
        if isinstance(no_bid_valuation, Mapping):
            value = no_bid_valuation.get("maximum_age_seconds")
            if isinstance(value, int) and value > 0:
                return value

    limits = definition.get("limits")
    if isinstance(limits, Mapping):
        value = limits.get("maximum_archived_bid_age_seconds")
        if isinstance(value, int) and value > 0:
            return value
    return DEFAULT_POSITION_VALUATION_MAX_AGE_SECONDS


def freshness_max_age_seconds(definition: Mapping[str, object]) -> int:
    """Return the active experiment's current order-book freshness age."""

    execution = definition.get("execution")
    if isinstance(execution, Mapping):
        value = execution.get("maximum_order_book_age_seconds")
        if isinstance(value, int) and value > 0:
            return value

    limits = definition.get("limits")
    if isinstance(limits, Mapping):
        value = limits.get("maximum_archived_bid_age_seconds")
        if isinstance(value, int) and value > 0:
            return value
    return DEFAULT_FRESHNESS_MAX_AGE_SECONDS
