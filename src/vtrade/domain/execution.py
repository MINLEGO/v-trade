"""Venue-neutral execution contracts for the Kalshi paper release.

The market adapter owns venue identifiers and raw evidence.  This module owns only
the semantic order, fee, fill, lifecycle, and reconciliation vocabulary shared by
paper execution and the deliberately disabled future real adapter.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from vtrade.domain.types import (
    ContractQuantity,
    MarketKey,
    MoneyMicros,
    OutcomeSide,
    PriceMicros,
    RawArtifact,
    Side,
    to_contract_quantity,
    to_money_micros,
    to_price_micros,
    utc_now,
)

ORDER_CONTRACT_VERSION = "vtrade-binary-order-v1"
FEE_SETTLEMENT_CONTRACT_VERSION = "vtrade-binary-fee-settlement-v1"
FEE_FORMULA_VERSION = "kalshi-quadratic-v1"
LIQUIDITY_HAIRCUT_CONTRACT_VERSION = "best-level-haircut-v1"


class OrderAmountType(StrEnum):
    CASH = "CASH"
    CONTRACTS = "CONTRACTS"


class TimeInForce(StrEnum):
    IOC = "IOC"
    FOK = "FOK"


class OrderState(StrEnum):
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class ReconciliationState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"
    RESOLVED = "RESOLVED"
    CONFLICT = "CONFLICT"


class SubmissionState(StrEnum):
    """Internal evidence about whether an order reached a venue boundary."""

    NOT_SUBMITTED = "NOT_SUBMITTED"
    UNKNOWN = "UNKNOWN"
    SUBMITTED = "SUBMITTED"


class FeeParticipantRole(StrEnum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class SemanticExecutionError(StrEnum):
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CONTEXT = "INVALID_CONTEXT"
    MARKET_NOT_TRADEABLE = "MARKET_NOT_TRADEABLE"
    STALE_BOOK = "STALE_BOOK"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    HAIRCUT_EVIDENCE_INSUFFICIENT = "HAIRCUT_EVIDENCE_INSUFFICIENT"
    PRICE_LIMIT = "PRICE_LIMIT"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_CONTRACTS = "INSUFFICIENT_CONTRACTS"
    CONCENTRATION_LIMIT = "CONCENTRATION_LIMIT"
    MISSING_FEE_POLICY = "MISSING_FEE_POLICY"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    FINALIZATION_REQUIRED = "FINALIZATION_REQUIRED"


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _price(value: object, field_name: str = "limit_price") -> PriceMicros:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an exact price")
    if isinstance(value, int):
        if not 0 <= value <= 1_000_000:
            raise ValueError(f"{field_name} must be between zero and one dollar")
        return PriceMicros(value)
    return to_price_micros(value, field=field_name)  # type: ignore[arg-type]


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{field_name} must not be a float")
    try:
        result = Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an exact decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _rational(
    value: object, field_name: str, *, allow_zero: bool = False
) -> tuple[int, int]:
    decimal_value = _decimal(value, field_name)
    if decimal_value < 0 or (decimal_value == 0 and not allow_zero):
        raise ValueError(f"{field_name} must be positive")
    numerator, denominator = decimal_value.as_integer_ratio()
    return numerator, denominator


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """The only request shape visible above the execution boundary."""

    agent_id: str
    market_ref: MarketKey | str
    outcome: OutcomeSide | str
    action: Side | str
    amount: int | str | Decimal | ContractQuantity | MoneyMicros
    amount_type: OrderAmountType | str
    idempotency_key: str
    limit_price: PriceMicros | int | str | Decimal | None = None
    time_in_force: TimeInForce | str = TimeInForce.IOC
    frozen_context_id: str | None = None
    frozen_cutoff: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _nonempty(str(self.agent_id), "agent_id"))
        if isinstance(self.market_ref, str):
            object.__setattr__(self, "market_ref", MarketKey(self.market_ref))
        if not isinstance(self.market_ref, MarketKey):
            raise ValueError("market_ref must be a MarketKey")
        try:
            object.__setattr__(self, "outcome", OutcomeSide(self.outcome))
            object.__setattr__(self, "action", Side(self.action))
            object.__setattr__(self, "amount_type", OrderAmountType(self.amount_type))
            object.__setattr__(self, "time_in_force", TimeInForce(self.time_in_force))
        except ValueError as exc:
            raise ValueError("order request contains an unsupported enum value") from exc
        object.__setattr__(
            self,
            "idempotency_key",
            _nonempty(self.idempotency_key, "idempotency_key"),
        )
        if len(self.idempotency_key) > 512:
            raise ValueError("idempotency_key is too long")
        if self.amount_type is OrderAmountType.CONTRACTS:
            if isinstance(self.amount, int) and not isinstance(self.amount, bool):
                units = self.amount
            else:
                units = int(to_contract_quantity(self.amount, field="amount"))
            if units <= 0:
                raise ValueError("contract amount must be positive")
            object.__setattr__(self, "amount", ContractQuantity(units))
        else:
            if isinstance(self.amount, int) and not isinstance(self.amount, bool):
                micros = self.amount
            else:
                micros = int(to_money_micros(self.amount, field="amount"))
            if micros <= 0:
                raise ValueError("cash amount must be positive")
            object.__setattr__(self, "amount", MoneyMicros(micros))
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", _price(self.limit_price))
        if self.frozen_context_id is not None:
            object.__setattr__(
                self, "frozen_context_id", _nonempty(self.frozen_context_id, "frozen_context_id")
            )
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        if self.frozen_cutoff is not None:
            object.__setattr__(self, "frozen_cutoff", _aware(self.frozen_cutoff, "frozen_cutoff"))

    @property
    def market_key(self) -> MarketKey:
        return cast(MarketKey, self.market_ref)

    @property
    def amount_kind(self) -> OrderAmountType:
        return cast(OrderAmountType, self.amount_type)

    @property
    def side(self) -> Side:
        return Side(self.action)

    @property
    def contract_units(self) -> ContractQuantity | None:
        return (
            cast(ContractQuantity, self.amount)
            if self.amount_kind is OrderAmountType.CONTRACTS
            else None
        )

    @property
    def cash_amount_micros(self) -> MoneyMicros | None:
        return cast(MoneyMicros, self.amount) if self.amount_kind is OrderAmountType.CASH else None

    @property
    def limit_price_micros(self) -> PriceMicros | None:
        return cast(PriceMicros, self.limit_price) if self.limit_price is not None else None

    def canonical_payload(self) -> dict[str, object]:
        return {
            "contract_version": ORDER_CONTRACT_VERSION,
            "agent_id": self.agent_id,
            "market_ref": self.market_key.canonical,
            "outcome": OutcomeSide(self.outcome).value,
            "action": Side(self.action).value,
            "amount_type": self.amount_kind.value,
            "amount": int(self.amount),
            "limit_price_micros": (
                int(self.limit_price) if self.limit_price is not None else None
            ),
            "time_in_force": TimeInForce(self.time_in_force).value,
            "frozen_context_id": self.frozen_context_id,
            "frozen_cutoff": self.frozen_cutoff.isoformat() if self.frozen_cutoff else None,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class FeePolicySnapshot:
    """Immutable exact fee inputs selected for one execution as-of time."""

    contract_version: str = FEE_SETTLEMENT_CONTRACT_VERSION
    schedule_version: str = "kalshi-schedule-v1"
    formula_version: str = FEE_FORMULA_VERSION
    participant_role: FeeParticipantRole | str = FeeParticipantRole.TAKER
    role: FeeParticipantRole | str | None = None
    series_multiplier_numerator: int = 1
    series_multiplier_denominator: int = 1
    series_multiplier: Decimal | str | None = None
    event_override_numerator: int | None = None
    event_override_denominator: int | None = None
    event_override: Decimal | str | None = None
    event_override_cleared: bool = False
    rate_numerator: int | None = None
    rate_denominator: int | None = None
    waiver: bool = False
    waiver_evidence: Mapping[str, object] | None = None
    as_of: datetime | None = None
    as_of_at: datetime | None = None
    effective_from: datetime | None = None
    effective_at: datetime | None = None
    scheduled_ts: datetime | None = None
    source_observed_at: datetime | None = None
    observed_at: datetime | None = None
    cutoff: datetime | None = None
    source_tier: str = "official"
    raw_artifact: RawArtifact | None = None
    exact_inputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.contract_version != FEE_SETTLEMENT_CONTRACT_VERSION:
            raise ValueError("unsupported fee/settlement contract version")
        selected_role = self.role if self.role is not None else self.participant_role
        normalized_role = FeeParticipantRole(selected_role)
        object.__setattr__(self, "participant_role", normalized_role)
        object.__setattr__(self, "role", normalized_role)
        _nonempty(self.schedule_version, "schedule_version")
        if self.formula_version != FEE_FORMULA_VERSION:
            raise ValueError("unsupported fee formula version")
        for name, value in (
            ("series_multiplier_numerator", self.series_multiplier_numerator),
            ("series_multiplier_denominator", self.series_multiplier_denominator),
        ):
            if _integer(value, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.series_multiplier is not None:
            series_num, series_den = _rational(self.series_multiplier, "series_multiplier")
            object.__setattr__(self, "series_multiplier_numerator", series_num)
            object.__setattr__(self, "series_multiplier_denominator", series_den)
        if self.event_override is not None:
            override_num, override_den = _rational(
                self.event_override, "event_override", allow_zero=True
            )
            object.__setattr__(self, "event_override_numerator", override_num)
            object.__setattr__(self, "event_override_denominator", override_den)
        if self.event_override_cleared and (
            self.event_override_numerator is not None
            or self.event_override_denominator is not None
        ):
            raise ValueError("a cleared event override cannot carry multiplier values")
        if (self.event_override_numerator is None) != (
            self.event_override_denominator is None
        ):
            raise ValueError("event override numerator and denominator are both required")
        if (
            self.event_override_numerator is not None
            and (
                self.event_override_numerator < 0
                or (
                    self.event_override_denominator is not None
                    and self.event_override_denominator <= 0
                )
            )
        ):
            raise ValueError("event override multiplier must be positive")
        if self.as_of is None and self.as_of_at is not None:
            object.__setattr__(self, "as_of", self.as_of_at)
        if self.effective_from is None and self.effective_at is not None:
            object.__setattr__(self, "effective_from", self.effective_at)
        if self.source_observed_at is None and self.observed_at is not None:
            object.__setattr__(self, "source_observed_at", self.observed_at)
        for name in ("as_of", "effective_from", "scheduled_ts", "source_observed_at", "cutoff"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        if self.as_of is not None and self.cutoff is not None and self.as_of > self.cutoff:
            raise ValueError("fee policy as_of cannot be after cutoff")
        if (
            self.source_observed_at is not None
            and self.cutoff is not None
            and self.source_observed_at > self.cutoff
        ):
            raise ValueError("fee policy observation cannot be after cutoff")
        if not isinstance(self.exact_inputs, Mapping):
            raise ValueError("exact fee inputs must be an object")
        if self.waiver_evidence is not None and not isinstance(self.waiver_evidence, Mapping):
            raise ValueError("waiver evidence must be an object")
        if self.waiver_evidence is not None:
            object.__setattr__(
                self,
                "waiver_evidence",
                MappingProxyType(dict(self.waiver_evidence)),
            )
            if self.waiver_evidence.get("waived") is True:
                object.__setattr__(self, "waiver", True)
        object.__setattr__(self, "exact_inputs", MappingProxyType(dict(self.exact_inputs)))

    @property
    def resolved_multiplier(self) -> tuple[int, int]:
        if self.event_override_numerator is not None:
            return self.event_override_numerator, self.event_override_denominator or 1
        return self.series_multiplier_numerator, self.series_multiplier_denominator

    @property
    def rate(self) -> tuple[int, int]:
        if self.rate_numerator is not None or self.rate_denominator is not None:
            if self.rate_numerator is None or self.rate_denominator is None:
                raise ValueError("fee rate numerator and denominator are both required")
            if self.rate_numerator <= 0 or self.rate_denominator <= 0:
                raise ValueError("fee rate must be positive")
            return self.rate_numerator, self.rate_denominator
        if FeeParticipantRole(self.participant_role) is FeeParticipantRole.MAKER:
            return 175, 10_000
        return 7, 100

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "contract_version": self.contract_version,
                "schedule_version": self.schedule_version,
                "formula_version": self.formula_version,
                "participant_role": FeeParticipantRole(self.participant_role).value,
                "series_multiplier": [
                    self.series_multiplier_numerator,
                    self.series_multiplier_denominator,
                ],
                "event_override": [self.event_override_numerator, self.event_override_denominator],
                "event_override_cleared": self.event_override_cleared,
                "rate": [self.rate_numerator, self.rate_denominator],
                "waiver": self.waiver,
                "waiver_evidence": (
                    dict(self.waiver_evidence) if self.waiver_evidence is not None else None
                ),
                "as_of": self.as_of.isoformat() if self.as_of else None,
                "effective_from": self.effective_from.isoformat() if self.effective_from else None,
                "scheduled_ts": self.scheduled_ts.isoformat() if self.scheduled_ts else None,
                "source_observed_at": (
                    self.source_observed_at.isoformat() if self.source_observed_at else None
                ),
                "cutoff": self.cutoff.isoformat() if self.cutoff else None,
                "source_tier": self.source_tier,
                "raw_sha256": self.raw_artifact.sha256 if self.raw_artifact else None,
                "exact_inputs": dict(self.exact_inputs),
            }
        )


@dataclass(frozen=True, slots=True)
class FeeCalculation:
    gross_micros: MoneyMicros
    trade_fee_raw_nanos: int
    trade_fee_micros: MoneyMicros
    rounding_fee_micros: MoneyMicros
    rebate_micros: MoneyMicros
    net_fee_micros: MoneyMicros
    posted_balance_change_micros: int
    accumulator_before_micros: int
    accumulator_after_micros: int
    price_micros: PriceMicros
    contract_units: ContractQuantity
    participant_role: FeeParticipantRole
    policy_fingerprint: str

    @property
    def fee_micros(self) -> MoneyMicros:
        return self.net_fee_micros


@dataclass(frozen=True, slots=True)
class EconomicFill:
    fill_id: str
    contract_units: ContractQuantity
    price_micros: PriceMicros
    gross_cash_micros: MoneyMicros
    fee_micros: MoneyMicros
    net_cash_delta_micros: int
    filled_at: datetime
    frozen_context_id: str | None = None
    execution_context_id: str | None = None
    authoritative: bool = True
    estimated_fee_micros: MoneyMicros | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.fill_id, "fill_id")
        if self.contract_units <= 0:
            raise ValueError("fill contract units must be positive")
        if not 0 <= self.price_micros <= 1_000_000:
            raise ValueError("fill price is outside the binary range")
        if self.gross_cash_micros < 0 or self.fee_micros < 0:
            raise ValueError("fill money values cannot be negative")
        if self.estimated_fee_micros is not None and self.estimated_fee_micros < 0:
            raise ValueError("estimated fill fee cannot be negative")
        object.__setattr__(self, "filled_at", _aware(self.filled_at, "filled_at"))
        if self.fingerprint is None:
            object.__setattr__(
                self,
                "fingerprint",
                _fingerprint(
                    {
                        "fill_id": self.fill_id,
                        "contract_units": int(self.contract_units),
                        "price_micros": int(self.price_micros),
                        "gross_cash_micros": int(self.gross_cash_micros),
                        "fee_micros": int(self.fee_micros),
                        "estimated_fee_micros": (
                            int(self.estimated_fee_micros)
                            if self.estimated_fee_micros is not None
                            else None
                        ),
                        "net_cash_delta_micros": self.net_cash_delta_micros,
                        "filled_at": self.filled_at.isoformat(),
                    }
                ),
            )

    @property
    def quantity_units(self) -> ContractQuantity:
        return self.contract_units

    @property
    def gross_micros(self) -> MoneyMicros:
        return self.gross_cash_micros

    @property
    def authoritative_fee_micros(self) -> MoneyMicros:
        return self.fee_micros


@dataclass(frozen=True, slots=True)
class OrderResult:
    request: OrderRequest
    operation_id: str
    state: OrderState | str
    reconciliation_state: ReconciliationState | str
    requested_units: ContractQuantity
    filled_units: ContractQuantity
    remaining_units: ContractQuantity
    cancelled_units: ContractQuantity
    fills: tuple[EconomicFill, ...] = ()
    gross_cash_delta_micros: int = 0
    fee_micros: MoneyMicros = field(default=MoneyMicros(0))
    net_cash_delta_micros: int = 0
    frozen_context_id: str | None = None
    execution_context_id: str | None = None
    submitted_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    error_code: SemanticExecutionError | str | None = None
    message: str | None = None
    submission_state: SubmissionState | str | None = None
    reconciliation_evidence: Mapping[str, object] = field(default_factory=dict)
    portfolio_before: Any = field(default=None, compare=False, repr=False)
    portfolio_after: Any = field(default=None, compare=False, repr=False)
    ledger_entries: tuple[Any, ...] = field(default=(), compare=False, repr=False)
    liquidity_audit: Any = field(default=None, compare=False, repr=False)
    fee_calculations: tuple[FeeCalculation, ...] = field(default=(), compare=False, repr=False)
    risk_check: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", OrderState(self.state))
        object.__setattr__(
            self,
            "reconciliation_state",
            ReconciliationState(self.reconciliation_state),
        )
        if self.submission_state is not None:
            object.__setattr__(self, "submission_state", SubmissionState(self.submission_state))
        if not isinstance(self.reconciliation_evidence, Mapping):
            raise ValueError("reconciliation evidence must be an object")
        object.__setattr__(
            self,
            "reconciliation_evidence",
            MappingProxyType(dict(self.reconciliation_evidence)),
        )
        _nonempty(self.operation_id, "operation_id")
        for name in ("requested_units", "filled_units", "remaining_units", "cancelled_units"):
            value = _integer(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.request.amount_type is OrderAmountType.CONTRACTS:
            if self.requested_units != int(self.request.amount):
                raise ValueError("requested units do not match the contract request")
            if (
                self.filled_units + self.remaining_units + self.cancelled_units
                != self.requested_units
            ):
                raise ValueError("order quantity accounting is not conserved")
        if self.filled_units != sum(int(fill.contract_units) for fill in self.fills):
            raise ValueError("filled units do not match economic fills")
        if self.fee_micros < 0:
            raise ValueError("aggregate fee cannot be negative")
        if self.net_cash_delta_micros != self.gross_cash_delta_micros - int(self.fee_micros):
            raise ValueError("net cash delta must equal gross delta less fee")
        if (
            self.state is OrderState.FILLED
            and self.request.amount_type is OrderAmountType.CONTRACTS
            and self.filled_units != self.requested_units
        ):
            raise ValueError("filled order must satisfy the complete contract request")
        if self.state is OrderState.PARTIALLY_FILLED and self.filled_units <= 0:
            raise ValueError("partially filled order must contain a fill")
        if self.state is OrderState.REJECTED and self.fills:
            raise ValueError("rejected order cannot contain fills")
        object.__setattr__(self, "submitted_at", _aware(self.submitted_at, "submitted_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))
        if self.updated_at < self.submitted_at:
            raise ValueError("order update cannot predate submission")

    @property
    def contract_version(self) -> str:
        return ORDER_CONTRACT_VERSION

    @property
    def status(self) -> OrderState:
        return cast(OrderState, self.state)

    @property
    def lifecycle_state(self) -> OrderState:
        return cast(OrderState, self.state)

    @property
    def request_fingerprint(self) -> str:
        return self.request.fingerprint

    @property
    def requested_quantity_units(self) -> ContractQuantity:
        return self.requested_units

    @property
    def filled_quantity_units(self) -> ContractQuantity:
        return self.filled_units

    @property
    def remaining_quantity_units(self) -> ContractQuantity:
        return self.remaining_units

    @property
    def cancelled_quantity_units(self) -> ContractQuantity:
        return self.cancelled_units

    @property
    def reconciliation(self) -> ReconciliationState:
        return ReconciliationState(self.reconciliation_state)

    @property
    def fee(self) -> MoneyMicros:
        return self.fee_micros

    @property
    def is_financially_mutating(self) -> bool:
        return bool(self.fills)


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    operation_id: str
    sequence_number: int
    state: OrderState
    observed_at: datetime
    idempotency_key: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.operation_id, "operation_id")
        _nonempty(self.idempotency_key, "idempotency_key")
        if self.sequence_number < 0:
            raise ValueError("lifecycle sequence cannot be negative")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class ReconciliationEvent:
    operation_id: str
    sequence_number: int
    state: ReconciliationState
    evidence: Mapping[str, object]
    observed_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        _nonempty(self.operation_id, "operation_id")
        _nonempty(self.idempotency_key, "idempotency_key")
        if self.sequence_number < 0:
            raise ValueError("reconciliation sequence cannot be negative")
        if not isinstance(self.evidence, Mapping):
            raise ValueError("reconciliation evidence must be an object")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    settlement_id: str
    market_ref: MarketKey | str
    outcome: OutcomeSide | str
    resolution_id: str
    settlement_ts: datetime
    contract_units: ContractQuantity
    gross_payout_micros: MoneyMicros
    entry_fees_deducted_micros: MoneyMicros
    realized_pnl_micros: int
    ledger_entry: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _nonempty(self.settlement_id, "settlement_id")
        _nonempty(self.resolution_id, "resolution_id")
        if isinstance(self.market_ref, str):
            object.__setattr__(self, "market_ref", MarketKey(self.market_ref))
        object.__setattr__(self, "outcome", OutcomeSide(self.outcome))
        if self.contract_units <= 0:
            raise ValueError("settlement quantity must be positive")
        if self.gross_payout_micros < 0 or self.entry_fees_deducted_micros < 0:
            raise ValueError("settlement money values cannot be negative")
        object.__setattr__(self, "settlement_ts", _aware(self.settlement_ts, "settlement_ts"))

    @property
    def payout_micros(self) -> MoneyMicros:
        return self.gross_payout_micros


def gross_cash_micros(contract_units: int, price_micros: int) -> MoneyMicros:
    """Convert hundredths of a contract at a micro-dollar price exactly.

    The numerator is rounded half-up only when a sub-micro-dollar product is
    mathematically unavoidable.  Normal Kalshi cent grids are exactly aligned.
    """

    if contract_units <= 0:
        raise ValueError("contract units must be positive")
    if not 0 <= price_micros <= 1_000_000:
        raise ValueError("price must be in the binary range")
    numerator = contract_units * price_micros
    quotient, remainder = divmod(numerator, 100)
    if remainder >= 50:
        quotient += 1
    return MoneyMicros(quotient)


def operation_uuid(agent_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{agent_id}\x1f{idempotency_key}".encode()).hexdigest()
    return digest[:32]


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


# Explicit aliases make the semantic vocabulary discoverable without creating
# alternate wire contracts.
BinaryOrderRequest = OrderRequest
SemanticOrderRequest = OrderRequest
ContractOrderRequest = OrderRequest
OrderOperationResult = OrderResult
BinaryOrderResult = OrderResult
ExecutionFill = EconomicFill
FeeSnapshot = FeePolicySnapshot
FeeRole = FeeParticipantRole
AmountType = OrderAmountType
OrderTimeInForce = TimeInForce
OrderLifecycleState = OrderState
OrderReconciliationState = ReconciliationState


__all__ = [
    "FEE_FORMULA_VERSION",
    "FEE_SETTLEMENT_CONTRACT_VERSION",
    "LIQUIDITY_HAIRCUT_CONTRACT_VERSION",
    "AmountType",
    "BinaryOrderRequest",
    "BinaryOrderResult",
    "ContractOrderRequest",
    "EconomicFill",
    "ExecutionFill",
    "FeeCalculation",
    "FeeParticipantRole",
    "FeePolicySnapshot",
    "FeeRole",
    "FeeSnapshot",
    "LifecycleEvent",
    "OrderAmountType",
    "OrderLifecycleState",
    "OrderOperationResult",
    "OrderReconciliationState",
    "OrderRequest",
    "OrderResult",
    "OrderState",
    "OrderTimeInForce",
    "ReconciliationEvent",
    "ReconciliationState",
    "SemanticExecutionError",
    "SemanticOrderRequest",
    "SettlementRecord",
    "SubmissionState",
    "TimeInForce",
    "gross_cash_micros",
    "operation_uuid",
]
