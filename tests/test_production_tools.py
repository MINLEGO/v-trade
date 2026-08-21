from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from vtrade.domain.execution import EconomicFill, OrderRequest, OrderResult
from vtrade.production_tools import ProductionToolRegistry, _execution_output


def test_registry_has_exact_schema_parity_for_all_27_names() -> None:
    context = SimpleNamespace(
        maximum_default_result_tokens=4_000,
        portfolio=lambda _arguments: {"items": [], "next_cursor": None, "has_more": False},
    )
    registry = ProductionToolRegistry(context)  # type: ignore[arg-type]
    names = {spec.name for spec in registry.tool_specs()}
    expected = {
        item["name"]
        for item in json.loads(Path("spec/tool-schemas-vtrade-kalshi-v1.json").read_text())["tools"]
    }
    assert names == expected
    assert len(names) == 27


def test_order_output_uses_contract_units_prices_fees_and_reconciliation() -> None:
    now = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    request = OrderRequest(
        agent_id="agent-1",
        market_ref="KXEXAMPLE-1",
        outcome="YES",
        action="BUY",
        amount=100,
        amount_type="CONTRACTS",
        idempotency_key="cycle-1-order-1",
        limit_price=400_000,
        time_in_force="IOC",
        frozen_context_id="cycle-1",
        frozen_cutoff=now,
        created_at=now,
    )
    fill = EconomicFill(
        fill_id="fill-1",
        contract_units=100,
        price_micros=400_000,
        gross_cash_micros=400_000,
        fee_micros=1_000,
        net_cash_delta_micros=-401_000,
        filled_at=now,
    )
    result = OrderResult(
        request=request,
        operation_id="operation-1",
        state="FILLED",
        reconciliation_state="NOT_REQUIRED",
        requested_units=100,
        filled_units=100,
        remaining_units=0,
        cancelled_units=0,
        fills=(fill,),
        gross_cash_delta_micros=-400_000,
        fee_micros=1_000,
        net_cash_delta_micros=-401_000,
        frozen_context_id="cycle-1",
        execution_context_id="execution-1",
        submitted_at=now,
        updated_at=now,
    )
    output = _execution_output(result)
    assert output["status"] == "FILLED"
    assert output["reconciliation_state"] == "NOT_REQUIRED"
    assert output["request"]["market_ref"] == "KXEXAMPLE-1"
    assert output["request"]["outcome"] == "YES"
    assert output["requested_contract_units"] == 100
    assert output["fills"][0]["price_micros"] == 400_000
    assert output["fee_micros"] == 1_000
