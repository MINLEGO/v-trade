#!/usr/bin/env python3
"""Find the maximum web_search calls in the 200 newest public cycles."""

from __future__ import annotations

import json
import gzip
import statistics
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

URL = "https://www.predictionarena.ai/api/polymarket/cycles?offset=0&limit=200"


def main() -> None:
    request = urllib.request.Request(
        URL,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "V-Trade web-search counter/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8-sig"))

    cycles = payload.get("cycles", [])
    rows = []
    for rank, cycle in enumerate(cycles, start=1):
        calls = cycle.get("tool_calls") or []
        web_searches = [call for call in calls if call.get("tool_name") == "web_search"]
        rows.append(
            {
                "rank": rank,
                "cycle_id": cycle.get("cycle_id") or cycle.get("id"),
                "model_id": cycle.get("model_id"),
                "agent_id": cycle.get("agent_id"),
                "status": cycle.get("status"),
                "created_at": cycle.get("created_at"),
                "web_search_count": len(web_searches),
                "total_tool_calls": len(calls),
            }
        )

    counts = [row["web_search_count"] for row in rows]
    maximum = max(counts, default=0)
    winners = [row for row in rows if row["web_search_count"] == maximum]
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "url": URL,
        "returned_cycles": len(cycles),
        "api_count": payload.get("count"),
        "has_more": payload.get("hasMore"),
        "web_search_count": {
            "maximum": maximum,
            "winning_cycles": winners,
            "mean": statistics.mean(counts) if counts else 0,
            "median": statistics.median(counts) if counts else 0,
            "distribution": dict(sorted(Counter(counts).items())),
            "cycles_with_at_least_one": sum(count > 0 for count in counts),
        },
    }
    Path("docs/predictionarena-web-search-200-cycle-analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
