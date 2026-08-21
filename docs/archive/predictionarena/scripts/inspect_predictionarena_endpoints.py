#!/usr/bin/env python3
"""Inspect PredictionArena Polymarket endpoints without printing large payloads.

The script intentionally keeps only structural information and short excerpts.
It can write a compact JSON report suitable for documentation; it never writes
the raw API responses.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE = "https://www.predictionarena.ai/api/polymarket"
ENDPOINTS = {
    "agents": f"{BASE}/agents",
    "account-value-history": f"{BASE}/account-value-history",
    "external-markets-history": f"{BASE}/external-markets-history",
    "cycles": f"{BASE}/cycles?offset=0&limit=50",
    "actions": f"{BASE}/actions?offset=0&limit=50",
    "markets": f"{BASE}/markets",
    "trading-gpt-5.4-actions": f"{BASE}/agents/trading-gpt-5.4/actions",
    "trading-claude-opus-4-6-actions": f"{BASE}/agents/trading-claude-opus-4-6/actions",
    "trading-claude-opus-4-6-positions": f"{BASE}/agents/trading-claude-opus-4-6/positions-with-prices",
    "trading-gpt-5.4-positions": f"{BASE}/agents/trading-gpt-5.4/positions-with-prices",
    "by-model-claude-opus-4-6": f"{BASE}/agents/by-model/claude-opus-4-6",
    "trading-claude-opus-4-6-settlements": f"{BASE}/agents/trading-claude-opus-4-6/settlements",
}

SENSITIVE_OR_LARGE_WORDS = (
    "prompt",
    "system",
    "message",
    "reasoning",
    "thought",
    "tool",
    "call",
    "memory",
    "content",
    "response",
    "output",
    "error",
)


def json_loads(raw: bytes) -> Any:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def scalar_summary(value: Any, path: str) -> dict[str, Any]:
    if value is None or isinstance(value, (bool, int, float)):
        return {"type": type(value).__name__, "example": value}
    text = str(value).replace("\r", " ").replace("\n", " ")
    result: dict[str, Any] = {"type": "string", "length": len(text)}
    if any(word in path.lower() for word in SENSITIVE_OR_LARGE_WORDS):
        result["excerpt"] = text[:240]
    elif len(text) <= 120:
        result["example"] = text
    else:
        result["example"] = text[:120] + "…"
    return result


def shape(value: Any, path: str = "$", depth: int = 0) -> dict[str, Any]:
    if depth > 4:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        keys = sorted(value)
        result: dict[str, Any] = {"type": "object", "keys": keys}
        children: dict[str, Any] = {}
        for key in keys:
            child_path = f"{path}.{key}"
            child = value[key]
            if isinstance(child, (dict, list)):
                children[key] = shape(child, child_path, depth + 1)
            else:
                children[key] = scalar_summary(child, child_path)
        result["fields"] = children
        return result
    if isinstance(value, list):
        result = {"type": "array", "length": len(value)}
        if value:
            result["item_types"] = sorted({type(item).__name__ for item in value})
            result["item_shape"] = shape(value[0], f"{path}[]", depth + 1)
        return result
    return scalar_summary(value, path)


def paths(value: Any, path: str = "$", out: Counter[str] | None = None) -> Counter[str]:
    out = out or Counter()
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            out[child_path] += 1
            paths(child, child_path, out)
    elif isinstance(value, list):
        out[f"{path}[]"] += len(value)
        for child in value[:3]:
            paths(child, f"{path}[]", out)
    return out


def request(url: str, timeout: int) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "V-Trade endpoint inspector/1.0",
        },
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def inspect(name: str, url: str, timeout: int) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    try:
        status, headers, raw = request(url, timeout)
        result: dict[str, Any] = {
            "name": name,
            "url": url,
            "checked_at": started,
            "http_status": status,
            "content_type": headers.get("Content-Type"),
            "compressed_bytes": len(raw),
        }
        if status >= 400:
            result["error_body_excerpt"] = raw[:500].decode("utf-8", errors="replace")
            return result
        data = json_loads(raw)
        result["decoded_json_bytes"] = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        result["top_level_type"] = type(data).__name__
        result["shape"] = shape(data)
        path_counter = paths(data)
        result["frequent_paths"] = [
            {"path": path, "observations": count}
            for path, count in path_counter.most_common(80)
        ]
        return result
    except Exception as exc:  # Keep inspecting other endpoints after one failure.
        return {
            "name": name,
            "url": url,
            "checked_at": started,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="write compact JSON report")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--only", action="append", choices=sorted(ENDPOINTS))
    args = parser.parse_args()

    selected = args.only or list(ENDPOINTS)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_count": len(selected),
        "endpoints": [inspect(name, ENDPOINTS[name], args.timeout) for name in selected],
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
