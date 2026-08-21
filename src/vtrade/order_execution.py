"""Shared semantic paper-execution context orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic as _monotonic

from vtrade.broker import BinaryPaperBroker
from vtrade.domain.execution import (
    FeePolicySnapshot,
    OrderRequest,
    OrderResult,
    SemanticExecutionError,
    operation_uuid,
)
from vtrade.domain.types import MarketContext
from vtrade.portfolio import ContractPortfolio


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


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



__all__ = [
    "FrozenDecisionContext",
    "PaperOrderExecutor",
    "RefreshedExecutionContext",
    "SemanticContextRefreshError",
    "SemanticOrderExecutor",
    "SharedOrderExecutor",
]

