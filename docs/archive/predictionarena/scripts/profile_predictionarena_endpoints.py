#!/usr/bin/env python3
"""Create a compact semantic profile of endpoint payloads; no raw payloads are saved."""

from __future__ import annotations

import json
import statistics
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.predictionarena.ai/api/polymarket"


def fetch(path: str):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json", "User-Agent": "V-Trade endpoint profiler/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def values(rows, key):
    return [row[key] for row in rows if isinstance(row, dict) and row.get(key) is not None]


def range_for(rows, key):
    data = values(rows, key)
    if not data:
        return None
    try:
        return {"min": min(data), "max": max(data), "median": statistics.median(data)}
    except TypeError:
        return {"min": min(map(str, data)), "max": max(map(str, data))}


def list_profile(rows, fields):
    return [{field: row.get(field) for field in fields if field in row} for row in rows if isinstance(row, dict)]


def main():
    agents = fetch("/agents")
    history = fetch("/account-value-history")["data"]
    sp500 = fetch("/external-markets-history").get("SP500", [])
    cycles = fetch("/cycles?offset=0&limit=50")["cycles"]
    actions = fetch("/actions?offset=0&limit=50")
    markets = fetch("/markets")
    agent_actions = fetch("/agents/trading-claude-opus-4-6/actions")
    positions = fetch("/agents/trading-claude-opus-4-6/positions-with-prices")
    detail = fetch("/agents/by-model/claude-opus-4-6")
    settlements = fetch("/agents/trading-claude-opus-4-6/settlements")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_notes": {
            "agents": {"count": agents.get("count"), "rows": list_profile(agents.get("data", []), ["id", "agent_id", "model_id", "status", "account_value", "latest_account_value", "cash_balance", "latest_cash_balance", "total_pnl", "return_percentage", "total_cycles", "number_of_trades"])},
            "account_value_history": {"count": len(history), "date_range": range_for(history, "date"), "models": dict(Counter(row.get("model_id") for row in history)), "statuses": dict(Counter(row.get("status") for row in history)), "value_range": range_for(history, "value")},
            "external_markets_history": {"series": list({key for row in sp500 for key in row}), "count": len(sp500), "date_range": range_for(sp500, "date"), "first": sp500[0] if sp500 else None, "last": sp500[-1] if sp500 else None},
            "cycles": {"count": len(cycles), "date_range": {"created_at": range_for(cycles, "created_at"), "completed_at": range_for(cycles, "completed_at")}, "models": dict(Counter(row.get("model_id") for row in cycles)), "statuses": dict(Counter(row.get("status") for row in cycles)), "tool_calls_per_cycle": {"min": min(len(row.get("tool_calls") or []) for row in cycles), "median": statistics.median(len(row.get("tool_calls") or []) for row in cycles), "max": max(len(row.get("tool_calls") or []) for row in cycles)}},
            "actions": {"count": len(actions), "date_range": range_for(actions, "timestamp"), "models": dict(Counter(row.get("model_id") for row in actions)), "action_types": dict(Counter(row.get("action_type") for row in actions)), "sides": dict(Counter(row.get("side") for row in actions)), "statuses": dict(Counter(row.get("status") for row in actions)), "settlement_statuses": dict(Counter(row.get("settlement_status") for row in actions)), "price_range": range_for(actions, "price"), "amount_range": range_for(actions, "amount"), "total_cost_range": range_for(actions, "total_cost")},
            "markets": {"count": len(markets), "active": sum(bool(row.get("active")) for row in markets), "outcome_counts": dict(Counter(len(row.get("outcomes") or []) for row in markets)), "date_range": {"start_date": range_for(markets, "start_date"), "end_date": range_for(markets, "end_date")}},
            "agent_actions": {"count": len(agent_actions), "date_range": range_for(agent_actions, "timestamp"), "action_types": dict(Counter(row.get("action_type") for row in agent_actions)), "sides": dict(Counter(row.get("side") for row in agent_actions)), "settlement_statuses": dict(Counter(row.get("settlement_status") for row in agent_actions))},
            "positions": {"count": len(positions), "date_range": range_for(positions, "updated_at"), "sides": dict(Counter(row.get("side") for row in positions)), "quantity_range": range_for(positions, "quantity"), "position_value_range": range_for(positions, "position_value"), "unrealized_pnl_range": range_for(positions, "unrealized_pnl")},
            "by_model": {key: (len(value) if isinstance(value, list) else value) for key, value in detail.items() if key not in {"long_term_plan", "next_cycle_plan", "general_beliefs"}},
            "by_model_nested": {"general_beliefs_count": len(detail.get("general_beliefs") or []), "belief_categories": dict(Counter(row.get("category") for row in detail.get("general_beliefs") or [])), "long_term_plan_fields": sorted(detail.get("long_term_plan", {})), "next_cycle_plan_fields": sorted(detail.get("next_cycle_plan", {})), "long_term_plan_chars": len((detail.get("long_term_plan") or {}).get("plan_content", "")), "next_cycle_plan_chars": len((detail.get("next_cycle_plan") or {}).get("plan_content", ""))},
            "settlements": {"count": len(settlements), "date_range": {"created_at": range_for(settlements, "created_at"), "settled_at": range_for(settlements, "settled_at")}, "results": dict(Counter(row.get("result") for row in settlements)), "realized_pnl_range": range_for(settlements, "realized_pnl"), "payout_range": range_for(settlements, "payout")},
        },
    }
    Path("docs/predictionarena-endpoint-profile.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
