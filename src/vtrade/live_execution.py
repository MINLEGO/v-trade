"""Live paper-order context contracts and validation."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from vtrade.broker import (
    ExecutionResult,
    ExecutionStatus,
    FeePolicy,
    LiquidityTimeInForce,
    OrderAmountType,
    PaperPolicy,
    PredictionArenaPaperBroker,
    RejectionCode,
)
from vtrade.domain.types import Market, OrderBookSnapshot, Outcome, RawArtifact
from vtrade.polymarket import FeePolicySnapshot


class OrderExecutionUnavailable(RuntimeError):
    pass


class LiveContextError(OrderExecutionUnavailable):
    """A live context could not be safely constructed for an order."""

    def __init__(
        self,
        message: str,
        *,
        code: RejectionCode,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def require_live_context_provider(
    broker: PredictionArenaPaperBroker,
    provider: LiveOrderContextProvider | None,
) -> None:
    if getattr(broker, "policy", None) is PaperPolicy.LIQUIDITY_AWARE and provider is None:
        raise OrderExecutionUnavailable(
            "liquidity-aware execution requires a live order-context provider"
        )


@dataclass(frozen=True, slots=True)
class LiveContextPersistence:
    market: Market
    book: OrderBookSnapshot
    fee_policy: FeePolicySnapshot
    market_artifact: RawArtifact | None = None


@dataclass(frozen=True, slots=True)
class LiveOrderContext:
    """The immutable, validated market context used by one live paper order."""

    market: Market
    outcome: Outcome
    book: OrderBookSnapshot
    fee_policy: FeePolicy
    market_snapshot_id: uuid.UUID
    book_snapshot_id: uuid.UUID
    fee_rate_snapshot_id: uuid.UUID
    requested_at: datetime
    validated_at: datetime
    market_observed_at: datetime
    book_observed_at: datetime
    fee_observed_at: datetime
    artifact_hashes: tuple[tuple[str, str], ...] = ()
    persistence_payload: LiveContextPersistence | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("requested_at", self.requested_at),
            ("validated_at", self.validated_at),
            ("market_observed_at", self.market_observed_at),
            ("book_observed_at", self.book_observed_at),
            ("fee_observed_at", self.fee_observed_at),
        ):
            _aware(value)
            if name == "validated_at" and value < self.requested_at:
                raise ValueError("live context validation cannot predate the request")

    @property
    def observations(self) -> tuple[datetime, ...]:
        return (
            self.market_observed_at,
            self.book_observed_at,
            self.fee_observed_at,
        )


@dataclass(frozen=True, slots=True)
class MarketOrderSubmission:
    intent_id: uuid.UUID
    market_id: uuid.UUID
    outcome_id: uuid.UUID
    side: str
    amount_micros: int
    shares: Decimal
    confidence: Decimal
    created_at: datetime
    amount_type: OrderAmountType
    cash_budget_micros: int | None
    limit_price: Decimal | None
    time_in_force: LiquidityTimeInForce


class LiveOrderContextProvider(Protocol):
    def build(
        self, submission: MarketOrderSubmission, *, requested_at: datetime
    ) -> LiveOrderContext: ...


class ValidatedLiveOrderContextProvider:
    """Validate provider observations without holding an agent transaction lock."""

    def __init__(
        self,
        refresh: Callable[[MarketOrderSubmission, datetime], LiveOrderContext],
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        persist: Callable[[LiveOrderContext], LiveOrderContext] | None = None,
        maximum_build_time: timedelta = timedelta(seconds=10),
        maximum_observation_age: timedelta = timedelta(minutes=5),
        maximum_source_skew: timedelta = timedelta(seconds=5),
    ) -> None:
        if maximum_build_time < timedelta(0):
            raise ValueError("live context build time cannot be negative")
        if maximum_observation_age < timedelta(0):
            raise ValueError("live context observation age cannot be negative")
        if maximum_source_skew < timedelta(0):
            raise ValueError("live context source skew cannot be negative")
        self._refresh = refresh
        self._clock = clock
        self._monotonic = monotonic
        self._persist = persist
        self._maximum_build_time = maximum_build_time
        self._maximum_observation_age = maximum_observation_age
        self._maximum_source_skew = maximum_source_skew

    def build(
        self, submission: MarketOrderSubmission, *, requested_at: datetime
    ) -> LiveOrderContext:
        _aware(requested_at)
        started = self._monotonic()
        try:
            context = self._refresh(submission, requested_at)
        except LiveContextError:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise LiveContextError(
                "live market provider failed while constructing the context",
                code=RejectionCode.NETWORK_ERROR,
                retryable=True,
            ) from exc
        except Exception as exc:
            raise LiveContextError(
                "live market context provider returned an unusable result",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            ) from exc
        finished = _aware(self._clock())
        elapsed = self._monotonic() - started
        if (
            elapsed > self._maximum_build_time.total_seconds()
            or finished - requested_at > self._maximum_build_time
        ):
            raise LiveContextError(
                "live order context construction exceeded its time budget",
                code=RejectionCode.LIVE_CONTEXT_EXPIRED,
            )
        if context.requested_at != requested_at:
            raise LiveContextError(
                "live context request timestamp does not match the intent",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            )
        observations = context.observations
        if any(observed > finished for observed in observations):
            raise LiveContextError(
                "live context contains a future observation",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            )
        if any(observed < requested_at for observed in observations):
            raise LiveContextError(
                "live context contains an observation before the order request",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            )
        if max(observations) - min(observations) > self._maximum_source_skew:
            raise LiveContextError(
                "live context sources are more than five seconds apart",
                code=RejectionCode.INCONSISTENT_LIVE_CONTEXT,
            )
        if finished - min(observations) > self._maximum_observation_age:
            raise LiveContextError(
                "live context contains an observation that is too old",
                code=RejectionCode.STALE_LIVE_DATA,
            )
        validated = replace(context, validated_at=finished)
        return self._persist(validated) if self._persist is not None else validated


@dataclass(frozen=True, slots=True)
class LiveExecutionAttempt:
    attempt: int
    status: str
    started_at: datetime
    completed_at: datetime
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LiveExecutionAudit:
    requested_at: datetime
    validated_at: datetime | None
    executed_at: datetime
    attempts: tuple[LiveExecutionAttempt, ...]
    context: LiveOrderContext | None = None


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    result: ExecutionResult
    order_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    attempts: tuple[LiveExecutionAttempt, ...] = ()


def live_context_rejection(
    context: LiveOrderContext,
    *,
    requested_at: datetime,
    now: datetime,
    maximum_context_age: timedelta,
    maximum_observation_age: timedelta,
) -> RejectionCode | None:
    if context.requested_at != requested_at:
        return RejectionCode.INCONSISTENT_LIVE_CONTEXT
    if now < context.validated_at or any(observed > now for observed in context.observations):
        return RejectionCode.INCONSISTENT_LIVE_CONTEXT
    if now - requested_at > maximum_context_age:
        return RejectionCode.LIVE_CONTEXT_EXPIRED
    if now - min(context.observations) > maximum_observation_age:
        return RejectionCode.STALE_LIVE_DATA
    return None


def persist_live_rejection_locked(
    cursor: Any,
    *,
    broker: PredictionArenaPaperBroker,
    repository: Any,
    claim: Any,
    submission: MarketOrderSubmission,
    item: Any,
    portfolio: Any,
    requested_at: datetime,
    rejection_code: RejectionCode,
    attempts: tuple[LiveExecutionAttempt, ...],
    context: LiveOrderContext | None,
    order_builder: Callable[[Any, MarketOrderSubmission, LiveOrderContext | None], Any],
    clock: Callable[[], datetime],
    executed_at: datetime | None = None,
) -> ExecutionReceipt:
    executed_at = _aware(clock()) if executed_at is None else executed_at
    if context is not None:
        executed_at = max(executed_at, context.validated_at)
    order = order_builder(item.order, submission, context)
    result = ExecutionResult(
        order=order,
        policy=broker.policy,
        status=ExecutionStatus.REJECTED,
        fills=(),
        rejection_code=rejection_code,
        portfolio_before=portfolio,
        portfolio=portfolio,
        ledger_entries=(),
        snapshot=context.book if context is not None else None,
        fee_policy=context.fee_policy if context is not None else None,
        executed_at=executed_at,
    )
    audit = LiveExecutionAudit(
        requested_at=requested_at,
        validated_at=context.validated_at if context is not None else None,
        executed_at=executed_at,
        attempts=attempts,
        context=context,
    )
    snapshot_id = context.book_snapshot_id if context is not None else None
    persisted = repository.persist_execution_locked(
        cursor,
        result,
        agent_id=claim.agent_id,
        intent_id=item.intent_id,
        market_id=item.market_id,
        outcome_id=item.outcome_id,
        snapshot_id=snapshot_id,
        live_audit=audit,
    )
    return ExecutionReceipt(result, persisted.record_id, snapshot_id, attempts)


def persist_live_rejection(
    state: Any,
    *,
    broker: PredictionArenaPaperBroker,
    repository: Any,
    claim: Any,
    frozen: Mapping[str, object],
    submission: MarketOrderSubmission,
    requested_at: datetime,
    rejection_code: RejectionCode,
    attempts: tuple[LiveExecutionAttempt, ...],
    pending_loader: Callable[..., Sequence[Any]],
    order_builder: Callable[[Any, MarketOrderSubmission, LiveOrderContext | None], Any],
    clock: Callable[[], datetime],
) -> ExecutionReceipt:
    locked_cursor = getattr(state, "locked_cursor", None)
    if locked_cursor is None:
        raise OrderExecutionUnavailable("live rejection requires transactional state")
    with locked_cursor(claim.agent_id) as cursor:
        pending = pending_loader(state, claim, frozen, cursor=cursor, include_existing=True)
        item = next((row for row in pending if row.intent_id == submission.intent_id), None)
        if item is None:
            raise OrderExecutionUnavailable("live order intent disappeared before rejection")
        portfolio = state.portfolio(claim.agent_id, cursor=cursor)
        return persist_live_rejection_locked(
            cursor,
            broker=broker,
            repository=repository,
            claim=claim,
            submission=submission,
            item=item,
            portfolio=portfolio,
            requested_at=requested_at,
            rejection_code=rejection_code,
            attempts=attempts,
            context=None,
            order_builder=order_builder,
            clock=clock,
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production timestamps must be timezone-aware")
    return value
