"""Shared dashboard freshness and position-valuation policy definitions."""

from __future__ import annotations

from collections.abc import Mapping

FRESHNESS_MAX_AGE_SECONDS = 300
DEFAULT_POSITION_VALUATION_MAX_AGE_SECONDS = 300

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
