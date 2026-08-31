from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from vtrade.broker_repository import OrderIntentReservation
from vtrade.domain.execution import (
    FeePolicySnapshot,
    OrderAmountType,
    OrderRequest,
    OrderResult,
    OrderState,
    ReconciliationState,
    SemanticExecutionError,
    SubmissionState,
)
from vtrade.domain.ports import FreshExecutionContextError
from vtrade.domain.types import (
    BinaryMarket,
    BinaryOutcome,
    EventKey,
    MarketContext,
    MarketKey,
    MarketStatus,
    MoneyMicros,
    OutcomeKey,
    OutcomeSide,
    PriceGrid,
    RawArtifact,
    SeriesKey,
    build_canonical_order_book,
)
from vtrade.portfolio import ContractPortfolio
from vtrade.runtime import AlertEvent, CycleClaim
from vtrade.semantic_runtime import (
    ProductionSemanticOrderExecutor,
    ProductionSemanticReconciliationPort,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
AGENT_ID = uuid.uuid4()
CYCLE_ID = uuid.uuid4()
MARKET_REF = "KXTEST-27"


def market_context(
    cutoff: datetime, *, no_bid: str, second_no_bid: str = "0.59", yes_bid: str = "0.39"
) -> MarketContext:
    market_key = MarketKey(MARKET_REF)
    grid = PriceGrid.from_ranges([{"start": "0.00", "end": "1.00", "step": "0.01"}])
    outcomes = (
        BinaryOutcome(OutcomeKey(market_key, OutcomeSide.YES), "YES", True),
        BinaryOutcome(OutcomeKey(market_key, OutcomeSide.NO), "NO", True),
    )
    market = BinaryMarket(
        market_key,
        SeriesKey("SERIES-27"),
        EventKey("EVENT-27"),
        "Will the issue 27 test resolve YES?",
        "Resolve from the official source.",
        None,
        NOW - timedelta(days=1),
        NOW + timedelta(days=1),
        NOW + timedelta(days=1),
        NOW + timedelta(days=1),
        MarketStatus.ACTIVE,
        True,
        grid,
        outcomes,
        cutoff - timedelta(seconds=1),
        RawArtifact("a" * 64, 1, "memory://issue-27", observed_at=cutoff - timedelta(seconds=1)),
    )
    levels = [[no_bid, "1.00"], [second_no_bid, "1.00"]]
    levels.extend([[f"0.{58 - index:02d}", "1.00"] for index in range(4)])
    book = build_canonical_order_book(
        market_key,
        grid,
        [[yes_bid, "10.00"]],
        levels,
        observed_at=cutoff - timedelta(seconds=1),
        cutoff=cutoff,
        artifact=RawArtifact(
            "b" * 64, 1, "memory://issue-27-book", observed_at=cutoff - timedelta(seconds=1)
        ),
    )
    return MarketContext(market, book)


def request(created_at: datetime, *, key: str = "issue-27-order-1") -> OrderRequest:
    return OrderRequest(
        agent_id=str(AGENT_ID),
        market_ref=MARKET_REF,
        outcome=OutcomeSide.YES,
        action="BUY",
        amount=100,
        amount_type=OrderAmountType.CONTRACTS,
        idempotency_key=key,
        frozen_cutoff=NOW,
        created_at=created_at,
    )


class FakeProvider:
    def __init__(self, context: MarketContext) -> None:
        self.context = context
        self.calls = 0

    def get_fresh_execution_context(
        self, _market_key: MarketKey, *, deadline: float | None = None
    ) -> MarketContext:
        assert deadline is not None
        self.calls += 1
        return self.context


class FakeRepository:
    def __init__(self, market_id: uuid.UUID, outcome_id: uuid.UUID) -> None:
        self.market_id = market_id
        self.outcome_id = outcome_id
        self.operation_id = uuid.uuid4()
        self.persisted: list[tuple[OrderResult, dict[str, object]]] = []
        self.replay: OrderResult | None = None
        self.replays: dict[str, OrderResult] = {}

    def prepare_order_intent(
        self, request: OrderRequest, **_kwargs: object
    ) -> OrderIntentReservation:
        replay = self.replays.get(request.idempotency_key)
        if replay is not None:
            return OrderIntentReservation(
                self.operation_id,
                self.market_id,
                self.outcome_id,
                False,
                existing_result=replay,
            )
        return OrderIntentReservation(self.operation_id, self.market_id, self.outcome_id, True)

    def persist_order_result(self, result: OrderResult, **kwargs: object) -> None:
        self.persisted.append((result, kwargs))
        self.replay = result
        self.replays[result.request.idempotency_key] = result

class FailingProvider:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def get_fresh_execution_context(
        self, _market_key: MarketKey, *, deadline: float | None = None
    ) -> MarketContext:
        assert deadline is not None
        self.calls += 1
        raise self.error


def test_production_order_uses_fresh_prices_and_strictly_replays_without_refresh() -> None:
    claim = CycleClaim(
        CYCLE_ID,
        AGENT_ID,
        NOW,
        NOW,
        "worker-27",
        NOW + timedelta(hours=1),
    )
    frozen = market_context(NOW, no_bid="0.60")
    fresh = market_context(
        NOW + timedelta(minutes=1), no_bid="0.65", second_no_bid="0.64", yes_bid="0.20"
    )
    provider = FakeProvider(fresh)
    market_key = MarketKey(MARKET_REF)
    repository = FakeRepository(market_key.stable_id, OutcomeKey(market_key, "YES").stable_id)
    portfolio = ContractPortfolio(str(AGENT_ID), MoneyMicros(100_000_000))

    def clock() -> datetime:
        return NOW + timedelta(minutes=1)

    with (
        patch("vtrade.semantic_runtime.PostgresBrokerRepository", return_value=repository),
        patch("vtrade.semantic_runtime.load_contract_portfolio", return_value=portfolio),
        patch("vtrade.semantic_runtime.frozen_context", return_value=frozen),
    ):
        executor = ProductionSemanticOrderExecutor(
            "postgresql://unused",
            clock=clock,
            maximum_book_age=timedelta(minutes=5),
            maximum_market_fraction=Decimal("0.15"),
            execution_context_provider=provider,
        )
        with patch.object(
            executor,
            "_fee_policy",
            return_value=FeePolicySnapshot(as_of=NOW, cutoff=NOW),
        ):
            first = executor.submit_and_execute(
                claim,
                {"contexts": [{"market_ref": MARKET_REF}]},
                request(clock()),
            )
            second = executor.submit_and_execute(
                claim,
                {"contexts": [{"market_ref": MARKET_REF}]},
                request(clock()),
            )

    assert first.fills[0].price_micros == 360_000
    assert second == repository.replay
    assert provider.calls == 1
    assert len(repository.persisted) == 1


def test_new_idempotency_key_refreshes_and_can_observe_new_prices() -> None:
    claim = CycleClaim(
        CYCLE_ID,
        AGENT_ID,
        NOW,
        NOW,
        "worker-27",
        NOW + timedelta(hours=1),
    )
    frozen = market_context(NOW, no_bid="0.60")
    first_fresh = market_context(
        NOW + timedelta(minutes=1), no_bid="0.65", second_no_bid="0.64", yes_bid="0.20"
    )
    later_fresh = market_context(
        NOW + timedelta(minutes=2), no_bid="0.70", second_no_bid="0.69", yes_bid="0.20"
    )
    provider = FakeProvider(first_fresh)
    market_key = MarketKey(MARKET_REF)
    repository = FakeRepository(market_key.stable_id, OutcomeKey(market_key, "YES").stable_id)
    portfolio = ContractPortfolio(str(AGENT_ID), MoneyMicros(100_000_000))

    with (
        patch("vtrade.semantic_runtime.PostgresBrokerRepository", return_value=repository),
        patch("vtrade.semantic_runtime.load_contract_portfolio", return_value=portfolio),
        patch("vtrade.semantic_runtime.frozen_context", return_value=frozen),
    ):
        executor = ProductionSemanticOrderExecutor(
            "postgresql://unused",
            clock=lambda: NOW + timedelta(minutes=2),
            maximum_book_age=timedelta(minutes=5),
            maximum_market_fraction=Decimal("0.15"),
            execution_context_provider=provider,
        )
        with patch.object(
            executor,
            "_fee_policy",
            return_value=FeePolicySnapshot(as_of=NOW, cutoff=NOW),
        ):
            first = executor.submit_and_execute(
                claim,
                {"contexts": [{"market_ref": MARKET_REF}]},
                request(NOW + timedelta(minutes=1), key="issue-27-first"),
            )
            provider.context = later_fresh
            second = executor.submit_and_execute(
                claim,
                {"contexts": [{"market_ref": MARKET_REF}]},
                request(NOW + timedelta(minutes=2), key="issue-27-second"),
            )

    assert first.fills[0].price_micros == 360_000
    assert second.fills[0].price_micros == 310_000
    assert provider.calls == 2
    assert len(repository.persisted) == 2


def test_refresh_failures_are_classified_and_marked_not_submitted() -> None:
    claim = CycleClaim(
        CYCLE_ID,
        AGENT_ID,
        NOW,
        NOW,
        "worker-27",
        NOW + timedelta(hours=1),
    )
    frozen = market_context(NOW, no_bid="0.60")
    market_key = MarketKey(MARKET_REF)
    portfolio = ContractPortfolio(str(AGENT_ID), MoneyMicros(100_000_000))

    terminal_provider = FailingProvider(
        FreshExecutionContextError(
            "book is too old", retryable=False, error_code="STALE_BOOK"
        )
    )
    terminal_repository = FakeRepository(
        market_key.stable_id, OutcomeKey(market_key, "YES").stable_id
    )
    with (
        patch(
            "vtrade.semantic_runtime.PostgresBrokerRepository",
            return_value=terminal_repository,
        ),
        patch("vtrade.semantic_runtime.load_contract_portfolio", return_value=portfolio),
        patch("vtrade.semantic_runtime.frozen_context", return_value=frozen),
    ):
        terminal_executor = ProductionSemanticOrderExecutor(
            "postgresql://unused",
            clock=lambda: NOW + timedelta(minutes=1),
            maximum_book_age=timedelta(minutes=5),
            maximum_market_fraction=Decimal("0.15"),
            execution_context_provider=terminal_provider,
        )
        terminal = terminal_executor.submit_and_execute(
            claim,
            {"contexts": [{"market_ref": MARKET_REF}]},
            request(NOW + timedelta(minutes=1), key="issue-27-terminal"),
        )

    assert terminal.state is OrderState.REJECTED
    assert terminal.reconciliation_state is ReconciliationState.NOT_REQUIRED
    assert terminal.error_code is SemanticExecutionError.STALE_BOOK
    assert terminal.submission_state is SubmissionState.NOT_SUBMITTED
    assert terminal.reconciliation_evidence["venue_submission_occurred"] is False

    pending_provider = FailingProvider(
        FreshExecutionContextError("refresh timed out", retryable=True)
    )
    pending_repository = FakeRepository(
        market_key.stable_id, OutcomeKey(market_key, "YES").stable_id
    )
    with (
        patch(
            "vtrade.semantic_runtime.PostgresBrokerRepository",
            return_value=pending_repository,
        ),
        patch("vtrade.semantic_runtime.load_contract_portfolio", return_value=portfolio),
        patch("vtrade.semantic_runtime.frozen_context", return_value=frozen),
    ):
        pending_executor = ProductionSemanticOrderExecutor(
            "postgresql://unused",
            clock=lambda: NOW + timedelta(minutes=1),
            maximum_book_age=timedelta(minutes=5),
            maximum_market_fraction=Decimal("0.15"),
            execution_context_provider=pending_provider,
        )
        pending = pending_executor.submit_and_execute(
            claim,
            {"contexts": [{"market_ref": MARKET_REF}]},
            request(NOW + timedelta(minutes=1), key="issue-27-pending"),
        )

    assert pending.state is OrderState.PENDING
    assert pending.reconciliation_state is ReconciliationState.REQUIRED
    assert pending.submission_state is SubmissionState.NOT_SUBMITTED
    assert pending.reconciliation_evidence["venue_submission_occurred"] is False


def test_future_fresh_evidence_rejects_without_persisting_an_invalid_context() -> None:
    claim = CycleClaim(
        CYCLE_ID,
        AGENT_ID,
        NOW,
        NOW,
        "worker-27",
        NOW + timedelta(hours=1),
    )
    frozen = market_context(NOW, no_bid="0.60")
    future = market_context(NOW + timedelta(hours=1), no_bid="0.60")
    provider = FakeProvider(future)
    market_key = MarketKey(MARKET_REF)
    repository = FakeRepository(
        market_key.stable_id, OutcomeKey(market_key, "YES").stable_id
    )
    portfolio = ContractPortfolio(str(AGENT_ID), MoneyMicros(100_000_000))

    with (
        patch("vtrade.semantic_runtime.PostgresBrokerRepository", return_value=repository),
        patch("vtrade.semantic_runtime.load_contract_portfolio", return_value=portfolio),
        patch("vtrade.semantic_runtime.frozen_context", return_value=frozen),
    ):
        executor = ProductionSemanticOrderExecutor(
            "postgresql://unused",
            clock=lambda: NOW + timedelta(minutes=1),
            maximum_book_age=timedelta(minutes=5),
            maximum_market_fraction=Decimal("0.15"),
            execution_context_provider=provider,
        )
        result = executor.submit_and_execute(
            claim,
            {"contexts": [{"market_ref": MARKET_REF}]},
            request(NOW + timedelta(minutes=1), key="issue-27-future"),
        )

    assert result.state is OrderState.REJECTED
    assert result.error_code is SemanticExecutionError.INVALID_CONTEXT
    assert result.execution_context_id is None
    assert result.submission_state is SubmissionState.NOT_SUBMITTED
    assert result.portfolio_before == portfolio
    assert result.portfolio_after == portfolio


def test_reconciliation_port_alerts_and_blocks_on_unresolved_operations() -> None:
    class BrokerRepository:
        def reconcile_not_submitted_operations(self, *, now: datetime) -> tuple[uuid.UUID, ...]:
            assert now == NOW
            return (AGENT_ID,)

    class RuntimeRepository:
        def __init__(self) -> None:
            self.alerts: list[AlertEvent] = []

        def open_alert(self, alert: AlertEvent) -> None:
            self.alerts.append(alert)

    broker_repository = BrokerRepository()
    runtime_repository = RuntimeRepository()
    with (
        patch(
            "vtrade.semantic_runtime.PostgresBrokerRepository",
            return_value=broker_repository,
        ),
        patch(
            "vtrade.semantic_runtime.PostgresRuntimeRepository",
            return_value=runtime_repository,
        ),
    ):
        reconciliation = ProductionSemanticReconciliationPort(
            "postgresql://unused", clock=lambda: NOW
        )
        try:
            reconciliation.reconcile_before_cycle(now=NOW)
        except RuntimeError as exc:
            assert str(exc) == "unresolved order reconciliation blocks the cycle"
        else:
            raise AssertionError("unresolved operations must block the cycle")

    assert len(runtime_repository.alerts) == 1
    alert = runtime_repository.alerts[0]
    assert alert.severity == "critical"
    assert alert.code == "order_reconciliation_required"
    assert alert.agent_id == AGENT_ID


def test_reconciliation_port_alerts_when_repository_reconciliation_fails() -> None:
    class BrokerRepository:
        def reconcile_not_submitted_operations(self, *, now: datetime) -> tuple[uuid.UUID, ...]:
            del now
            raise RuntimeError("database unavailable")

    class RuntimeRepository:
        def __init__(self) -> None:
            self.alerts: list[AlertEvent] = []

        def open_alert(self, alert: AlertEvent) -> None:
            self.alerts.append(alert)

    broker_repository = BrokerRepository()
    runtime_repository = RuntimeRepository()
    with (
        patch(
            "vtrade.semantic_runtime.PostgresBrokerRepository",
            return_value=broker_repository,
        ),
        patch(
            "vtrade.semantic_runtime.PostgresRuntimeRepository",
            return_value=runtime_repository,
        ),
    ):
        reconciliation = ProductionSemanticReconciliationPort(
            "postgresql://unused", clock=lambda: NOW
        )
        try:
            reconciliation.reconcile_before_cycle(now=NOW)
        except RuntimeError as exc:
            assert str(exc) == "automatic order reconciliation failed"
        else:
            raise AssertionError("reconciliation failure must block the cycle")

    assert len(runtime_repository.alerts) == 1
    alert = runtime_repository.alerts[0]
    assert alert.severity == "critical"
    assert alert.code == "order_reconciliation_failed"
    assert alert.agent_id is None
