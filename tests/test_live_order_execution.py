from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from vtrade.broker import (
    ExecutionResult,
    ExecutionStatus,
    FeePolicy,
    LiquidityTimeInForce,
    OrderAmountType,
    PaperOrder,
    PaperPolicy,
    PortfolioState,
    PositionState,
    PredictionArenaPaperBroker,
    RejectionCode,
)
from vtrade.domain.types import (
    MarketStatus,
    MicroDollars,
    OrderBookSnapshot,
    PriceLevel,
    RawArtifact,
    Side,
)
from vtrade.order_execution import (
    ExecutionReceipt,
    LiveContextError,
    LiveExecutionAttempt,
    LiveOrderContext,
    MarketOrderExecutor,
    MarketOrderSubmission,
    ValidatedLiveOrderContextProvider,
)
from vtrade.production_tools import _execution_output

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def submission() -> MarketOrderSubmission:
    return MarketOrderSubmission(
        intent_id=uuid.uuid4(),
        market_id=uuid.uuid4(),
        outcome_id=uuid.uuid4(),
        side="BUY",
        amount_micros=1_000_000,
        shares=Decimal(2),
        confidence=Decimal("0.5"),
        created_at=NOW,
        amount_type=OrderAmountType.SHARES,
        cash_budget_micros=None,
        limit_price=None,
        time_in_force=LiquidityTimeInForce.IOC,
    )


def live_context(
    requested_at: datetime,
    *,
    market_observed_at: datetime | None = None,
    book_observed_at: datetime | None = None,
    fee_observed_at: datetime | None = None,
) -> LiveOrderContext:
    return LiveOrderContext(
        market=SimpleNamespace(),
        outcome=SimpleNamespace(),
        book=SimpleNamespace(
            token_id="token",
            best_ask=Decimal("0.40"),
        ),
        fee_policy=SimpleNamespace(),
        market_snapshot_id=uuid.uuid4(),
        book_snapshot_id=uuid.uuid4(),
        fee_rate_snapshot_id=uuid.uuid4(),
        requested_at=requested_at,
        validated_at=requested_at,
        market_observed_at=market_observed_at or requested_at,
        book_observed_at=book_observed_at or requested_at,
        fee_observed_at=fee_observed_at or requested_at,
    )


def test_live_context_accepts_observations_after_agent_decision() -> None:
    provider = ValidatedLiveOrderContextProvider(
        lambda _submission, requested_at: live_context(requested_at),
        clock=lambda: NOW + timedelta(seconds=1),
    )

    result = provider.build(submission(), requested_at=NOW - timedelta(seconds=5))

    assert result.validated_at == NOW + timedelta(seconds=1)
    assert result.requested_at == NOW - timedelta(seconds=5)


def test_live_context_rejects_six_second_source_skew() -> None:
    persisted = 0

    def persist(context: LiveOrderContext) -> LiveOrderContext:
        nonlocal persisted
        persisted += 1
        return context

    provider = ValidatedLiveOrderContextProvider(
        lambda _submission, requested_at: live_context(
            requested_at,
            market_observed_at=NOW,
            book_observed_at=NOW + timedelta(seconds=6),
            fee_observed_at=NOW,
        ),
        clock=lambda: NOW + timedelta(seconds=10),
        persist=persist,
    )

    with pytest.raises(LiveContextError) as error:
        provider.build(submission(), requested_at=NOW)

    assert error.value.code is RejectionCode.INCONSISTENT_LIVE_CONTEXT
    assert persisted == 0


def test_live_context_rejects_expired_build_and_stale_data() -> None:
    expired = ValidatedLiveOrderContextProvider(
        lambda _submission, requested_at: live_context(requested_at),
        clock=lambda: NOW,
        monotonic=iter((0.0, 10.1)).__next__,
    )
    with pytest.raises(LiveContextError) as expired_error:
        expired.build(submission(), requested_at=NOW)
    assert expired_error.value.code is RejectionCode.LIVE_CONTEXT_EXPIRED

    stale = ValidatedLiveOrderContextProvider(
        lambda _submission, requested_at: live_context(requested_at),
        clock=lambda: NOW + timedelta(seconds=301),
        maximum_build_time=timedelta(seconds=400),
    )
    with pytest.raises(LiveContextError) as stale_error:
        stale.build(submission(), requested_at=NOW)
    assert stale_error.value.code is RejectionCode.STALE_LIVE_DATA


def test_live_refresh_retries_network_once_then_validates() -> None:
    calls = 0

    class Provider:
        def build(self, _submission, *, requested_at):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LiveContextError(
                    "temporary transport error",
                    code=RejectionCode.NETWORK_ERROR,
                    retryable=True,
                )
            return live_context(requested_at)

    clock_values = iter((NOW, NOW, NOW, NOW))
    executor = MarketOrderExecutor(
        object(),
        object(),
        object(),
        broker=PredictionArenaPaperBroker(policy=PaperPolicy.LIQUIDITY_AWARE),
        clock=clock_values.__next__,
        live_context_provider=Provider(),
    )

    context, attempts, error_code = executor._build_live_context(
        submission(), requested_at=NOW
    )

    assert context is not None
    assert error_code is None
    assert calls == 2
    assert [attempt.status for attempt in attempts] == ["failed", "validated"]
    assert attempts[0].error_code == RejectionCode.NETWORK_ERROR.value


def test_live_buy_rejects_missing_historical_bid_without_mutation() -> None:
    broker = PredictionArenaPaperBroker(policy=PaperPolicy.LIQUIDITY_AWARE)
    position = PositionState(
        market_id="market",
        outcome_id="outcome",
        shares=Decimal(1),
        average_cost=Decimal("0.4"),
        cost_basis_micros=MicroDollars(400_000),
    )
    portfolio = PortfolioState(
        "agent",
        MicroDollars(9_600_000),
        positions=(position,),
    )
    order = PaperOrder(
        "order",
        "agent",
        "market",
        "outcome",
        Side.BUY,
        Decimal(1),
        NOW - timedelta(seconds=10),
    )
    outcome = SimpleNamespace(
        id="outcome",
        market_id="market",
        venue_token_id="token",
        tradeable=True,
    )
    market = SimpleNamespace(
        id="market",
        status=MarketStatus.OPEN,
        opens_at=None,
        closes_at=None,
        tradeable=True,
        observed_at=NOW,
    )
    snapshot = OrderBookSnapshot(
        token_id="token",
        condition_id="condition",
        observed_at=NOW,
        source_created_at=NOW,
        bids=(PriceLevel(Decimal("0.39"), Decimal(10)),),
        asks=(PriceLevel(Decimal("0.40"), Decimal(10)),),
        tick_size=Decimal("0.01"),
        minimum_order_size=Decimal(1),
        negative_risk=False,
        artifact=RawArtifact("a" * 64, 1, "memory://book"),
    )

    result = broker.place(
        order,
        market=market,
        outcome=outcome,
        snapshot=snapshot,
        portfolio=portfolio,
        executable_bids={},
        fee_policy=FeePolicy(Decimal(0)),
        now=NOW,
        live_context=True,
        valuation_as_of=NOW,
    )

    assert result.status is ExecutionStatus.REJECTED
    assert result.rejection_code is RejectionCode.NO_BID_VALUATION
    assert result.portfolio == portfolio


def test_pre_context_rejection_omits_snapshot_and_exposes_attempts_only() -> None:
    order = PaperOrder(
        "order",
        "agent",
        "market",
        "outcome",
        Side.BUY,
        Decimal(1),
        NOW,
    )
    portfolio = PortfolioState("agent", MicroDollars(1_000_000))
    result = ExecutionResult(
        order=order,
        policy=PaperPolicy.LIQUIDITY_AWARE,
        status=ExecutionStatus.REJECTED,
        fills=(),
        rejection_code=RejectionCode.NETWORK_ERROR,
        portfolio_before=portfolio,
        portfolio=portfolio,
        ledger_entries=(),
        snapshot=None,
        fee_policy=None,
        executed_at=NOW,
    )
    receipt = ExecutionReceipt(
        result,
        uuid.uuid4(),
        None,
        (
            LiveExecutionAttempt(
                1,
                "failed",
                NOW,
                NOW + timedelta(milliseconds=1),
                RejectionCode.NETWORK_ERROR.value,
            ),
        ),
    )

    output = _execution_output(
        receipt,
        intent_id=uuid.uuid4(),
        requested_amount=Decimal(1),
        amount_type=OrderAmountType.SHARES,
    )

    assert "snapshot" not in output
    assert output["rejection_code"] == RejectionCode.NETWORK_ERROR.value
    assert output["attempts"] == [{"attempt": 1, "error": "network_error"}]
