"""Canonical market and execution domain contracts."""

from vtrade.domain.execution import (
    BinaryOrderRequest,
    BinaryOrderResult,
    EconomicFill,
    FeeCalculation,
    FeePolicySnapshot,
    OrderAmountType,
    OrderRequest,
    OrderResult,
    OrderState,
    ReconciliationState,
    SettlementRecord,
    TimeInForce,
)

__all__ = [
    "BinaryOrderRequest",
    "BinaryOrderResult",
    "EconomicFill",
    "FeeCalculation",
    "FeePolicySnapshot",
    "OrderAmountType",
    "OrderRequest",
    "OrderResult",
    "OrderState",
    "ReconciliationState",
    "SettlementRecord",
    "TimeInForce",
]
