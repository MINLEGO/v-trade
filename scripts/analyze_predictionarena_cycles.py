#!/usr/bin/env python3
"""Extract compact cycle/prompt/tool-call facts from the public cycles endpoint."""

from __future__ import annotations

import json
import re
import statistics
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

URL = "https://www.predictionarena.ai/api/polymarket/cycles?offset=0&limit=50"


def fetch() -> dict:
    request = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": "V-Trade cycle inspector/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def compact_value(value):
    if isinstance(value, dict):
        return {"keys": sorted(value), "key_count": len(value)}
    if isinstance(value, list):
        return {"type": "array", "length": len(value)}
    if isinstance(value, str):
        return {"type": "string", "length": len(value), "excerpt": value[:180].replace("\n", " ")}
    return value


def main() -> None:
    payload = fetch()
    cycles = payload.get("cycles", [])
    prompts = [c.get("prompt") for c in cycles if isinstance(c.get("prompt"), str)]
    tool_calls = [call for c in cycles for call in (c.get("tool_calls") or [])]

    tools = Counter(call.get("tool_name") for call in tool_calls)
    categories = Counter(call.get("category") for call in tool_calls)
    display_names = Counter(call.get("display_name") for call in tool_calls)
    successes = Counter(call.get("success") for call in tool_calls)
    argument_shapes = defaultdict(Counter)
    argument_examples = defaultdict(dict)
    output_examples = {}
    for call in tool_calls:
        name = call.get("tool_name") or "<missing>"
        args = call.get("arguments")
        if isinstance(args, dict):
            argument_shapes[name]["keys=" + ",".join(sorted(args))] += 1
            for key, value in args.items():
                if key not in argument_examples[name]:
                    if isinstance(value, str):
                        argument_examples[name][key] = value[:120]
                    elif isinstance(value, (int, float, bool)) or value is None:
                        argument_examples[name][key] = value
                    else:
                        argument_examples[name][key] = {"type": type(value).__name__}
        output = call.get("output")
        if isinstance(output, str) and output:
            output_examples.setdefault(name, output[:240].replace("\n", " "))

    prompt_markers = Counter()
    prompt_lines = Counter()
    prompt_keyword_presence = Counter()
    marker_pattern = re.compile(r"^[A-Z][A-Z0-9 _/&'().,:?%-]{3,100}$")
    for prompt in prompts:
        for line in prompt.splitlines():
            clean = line.strip()
            if clean and len(clean) <= 240:
                prompt_lines[clean] += 1
            if marker_pattern.match(clean):
                prompt_markers[clean] += 1
        lowered = prompt.lower()
        for keyword in ("current date", "account value", "positions", "research", "discovery", "memory", "belief", "plan", "critical learning", "expected value", "risk", "buy", "sell", "tool"):
            if keyword in lowered:
                prompt_keyword_presence[keyword] += 1

    lengths = [len(prompt) for prompt in prompts]
    reasoning_lengths = [len(c["reasoning"]) for c in cycles if isinstance(c.get("reasoning"), str)]
    research_values = [c.get("research_data") for c in cycles if c.get("research_data") is not None]
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "cycle_count": len(cycles),
        "cycle_statuses": Counter(c.get("status") for c in cycles),
        "models": Counter(c.get("model_id") for c in cycles),
        "prompt": {
            "count": len(prompts),
            "length_chars": {"min": min(lengths) if lengths else None, "median": statistics.median(lengths) if lengths else None, "max": max(lengths) if lengths else None},
            "keyword_presence_count": dict(prompt_keyword_presence),
            "marker_lines": [{"text": text, "count": count} for text, count in prompt_markers.most_common(80)],
            "common_lines": [{"text": text, "count": count} for text, count in prompt_lines.most_common() if count >= max(3, len(prompts) - 5)][:120],
        },
        "reasoning_length_chars": {"min": min(reasoning_lengths) if reasoning_lengths else None, "median": statistics.median(reasoning_lengths) if reasoning_lengths else None, "max": max(reasoning_lengths) if reasoning_lengths else None},
        "tool_calls": {
            "total": len(tool_calls),
            "success_values": dict(successes),
            "categories": dict(categories),
            "names": dict(tools),
            "display_names": dict(display_names),
            "argument_shapes": {name: dict(counter) for name, counter in argument_shapes.items()},
            "argument_examples": dict(argument_examples),
            "output_examples": output_examples,
        },
        "cycle_field_presence": {field: sum(field in c for c in cycles) for field in sorted({field for c in cycles for field in c})},
        "field_types": {field: sorted({type(c.get(field)).__name__ for c in cycles}) for field in sorted({field for c in cycles for field in c})},
        "research_data_non_null": sum(c.get("research_data") is not None for c in cycles),
        "research_data_shapes": [{"type": type(value).__name__, "keys": sorted(value) if isinstance(value, dict) else None, "length": len(value) if isinstance(value, (dict, list, str)) else None} for value in research_values[:10]],
        "settlement_counts": Counter(len(c.get("settlements") or []) for c in cycles),
    }
    Path("docs/predictionarena-cycle-analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
