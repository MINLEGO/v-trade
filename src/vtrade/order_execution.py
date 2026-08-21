"""Shared orchestration for immediate and post-harness paper-order execution.

Database reads are supplied by the production state adapter.  Keeping this
service independent from ``worker.py`` lets the tool path and recovery broker
path use the identical validation, simulation, and persistence sequence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

from vtrade.broker import (
    ExecutionResult,
    ExecutionStatus,
    LiquidityTimeInForce,
    OrderAmountType,
    PaperOrder,
    PaperPolicy,
    PredictionArenaPaperBroker,
    RejectionCode,
)
from vtrade.domain.types import MicroDollars, PriceLevel
from vtrade.liquidity import (
    VirtualLiquidityMetrics,
    VirtualLiquidityReservation,
)
from vtrade.live_execution import (
    ExecutionReceipt,
    LiveContextError,
    LiveContextPersistence,
    LiveExecutionAttempt,
    LiveExecutionAudit,
    LiveOrderContext,
    LiveOrderContextProvider,
    MarketOrderSubmission,
    OrderExecutionUnavailable,
    ValidatedLiveOrderContextProvider,
    live_context_rejection,
    persist_live_rejection,
    persist_live_rejection_locked,
    require_live_context_provider,
)
from vtrade.runtime import CycleClaim

__all__ = [
    "ExecutionReceipt",
    "LiveContextError",
    "LiveContextPersistence",
    "LiveExecutionAttempt",
    "LiveExecutionAudit",
    "LiveOrderContext",
    "LiveOrderContextProvider",
    "MarketOrderExecutor",
    "MarketOrderSubmission",
    "OrderExecutionUnavailable",
    "ValidatedLiveOrderContextProvider",
]


class _TradingState(Protocol):
    def pending_intents(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        *,
        include_existing: bool = False,
    ) -> Sequence[Any]: ...

    def incomplete_intents(
        self, claim: CycleClaim, *, cursor: Any | None = None
    ) -> Sequence[Any]: ...

    def portfolio(self, agent_id: uuid.UUID) -> Any: ...

    def executable_bids(
        self,
        portfolio: Any,
        *,
        cutoff: datetime,
        order_book_snapshot_ids: Sequence[uuid.UUID],
    ) -> Mapping[str, Any]: ...

    def live_executable_bids(
        self,
        portfolio: Any,
        *,
        as_of: datetime,
        maximum_bid_age: timedelta,
        cursor: Any | None = None,
    ) -> Mapping[str, Any]: ...

    def prepare_virtual_liquidity(
        self,
        cursor: Any,
        claim: CycleClaim,
        order: Any,
        *,
        snapshot_id: uuid.UUID,
        snapshot: Any,
        maximum_book_depth: int,
        ignored_best_levels: int,
        maximum_ignored_depth_fraction: Decimal,
        liquidity_rule_version: str,
    ) -> VirtualLiquidityReservation: ...

    def finalize_virtual_liquidity(
        self,
        cursor: Any,
        reservation: VirtualLiquidityReservation,
        result: ExecutionResult,
        *,
        completed_at: datetime,
    ) -> VirtualLiquidityMetrics: ...


class _MarketRepository(Protocol):
    def frozen_fee_policy(
        self,
        token_id: str,
        *,
        cutoff: datetime,
        fee_rate_snapshot_ids: Sequence[uuid.UUID],
    ) -> Any: ...


class _ExecutionRepository(Protocol):
    def persist_execution(
        self,
        result: ExecutionResult,
        *,
        agent_id: uuid.UUID,
        intent_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
        live_audit: LiveExecutionAudit | None = None,
    ) -> Any: ...

    def persist_execution_locked(
        self,
        cursor: Any,
        result: ExecutionResult,
        *,
        agent_id: uuid.UUID,
        intent_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
        live_audit: LiveExecutionAudit | None = None,
    ) -> Any: ...


class MarketOrderExecutor:
    """Execute paper intents through either the historical or live pipeline."""

    def __init__(
        self,
        state: _TradingState,
        market_repository: _MarketRepository,
        repository: _ExecutionRepository,
        *,
        broker: PredictionArenaPaperBroker,
        clock: Callable[[], datetime],
        live_context_provider: LiveOrderContextProvider | None = None,
        maximum_live_retries: int = 1,
        maximum_live_context_age: timedelta = timedelta(seconds=10),
        maximum_live_observation_age: timedelta = timedelta(minutes=5),
    ) -> None:
        if maximum_live_retries < 0:
            raise ValueError("maximum_live_retries cannot be negative")
        if maximum_live_context_age < timedelta(0):
            raise ValueError("maximum_live_context_age cannot be negative")
        if maximum_live_observation_age < timedelta(0):
            raise ValueError("maximum_live_observation_age cannot be negative")
        self._state = state
        self._market_repository = market_repository
        self._repository = repository
        self._broker = broker
        self._clock = clock
        self._live_context_provider = live_context_provider
        self._maximum_live_retries = maximum_live_retries
        self._maximum_live_context_age = maximum_live_context_age
        self._maximum_live_observation_age = maximum_live_observation_age

    @property
    def uses_live_context(self) -> bool:
        return self._live_context_provider is not None

    def execute(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        intent_id: uuid.UUID,
        *,
        amount_type: OrderAmountType | None = None,
        cash_budget_micros: int | None = None,
        limit_price: Decimal | None = None,
        time_in_force: LiquidityTimeInForce | None = None,
    ) -> ExecutionResult:
        return self.execute_with_receipt(
            claim,
            frozen,
            intent_id,
            amount_type=amount_type,
            cash_budget_micros=cash_budget_micros,
            limit_price=limit_price,
            time_in_force=time_in_force,
        ).result

    def submit_and_execute(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        submission: MarketOrderSubmission,
    ) -> ExecutionReceipt:
        """Persist the intent briefly, then build and execute a live context."""
        require_live_context_provider(self._broker, self._live_context_provider)
        locked_cursor = getattr(self._state, "locked_cursor", None)
        insert_intent = getattr(self._state, "insert_intent", None)
        if locked_cursor is None or insert_intent is None:
            raise OrderExecutionUnavailable("immediate submission requires transactional state")
        with locked_cursor(claim.agent_id) as cursor:
            requested_at = _aware(self._clock())
            _insert_intent(insert_intent, cursor, claim, submission, requested_at=requested_at)
            requested_at = _intent_requested_at(cursor, submission.intent_id, requested_at)
            if self._live_context_provider is None:
                return self._execute_locked(claim, frozen, submission, cursor)

        context, attempts, failure_code = self._build_live_context(
            submission, requested_at=requested_at
        )
        if context is None:
            return persist_live_rejection(
                self._state,
                broker=self._broker,
                repository=self._repository,
                claim=claim,
                frozen=frozen,
                submission=submission,
                requested_at=requested_at,
                rejection_code=failure_code or RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                attempts=attempts,
                pending_loader=_pending_intents,
                order_builder=self._order_for_submission,
                clock=self._clock,
            )
        return self._execute_live_context(
            claim,
            frozen,
            submission,
            context,
            requested_at=requested_at,
            attempts=attempts,
        )

    def cancel_incomplete_on_restart(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
    ) -> tuple[ExecutionReceipt, ...]:
        """Finalize intents left between submission and execution after a restart."""
        if self._broker.policy is not PaperPolicy.LIQUIDITY_AWARE:
            raise OrderExecutionUnavailable(
                "restart cancellation is only defined for liquidity-aware execution"
            )
        locked_cursor = getattr(self._state, "locked_cursor", None)
        if locked_cursor is None:
            raise OrderExecutionUnavailable("restart cancellation requires transactional state")
        receipts: list[ExecutionReceipt] = []
        with locked_cursor(claim.agent_id) as cursor:
            state = cast(Any, self._state)
            incomplete = getattr(state, "incomplete_intents", None)
            pending = (
                tuple(incomplete(claim, cursor=cursor))
                if incomplete is not None
                else _pending_intents(
                    state, claim, frozen, cursor=cursor, include_existing=False
                )
            )
            for item in pending:
                portfolio = state.portfolio(claim.agent_id, cursor=cursor)
                executed_at = _aware(self._clock())
                result = ExecutionResult(
                    order=item.order,
                    policy=self._broker.policy,
                    status=ExecutionStatus.REJECTED,
                    fills=(),
                    rejection_code=RejectionCode.CANCELLED_BY_RESTART,
                    portfolio_before=portfolio,
                    portfolio=portfolio,
                    ledger_entries=(),
                    snapshot=None,
                    fee_policy=None,
                    executed_at=executed_at,
                )
                audit = LiveExecutionAudit(
                    requested_at=(getattr(item, "requested_at", None) or item.order.created_at),
                    validated_at=None,
                    executed_at=executed_at,
                    attempts=(),
                )
                persisted = cast(Any, self._repository).persist_execution_locked(
                    cursor,
                    result,
                    agent_id=claim.agent_id,
                    intent_id=item.intent_id,
                    market_id=item.market_id,
                    outcome_id=item.outcome_id,
                    snapshot_id=None,
                    live_audit=audit,
                )
                receipts.append(ExecutionReceipt(result, persisted.record_id, None))
        return tuple(receipts)

    def execute_with_receipt(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        intent_id: uuid.UUID,
        *,
        amount_type: OrderAmountType | None = None,
        cash_budget_micros: int | None = None,
        limit_price: Decimal | None = None,
        time_in_force: LiquidityTimeInForce | None = None,
    ) -> ExecutionReceipt:
        require_live_context_provider(self._broker, self._live_context_provider)
        if self._live_context_provider is not None:
            return self._execute_existing_live(
                claim,
                frozen,
                intent_id,
                amount_type=amount_type,
                cash_budget_micros=cash_budget_micros,
                limit_price=limit_price,
                time_in_force=time_in_force,
            )
        fee_ids = _uuid_membership(frozen, "fee_rate_snapshot_ids")
        book_ids = _uuid_membership(frozen, "order_book_snapshot_ids")
        if not fee_ids:
            raise OrderExecutionUnavailable("cycle has no frozen fee-rate membership")
        pending = _pending_intents(self._state, claim, frozen, cursor=None, include_existing=True)
        item = next((row for row in pending if row.intent_id == intent_id), None)
        if item is None:
            raise OrderExecutionUnavailable("order intent is not executable")
        cutoff = _cutoff(claim)
        fee = self._market_repository.frozen_fee_policy(
            item.outcome.venue_token_id,
            cutoff=cutoff,
            fee_rate_snapshot_ids=fee_ids,
        )
        order = replace(
            item.order,
            liquidity_time_in_force=time_in_force or item.order.liquidity_time_in_force,
            amount_type=amount_type or item.order.amount_type,
            cash_budget_micros=(
                MicroDollars(cash_budget_micros) if cash_budget_micros is not None else None
            ),
            limit_price=limit_price,
        )
        locked_cursor = getattr(self._state, "locked_cursor", None)
        if locked_cursor is None:
            portfolio = self._state.portfolio(claim.agent_id)
            bids = self._state.executable_bids(
                portfolio, cutoff=cutoff, order_book_snapshot_ids=book_ids
            )
            result = self._place(order, item, portfolio, bids, fee)
            persisted = self._repository.persist_execution(
                result,
                agent_id=claim.agent_id,
                intent_id=item.intent_id,
                market_id=item.market_id,
                outcome_id=item.outcome_id,
                snapshot_id=item.book_snapshot_id,
            )
        else:
            with locked_cursor(claim.agent_id) as cursor:
                state = cast(Any, self._state)
                locked_pending = _pending_intents(
                    state, claim, frozen, cursor=cursor, include_existing=True
                )
                locked_item = next(
                    (row for row in locked_pending if row.intent_id == intent_id), None
                )
                if locked_item is None:
                    raise OrderExecutionUnavailable("order intent disappeared before execution")
                item = locked_item
                portfolio = state.portfolio(claim.agent_id, cursor=cursor)
                bids = state.executable_bids(
                    portfolio,
                    cutoff=cutoff,
                    order_book_snapshot_ids=book_ids,
                    cursor=cursor,
                )
                reservation = self._prepare_virtual_liquidity(state, cursor, claim, order, item)
                portfolio_for_order = (
                    reservation.retry_portfolio
                    if reservation is not None and reservation.retry_portfolio is not None
                    else portfolio
                )
                if portfolio_for_order is not portfolio:
                    bids = state.executable_bids(
                        portfolio_for_order,
                        cutoff=cutoff,
                        order_book_snapshot_ids=book_ids,
                        cursor=cursor,
                    )
                execution_at = (
                    reservation.retry_now
                    if reservation is not None and reservation.retry_now is not None
                    else _aware(self._clock())
                )
                result = self._place(
                    order,
                    item,
                    portfolio_for_order,
                    bids,
                    fee,
                    snapshot=reservation.snapshot if reservation is not None else None,
                    now=execution_at,
                    effective_levels=_reservation_effective_levels(reservation),
                )
                if reservation is not None:
                    metrics = state.finalize_virtual_liquidity(
                        cursor,
                        reservation,
                        result,
                        completed_at=execution_at,
                    )
                    result = replace(result, virtual_liquidity=metrics)
                persisted = cast(Any, self._repository).persist_execution_locked(
                    cursor,
                    result,
                    agent_id=claim.agent_id,
                    intent_id=item.intent_id,
                    market_id=item.market_id,
                    outcome_id=item.outcome_id,
                    snapshot_id=item.book_snapshot_id,
                )
        return ExecutionReceipt(result, persisted.record_id, item.book_snapshot_id)

    def _build_live_context(
        self,
        submission: MarketOrderSubmission,
        *,
        requested_at: datetime,
    ) -> tuple[LiveOrderContext | None, tuple[LiveExecutionAttempt, ...], RejectionCode | None]:
        provider = self._live_context_provider
        if provider is None:
            raise OrderExecutionUnavailable("live context provider is not configured")
        attempts: list[LiveExecutionAttempt] = []
        for attempt_number in range(1, self._maximum_live_retries + 2):
            started_at = _aware(self._clock())
            try:
                context = provider.build(submission, requested_at=requested_at)
            except LiveContextError as exc:
                completed_at = _aware(self._clock())
                attempts.append(
                    LiveExecutionAttempt(
                        attempt_number,
                        "failed",
                        started_at,
                        completed_at,
                        exc.code.value,
                    )
                )
                if exc.retryable and attempt_number <= self._maximum_live_retries:
                    continue
                return None, tuple(attempts), exc.code
            except (ConnectionError, TimeoutError, OSError):
                completed_at = _aware(self._clock())
                attempts.append(
                    LiveExecutionAttempt(
                        attempt_number,
                        "failed",
                        started_at,
                        completed_at,
                        RejectionCode.NETWORK_ERROR.value,
                    )
                )
                if attempt_number <= self._maximum_live_retries:
                    continue
                return None, tuple(attempts), RejectionCode.NETWORK_ERROR
            except Exception:
                completed_at = _aware(self._clock())
                attempts.append(
                    LiveExecutionAttempt(
                        attempt_number,
                        "failed",
                        started_at,
                        completed_at,
                        RejectionCode.INCONSISTENT_LIVE_CONTEXT.value,
                    )
                )
                return None, tuple(attempts), RejectionCode.INCONSISTENT_LIVE_CONTEXT
            completed_at = _aware(self._clock())
            attempts.append(
                LiveExecutionAttempt(attempt_number, "validated", started_at, completed_at)
            )
            return context, tuple(attempts), None
        raise AssertionError("live context retry loop did not return")

    def _execute_existing_live(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        intent_id: uuid.UUID,
        *,
        amount_type: OrderAmountType | None,
        cash_budget_micros: int | None,
        limit_price: Decimal | None,
        time_in_force: LiquidityTimeInForce | None,
    ) -> ExecutionReceipt:
        pending = _pending_intents(self._state, claim, frozen, cursor=None, include_existing=True)
        item = next((row for row in pending if row.intent_id == intent_id), None)
        if item is None:
            raise OrderExecutionUnavailable("order intent is not executable")
        order = replace(
            item.order,
            amount_type=amount_type or item.order.amount_type,
            cash_budget_micros=(
                MicroDollars(cash_budget_micros) if cash_budget_micros is not None else None
            ),
            limit_price=limit_price,
            liquidity_time_in_force=time_in_force or item.order.liquidity_time_in_force,
        )
        submission = MarketOrderSubmission(
            intent_id=item.intent_id,
            market_id=item.market_id,
            outcome_id=item.outcome_id,
            side=item.order.side.value,
            amount_micros=(
                cash_budget_micros
                if cash_budget_micros is not None
                else int(item.order.shares * Decimal(1_000_000))
            ),
            shares=order.shares,
            confidence=Decimal("0.5"),
            created_at=item.order.created_at,
            amount_type=order.amount_type,
            cash_budget_micros=cash_budget_micros,
            limit_price=limit_price,
            time_in_force=order.liquidity_time_in_force,
        )
        requested_at = _aware(
            getattr(item, "requested_at", None) or self._clock()
        )
        context, attempts, failure_code = self._build_live_context(
            submission, requested_at=requested_at
        )
        if context is None:
            return persist_live_rejection(
                self._state,
                broker=self._broker,
                repository=self._repository,
                claim=claim,
                frozen=frozen,
                submission=submission,
                requested_at=requested_at,
                rejection_code=failure_code or RejectionCode.INCONSISTENT_LIVE_CONTEXT,
                attempts=attempts,
                pending_loader=_pending_intents,
                order_builder=self._order_for_submission,
                clock=self._clock,
            )
        return self._execute_live_context(
            claim,
            frozen,
            submission,
            context,
            requested_at=requested_at,
            attempts=attempts,
        )

    def _execute_live_context(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        submission: MarketOrderSubmission,
        context: LiveOrderContext,
        *,
        requested_at: datetime,
        attempts: tuple[LiveExecutionAttempt, ...],
    ) -> ExecutionReceipt:
        locked_cursor = getattr(self._state, "locked_cursor", None)
        if locked_cursor is None:
            raise OrderExecutionUnavailable("live execution requires transactional state")
        with locked_cursor(claim.agent_id) as cursor:
            state = cast(Any, self._state)
            pending = _pending_intents(state, claim, frozen, cursor=cursor, include_existing=True)
            item = next((row for row in pending if row.intent_id == submission.intent_id), None)
            if item is None:
                raise OrderExecutionUnavailable("live order intent disappeared before execution")
            live_item = replace(
                item,
                market=context.market,
                outcome=context.outcome,
                book=context.book,
                book_snapshot_id=context.book_snapshot_id,
            )
            portfolio = state.portfolio(claim.agent_id, cursor=cursor)
            execution_clock = _aware(self._clock())
            rejection_code = live_context_rejection(
                context,
                requested_at=requested_at,
                now=execution_clock,
                maximum_context_age=self._maximum_live_context_age,
                maximum_observation_age=self._maximum_live_observation_age,
            )
            if rejection_code is not None:
                return persist_live_rejection_locked(
                    cursor,
                    broker=self._broker,
                    repository=self._repository,
                    claim=claim,
                    submission=submission,
                    item=item,
                    portfolio=portfolio,
                    requested_at=requested_at,
                    rejection_code=rejection_code,
                    attempts=attempts,
                    context=context,
                    order_builder=self._order_for_submission,
                    clock=self._clock,
                    executed_at=execution_clock,
                )
            order = self._order_for_submission(item.order, submission, context)
            bids = self._live_bids(
                state, portfolio, context, cursor=cursor, frozen=frozen, claim=claim
            )
            reservation = self._prepare_virtual_liquidity(state, cursor, claim, order, live_item)
            portfolio_for_order = (
                reservation.retry_portfolio
                if reservation is not None and reservation.retry_portfolio is not None
                else portfolio
            )
            if portfolio_for_order is not portfolio:
                bids = self._live_bids(
                    state,
                    portfolio_for_order,
                    context,
                    cursor=cursor,
                    frozen=frozen,
                    claim=claim,
                )
            execution_clock = _aware(self._clock())
            rejection_code = live_context_rejection(
                context,
                requested_at=requested_at,
                now=execution_clock,
                maximum_context_age=self._maximum_live_context_age,
                maximum_observation_age=self._maximum_live_observation_age,
            )
            if rejection_code is not None:
                return persist_live_rejection_locked(
                    cursor,
                    broker=self._broker,
                    repository=self._repository,
                    claim=claim,
                    submission=submission,
                    item=item,
                    portfolio=portfolio,
                    requested_at=requested_at,
                    rejection_code=rejection_code,
                    attempts=attempts,
                    context=context,
                    order_builder=self._order_for_submission,
                    clock=self._clock,
                    executed_at=execution_clock,
                )
            execution_at = max(
                context.validated_at,
                reservation.retry_now
                if reservation is not None and reservation.retry_now is not None
                else context.validated_at,
                execution_clock,
            )
            result = self._place(
                order,
                live_item,
                portfolio_for_order,
                bids,
                context.fee_policy,
                snapshot=reservation.snapshot if reservation is not None else context.book,
                now=execution_at,
                live_context=True,
                valuation_as_of=context.validated_at,
                effective_levels=_reservation_effective_levels(reservation),
            )
            if reservation is not None:
                metrics = state.finalize_virtual_liquidity(
                    cursor,
                    reservation,
                    result,
                    completed_at=execution_at,
                )
                result = replace(result, virtual_liquidity=metrics)
            audit = LiveExecutionAudit(
                requested_at=requested_at,
                validated_at=context.validated_at,
                executed_at=execution_at,
                attempts=attempts,
                context=context,
            )
            persisted = cast(Any, self._repository).persist_execution_locked(
                cursor,
                result,
                agent_id=claim.agent_id,
                intent_id=item.intent_id,
                market_id=item.market_id,
                outcome_id=item.outcome_id,
                snapshot_id=context.book_snapshot_id,
                live_audit=audit,
            )
        return ExecutionReceipt(
            result,
            persisted.record_id,
            context.book_snapshot_id,
            attempts,
        )

    def _order_for_submission(
        self,
        original: PaperOrder,
        submission: MarketOrderSubmission,
        context: LiveOrderContext | None,
    ) -> PaperOrder:
        shares = submission.shares
        if (
            submission.amount_type is OrderAmountType.CASH
            and context is not None
            and context.book.best_ask is not None
            and context.book.best_ask > 0
        ):
            shares = Decimal(submission.amount_micros) / Decimal(1_000_000) / context.book.best_ask
        return replace(
            original,
            shares=shares,
            amount_type=submission.amount_type,
            cash_budget_micros=(
                MicroDollars(submission.cash_budget_micros)
                if submission.cash_budget_micros is not None
                else None
            ),
            limit_price=submission.limit_price,
            liquidity_time_in_force=submission.time_in_force,
        )

    def _live_bids(
        self,
        state: Any,
        portfolio: Any,
        context: LiveOrderContext,
        *,
        cursor: Any,
        frozen: Mapping[str, object],
        claim: CycleClaim,
    ) -> Mapping[str, Any]:
        live_bids = getattr(state, "live_executable_bids", None)
        if live_bids is not None:
            return cast(
                Mapping[str, Any],
                live_bids(
                    portfolio,
                    as_of=context.validated_at,
                    maximum_bid_age=self._broker.maximum_valuation_bid_age,
                    cursor=cursor,
                ),
            )
        if getattr(self._broker, "policy", None) is PaperPolicy.LIQUIDITY_AWARE:
            raise OrderExecutionUnavailable(
                "liquidity-aware execution requires live historical bid valuation"
            )
        cutoff = _cutoff(claim)
        book_ids = _uuid_membership(frozen, "order_book_snapshot_ids")
        return cast(
            Mapping[str, Any],
            state.executable_bids(
                portfolio,
                cutoff=cutoff,
                order_book_snapshot_ids=book_ids,
                cursor=cursor,
            ),
        )

    def _execute_locked(
        self,
        claim: CycleClaim,
        frozen: Mapping[str, object],
        submission: MarketOrderSubmission,
        cursor: Any,
    ) -> ExecutionReceipt:
        fee_ids = _uuid_membership(frozen, "fee_rate_snapshot_ids")
        book_ids = _uuid_membership(frozen, "order_book_snapshot_ids")
        state = cast(Any, self._state)
        pending = _pending_intents(state, claim, frozen, cursor=cursor, include_existing=True)
        item = next((row for row in pending if row.intent_id == submission.intent_id), None)
        if item is None:
            raise OrderExecutionUnavailable("inserted order intent is not executable")
        cutoff = _cutoff(claim)
        portfolio = state.portfolio(claim.agent_id, cursor=cursor)
        bids = state.executable_bids(
            portfolio, cutoff=cutoff, order_book_snapshot_ids=book_ids, cursor=cursor
        )
        fee = self._market_repository.frozen_fee_policy(
            item.outcome.venue_token_id, cutoff=cutoff, fee_rate_snapshot_ids=fee_ids
        )
        order = replace(
            item.order,
            liquidity_time_in_force=submission.time_in_force,
            amount_type=submission.amount_type,
            cash_budget_micros=(
                MicroDollars(submission.cash_budget_micros)
                if submission.cash_budget_micros is not None
                else None
            ),
            limit_price=submission.limit_price,
        )
        reservation = self._prepare_virtual_liquidity(state, cursor, claim, order, item)
        portfolio_for_order = (
            reservation.retry_portfolio
            if reservation is not None and reservation.retry_portfolio is not None
            else portfolio
        )
        if portfolio_for_order is not portfolio:
            bids = state.executable_bids(
                portfolio_for_order,
                cutoff=cutoff,
                order_book_snapshot_ids=book_ids,
                cursor=cursor,
            )
        execution_at = (
            reservation.retry_now
            if reservation is not None and reservation.retry_now is not None
            else _aware(self._clock())
        )
        result = self._place(
            order,
            item,
            portfolio_for_order,
            bids,
            fee,
            snapshot=reservation.snapshot if reservation is not None else None,
            now=execution_at,
            effective_levels=_reservation_effective_levels(reservation),
        )
        if reservation is not None:
            metrics = state.finalize_virtual_liquidity(
                cursor,
                reservation,
                result,
                completed_at=execution_at,
            )
            result = replace(result, virtual_liquidity=metrics)
        persisted = cast(Any, self._repository).persist_execution_locked(
            cursor,
            result,
            agent_id=claim.agent_id,
            intent_id=item.intent_id,
            market_id=item.market_id,
            outcome_id=item.outcome_id,
            snapshot_id=item.book_snapshot_id,
        )
        return ExecutionReceipt(result, persisted.record_id, item.book_snapshot_id)

    def _prepare_virtual_liquidity(
        self,
        state: Any,
        cursor: Any,
        claim: CycleClaim,
        order: Any,
        item: Any,
    ) -> VirtualLiquidityReservation | None:
        prepare = getattr(state, "prepare_virtual_liquidity", None)
        if (
            getattr(self._broker, "policy", None) is not PaperPolicy.LIQUIDITY_AWARE
            or prepare is None
        ):
            return None
        return cast(
            VirtualLiquidityReservation,
            prepare(
                cursor,
                claim,
                order,
                snapshot_id=item.book_snapshot_id,
                snapshot=item.book,
                maximum_book_depth=self._broker.maximum_book_depth_limit,
                ignored_best_levels=getattr(self._broker, "ignored_best_levels", 0),
                maximum_ignored_depth_fraction=getattr(
                    self._broker, "maximum_ignored_depth_fraction", Decimal(0)
                ),
                liquidity_rule_version=getattr(
                    self._broker,
                    "liquidity_rule_version",
                    "best-level-haircut-v1",
                ),
            ),
        )

    def _place(
        self,
        order: Any,
        item: Any,
        portfolio: Any,
        bids: Any,
        fee: Any,
        *,
        snapshot: Any = None,
        now: datetime | None = None,
        live_context: bool = False,
        valuation_as_of: datetime | None = None,
        effective_levels: Sequence[PriceLevel] | None = None,
    ) -> ExecutionResult:
        kwargs: dict[str, Any] = {
            "market": item.market,
            "outcome": item.outcome,
            "snapshot": item.book if snapshot is None else snapshot,
            "portfolio": portfolio,
            "executable_bids": bids,
            "fee_policy": fee,
            "now": _aware(self._clock()) if now is None else now,
        }
        if live_context:
            kwargs["live_context"] = True
        if valuation_as_of is not None:
            kwargs["valuation_as_of"] = valuation_as_of
        if effective_levels is not None:
            kwargs["effective_levels"] = effective_levels
        return self._broker.place(
            order,
            **kwargs,
        )


def _reservation_effective_levels(
    reservation: VirtualLiquidityReservation | None,
) -> tuple[PriceLevel, ...] | None:
    if reservation is None:
        return None
    levels = tuple(
        PriceLevel(level.price, level.available_shares)
        for level in reservation.levels
        if level.executable and level.available_shares > 0
    )
    return levels


def _uuid_membership(value: Mapping[str, object], key: str) -> tuple[uuid.UUID, ...]:
    rows = value.get(key)
    if not isinstance(rows, list):
        raise OrderExecutionUnavailable(f"cycle freeze lacks {key}")
    try:
        result = tuple(uuid.UUID(str(row)) for row in rows)
    except ValueError as exc:
        raise OrderExecutionUnavailable(f"cycle freeze has malformed {key}") from exc
    if len(set(result)) != len(result):
        raise OrderExecutionUnavailable(f"cycle freeze has duplicate {key}")
    return result


def _insert_intent(
    insert_intent: Callable[..., object],
    cursor: Any,
    claim: CycleClaim,
    submission: MarketOrderSubmission,
    *,
    requested_at: datetime,
) -> None:
    try:
        insert_intent(cursor, claim, submission, requested_at=requested_at)
    except TypeError as exc:
        if "keyword" not in str(exc) and "argument" not in str(exc):
            raise
        insert_intent(cursor, claim, submission)


def _intent_requested_at(cursor: Any, intent_id: uuid.UUID, fallback: datetime) -> datetime:
    fetchone = getattr(cursor, "fetchone", None)
    if fetchone is None:
        return fallback
    cursor.execute("SELECT requested_at FROM order_intents WHERE id = %s", (intent_id,))
    row = fetchone()
    if row is None or not isinstance(row[0], datetime):
        return fallback
    return _aware(row[0])


def _pending_intents(
    state: Any,
    claim: CycleClaim,
    frozen: Mapping[str, object],
    *,
    cursor: Any | None,
    include_existing: bool,
) -> Sequence[Any]:
    try:
        kwargs: dict[str, object] = {"include_existing": include_existing}
        if cursor is not None:
            kwargs["cursor"] = cursor
        return cast(Sequence[Any], state.pending_intents(claim, frozen, **kwargs))
    except TypeError as exc:
        if "keyword" not in str(exc) and "argument" not in str(exc):
            raise
        try:
            if cursor is None:
                return cast(Sequence[Any], state.pending_intents(claim, frozen))
            return cast(Sequence[Any], state.pending_intents(claim, frozen, cursor=cursor))
        except TypeError as fallback_exc:
            if "keyword" not in str(fallback_exc) and "argument" not in str(fallback_exc):
                raise
            return cast(Sequence[Any], state.pending_intents(claim, frozen))


def _cutoff(claim: CycleClaim) -> datetime:
    if claim.data_cutoff is None:
        raise OrderExecutionUnavailable("cycle cutoff is not finalized")
    return _aware(claim.data_cutoff)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production timestamps must be timezone-aware")
    return value


# ---------------------------------------------------------------------------
# Shared semantic paper/real execution boundary
# ---------------------------------------------------------------------------


from time import monotonic as _monotonic  # noqa: E402

from vtrade.broker import BinaryPaperBroker  # noqa: E402
from vtrade.domain.execution import (  # noqa: E402
    FeePolicySnapshot,
    OrderRequest,
    OrderResult,
    SemanticExecutionError,
    operation_uuid,
)
from vtrade.domain.types import MarketContext  # noqa: E402
from vtrade.portfolio import ContractPortfolio  # noqa: E402


@dataclass(frozen=True, slots=True)
class FrozenDecisionContext:
    """Immutable context used to make an order decision."""

    context_id: str
    market_context: MarketContext
    cutoff: datetime

    def __post_init__(self) -> None:
        if not self.context_id:
            raise ValueError("frozen context id is required")
        _aware(self.cutoff)
        if self.market_context.order_book.cutoff != self.cutoff:
            raise ValueError("frozen context cutoff must match the canonical book cutoff")


@dataclass(frozen=True, slots=True)
class RefreshedExecutionContext:
    """Context obtained immediately before a paper fill."""

    context_id: str
    market_context: MarketContext
    refreshed_at: datetime

    def __post_init__(self) -> None:
        if not self.context_id:
            raise ValueError("execution context id is required")
        _aware(self.refreshed_at)
        if self.market_context.order_book.observed_at > self.refreshed_at:
            raise ValueError("execution book cannot be observed in the future")


class SemanticContextRefreshError(RuntimeError):
    """A bounded context refresh failed before any financial mutation."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class SemanticOrderExecutor:
    """Orchestrate frozen decisions and refreshed paper execution contexts."""

    def __init__(
        self,
        broker: BinaryPaperBroker,
        refresh_context: Callable[..., MarketContext | RefreshedExecutionContext],
        fee_policy: FeePolicySnapshot | Callable[..., FeePolicySnapshot | None],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        maximum_wait: timedelta = timedelta(seconds=10),
        monotonic: Callable[[], float] = _monotonic,
    ) -> None:
        if maximum_wait < timedelta(0):
            raise ValueError("maximum context wait cannot be negative")
        self._broker = broker
        self._refresh_context = refresh_context
        self._fee_policy = fee_policy
        self._clock = clock
        self._maximum_wait = maximum_wait
        self._monotonic = monotonic

    def submit(
        self,
        request: OrderRequest,
        *,
        frozen_context: FrozenDecisionContext | MarketContext,
        portfolio: ContractPortfolio,
    ) -> OrderResult:
        frozen = (
            frozen_context
            if isinstance(frozen_context, FrozenDecisionContext)
            else FrozenDecisionContext(
                request.frozen_context_id or "frozen-context",
                frozen_context,
                frozen_context.order_book.cutoff,
            )
        )
        started = self._monotonic()
        try:
            refreshed = self._call_refresh(request, frozen)
            finished = _aware(self._clock())
            if self._monotonic() - started > self._maximum_wait.total_seconds():
                raise SemanticContextRefreshError("execution context refresh exceeded its bound")
            execution = (
                refreshed
                if isinstance(refreshed, RefreshedExecutionContext)
                else RefreshedExecutionContext(
                    f"execution:{request.idempotency_key}", refreshed, finished
                )
            )
            self._validate_pair(request, frozen, execution, finished)
            policy = self._call_fee_policy(request, execution)
        except (SemanticContextRefreshError, ConnectionError, TimeoutError, OSError):
            return self._broker.execute(
                request,
                context=None,
                portfolio=portfolio,
                fee_policy=None,
                frozen_context_id=frozen.context_id,
                now=_aware(self._clock()),
                pending=True,
            )
        except ValueError as exc:
            return self._rejected_without_submission(
                request, portfolio, str(exc), frozen.context_id
            )
        return self._broker.execute(
            request,
            context=execution.market_context,
            portfolio=portfolio,
            fee_policy=policy,
            frozen_context_id=frozen.context_id,
            execution_context_id=execution.context_id,
            now=_aware(self._clock()),
        )

    def submit_order(
        self,
        request: OrderRequest,
        *,
        frozen_context: FrozenDecisionContext | MarketContext,
        portfolio: ContractPortfolio,
    ) -> OrderResult:
        return self.submit(request, frozen_context=frozen_context, portfolio=portfolio)

    def recent_activity(self, agent_id: str) -> tuple[OrderResult, ...]:
        return self._broker.recent_activity(agent_id)

    def _call_refresh(
        self, request: OrderRequest, frozen: FrozenDecisionContext
    ) -> MarketContext | RefreshedExecutionContext:
        try:
            return self._refresh_context(request, frozen)
        except TypeError as first_error:
            try:
                return self._refresh_context(request)
            except TypeError:
                raise SemanticContextRefreshError(
                    "context refresher has an invalid signature"
                ) from first_error

    def _call_fee_policy(
        self, request: OrderRequest, execution: RefreshedExecutionContext
    ) -> FeePolicySnapshot | None:
        if isinstance(self._fee_policy, FeePolicySnapshot):
            return self._fee_policy
        return self._fee_policy(request, execution)

    @staticmethod
    def _validate_pair(
        request: OrderRequest,
        frozen: FrozenDecisionContext,
        execution: RefreshedExecutionContext,
        now: datetime,
    ) -> None:
        if execution.market_context.market.key != request.market_ref:
            raise ValueError("refreshed market differs from market_ref")
        if execution.market_context.order_book.cutoff < frozen.cutoff:
            raise ValueError("refreshed context is older than the frozen decision")
        if execution.market_context.order_book.observed_at > now:
            raise ValueError("refreshed order book is from the future")

    @staticmethod
    def _rejected_without_submission(
        request: OrderRequest,
        portfolio: ContractPortfolio,
        message: str,
        frozen_context_id: str,
    ) -> OrderResult:
        return BinaryPaperBroker._rejected(
            request,
            portfolio,
            SemanticExecutionError.INVALID_CONTEXT,
            request.created_at,
            operation_id=operation_uuid(request.agent_id, request.idempotency_key),
            message=message,
        )


PaperOrderExecutor = SemanticOrderExecutor
SharedOrderExecutor = SemanticOrderExecutor
