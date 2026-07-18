from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from vtrade.domain.types import Market, MarketStatus, MicroDollars, OrderBookSnapshot, Outcome, Side
from vtrade.ledger import AppendOnlyLedger, LedgerAccount, LedgerEntry, Posting

_MICROS = Decimal(1_000_000)
_FEE_QUANTUM = Decimal("0.00001")


class PaperPolicy(StrEnum):
    PREDICTIONARENA_UNCONDITIONAL = "predictionarena_unconditional"
    LIQUIDITY_AWARE = "liquidity_aware"


class LiquidityTimeInForce(StrEnum):
    """Remainder semantics for the non-baseline liquidity-aware policy."""

    FAK = "FAK"
    FOK = "FOK"


class ExecutionStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"


class RejectionCode(StrEnum):
    AGENT_MISMATCH = "agent_mismatch"
    MARKET_NOT_OPEN = "market_not_open"
    MARKET_NOT_TRADEABLE = "market_not_tradeable"
    OUTCOME_NOT_TRADEABLE = "outcome_not_tradeable"
    TOKEN_MISMATCH = "token_mismatch"
    LOOK_AHEAD = "look_ahead"
    STALE_BOOK = "stale_book"
    CROSSED_BOOK = "crossed_book"
    REQUIRED_QUOTE_ABSENT = "required_quote_absent"
    INVALID_PRICE = "invalid_price"
    INVALID_TICK = "invalid_tick"
    BELOW_MINIMUM_SIZE = "below_minimum_size"
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_SHARES = "insufficient_shares"
    CONCENTRATION_LIMIT = "concentration_limit"
    NO_LIQUIDITY = "no_liquidity"
    FOK_NOT_FILLED = "fok_not_filled"


class NoBidValuationPolicy(StrEnum):
    LAST_KNOWN_BID = "last_known_bid"


class SnapshotValuationBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArchivedBid:
    price: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.price.is_finite() or not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("archived bid must be finite and between zero and one")
        _require_aware(self.observed_at, "archived_bid.observed_at")


@dataclass(frozen=True, slots=True)
class FeePolicy:
    """Documented fee formula plus raw market parameters retained for audit.

    Polymarket exposes ``fd.e`` but its current public formula is explicitly
    ``shares * rate * p * (1-p)``. The exponent is stored without silently applying
    an unpublished generalized curve.
    """

    rate: Decimal
    enabled: bool = True
    exponent: Decimal | None = None
    taker_only: bool = True
    formula_version: str = "polymarket-v2-p-one-minus-p"

    def __post_init__(self) -> None:
        if not self.rate.is_finite() or not Decimal(0) <= self.rate <= Decimal(1):
            raise ValueError("fee rate must be finite and between zero and one")
        if self.exponent is not None and (
            not self.exponent.is_finite() or self.exponent < 0
        ):
            raise ValueError("fee exponent must be finite and non-negative")

    def calculate_micros(self, shares: Decimal, price: Decimal) -> MicroDollars:
        if not shares.is_finite() or shares <= 0:
            raise ValueError("fee shares must be finite and positive")
        if not price.is_finite() or not Decimal(0) <= price <= Decimal(1):
            raise ValueError("fee price must be finite and between zero and one")
        if not self.enabled or self.rate == 0:
            return MicroDollars(0)
        fee = (shares * self.rate * price * (Decimal(1) - price)).quantize(
            _FEE_QUANTUM, rounding=ROUND_HALF_UP
        )
        return MicroDollars(int(fee * _MICROS))


@dataclass(frozen=True, slots=True)
class PositionState:
    market_id: str
    outcome_id: str
    shares: Decimal
    average_cost: Decimal
    cost_basis_micros: MicroDollars
    realized_pnl_micros: MicroDollars = field(default_factory=lambda: MicroDollars(0))

    def __post_init__(self) -> None:
        if not self.shares.is_finite() or not self.average_cost.is_finite():
            raise ValueError("position decimals must be finite")
        if self.shares < 0 or self.average_cost < 0 or int(self.cost_basis_micros) < 0:
            raise ValueError("position values cannot be negative")
        if self.shares == 0 and (self.average_cost != 0 or int(self.cost_basis_micros) != 0):
            raise ValueError("a zero-share position must have zero average cost and basis")


@dataclass(frozen=True, slots=True)
class PendingOrder:
    id: str
    market_id: str
    outcome_id: str
    side: Side
    reserved_cash_micros: MicroDollars = field(default_factory=lambda: MicroDollars(0))
    reserved_shares: Decimal = field(default_factory=lambda: Decimal(0))
    reserved_cost_basis_micros: MicroDollars = field(
        default_factory=lambda: MicroDollars(0)
    )

    def __post_init__(self) -> None:
        if int(self.reserved_cash_micros) < 0 or int(self.reserved_cost_basis_micros) < 0:
            raise ValueError("pending money reservations cannot be negative")
        if not self.reserved_shares.is_finite() or self.reserved_shares < 0:
            raise ValueError("pending share reservations must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    agent_id: str
    cash_micros: MicroDollars
    positions: tuple[PositionState, ...] = ()
    pending_orders: tuple[PendingOrder, ...] = ()
    version: int = 0

    def __post_init__(self) -> None:
        if int(self.cash_micros) < 0:
            raise ValueError("cash cannot be negative")
        if self.version < 0:
            raise ValueError("portfolio version cannot be negative")
        if len({position.outcome_id for position in self.positions}) != len(self.positions):
            raise ValueError("portfolio cannot contain duplicate outcome positions")
        if int(self.available_cash_micros) < 0:
            raise ValueError("pending orders cannot reserve more cash than the portfolio owns")
        sell_outcomes = {
            order.outcome_id for order in self.pending_orders if order.side is Side.SELL
        }
        for outcome_id in sell_outcomes:
            if self.available_shares(outcome_id) < 0:
                raise ValueError("pending orders cannot reserve more shares than are owned")

    def position(self, outcome_id: str) -> PositionState | None:
        return next((item for item in self.positions if item.outcome_id == outcome_id), None)

    def account_value_micros(
        self,
        executable_bids: Mapping[str, ArchivedBid | None],
        *,
        as_of: datetime,
        maximum_bid_age: timedelta = timedelta(minutes=5),
        no_bid_policy: NoBidValuationPolicy = NoBidValuationPolicy.LAST_KNOWN_BID,
    ) -> MicroDollars:
        _require_aware(as_of, "valuation.as_of")
        if maximum_bid_age < timedelta(0):
            raise ValueError("maximum bid age cannot be negative")
        if no_bid_policy is not NoBidValuationPolicy.LAST_KNOWN_BID:
            raise ValueError("unsupported no-bid valuation policy")
        total = int(self.cash_micros)
        for position in self.positions:
            bid = executable_bids.get(position.outcome_id)
            if bid is None:
                raise SnapshotValuationBlocked(
                    f"no archived bid exists for held outcome {position.outcome_id}"
                )
            if bid.observed_at > as_of:
                raise SnapshotValuationBlocked("archived bid is newer than the valuation cutoff")
            if as_of - bid.observed_at > maximum_bid_age:
                raise SnapshotValuationBlocked(
                    f"archived bid for {position.outcome_id} is older than {maximum_bid_age}"
                )
            total += int(_money_micros(position.shares * bid.price))
        return MicroDollars(total)

    def market_cost_basis_micros(self, market_id: str) -> MicroDollars:
        held = sum(
            int(position.cost_basis_micros)
            for position in self.positions
            if position.market_id == market_id
        )
        pending = sum(
            int(order.reserved_cost_basis_micros)
            for order in self.pending_orders
            if order.market_id == market_id and order.side is Side.BUY
        )
        return MicroDollars(held + pending)

    @property
    def available_cash_micros(self) -> MicroDollars:
        reserved = sum(int(order.reserved_cash_micros) for order in self.pending_orders)
        return MicroDollars(int(self.cash_micros) - reserved)

    def available_shares(self, outcome_id: str) -> Decimal:
        position = self.position(outcome_id)
        held = position.shares if position else Decimal(0)
        reserved = sum(
            order.reserved_shares
            for order in self.pending_orders
            if order.outcome_id == outcome_id and order.side is Side.SELL
        )
        return held - reserved


@dataclass(frozen=True, slots=True)
class PaperOrder:
    id: str
    agent_id: str
    market_id: str
    outcome_id: str
    side: Side
    shares: Decimal
    created_at: datetime
    liquidity_time_in_force: LiquidityTimeInForce = LiquidityTimeInForce.FAK

    def __post_init__(self) -> None:
        if not self.shares.is_finite() or self.shares <= 0:
            raise ValueError("paper order shares must be finite and positive")
        _require_aware(self.created_at, "order.created_at")


@dataclass(frozen=True, slots=True)
class PaperFill:
    id: str
    order_id: str
    fill_index: int
    shares: Decimal
    price: Decimal
    gross_micros: MicroDollars
    fee_micros: MicroDollars
    filled_at: datetime

    def __post_init__(self) -> None:
        if self.fill_index < 0:
            raise ValueError("fill index cannot be negative")
        if not self.shares.is_finite() or self.shares <= 0:
            raise ValueError("fill shares must be finite and positive")
        if not self.price.is_finite() or not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError("fill price must be finite and between zero and one")
        if int(self.gross_micros) < 0 or int(self.fee_micros) < 0:
            raise ValueError("fill money values cannot be negative")
        _require_aware(self.filled_at, "fill.filled_at")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    order: PaperOrder
    policy: PaperPolicy
    status: ExecutionStatus
    fills: tuple[PaperFill, ...]
    rejection_code: RejectionCode | None
    portfolio_before: PortfolioState
    portfolio: PortfolioState
    ledger_entries: tuple[LedgerEntry, ...]
    snapshot: OrderBookSnapshot
    fee_policy: FeePolicy

    def __post_init__(self) -> None:
        if self.status is ExecutionStatus.REJECTED:
            if self.rejection_code is None or self.fills or self.ledger_entries:
                raise ValueError("rejected execution must have a code and no financial events")
            if self.portfolio != self.portfolio_before:
                raise ValueError("rejected execution cannot mutate the portfolio")
            return
        if self.rejection_code is not None or not self.fills or not self.ledger_entries:
            raise ValueError("accepted execution requires fills and ledger entries")
        if self.portfolio.version != self.portfolio_before.version + 1:
            raise ValueError("accepted execution must advance the portfolio version once")
        filled = sum((fill.shares for fill in self.fills), start=Decimal(0))
        if any(fill.order_id != self.order.id for fill in self.fills):
            raise ValueError("fill order IDs must match the execution order")
        if tuple(fill.fill_index for fill in self.fills) != tuple(range(len(self.fills))):
            raise ValueError("fill indexes must be contiguous")
        if self.status is ExecutionStatus.FILLED and filled != self.order.shares:
            raise ValueError("filled execution must satisfy all requested shares")
        if self.status is ExecutionStatus.PARTIAL and not Decimal(0) < filled < self.order.shares:
            raise ValueError("partial execution must fill a strict subset of requested shares")


class PredictionArenaPaperBroker:
    def __init__(
        self,
        *,
        policy: PaperPolicy = PaperPolicy.PREDICTIONARENA_UNCONDITIONAL,
        maximum_market_cost_basis_fraction: Decimal = Decimal("0.15"),
        maximum_book_age: timedelta = timedelta(minutes=5),
        maximum_valuation_bid_age: timedelta = timedelta(minutes=5),
        no_bid_valuation_policy: NoBidValuationPolicy = NoBidValuationPolicy.LAST_KNOWN_BID,
    ) -> None:
        if not Decimal(0) < maximum_market_cost_basis_fraction <= Decimal(1):
            raise ValueError("market concentration fraction must be in (0, 1]")
        if maximum_book_age < timedelta(0):
            raise ValueError("maximum book age cannot be negative")
        if maximum_valuation_bid_age < timedelta(0):
            raise ValueError("maximum valuation bid age cannot be negative")
        self.policy = policy
        self.maximum_market_cost_basis_fraction = maximum_market_cost_basis_fraction
        self.maximum_book_age = maximum_book_age
        self.maximum_valuation_bid_age = maximum_valuation_bid_age
        self.no_bid_valuation_policy = no_bid_valuation_policy

    def place(
        self,
        order: PaperOrder,
        *,
        market: Market,
        outcome: Outcome,
        snapshot: OrderBookSnapshot,
        portfolio: PortfolioState,
        executable_bids: Mapping[str, ArchivedBid | None],
        fee_policy: FeePolicy,
        now: datetime,
    ) -> ExecutionResult:
        _require_aware(now, "now")
        rejection = self._validate_context(order, market, outcome, snapshot, portfolio, now)
        if rejection is not None:
            return self._rejected(order, snapshot, portfolio, fee_policy, rejection)
        if order.shares < snapshot.minimum_order_size:
            return self._rejected(
                order,
                snapshot,
                portfolio,
                fee_policy,
                RejectionCode.BELOW_MINIMUM_SIZE,
            )
        planned = self._plan_fills(order, snapshot, fee_policy, now)
        if not planned:
            code = (
                RejectionCode.REQUIRED_QUOTE_ABSENT
                if self.policy is PaperPolicy.PREDICTIONARENA_UNCONDITIONAL
                else RejectionCode.NO_LIQUIDITY
            )
            return self._rejected(order, snapshot, portfolio, fee_policy, code)
        total_shares = sum((fill.shares for fill in planned), start=Decimal(0))
        if (
            self.policy is PaperPolicy.LIQUIDITY_AWARE
            and order.liquidity_time_in_force is LiquidityTimeInForce.FOK
            and total_shares != order.shares
        ):
            return self._rejected(
                order,
                snapshot,
                portfolio,
                fee_policy,
                RejectionCode.FOK_NOT_FILLED,
            )
        gross = MicroDollars(sum(int(fill.gross_micros) for fill in planned))
        fees = MicroDollars(sum(int(fill.fee_micros) for fill in planned))
        financial_rejection = self._validate_financials(
            order, market, portfolio, executable_bids, total_shares, gross, fees
        )
        if financial_rejection is not None:
            return self._rejected(
                order, snapshot, portfolio, fee_policy, financial_rejection
            )
        updated, ledger_entry = self._apply(order, portfolio, total_shares, gross, fees, now)
        status = ExecutionStatus.FILLED if total_shares == order.shares else ExecutionStatus.PARTIAL
        return ExecutionResult(
            order=order,
            policy=self.policy,
            status=status,
            fills=planned,
            rejection_code=None,
            portfolio_before=portfolio,
            portfolio=updated,
            ledger_entries=(ledger_entry,),
            snapshot=snapshot,
            fee_policy=fee_policy,
        )

    def _validate_context(
        self,
        order: PaperOrder,
        market: Market,
        outcome: Outcome,
        snapshot: OrderBookSnapshot,
        portfolio: PortfolioState,
        now: datetime,
    ) -> RejectionCode | None:
        _require_aware(snapshot.observed_at, "snapshot.observed_at")
        if snapshot.source_created_at is not None:
            _require_aware(snapshot.source_created_at, "snapshot.source_created_at")
        for label, timestamp in (
            ("market.observed_at", market.observed_at),
            ("market.opens_at", market.opens_at),
            ("market.closes_at", market.closes_at),
        ):
            if timestamp is not None:
                _require_aware(timestamp, label)
        if order.agent_id != portfolio.agent_id:
            return RejectionCode.AGENT_MISMATCH
        if market.status is not MarketStatus.OPEN:
            return RejectionCode.MARKET_NOT_OPEN
        if market.opens_at is not None and order.created_at < market.opens_at:
            return RejectionCode.MARKET_NOT_OPEN
        if market.closes_at is not None and order.created_at >= market.closes_at:
            return RejectionCode.MARKET_NOT_OPEN
        if not market.tradeable:
            return RejectionCode.MARKET_NOT_TRADEABLE
        if not outcome.tradeable:
            return RejectionCode.OUTCOME_NOT_TRADEABLE
        if (
            order.market_id != market.id
            or order.outcome_id != outcome.id
            or outcome.market_id != market.id
            or snapshot.token_id != outcome.venue_token_id
        ):
            return RejectionCode.TOKEN_MISMATCH
        if (
            order.created_at > now
            or snapshot.observed_at > order.created_at
            or (
                snapshot.source_created_at is not None
                and snapshot.source_created_at > snapshot.observed_at
            )
            or (market.observed_at is not None and market.observed_at > snapshot.observed_at)
        ):
            return RejectionCode.LOOK_AHEAD
        if now - snapshot.observed_at > self.maximum_book_age:
            return RejectionCode.STALE_BOOK
        if (
            snapshot.best_bid is not None
            and snapshot.best_ask is not None
            and snapshot.best_bid >= snapshot.best_ask
        ):
            return RejectionCode.CROSSED_BOOK
        required_levels = snapshot.asks if order.side is Side.BUY else snapshot.bids
        levels = (
            required_levels[:1]
            if self.policy is PaperPolicy.PREDICTIONARENA_UNCONDITIONAL
            else required_levels
        )
        if any(
            not level.price.is_finite()
            or not Decimal(0) <= level.price <= Decimal(1)
            or not level.size.is_finite()
            or level.size <= 0
            for level in levels
        ):
            return RejectionCode.INVALID_PRICE
        if any(not _is_tick_aligned(level.price, snapshot.tick_size) for level in levels):
            return RejectionCode.INVALID_TICK
        return None

    def _plan_fills(
        self,
        order: PaperOrder,
        snapshot: OrderBookSnapshot,
        fee_policy: FeePolicy,
        now: datetime,
    ) -> tuple[PaperFill, ...]:
        levels = snapshot.asks if order.side is Side.BUY else snapshot.bids
        ordered = sorted(
            levels,
            key=lambda level: level.price,
            reverse=order.side is Side.SELL,
        )
        if not ordered:
            return ()
        if self.policy is PaperPolicy.PREDICTIONARENA_UNCONDITIONAL:
            ordered = ordered[:1]
        remaining = order.shares
        fills: list[PaperFill] = []
        for level in ordered:
            shares = (
                remaining
                if self.policy is PaperPolicy.PREDICTIONARENA_UNCONDITIONAL
                else min(remaining, level.size)
            )
            if shares <= 0:
                continue
            index = len(fills)
            fills.append(
                PaperFill(
                    id=_stable_uuid(f"fill:{order.id}:{index}"),
                    order_id=order.id,
                    fill_index=index,
                    shares=shares,
                    price=level.price,
                    gross_micros=_money_micros(shares * level.price),
                    fee_micros=fee_policy.calculate_micros(shares, level.price),
                    filled_at=now,
                )
            )
            remaining -= shares
            if remaining <= 0:
                break
        return tuple(fills)

    def _validate_financials(
        self,
        order: PaperOrder,
        market: Market,
        portfolio: PortfolioState,
        executable_bids: Mapping[str, ArchivedBid | None],
        filled_shares: Decimal,
        gross: MicroDollars,
        fees: MicroDollars,
    ) -> RejectionCode | None:
        if order.side is Side.BUY:
            required = int(gross) + int(fees)
            if required > int(portfolio.available_cash_micros):
                return RejectionCode.INSUFFICIENT_CASH
            account_value = int(
                portfolio.account_value_micros(
                    executable_bids,
                    as_of=order.created_at,
                    maximum_bid_age=self.maximum_valuation_bid_age,
                    no_bid_policy=self.no_bid_valuation_policy,
                )
            )
            maximum_basis = Decimal(account_value) * self.maximum_market_cost_basis_fraction
            cap = int(maximum_basis.to_integral_value(rounding=ROUND_HALF_UP))
            existing = int(portfolio.market_cost_basis_micros(market.id))
            if existing + int(gross) > cap:
                return RejectionCode.CONCENTRATION_LIMIT
        elif filled_shares > portfolio.available_shares(order.outcome_id):
            return RejectionCode.INSUFFICIENT_SHARES
        return None

    def _apply(
        self,
        order: PaperOrder,
        portfolio: PortfolioState,
        shares: Decimal,
        gross: MicroDollars,
        fees: MicroDollars,
        now: datetime,
    ) -> tuple[PortfolioState, LedgerEntry]:
        positions = list(portfolio.positions)
        current = portfolio.position(order.outcome_id)
        if order.side is Side.BUY:
            new_shares = (current.shares if current else Decimal(0)) + shares
            new_basis = (int(current.cost_basis_micros) if current else 0) + int(gross)
            position = PositionState(
                market_id=order.market_id,
                outcome_id=order.outcome_id,
                shares=new_shares,
                average_cost=Decimal(new_basis) / _MICROS / new_shares,
                cost_basis_micros=MicroDollars(new_basis),
                realized_pnl_micros=(
                    current.realized_pnl_micros if current else MicroDollars(0)
                ),
            )
            cash_change = -(int(gross) + int(fees))
            postings = _trade_postings(
                order,
                cash_change=cash_change,
                position_cost_change=int(gross),
                shares_change=shares,
                fee=int(fees),
                realized_pnl_credit=0,
            )
        else:
            if current is None:
                raise AssertionError("validated sell must have a position")
            removed_cost = (
                current.cost_basis_micros
                if shares == current.shares
                else _money_micros(current.average_cost * shares)
            )
            remaining_shares = current.shares - shares
            remaining_basis = int(current.cost_basis_micros) - int(removed_cost)
            realized = int(gross) - int(removed_cost) - int(fees)
            position = PositionState(
                market_id=current.market_id,
                outcome_id=current.outcome_id,
                shares=remaining_shares,
                average_cost=(
                    Decimal(remaining_basis) / _MICROS / remaining_shares
                    if remaining_shares
                    else Decimal(0)
                ),
                cost_basis_micros=MicroDollars(remaining_basis),
                realized_pnl_micros=MicroDollars(
                    int(current.realized_pnl_micros) + realized
                ),
            )
            cash_change = int(gross) - int(fees)
            postings = _trade_postings(
                order,
                cash_change=cash_change,
                position_cost_change=-int(removed_cost),
                shares_change=-shares,
                fee=int(fees),
                realized_pnl_credit=int(removed_cost) - int(gross),
            )
        positions = [item for item in positions if item.outcome_id != order.outcome_id]
        if position.shares > 0:
            positions.append(position)
        updated = replace(
            portfolio,
            cash_micros=MicroDollars(int(portfolio.cash_micros) + cash_change),
            positions=tuple(sorted(positions, key=lambda item: item.outcome_id)),
            version=portfolio.version + 1,
        )
        entry = LedgerEntry(
            id=_stable_uuid(f"ledger:trade:{order.id}"),
            agent_id=order.agent_id,
            idempotency_key=f"trade:{order.id}",
            event_type="paper_trade",
            occurred_at=now,
            postings=postings,
        )
        return updated, entry

    def _rejected(
        self,
        order: PaperOrder,
        snapshot: OrderBookSnapshot,
        portfolio: PortfolioState,
        fee_policy: FeePolicy,
        code: RejectionCode,
    ) -> ExecutionResult:
        return ExecutionResult(
            order=order,
            policy=self.policy,
            status=ExecutionStatus.REJECTED,
            fills=(),
            rejection_code=code,
            portfolio_before=portfolio,
            portfolio=portfolio,
            ledger_entries=(),
            snapshot=snapshot,
            fee_policy=fee_policy,
        )


@dataclass(frozen=True, slots=True)
class SettlementObservation:
    id: str
    market_id: str
    winning_outcome_id: str | None
    source_created_at: datetime
    observed_at: datetime
    eligible_after: datetime

    def __post_init__(self) -> None:
        _require_aware(self.source_created_at, "resolution.source_created_at")
        _require_aware(self.observed_at, "resolution.observed_at")
        _require_aware(self.eligible_after, "resolution.eligible_after")
        if self.source_created_at > self.observed_at:
            raise ValueError("resolution cannot be observed before its source timestamp")


@dataclass(frozen=True, slots=True)
class SettlementResult:
    settlement_id: str
    resolution: SettlementObservation
    position: PositionState
    payout_micros: MicroDollars
    realized_pnl_micros: MicroDollars
    portfolio_before: PortfolioState
    portfolio: PortfolioState
    ledger_entry: LedgerEntry
    as_of: datetime
    settled_at: datetime


class SettlementEngine:
    def __init__(self) -> None:
        self._results: dict[str, tuple[SettlementObservation, PositionState, SettlementResult]] = {}

    def settle(
        self,
        *,
        resolution: SettlementObservation,
        position: PositionState,
        portfolio: PortfolioState,
        as_of: datetime,
        settled_at: datetime,
    ) -> SettlementResult:
        _require_aware(as_of, "settlement.as_of")
        _require_aware(settled_at, "settlement.settled_at")
        if resolution.market_id != position.market_id:
            raise ValueError("resolution market does not match the position")
        if resolution.observed_at > as_of or resolution.eligible_after > as_of:
            raise ValueError("resolution is not eligible at the settlement cutoff")
        if settled_at < as_of:
            raise ValueError("settlement persistence cannot predate its as-of cutoff")
        current = portfolio.position(position.outcome_id)
        if current != position:
            raise ValueError(
                "settlement position must exactly match the current portfolio position"
            )
        key = f"settlement:{portfolio.agent_id}:{position.outcome_id}:{resolution.id}"
        existing = self._results.get(key)
        if existing is not None:
            previous_resolution, previous_position, result = existing
            if previous_resolution != resolution or previous_position != position:
                raise ValueError("settlement idempotency key reused with different inputs")
            return result
        if resolution.winning_outcome_id is None:
            payout = _money_micros(position.shares * Decimal("0.5"))
        elif position.outcome_id == resolution.winning_outcome_id:
            payout = _money_micros(position.shares)
        else:
            payout = MicroDollars(0)
        realized = MicroDollars(int(payout) - int(position.cost_basis_micros))
        positions = tuple(
            item for item in portfolio.positions if item.outcome_id != position.outcome_id
        )
        updated = replace(
            portfolio,
            cash_micros=MicroDollars(int(portfolio.cash_micros) + int(payout)),
            positions=positions,
            version=portfolio.version + 1,
        )
        postings = _nonzero_postings(
            (
                (LedgerAccount.CASH, int(payout), None, None, None),
                (
                    LedgerAccount.POSITION_COST,
                    -int(position.cost_basis_micros),
                    position.market_id,
                    position.outcome_id,
                    -position.shares,
                ),
                (
                    LedgerAccount.REALIZED_PNL,
                    int(position.cost_basis_micros) - int(payout),
                    position.market_id,
                    position.outcome_id,
                    None,
                ),
            )
        )
        entry = LedgerEntry(
            id=_stable_uuid(f"ledger:{key}"),
            agent_id=portfolio.agent_id,
            idempotency_key=key,
            event_type="settlement",
            occurred_at=settled_at,
            postings=postings,
        )
        result = SettlementResult(
            settlement_id=_stable_uuid(key),
            resolution=resolution,
            position=position,
            payout_micros=payout,
            realized_pnl_micros=realized,
            portfolio_before=portfolio,
            portfolio=updated,
            ledger_entry=entry,
            as_of=as_of,
            settled_at=settled_at,
        )
        self._results[key] = (resolution, position, result)
        return result


def replay_portfolio(initial: PortfolioState, entries: Sequence[LedgerEntry]) -> PortfolioState:
    """Rebuild cash and positions from postings, never from cached result projections."""

    cash = int(initial.cash_micros)
    positions = {position.outcome_id: position for position in initial.positions}
    ledger = AppendOnlyLedger()
    seen: set[str] = set()
    last_occurred_at: datetime | None = None
    for entry in entries:
        if entry.agent_id != initial.agent_id:
            raise ValueError("ledger replay cannot mix agents")
        _require_aware(entry.occurred_at, "ledger.occurred_at")
        if last_occurred_at is not None and entry.occurred_at < last_occurred_at:
            raise ValueError("ledger replay entries must be in chronological order")
        last_occurred_at = entry.occurred_at
        ledger.append(entry)
        if entry.idempotency_key in seen:
            continue
        seen.add(entry.idempotency_key)
        cash += sum(
            int(posting.amount_micros)
            for posting in entry.postings
            if posting.account is LedgerAccount.CASH
        )
        position_postings = [
            posting
            for posting in entry.postings
            if posting.account is LedgerAccount.POSITION_COST
        ]
        for posting in position_postings:
            if (
                posting.market_id is None
                or posting.outcome_id is None
                or posting.shares_delta is None
            ):
                raise ValueError("position-cost postings require market, outcome, and shares")
            current = positions.get(posting.outcome_id)
            old_shares = current.shares if current else Decimal(0)
            old_basis = int(current.cost_basis_micros) if current else 0
            old_realized = int(current.realized_pnl_micros) if current else 0
            shares = old_shares + posting.shares_delta
            basis = old_basis + int(posting.amount_micros)
            if shares < 0 or basis < 0 or (shares == 0) != (basis == 0):
                raise ValueError("ledger replay produced an invalid position state")
            realized = old_realized
            if posting.shares_delta < 0:
                realized_debits = sum(
                    int(item.amount_micros)
                    for item in entry.postings
                    if item.account is LedgerAccount.REALIZED_PNL
                    and item.outcome_id == posting.outcome_id
                )
                fees = sum(
                    int(item.amount_micros)
                    for item in entry.postings
                    if item.account is LedgerAccount.FEES
                    and item.outcome_id == posting.outcome_id
                )
                realized += -realized_debits - fees
            if shares == 0:
                positions.pop(posting.outcome_id, None)
            else:
                positions[posting.outcome_id] = PositionState(
                    market_id=posting.market_id,
                    outcome_id=posting.outcome_id,
                    shares=shares,
                    average_cost=Decimal(basis) / _MICROS / shares,
                    cost_basis_micros=MicroDollars(basis),
                    realized_pnl_micros=MicroDollars(realized),
                )
    if cash < 0:
        raise ValueError("ledger replay produced negative cash")
    return PortfolioState(
        agent_id=initial.agent_id,
        cash_micros=MicroDollars(cash),
        positions=tuple(sorted(positions.values(), key=lambda item: item.outcome_id)),
        pending_orders=initial.pending_orders,
        version=initial.version
        + sum(
            1
            for entry in ledger.entries
            if entry.event_type != "initial_capital"
            and entry.idempotency_key in seen
        ),
    )


def initial_capital_entry(
    agent_id: str, cash_micros: MicroDollars, *, occurred_at: datetime
) -> LedgerEntry:
    """Represent starting cash as a balanced event so cash can be replayed from zero."""

    _require_aware(occurred_at, "initial_capital.occurred_at")
    if int(cash_micros) <= 0:
        raise ValueError("initial capital must be positive")
    return LedgerEntry(
        id=_stable_uuid(f"ledger:initial-capital:{agent_id}"),
        agent_id=agent_id,
        idempotency_key=f"initial-capital:{agent_id}",
        event_type="initial_capital",
        occurred_at=occurred_at,
        postings=(
            Posting(LedgerAccount.CASH, cash_micros),
            Posting(LedgerAccount.OWNER_EQUITY, MicroDollars(-int(cash_micros))),
        ),
    )


def _trade_postings(
    order: PaperOrder,
    *,
    cash_change: int,
    position_cost_change: int,
    shares_change: Decimal,
    fee: int,
    realized_pnl_credit: int,
) -> tuple[Posting, ...]:
    return _nonzero_postings(
        (
            (LedgerAccount.CASH, cash_change, None, None, None),
            (
                LedgerAccount.POSITION_COST,
                position_cost_change,
                order.market_id,
                order.outcome_id,
                shares_change,
            ),
            (LedgerAccount.FEES, fee, order.market_id, order.outcome_id, None),
            (
                LedgerAccount.REALIZED_PNL,
                realized_pnl_credit,
                order.market_id,
                order.outcome_id,
                None,
            ),
        )
    )


def _money_micros(value: Decimal) -> MicroDollars:
    return MicroDollars(int((value * _MICROS).to_integral_value(rounding=ROUND_HALF_UP)))


def _is_tick_aligned(price: Decimal, tick: Decimal) -> bool:
    if not tick.is_finite() or tick <= 0:
        return False
    return price % tick == 0


def _stable_uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:{value}"))


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _nonzero_postings(
    values: Sequence[
        tuple[LedgerAccount, int, str | None, str | None, Decimal | None]
    ],
) -> tuple[Posting, ...]:
    postings = tuple(
        Posting(
            account,
            MicroDollars(amount),
            market_id=market_id,
            outcome_id=outcome_id,
            shares_delta=shares_delta,
        )
        for account, amount, market_id, outcome_id, shares_delta in values
        if amount != 0 or shares_delta is not None
    )
    if len(postings) < 2:
        raise ValueError("financial event must produce at least two postings")
    return postings
