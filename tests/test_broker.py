from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from vtrade.broker import (
    ArchivedBid,
    ExecutionStatus,
    FeePolicy,
    LiquidityTimeInForce,
    PaperOrder,
    PaperPolicy,
    PendingOrder,
    PortfolioState,
    PositionState,
    PredictionArenaPaperBroker,
    RejectionCode,
    SettlementEngine,
    SettlementObservation,
    SnapshotValuationBlocked,
    initial_capital_entry,
    replay_portfolio,
)
from vtrade.domain.types import (
    Market,
    MarketStatus,
    MicroDollars,
    OrderBookSnapshot,
    Outcome,
    PriceLevel,
    RawArtifact,
    Side,
)

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
ARTIFACT = RawArtifact("a" * 64, 1, "memory://book")


def market_and_outcome(*, tradeable: bool = True) -> tuple[Market, Outcome]:
    outcome = Outcome(
        id="outcome-yes",
        market_id="market-1",
        name="Yes",
        venue_token_id="token-yes",
        best_bid_micros=None,
        best_ask_micros=None,
        tick_size_micros=MicroDollars(10_000),
        minimum_order_micros=MicroDollars(1_000_000),
        tradeable=tradeable,
    )
    market = Market(
        id="market-1",
        venue_id="1",
        event_id="event-1",
        question="Will it happen?",
        resolution_rules="Official source.",
        opens_at=NOW - timedelta(days=1),
        closes_at=NOW + timedelta(days=1),
        status=MarketStatus.OPEN,
        category="test",
        volume_micros=MicroDollars(1_000_000),
        liquidity_micros=MicroDollars(1_000_000),
        tradeable=tradeable,
        outcomes=(outcome,),
        observed_at=NOW - timedelta(seconds=1),
    )
    return market, outcome


def book(
    *,
    bids: tuple[tuple[str, str], ...] = (("0.39", "100"),),
    asks: tuple[tuple[str, str], ...] = (("0.40", "1"),),
    observed_at: datetime = NOW - timedelta(seconds=1),
    tick: str = "0.01",
    minimum: str = "1",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="token-yes",
        condition_id="condition-1",
        observed_at=observed_at,
        source_created_at=observed_at,
        bids=tuple(PriceLevel(Decimal(price), Decimal(size)) for price, size in bids),
        asks=tuple(PriceLevel(Decimal(price), Decimal(size)) for price, size in asks),
        tick_size=Decimal(tick),
        minimum_order_size=Decimal(minimum),
        negative_risk=False,
        artifact=ARTIFACT,
    )


def order(*, side: Side = Side.BUY, shares: str = "10", suffix: str = "1") -> PaperOrder:
    return PaperOrder(
        id=f"order-{suffix}",
        agent_id="agent-1",
        market_id="market-1",
        outcome_id="outcome-yes",
        side=side,
        shares=Decimal(shares),
        created_at=NOW,
    )


def archived_bid(price: str, *, age: timedelta = timedelta(seconds=1)) -> ArchivedBid:
    return ArchivedBid(Decimal(price), NOW - age)


def resolution(*, winner: str | None = "outcome-yes") -> SettlementObservation:
    return SettlementObservation(
        id="resolution-1",
        market_id="market-1",
        winning_outcome_id=winner,
        source_created_at=NOW - timedelta(minutes=2),
        observed_at=NOW - timedelta(minutes=1),
        eligible_after=NOW - timedelta(minutes=2),
    )


class BrokerScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.market, self.outcome = market_and_outcome()
        self.portfolio = PortfolioState("agent-1", MicroDollars(10_000_000_000))
        self.fees = FeePolicy(Decimal("0.05"))

    def place(
        self,
        paper_order: PaperOrder,
        *,
        snapshot: OrderBookSnapshot | None = None,
        portfolio: PortfolioState | None = None,
        broker: PredictionArenaPaperBroker | None = None,
        bids: dict[str, ArchivedBid | None] | None = None,
    ):
        return (broker or PredictionArenaPaperBroker()).place(
            paper_order,
            market=self.market,
            outcome=self.outcome,
            snapshot=snapshot or book(),
            portfolio=portfolio or self.portfolio,
            executable_bids=bids or {},
            fee_policy=self.fees,
            now=NOW,
        )

    def test_unconditional_fill_ignores_displayed_size_but_uses_best_ask(self) -> None:
        result = self.place(order(shares="10"))
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        self.assertEqual(result.fills[0].shares, Decimal(10))
        self.assertEqual(result.fills[0].price, Decimal("0.40"))
        self.assertEqual(result.portfolio.position("outcome-yes").average_cost, Decimal("0.4"))
        self.assertEqual(sum(int(p.amount_micros) for p in result.ledger_entries[0].postings), 0)

    def test_liquidity_aware_walks_levels_and_returns_partial_fill(self) -> None:
        snapshot = book(asks=(("0.40", "1"), ("0.41", "2")))
        broker = PredictionArenaPaperBroker(policy=PaperPolicy.LIQUIDITY_AWARE)
        result = self.place(order(shares="5"), snapshot=snapshot, broker=broker)
        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual([fill.shares for fill in result.fills], [Decimal(1), Decimal(2)])

    def test_liquidity_aware_fok_rejects_atomically_when_depth_is_short(self) -> None:
        snapshot = book(asks=(("0.40", "1"), ("0.41", "2")))
        broker = PredictionArenaPaperBroker(policy=PaperPolicy.LIQUIDITY_AWARE)
        paper_order = replace(
            order(shares="5"), liquidity_time_in_force=LiquidityTimeInForce.FOK
        )
        result = self.place(paper_order, snapshot=snapshot, broker=broker)
        self.assertEqual(result.status, ExecutionStatus.REJECTED)
        self.assertEqual(result.rejection_code, RejectionCode.FOK_NOT_FILLED)
        self.assertEqual(result.fills, ())

    def test_required_quote_and_deterministic_validation_rejections(self) -> None:
        scenarios = (
            (book(asks=()), RejectionCode.REQUIRED_QUOTE_ABSENT),
            (book(asks=(("0.405", "10"),)), RejectionCode.INVALID_TICK),
            (book(minimum="11"), RejectionCode.BELOW_MINIMUM_SIZE),
            (
                book(observed_at=NOW - timedelta(minutes=6)),
                RejectionCode.LOOK_AHEAD,
            ),
            (
                book(bids=(("0.40", "10"),), asks=(("0.40", "10"),)),
                RejectionCode.CROSSED_BOOK,
            ),
        )
        for snapshot, expected in scenarios:
            with self.subTest(expected=expected):
                self.assertEqual(
                    self.place(order(shares="10"), snapshot=snapshot).rejection_code,
                    expected,
                )
        stale_broker = PredictionArenaPaperBroker(maximum_book_age=timedelta(0))
        self.assertEqual(
            self.place(order(shares="10"), broker=stale_broker).rejection_code,
            RejectionCode.STALE_BOOK,
        )

    def test_pending_orders_are_included_in_solvency(self) -> None:
        portfolio = PortfolioState(
            "agent-1",
            MicroDollars(10_000_000),
            pending_orders=(
                PendingOrder(
                    "pending",
                    "other-market",
                    "other-outcome",
                    Side.BUY,
                    reserved_cash_micros=MicroDollars(9_000_000),
                ),
            ),
        )
        result = self.place(order(shares="10"), portfolio=portfolio)
        self.assertEqual(result.rejection_code, RejectionCode.INSUFFICIENT_CASH)

    def test_current_and_pending_cost_basis_enforce_fifteen_percent_cap(self) -> None:
        position = PositionState(
            "market-1",
            "outcome-yes",
            Decimal(1490),
            Decimal(1),
            MicroDollars(1_490_000_000),
        )
        portfolio = PortfolioState(
            "agent-1", MicroDollars(8_510_000_000), positions=(position,)
        )
        snapshot = book(bids=(("0.99", "100"),), asks=(("1.00", "100"),))
        result = self.place(
            order(shares="20"),
            snapshot=snapshot,
            portfolio=portfolio,
            bids={"outcome-yes": archived_bid("1")},
        )
        self.assertEqual(result.rejection_code, RejectionCode.CONCENTRATION_LIMIT)

    def test_successive_buys_use_weighted_average_cost(self) -> None:
        first = self.place(
            order(shares="10", suffix="first"),
            snapshot=book(asks=(("0.40", "20"),)),
        )
        second = self.place(
            order(shares="10", suffix="second"),
            snapshot=book(bids=(("0.50", "20"),), asks=(("0.60", "20"),)),
            portfolio=first.portfolio,
            bids={"outcome-yes": archived_bid("0.50")},
        )
        position = second.portfolio.position("outcome-yes")
        self.assertEqual(position.shares, Decimal(20))
        self.assertEqual(position.average_cost, Decimal("0.5"))

    def test_sell_cannot_exceed_owned_or_pending_available_shares(self) -> None:
        position = PositionState(
            "market-1",
            "outcome-yes",
            Decimal(10),
            Decimal("0.4"),
            MicroDollars(4_000_000),
        )
        portfolio = PortfolioState(
            "agent-1",
            MicroDollars(100_000_000),
            positions=(position,),
            pending_orders=(
                PendingOrder(
                    "pending-sell",
                    "market-1",
                    "outcome-yes",
                    Side.SELL,
                    reserved_shares=Decimal(3),
                ),
            ),
        )
        result = self.place(
            order(side=Side.SELL, shares="8"), portfolio=portfolio
        )
        self.assertEqual(result.rejection_code, RejectionCode.INSUFFICIENT_SHARES)

    def test_fee_formula_is_official_taker_curve_rounded_to_five_decimals(self) -> None:
        self.assertEqual(
            FeePolicy(Decimal("0.05")).calculate_micros(Decimal(100), Decimal("0.5")),
            1_250_000,
        )

    def test_settlement_is_idempotent_and_removes_position_once(self) -> None:
        position = PositionState(
            "market-1",
            "outcome-yes",
            Decimal(10),
            Decimal("0.4"),
            MicroDollars(4_000_000),
        )
        portfolio = PortfolioState(
            "agent-1", MicroDollars(100_000_000), positions=(position,)
        )
        engine = SettlementEngine()
        first = engine.settle(
            resolution=resolution(),
            position=position,
            portfolio=portfolio,
            as_of=NOW,
            settled_at=NOW,
        )
        second = engine.settle(
            resolution=resolution(),
            position=position,
            portfolio=portfolio,
            as_of=NOW,
            settled_at=NOW,
        )
        self.assertIs(first, second)
        self.assertEqual(first.payout_micros, 10_000_000)
        self.assertEqual(first.portfolio.positions, ())
        self.assertEqual(sum(int(p.amount_micros) for p in first.ledger_entry.postings), 0)

    def test_fifty_fifty_settlement_pays_half_per_share_and_is_idempotent(self) -> None:
        position = PositionState(
            "market-1",
            "outcome-yes",
            Decimal(10),
            Decimal("0.4"),
            MicroDollars(4_000_000),
        )
        portfolio = PortfolioState(
            "agent-1", MicroDollars(100_000_000), positions=(position,)
        )
        engine = SettlementEngine()
        split = resolution(winner=None)

        first = engine.settle(
            resolution=split,
            position=position,
            portfolio=portfolio,
            as_of=NOW,
            settled_at=NOW,
        )
        second = engine.settle(
            resolution=split,
            position=position,
            portfolio=portfolio,
            as_of=NOW,
            settled_at=NOW,
        )

        self.assertIs(first, second)
        self.assertEqual(first.payout_micros, 5_000_000)
        self.assertEqual(first.realized_pnl_micros, 1_000_000)
        self.assertEqual(first.portfolio.cash_micros, 105_000_000)
        self.assertEqual(first.portfolio.positions, ())
        self.assertEqual(sum(int(p.amount_micros) for p in first.ledger_entry.postings), 0)

    def test_last_archived_bid_at_five_minutes_is_accepted(self) -> None:
        position = PositionState(
            "market-1",
            "outcome-yes",
            Decimal(1),
            Decimal("0.4"),
            MicroDollars(400_000),
        )
        portfolio = PortfolioState(
            "agent-1", MicroDollars(1_000_000), positions=(position,)
        )
        value = portfolio.account_value_micros(
            {"outcome-yes": archived_bid("0.25", age=timedelta(minutes=5))},
            as_of=NOW,
        )
        self.assertEqual(value, 1_250_000)

    def test_missing_or_older_archived_bid_blocks_snapshot(self) -> None:
        position = PositionState(
            "market-1",
            "outcome-yes",
            Decimal(1),
            Decimal("0.4"),
            MicroDollars(400_000),
        )
        portfolio = PortfolioState(
            "agent-1", MicroDollars(1_000_000), positions=(position,)
        )
        scenarios = (
            {"outcome-yes": None},
            {"outcome-yes": archived_bid("0.25", age=timedelta(minutes=5, seconds=1))},
        )
        for bids in scenarios:
            with self.subTest(bids=bids), self.assertRaises(SnapshotValuationBlocked):
                portfolio.account_value_micros(bids, as_of=NOW)

    def test_cross_agent_order_is_rejected(self) -> None:
        other = replace(order(), agent_id="agent-2")
        self.assertEqual(self.place(other).rejection_code, RejectionCode.AGENT_MISMATCH)

    def test_settlement_rejects_stale_position_projection(self) -> None:
        held = PositionState(
            "market-1", "outcome-yes", Decimal(10), Decimal("0.4"), MicroDollars(4_000_000)
        )
        stale = replace(held, shares=Decimal(9), cost_basis_micros=MicroDollars(3_600_000))
        portfolio = PortfolioState("agent-1", MicroDollars(10_000_000), positions=(held,))
        with self.assertRaises(ValueError):
            SettlementEngine().settle(
                resolution=resolution(),
                position=stale,
                portfolio=portfolio,
                as_of=NOW,
                settled_at=NOW,
            )

    def test_replay_reconstructs_projection_only_from_postings(self) -> None:
        initial = self.portfolio
        buy = self.place(order(shares="10", suffix="buy"))
        sell = self.place(
            order(side=Side.SELL, shares="4", suffix="sell"),
            portfolio=buy.portfolio,
        )
        entries = buy.ledger_entries + sell.ledger_entries
        replayed = replay_portfolio(initial, entries)
        self.assertEqual(replayed, sell.portfolio)
        self.assertEqual(replay_portfolio(initial, entries + entries[-1:]), sell.portfolio)

    def test_initial_cash_and_trades_replay_from_ledger_with_zero_seed(self) -> None:
        buy = self.place(order(shares="10", suffix="buy-from-zero"))
        capital = initial_capital_entry(
            "agent-1", MicroDollars(10_000_000_000), occurred_at=NOW - timedelta(minutes=1)
        )
        zero = PortfolioState("agent-1", MicroDollars(0))
        replayed = replay_portfolio(zero, (capital, *buy.ledger_entries))
        self.assertEqual(replayed.cash_micros, buy.portfolio.cash_micros)
        self.assertEqual(replayed.positions, buy.portfolio.positions)


class BrokerPropertyTests(unittest.TestCase):
    @settings(max_examples=75, deadline=None)
    @given(
        price_cents=st.integers(min_value=1, max_value=99),
        shares=st.integers(min_value=1, max_value=1000),
        fee_basis_points=st.integers(min_value=0, max_value=700),
    )
    def test_accepted_buys_never_create_negative_cash_or_unbalanced_ledger(
        self, price_cents: int, shares: int, fee_basis_points: int
    ) -> None:
        market, outcome = market_and_outcome()
        price = Decimal(price_cents) / Decimal(100)
        bid = max(price - Decimal("0.01"), Decimal(0))
        snapshot = book(
            bids=((str(bid), "10000"),),
            asks=((str(price), "10000"),),
        )
        result = PredictionArenaPaperBroker().place(
            order(shares=str(shares), suffix=f"{price_cents}-{shares}-{fee_basis_points}"),
            market=market,
            outcome=outcome,
            snapshot=snapshot,
            portfolio=PortfolioState("agent-1", MicroDollars(10_000_000_000)),
            executable_bids={},
            fee_policy=FeePolicy(Decimal(fee_basis_points) / Decimal(10_000)),
            now=NOW,
        )
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        self.assertGreaterEqual(int(result.portfolio.cash_micros), 0)
        self.assertGreaterEqual(result.portfolio.positions[0].shares, 0)
        self.assertLessEqual(result.portfolio.positions[0].cost_basis_micros, 1_500_000_000)
        self.assertEqual(sum(int(p.amount_micros) for p in result.ledger_entries[0].postings), 0)

    @settings(max_examples=50, deadline=None)
    @given(
        price_cents=st.integers(min_value=2, max_value=99),
        bought_shares=st.integers(min_value=1, max_value=1000),
        requested_sell=st.integers(min_value=1, max_value=1000),
    )
    def test_buy_sell_projection_matches_posting_replay(
        self, price_cents: int, bought_shares: int, requested_sell: int
    ) -> None:
        market, outcome = market_and_outcome()
        ask = Decimal(price_cents) / Decimal(100)
        bid = ask - Decimal("0.01")
        snapshot = book(
            bids=((str(bid), "10000"),),
            asks=((str(ask), "10000"),),
        )
        initial = PortfolioState("agent-1", MicroDollars(10_000_000_000))
        broker = PredictionArenaPaperBroker()
        buy = broker.place(
            order(shares=str(bought_shares), suffix=f"buy-{price_cents}-{bought_shares}"),
            market=market,
            outcome=outcome,
            snapshot=snapshot,
            portfolio=initial,
            executable_bids={},
            fee_policy=FeePolicy(Decimal("0.05")),
            now=NOW,
        )
        sold_shares = min(bought_shares, requested_sell)
        sell = broker.place(
            order(
                side=Side.SELL,
                shares=str(sold_shares),
                suffix=f"sell-{price_cents}-{bought_shares}-{sold_shares}",
            ),
            market=market,
            outcome=outcome,
            snapshot=snapshot,
            portfolio=buy.portfolio,
            executable_bids={"outcome-yes": archived_bid(str(bid))},
            fee_policy=FeePolicy(Decimal("0.05")),
            now=NOW,
        )
        self.assertEqual(
            replay_portfolio(initial, buy.ledger_entries + sell.ledger_entries),
            sell.portfolio,
        )


if __name__ == "__main__":
    unittest.main()
