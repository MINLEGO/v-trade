from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vtrade.broker import (
    BinaryPaperBroker,
    BinarySettlementEngine,
    ExactFeeCalculator,
    SettlementBlockedError,
)
from vtrade.domain.execution import (
    FeeParticipantRole,
    FeePolicySnapshot,
    OrderAmountType,
    OrderRequest,
    OrderState,
    SemanticExecutionError,
    TimeInForce,
)
from vtrade.domain.types import (
    BinaryMarket,
    BinaryOutcome,
    EventKey,
    MarketContext,
    MarketKey,
    MarketStatus,
    OutcomeKey,
    OutcomeSide,
    PriceGrid,
    RawArtifact,
    ResolutionObservation,
    SeriesKey,
    build_canonical_order_book,
)
from vtrade.liquidity import LiquidityEvidenceError, apply_best_level_haircut
from vtrade.live_execution import DisabledRealExecutionAdapter, RealExecutionDisabled
from vtrade.order_execution import (
    FrozenDecisionContext,
    RefreshedExecutionContext,
    SemanticOrderExecutor,
)
from vtrade.portfolio import ContractPortfolio, ContractPosition, replay_contract_portfolio

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
ARTIFACT = RawArtifact("a" * 64, 1, "memory://issue-14")


def context(*, best_quantity: str = "1.00") -> MarketContext:
    market_key = MarketKey("KXTEST-14")
    grid = PriceGrid.from_ranges([{"start": "0.00", "end": "1.00", "step": "0.01"}])
    outcomes = (
        BinaryOutcome(OutcomeKey(market_key, OutcomeSide.YES), "YES", True),
        BinaryOutcome(OutcomeKey(market_key, OutcomeSide.NO), "NO", True),
    )
    market = BinaryMarket(
        key=market_key,
        series_key=SeriesKey("SERIES-14"),
        event_key=EventKey("EVENT-14"),
        question="Will the binary event resolve YES?",
        resolution_rules="Official source.",
        resolution_source=None,
        open_time=NOW - timedelta(days=1),
        close_time=NOW + timedelta(days=1),
        expected_expiration_time=NOW + timedelta(days=1),
        latest_expiration_time=NOW + timedelta(days=1),
        status=MarketStatus.ACTIVE,
        eligible=True,
        price_grid=grid,
        outcomes=outcomes,
        observed_at=NOW - timedelta(seconds=2),
        audit=ARTIFACT,
    )
    no_bids = [
        ["0.60", best_quantity],
        ["0.59", "1.00"],
        ["0.58", "1.00"],
        ["0.57", "1.00"],
        ["0.56", "1.00"],
        ["0.55", "1.00"],
    ]
    book = build_canonical_order_book(
        market_key,
        grid,
        [["0.39", "10.00"]],
        no_bids,
        observed_at=NOW - timedelta(seconds=1),
        cutoff=NOW,
        artifact=ARTIFACT,
    )
    return MarketContext(market, book)


def request(
    *,
    amount: int = 200,
    tif: TimeInForce = TimeInForce.IOC,
    key: str = "order-14",
) -> OrderRequest:
    return OrderRequest(
        agent_id="agent-14",
        market_ref=MarketKey("KXTEST-14"),
        outcome=OutcomeSide.YES,
        action="BUY",
        amount=amount,
        amount_type=OrderAmountType.CONTRACTS,
        idempotency_key=key,
        time_in_force=tif,
        frozen_cutoff=NOW,
        created_at=NOW,
    )


def fees(role: FeeParticipantRole = FeeParticipantRole.TAKER) -> FeePolicySnapshot:
    return FeePolicySnapshot(participant_role=role, as_of=NOW, cutoff=NOW)


def test_haircut_rejects_best_level_that_breaks_the_retained_depth_floor() -> None:
    with pytest.raises(LiquidityEvidenceError, match="50%"):
        apply_best_level_haircut(
            context(best_quantity="6.00").order_book,
            outcome=OutcomeSide.YES,
            action="BUY",
        )


def test_paper_ioc_uses_next_five_levels_and_applies_atomic_accounting() -> None:
    portfolio = ContractPortfolio("agent-14", 100_000_000)
    result = BinaryPaperBroker().execute(
        request(amount=200),
        context=context(),
        portfolio=portfolio,
        fee_policy=fees(),
        frozen_context_id="freeze-14",
        execution_context_id="execution-14",
        now=NOW,
    )

    assert result.state is OrderState.FILLED
    assert result.filled_units == 200
    assert [fill.price_micros for fill in result.fills] == [410_000, 420_000]
    assert result.portfolio_after is not None
    assert result.portfolio_after.cash_micros < portfolio.cash_micros
    assert result.liquidity_audit.ignored_quantity_units == 100
    assert result.liquidity_audit.effective_quantity_units == 500
    assert result.net_cash_delta_micros == result.gross_cash_delta_micros - result.fee_micros
    replayed = replay_contract_portfolio(portfolio, result.ledger_entries)
    assert replayed.cash_micros == result.portfolio_after.cash_micros
    assert replayed.positions == result.portfolio_after.positions


def test_fok_shortfall_is_rejected_without_portfolio_or_ledger_mutation() -> None:
    portfolio = ContractPortfolio("agent-14", 100_000_000)
    result = BinaryPaperBroker().execute(
        request(amount=600, tif=TimeInForce.FOK, key="fok-14"),
        context=context(),
        portfolio=portfolio,
        fee_policy=fees(),
        now=NOW,
    )

    assert result.state is OrderState.REJECTED
    assert result.error_code is SemanticExecutionError.INSUFFICIENT_LIQUIDITY
    assert result.portfolio_after == portfolio
    assert result.ledger_entries == ()


def test_fee_formula_and_role_are_exact() -> None:
    calculator = ExactFeeCalculator()
    taker = calculator.calculate(
        contract_units=10_000,
        price_micros=500_000,
        policy=fees(),
        action="BUY",
    )
    maker = calculator.calculate(
        contract_units=10_000,
        price_micros=500_000,
        policy=fees(FeeParticipantRole.MAKER),
        action="BUY",
    )
    assert taker.trade_fee_micros == 1_750_000
    assert maker.trade_fee_micros == 437_500
    assert taker.net_fee_micros >= 0


def test_pending_blocks_new_orders_and_finalized_settlement_is_idempotent() -> None:
    broker = BinaryPaperBroker()
    portfolio = ContractPortfolio(
        "agent-14",
        100_000_000,
        positions=(ContractPosition(MarketKey("KXTEST-14"), OutcomeSide.YES, 100, 40_000_000),),
    )
    pending = broker.execute(
        request(amount=100, key="pending-14"),
        context=None,
        portfolio=portfolio,
        fee_policy=fees(),
        now=NOW,
    )
    assert pending.state is OrderState.PENDING
    assert pending.submission_state.value == "NOT_SUBMITTED"
    assert pending.reconciliation_evidence["venue_submission_occurred"] is False
    blocked = broker.execute(
        request(amount=100, key="blocked-14"),
        context=context(),
        portfolio=portfolio,
        fee_policy=fees(),
        now=NOW,
    )
    assert blocked.error_code is SemanticExecutionError.RECONCILIATION_REQUIRED

    observation = ResolutionObservation(
        MarketKey("KXTEST-14"),
        MarketStatus.FINALIZED,
        OutcomeSide.YES,
        NOW - timedelta(minutes=1),
        NOW - timedelta(minutes=1),
        NOW - timedelta(seconds=30),
        ARTIFACT,
    )
    engine = BinarySettlementEngine()
    settled, record = engine.settle(
        observation=observation,
        position=portfolio.positions[0],
        portfolio=portfolio,
        as_of=NOW,
        settled_at=NOW,
    )
    repeated, same_record = engine.settle(
        observation=observation,
        position=portfolio.positions[0],
        portfolio=portfolio,
        as_of=NOW,
        settled_at=NOW,
    )
    assert settled == repeated
    assert record == same_record
    assert record.gross_payout_micros == 1_000_000
    assert settled.positions == ()

    determined = ResolutionObservation(
        MarketKey("KXTEST-14"),
        MarketStatus.DETERMINED,
        OutcomeSide.YES,
        NOW - timedelta(minutes=1),
        NOW - timedelta(minutes=1),
        None,
        ARTIFACT,
    )
    with pytest.raises(SettlementBlockedError):
        engine.settle(
            observation=determined,
            position=portfolio.positions[0],
            portfolio=portfolio,
            as_of=NOW,
            settled_at=NOW,
        )


def test_executor_refreshes_context_and_turns_transport_ambiguity_into_pending() -> None:
    frozen = FrozenDecisionContext("freeze-14", context(), NOW)
    executor = SemanticOrderExecutor(
        BinaryPaperBroker(),
        lambda _request, _frozen: RefreshedExecutionContext(
            "execution-14", context(), NOW
        ),
        fees(),
        clock=lambda: NOW,
    )
    filled = executor.submit(
        request(key="executor-filled"),
        frozen_context=frozen,
        portfolio=ContractPortfolio("agent-14", 100_000_000),
    )
    assert filled.state is OrderState.FILLED
    assert filled.frozen_context_id == "freeze-14"
    assert filled.execution_context_id == "execution-14"

    pending_executor = SemanticOrderExecutor(
        BinaryPaperBroker(),
        lambda _request, _frozen: (_ for _ in ()).throw(ConnectionError("ambiguous")),
        fees(),
        clock=lambda: NOW,
    )
    pending = pending_executor.submit(
        request(key="executor-pending"),
        frozen_context=frozen,
        portfolio=ContractPortfolio("agent-14", 100_000_000),
    )
    assert pending.state is OrderState.PENDING


def test_real_execution_boundary_is_unconditionally_disabled() -> None:
    with pytest.raises(RealExecutionDisabled):
        DisabledRealExecutionAdapter().submit("order")
