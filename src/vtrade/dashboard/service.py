"""Small deterministic diagnostics for a recorded agent cycle."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence

type DashboardValue = object
type DashboardRow = Mapping[str, DashboardValue]


def build_cycle_diagnostics(detail: Mapping[str, DashboardValue]) -> list[dict[str, object]]:
    """Return reproducible, evidence-linked diagnostics without model inference.

    The function deliberately makes no judgement about belief confidence: beliefs are
    agent memory and are not trade probabilities.  Trade-probability review belongs to
    ``order_intents`` where the recorded ``estimated_probability`` is available.
    """

    diagnostics: list[dict[str, object]] = []
    metadata = _mapping(detail.get("metadata"))
    model_turns = _rows(detail.get("model_turns"))
    tool_calls = _rows(detail.get("tool_calls"))
    research = _rows(detail.get("research"))
    order_intents = _rows(detail.get("order_intents"))

    failed_calls = [row for row in tool_calls if row.get("success") is False]
    if failed_calls:
        diagnostics.append(
            _diagnostic(
                "failed_tools",
                "warning",
                f"{len(failed_calls)} tool call(s) failed.",
                [str(row.get("id")) for row in failed_calls],
            )
        )

    repeated = _repeated_tool_calls(tool_calls)
    if repeated:
        diagnostics.append(
            _diagnostic(
                "repeated_tool_calls",
                "warning",
                "Identical tool calls were repeated in this cycle.",
                [signature for signature, count in repeated.items() if count > 1],
            )
        )

    if len(tool_calls) > 20:
        diagnostics.append(
            _diagnostic(
                "high_tool_count",
                "info",
                f"The cycle made {len(tool_calls)} tool calls (threshold: 20).",
                [],
            )
        )
    if len(research) > 8:
        diagnostics.append(
            _diagnostic(
                "high_search_count",
                "info",
                f"The cycle produced {len(research)} research artifact(s) (threshold: 8).",
                [],
            )
        )
    if not order_intents:
        diagnostics.append(
            _diagnostic(
                "no_action",
                "info",
                "No order intent was recorded for this cycle.",
                [],
            )
        )

    termination = str(metadata.get("model_termination_status") or "").lower()
    harness_termination = str(metadata.get("harness_termination_status") or "").lower()
    status = str(metadata.get("status") or "").lower()
    failed_termination = {"failed", "error", "interrupted", "timeout", "cancelled"}
    if (
        status in failed_termination
        or termination in failed_termination
        or harness_termination in failed_termination
    ):
        diagnostics.append(
            _diagnostic(
                "termination_failure",
                "error",
                "The cycle or model harness ended unsuccessfully.",
                [],
            )
        )
    if not model_turns and status == "completed":
        diagnostics.append(
            _diagnostic(
                "missing_model_turns",
                "warning",
                "A completed cycle has no retained model turns.",
                [],
            )
        )
    return diagnostics


def _diagnostic(
    code: str, severity: str, message: str, evidence_ids: Sequence[str]
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence_ids": list(evidence_ids),
    }


def _repeated_tool_calls(tool_calls: Sequence[DashboardRow]) -> Counter[str]:
    signatures: Counter[str] = Counter()
    for call in tool_calls:
        arguments = call.get("arguments")
        encoded = json.dumps(arguments, sort_keys=True, default=str, separators=(",", ":"))
        signatures[f"{call.get('tool_name')}:{encoded}"] += 1
    return Counter({signature: count for signature, count in signatures.items() if count > 1})


def _mapping(value: object) -> DashboardRow:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[DashboardRow]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]
