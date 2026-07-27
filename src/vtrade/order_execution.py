"""Shared orchestration for immediate and post-harness paper-order execution.

Database reads are supplied by the production state adapter.  Keeping this
service independent from ``worker.py`` lets the tool path and recovery broker
path use the identical validation, simulation, and persistence sequence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from vtrade.broker import (
    ExecutionResult,
    LiquidityTimeInForce,
    OrderAmountType,
    PredictionArenaPaperBroker,
)
from vtrade.domain.types import MicroDollars
from vtrade.runtime import CycleClaim


class OrderExecutionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    result: ExecutionResult
    order_id: uuid.UUID
    snapshot_id: uuid.UUID


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


class _TradingState(Protocol):
    def pending_intents(self, claim: CycleClaim, frozen: Mapping[str, object]) -> Sequence[Any]: ...

    def portfolio(self, agent_id: uuid.UUID) -> Any: ...

    def executable_bids(
        self,
        portfolio: Any,
        *,
        cutoff: datetime,
        order_book_snapshot_ids: Sequence[uuid.UUID],
    ) -> Mapping[str, Any]: ...


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
        snapshot_id: uuid.UUID,
    ) -> Any: ...



class MarketOrderExecutor:
    """Execute one frozen intent through the common paper-broker pipeline."""

    def __init__(
        self,
        state: _TradingState,
        market_repository: _MarketRepository,
        repository: _ExecutionRepository,
        *,
        broker: PredictionArenaPaperBroker,
        clock: Callable[[], datetime],
    ) -> None:
        self._state = state
        self._market_repository = market_repository
        self._repository = repository
        self._broker = broker
        self._clock = clock

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
        """Create the append-only intent and its financial result atomically."""
        locked_cursor = getattr(self._state, "locked_cursor", None)
        insert_intent = getattr(self._state, "insert_intent", None)
        if locked_cursor is None or insert_intent is None:
            raise OrderExecutionUnavailable("immediate submission requires transactional state")
        with locked_cursor(claim.agent_id) as cursor:
            insert_intent(cursor, claim, submission)
            return self._execute_locked(claim, frozen, submission, cursor)

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
        fee_ids = _uuid_membership(frozen, "fee_rate_snapshot_ids")
        book_ids = _uuid_membership(frozen, "order_book_snapshot_ids")
        if not fee_ids:
            raise OrderExecutionUnavailable("cycle has no frozen fee-rate membership")
        pending = self._state.pending_intents(claim, frozen)
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
                locked_pending = state.pending_intents(claim, frozen, cursor=cursor)
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
                result = self._place(order, item, portfolio, bids, fee)
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
        pending = state.pending_intents(claim, frozen, cursor=cursor)
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
        result = self._place(order, item, portfolio, bids, fee)
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

    def _place(self, order: Any, item: Any, portfolio: Any, bids: Any, fee: Any) -> ExecutionResult:
        return self._broker.place(
            order,
            market=item.market,
            outcome=item.outcome,
            snapshot=item.book,
            portfolio=portfolio,
            executable_bids=bids,
            fee_policy=fee,
            now=_aware(self._clock()),
        )


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


def _cutoff(claim: CycleClaim) -> datetime:
    if claim.data_cutoff is None:
        raise OrderExecutionUnavailable("cycle cutoff is not finalized")
    return _aware(claim.data_cutoff)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production timestamps must be timezone-aware")
    return value
