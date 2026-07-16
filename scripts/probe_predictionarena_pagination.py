#!/usr/bin/env python3
"""Probe pagination/default limits while retaining only identities and counts."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.predictionarena.ai/api/polymarket"
URLS = {
    "cycles-limit-1": f"{BASE}/cycles?offset=0&limit=1",
    "cycles-offset-1": f"{BASE}/cycles?offset=1&limit=1",
    "cycles-offset-50": f"{BASE}/cycles?offset=50&limit=1",
    "cycles-limit-100": f"{BASE}/cycles?offset=0&limit=100",
    "actions-limit-1": f"{BASE}/actions?offset=0&limit=1",
    "actions-offset-1": f"{BASE}/actions?offset=1&limit=1",
    "actions-limit-100": f"{BASE}/actions?offset=0&limit=100",
    "agent-actions-limit-1": f"{BASE}/agents/trading-claude-opus-4-6/actions?limit=1",
    "agent-actions-offset-limit": f"{BASE}/agents/trading-claude-opus-4-6/actions?offset=0&limit=1",
    "settlements-limit-1": f"{BASE}/agents/trading-claude-opus-4-6/settlements?limit=1",
}


def summarize(value):
    if isinstance(value, dict):
        result = {"type": "object", "keys": sorted(value)}
        for key in ("count", "hasMore", "total", "offset", "limit"):
            if key in value:
                result[key] = value[key]
        collection_keys = [key for key, child in value.items() if isinstance(child, list)]
        result["collections"] = {key: summarize(value[key]) for key in collection_keys}
        return result
    if isinstance(value, list):
        result = {"type": "array", "length": len(value)}
        if value:
            first = value[0]
            last = value[-1]
            result["item_keys"] = sorted(first) if isinstance(first, dict) else None
            fields = ("id", "cycle_id", "timestamp", "created_at", "updated_at", "model_id", "agent_id", "status")
            result["first_identity"] = {field: first.get(field) for field in fields if isinstance(first, dict) and field in first}
            result["last_identity"] = {field: last.get(field) for field in fields if isinstance(last, dict) and field in last}
        return result
    return {"type": type(value).__name__, "value": value}


def main():
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "probes": []}
    for name, url in URLS.items():
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "V-Trade pagination inspector/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8-sig"))
                report["probes"].append({"name": name, "url": url, "status": response.status, "summary": summarize(data)})
        except Exception as exc:
            report["probes"].append({"name": name, "url": url, "error": f"{type(exc).__name__}: {exc}"})
    Path("docs/predictionarena-pagination-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
