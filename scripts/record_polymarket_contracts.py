"""Record bounded public Polymarket responses for offline contract replay."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

ROOT = Path("spec/fixtures/polymarket")


def array_field(value: Any, field: str) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise RuntimeError(f"live {field} is not an array")
    return value


def fetch(client: httpx.Client, name: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = client.get(url, params=params)
    response.raise_for_status()
    if len(response.content) > 25_000_000:
        raise RuntimeError(f"live {name} response exceeds 25 MB contract bound")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"live {name} response is not an object")
    ROOT.mkdir(parents=True, exist_ok=True)
    destination = ROOT / f"{name}.json"
    destination.write_bytes(response.content)
    return {
        "name": name,
        "url": str(response.request.url),
        "status_code": response.status_code,
        "byte_length": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "payload": payload,
    }


def find_exact_resolved_id(client: httpx.Client) -> str:
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {
            "limit": 100,
            "closed": "true",
            "uma_resolution_status": "resolved",
        }
        if cursor is not None:
            params["after_cursor"] = cursor
        response = client.get(
            "https://gamma-api.polymarket.com/markets/keyset", params=params
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("resolved market scan lacks markets array")
        for row in rows:
            if not isinstance(row, dict) or row.get("umaResolutionStatus") != "resolved":
                continue
            prices = array_field(row.get("outcomePrices"), "outcomePrices")
            decimals = [Decimal(str(value)) for value in prices]
            if decimals.count(Decimal(1)) == 1 and all(
                value in (Decimal(0), Decimal(1)) for value in decimals
            ):
                market_id = row.get("id")
                if isinstance(market_id, str) and market_id:
                    return market_id
        cursor = payload.get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            break
    raise RuntimeError("no exact 1/0 resolved market found in five bounded pages")


def main() -> None:
    captured_at = datetime.now(UTC).isoformat()
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        events = fetch(
            client,
            "events-keyset-limit-1",
            "https://gamma-api.polymarket.com/events/keyset",
            {"limit": 1, "closed": "false"},
        )
        markets = fetch(
            client,
            "markets-keyset-limit-1",
            "https://gamma-api.polymarket.com/markets/keyset",
            {"limit": 1, "closed": "false", "include_tag": "true"},
        )
        rows = markets["payload"].get("markets")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise RuntimeError("live markets fixture has no market")
        tokens = array_field(rows[0].get("clobTokenIds"), "clobTokenIds")
        if not tokens or not isinstance(tokens[0], str):
            raise RuntimeError("live market has no CLOB token")
        book = fetch(
            client,
            "clob-book",
            "https://clob.polymarket.com/book",
            {"token_id": tokens[0]},
        )
        resolved_id = find_exact_resolved_id(client)
        resolved = fetch(
            client,
            "resolved-markets-keyset-limit-1",
            "https://gamma-api.polymarket.com/markets/keyset",
            {
                "limit": 1,
                "closed": "true",
                "uma_resolution_status": "resolved",
                "id": resolved_id,
            },
        )
    manifest = {
        "captured_at": captured_at,
        "contracts": [
            {key: value for key, value in record.items() if key != "payload"}
            for record in (events, markets, book, resolved)
        ],
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "captured_at": captured_at,
                "contracts": len(manifest["contracts"]),
                "total_bytes": sum(
                    item["byte_length"] for item in manifest["contracts"]  # type: ignore[index]
                ),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
