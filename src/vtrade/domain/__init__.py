"""Canonical market and execution domain contracts."""

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from vtrade.fee_policy import (
        FeeChange,
        FeeEvidence,
        FeeEvidenceRole,
        FeePolicyReason,
        FeePolicyResolution,
        FeePolicySourceConflictError,
        FeePolicyStatus,
        FeeSchedule,
    )


_FEE_POLICY_EXPORTS = frozenset(
    {
        "FeeChange",
        "FeeEvidence",
        "FeeEvidenceRole",
        "FeePolicyReason",
        "FeePolicyResolution",
        "FeePolicySourceConflictError",
        "FeePolicyStatus",
        "FeeSchedule",
    }
)


def __getattr__(name: str) -> Any:
    if name in _FEE_POLICY_EXPORTS:
        from vtrade import fee_policy

        return getattr(fee_policy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BinaryOrderRequest",
    "BinaryOrderResult",
    "EconomicFill",
    "FeeCalculation",
    "FeeChange",
    "FeeEvidence",
    "FeeEvidenceRole",
    "FeePolicyReason",
    "FeePolicyResolution",
    "FeePolicySnapshot",
    "FeePolicySourceConflictError",
    "FeePolicyStatus",
    "FeeSchedule",
    "OrderAmountType",
    "OrderRequest",
    "OrderResult",
    "OrderState",
    "ReconciliationState",
    "SettlementRecord",
    "TimeInForce",
]
