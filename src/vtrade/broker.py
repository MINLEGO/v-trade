"""Deterministic Kalshi paper execution, accounting, and settlement.

Only semantic MarketKey/YES/NO order contracts are exposed here. Venue transport and
raw evidence stay below the market-data boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from vtrade.domain.execution import (
    EconomicFill,
    FeeCalculation,
    FeeParticipantRole,
    FeePolicySnapshot,
    OrderRequest,
    OrderResult,
    OrderState,
    ReconciliationState,
    SemanticExecutionError,
    SettlementRecord,
    TimeInForce,
    operation_uuid,
)
from vtrade.domain.execution import OrderAmountType as ContractAmountType
from vtrade.domain.execution import (
    gross_cash_micros as contract_gross_cash_micros,
)
from vtrade.domain.types import (
    CanonicalLevel,
    ContractQuantity,
    MarketContext,
    MarketKey,
    MicroDollars,
    MoneyMicros,
    PriceMicros,
    ResolutionObservation,
)
from vtrade.domain.types import Side as ContractSide
from vtrade.ledger import LedgerAccount, LedgerEntry, Posting
from vtrade.liquidity import HaircutAudit, LiquidityEvidenceError, apply_best_level_haircut
from vtrade.portfolio import ContractPortfolio, ContractPosition, apply_order_fills
from vtrade.risk import check_market_concentration

# ---------------------------------------------------------------------------
# vtrade-binary-order-v1 semantic paper execution
# ---------------------------------------------------------------------------

_CENT_MICROS = 10_000


class ExactFeeCalculator:
    """Pure integer/rational implementation of ``kalshi-quadratic-v1``."""

    def calculate(
        self,
        *,
        contract_units: int,
        price_micros: int,
        policy: FeePolicySnapshot,
        action: ContractSide | str,
        accumulator_before_micros: int = 0,
    ) -> FeeCalculation:
        if contract_units <= 0:
            raise ValueError("fee quantity must be positive")
        if not 0 <= price_micros <= 1_000_000:
            raise ValueError("fee price is outside the binary range")
        if accumulator_before_micros < 0 or accumulator_before_micros >= _CENT_MICROS:
            raise ValueError("fee accumulator must be a sub-cent remainder")
        gross = int(contract_gross_cash_micros(contract_units, price_micros))
        multiplier_num, multiplier_den = policy.resolved_multiplier
        rate_num, rate_den = policy.rate
        # Raw fee in microdollars is:
        # M * R * (units / 100) * (p / 1e6) * (1 - p / 1e6) * 1e6.
        numerator = (
            multiplier_num
            * rate_num
            * contract_units
            * price_micros
            * (1_000_000 - price_micros)
        )
        denominator = multiplier_den * rate_den * 100 * 1_000_000
        raw_micros_ceil = (numerator + denominator - 1) // denominator if numerator else 0
        trade_fee = 0 if policy.waiver else ((raw_micros_ceil + 99) // 100) * 100
        revenue = gross if ContractSide(action) is ContractSide.SELL else -gross
        balance_change = revenue - trade_fee
        posted_balance_change = (balance_change // _CENT_MICROS) * _CENT_MICROS
        rounding_fee = balance_change - posted_balance_change
        accumulated = accumulator_before_micros + rounding_fee
        rebate = (accumulated // _CENT_MICROS) * _CENT_MICROS
        # A rebate is funded by prior alignment residue.  Clamp the current
        # record at zero while retaining only the sub-cent remainder.
        rebate = min(rebate, trade_fee + rounding_fee)
        after = accumulated - rebate
        net_fee = trade_fee + rounding_fee - rebate
        if net_fee < 0:
            raise ValueError("fee calculation produced a negative net fee")
        raw_nanos = (numerator * 1_000) // denominator if numerator else 0
        return FeeCalculation(
            gross_micros=MoneyMicros(gross),
            trade_fee_raw_nanos=raw_nanos,
            trade_fee_micros=MoneyMicros(trade_fee),
            rounding_fee_micros=MoneyMicros(rounding_fee),
            rebate_micros=MoneyMicros(rebate),
            net_fee_micros=MoneyMicros(net_fee),
            posted_balance_change_micros=posted_balance_change + rebate,
            accumulator_before_micros=accumulator_before_micros,
            accumulator_after_micros=after,
            price_micros=PriceMicros(price_micros),
            contract_units=ContractQuantity(contract_units),
            participant_role=FeeParticipantRole(policy.participant_role),
            policy_fingerprint=policy.fingerprint,
        )

    def calculate_fills(
        self,
        fills: Sequence[tuple[int, int]],
        *,
        policy: FeePolicySnapshot,
        action: ContractSide | str,
    ) -> tuple[FeeCalculation, ...]:
        accumulator = 0
        calculations: list[FeeCalculation] = []
        for contract_units, price_micros in fills:
            calculation = self.calculate(
                contract_units=contract_units,
                price_micros=price_micros,
                policy=policy,
                action=action,
                accumulator_before_micros=accumulator,
            )
            calculations.append(calculation)
            accumulator = calculation.accumulator_after_micros
        return tuple(calculations)


FeeCalculator = ExactFeeCalculator
calculate_fee = ExactFeeCalculator().calculate


@dataclass(frozen=True, slots=True)
class _PlannedFill:
    level_index: int
    price_micros: int
    contract_units: int


class BinaryPaperBroker:
    """Deterministic paper adapter over one refreshed canonical Kalshi context."""

    def __init__(
        self,
        *,
        maximum_market_concentration_numerator: int = 15,
        maximum_market_concentration_denominator: int = 100,
        maximum_book_age: timedelta = timedelta(seconds=15),
        fee_calculator: ExactFeeCalculator | None = None,
    ) -> None:
        if maximum_market_concentration_numerator <= 0:
            raise ValueError("market concentration numerator must be positive")
        if maximum_market_concentration_denominator <= 0:
            raise ValueError("market concentration denominator must be positive")
        if maximum_market_concentration_numerator > maximum_market_concentration_denominator:
            raise ValueError("market concentration cannot exceed one")
        if maximum_book_age < timedelta(0):
            raise ValueError("maximum book age cannot be negative")
        self.maximum_market_concentration_numerator = maximum_market_concentration_numerator
        self.maximum_market_concentration_denominator = maximum_market_concentration_denominator
        self.maximum_book_age = maximum_book_age
        self.fee_calculator = fee_calculator or ExactFeeCalculator()
        self._results: dict[tuple[str, str], OrderResult] = {}
        self._blocked_agents: set[str] = set()

    def execute(
        self,
        request: OrderRequest,
        *,
        context: MarketContext | None,
        portfolio: ContractPortfolio,
        fee_policy: FeePolicySnapshot | None,
        frozen_context_id: str | None = None,
        execution_context_id: str | None = None,
        now: datetime | None = None,
        pending: bool = False,
        account_value_micros: int | None = None,
        valuation_contexts: Mapping[MarketKey | str, MarketContext] | None = None,
    ) -> OrderResult:
        """Execute or return a side-effect-free semantic result.

        The broker owns the idempotency map in this pure form.  The PostgreSQL
        repository applies the same fingerprint and fill rules transactionally.
        """

        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("execution time must be timezone-aware")
        key = (request.agent_id, request.idempotency_key)
        existing = self._results.get(key)
        if existing is not None:
            if existing.request.fingerprint == request.fingerprint:
                return existing
            return self._rejected(
                request,
                portfolio,
                SemanticExecutionError.IDEMPOTENCY_CONFLICT,
                current_time,
                operation_id=existing.operation_id,
            )
        operation_id = operation_uuid(request.agent_id, request.idempotency_key)
        if request.agent_id in self._blocked_agents or portfolio.reconciliation_blocked:
            result = self._rejected(
                request,
                portfolio,
                SemanticExecutionError.RECONCILIATION_REQUIRED,
                current_time,
                operation_id=operation_id,
            )
            self._results[key] = result
            return result
        if pending or context is None:
            result = self._pending(
                request,
                portfolio,
                current_time,
                operation_id=operation_id,
                frozen_context_id=frozen_context_id,
            )
            self._results[key] = result
            self._blocked_agents.add(request.agent_id)
            return result
        try:
            self._validate_context(request, context, current_time)
        except (ValueError, LiquidityEvidenceError) as exc:
            code = (
                SemanticExecutionError.HAIRCUT_EVIDENCE_INSUFFICIENT
                if isinstance(exc, LiquidityEvidenceError)
                else SemanticExecutionError.STALE_BOOK
                if "stale" in str(exc).lower()
                else SemanticExecutionError.INVALID_CONTEXT
            )
            result = self._rejected(
                request,
                portfolio,
                code,
                current_time,
                operation_id=operation_id,
                message=str(exc),
            )
            self._results[key] = result
            return result
        resolved_frozen_context_id = (
            frozen_context_id
            or request.frozen_context_id
            or str(context.market.snapshot_id)
        )
        resolved_execution_context_id = execution_context_id or str(
            context.order_book.snapshot_id
        )
        if fee_policy is None:
            result = self._rejected(
                request,
                portfolio,
                SemanticExecutionError.MISSING_FEE_POLICY,
                current_time,
                operation_id=operation_id,
            )
            self._results[key] = result
            return result
        try:
            levels, haircut = apply_best_level_haircut(
                context.order_book,
                outcome=request.outcome,
                action=request.action,
            )
        except LiquidityEvidenceError as exc:
            result = self._rejected(
                request,
                portfolio,
                SemanticExecutionError.HAIRCUT_EVIDENCE_INSUFFICIENT,
                current_time,
                operation_id=operation_id,
                message=str(exc),
            )
            self._results[key] = result
            return result
        position = portfolio.position(request.market_ref, request.outcome)
        if request.action is ContractSide.SELL and position is None:
            result = self._rejected(
                request,
                portfolio,
                SemanticExecutionError.INSUFFICIENT_CONTRACTS,
                current_time,
                operation_id=operation_id,
            )
            self._results[key] = result
            return result
        planned = self._plan(request, levels, position)
        if not planned:
            result = self._rejected(
                request,
                portfolio,
                (
                    SemanticExecutionError.PRICE_LIMIT
                    if request.limit_price_micros is not None
                    else SemanticExecutionError.INSUFFICIENT_LIQUIDITY
                ),
                current_time,
                operation_id=operation_id,
                liquidity_audit=haircut,
            )
            self._results[key] = result
            return result
        planned, calculations = self._fit_cash_budget(
            request, portfolio, planned, fee_policy
        )
        filled_units = sum(item.contract_units for item in planned)
        requested_units = (
            int(request.contract_units) if request.contract_units is not None else filled_units
        )
        if request.time_in_force is TimeInForce.FOK:
            fok_complete = (
                filled_units == requested_units
                if request.contract_units is not None
                else sum(
                    int(contract_gross_cash_micros(item.contract_units, item.price_micros))
                    for item in planned
                )
                == int(request.cash_amount_micros or 0)
            )
            if not fok_complete:
                result = self._rejected(
                    request,
                    portfolio,
                    (
                        SemanticExecutionError.PRICE_LIMIT
                        if request.limit_price_micros is not None and not filled_units
                        else SemanticExecutionError.INSUFFICIENT_LIQUIDITY
                    ),
                    current_time,
                    operation_id=operation_id,
                    liquidity_audit=haircut,
                )
                self._results[key] = result
                return result
        if (
            request.contract_units is not None
            and request.action is ContractSide.SELL
            and (position is None or filled_units > position.contract_units)
        ):
            result = self._rejected(
                request,
                portfolio,
                SemanticExecutionError.INSUFFICIENT_CONTRACTS,
                current_time,
                operation_id=operation_id,
            )
            self._results[key] = result
            return result
        gross_total = sum(int(calculation.gross_micros) for calculation in calculations)
        fee_total = sum(int(calculation.net_fee_micros) for calculation in calculations)
        if request.action is ContractSide.BUY:
            if gross_total + fee_total > int(portfolio.cash_micros):
                result = self._rejected(
                    request,
                    portfolio,
                    SemanticExecutionError.INSUFFICIENT_CASH,
                    current_time,
                    operation_id=operation_id,
                    liquidity_audit=haircut,
                )
                self._results[key] = result
                return result
            try:
                account_value = (
                    account_value_micros
                    if account_value_micros is not None
                    else int(portfolio.account_value_micros(valuation_contexts))
                )
            except ValueError as exc:
                result = self._rejected(
                    request,
                    portfolio,
                    SemanticExecutionError.INVALID_CONTEXT,
                    current_time,
                    operation_id=operation_id,
                    message=str(exc),
                )
                self._results[key] = result
                return result
            concentration = check_market_concentration(
                account_value,
                int(portfolio.market_cost_basis_micros(request.market_ref)),
                gross_total,
                numerator=self.maximum_market_concentration_numerator,
                denominator=self.maximum_market_concentration_denominator,
            )
            if not concentration.approved:
                result = self._rejected(
                    request,
                    portfolio,
                    SemanticExecutionError.CONCENTRATION_LIMIT,
                    current_time,
                    operation_id=operation_id,
                    risk_check=concentration,
                )
                self._results[key] = result
                return result
        fills = tuple(
            EconomicFill(
                fill_id=f"{operation_id}:{index}",
                contract_units=ContractQuantity(item.contract_units),
                price_micros=PriceMicros(item.price_micros),
                gross_cash_micros=calculation.gross_micros,
                fee_micros=calculation.net_fee_micros,
                net_cash_delta_micros=(
                    -int(calculation.gross_micros) - int(calculation.net_fee_micros)
                    if request.action is ContractSide.BUY
                    else int(calculation.gross_micros) - int(calculation.net_fee_micros)
                ),
                filled_at=current_time,
                frozen_context_id=resolved_frozen_context_id,
                execution_context_id=resolved_execution_context_id,
                estimated_fee_micros=calculation.net_fee_micros,
            )
            for index, (item, calculation) in enumerate(zip(planned, calculations, strict=True))
        )
        try:
            updated_portfolio, ledger_entry, _accounting = apply_order_fills(
                portfolio,
                request,
                fills,
                operation_id=operation_id,
                occurred_at=current_time,
            )
        except ValueError as exc:
            code = (
                SemanticExecutionError.INSUFFICIENT_CASH
                if "cash" in str(exc)
                else SemanticExecutionError.INSUFFICIENT_CONTRACTS
            )
            result = self._rejected(
                request,
                portfolio,
                code,
                current_time,
                operation_id=operation_id,
                message=str(exc),
            )
            self._results[key] = result
            return result
        consumed = {item.level_index: item.contract_units for item in planned}
        audit = haircut.after_fills(
            consumed,
            requested_quantity_units=requested_units,
        )
        cancelled_units = max(0, requested_units - filled_units)
        state = (
            OrderState.FILLED
            if cancelled_units == 0
            else OrderState.PARTIALLY_FILLED
        )
        gross_delta = sum(
            int(fill.gross_cash_micros)
            if request.action is ContractSide.SELL
            else -int(fill.gross_cash_micros)
            for fill in fills
        )
        result = OrderResult(
            request=request,
            operation_id=operation_id,
            state=state,
            reconciliation_state=ReconciliationState.NOT_REQUIRED,
            requested_units=ContractQuantity(requested_units),
            filled_units=ContractQuantity(filled_units),
            remaining_units=ContractQuantity(cancelled_units),
            cancelled_units=ContractQuantity(cancelled_units),
            fills=fills,
            gross_cash_delta_micros=gross_delta,
            fee_micros=MoneyMicros(fee_total),
            net_cash_delta_micros=gross_delta - fee_total,
            frozen_context_id=resolved_frozen_context_id,
            execution_context_id=resolved_execution_context_id,
            submitted_at=request.created_at,
            updated_at=current_time,
            portfolio_before=portfolio,
            portfolio_after=updated_portfolio,
            ledger_entries=(ledger_entry,),
            liquidity_audit=audit,
            fee_calculations=calculations,
            risk_check=concentration if request.action is ContractSide.BUY else None,
        )
        self._results[key] = result
        return result

    def place_order(self, request: OrderRequest, **kwargs: object) -> OrderResult:
        return self.execute(request, **kwargs)  # type: ignore[arg-type]

    place = execute

    def reconcile(
        self,
        operation_id: str,
        *,
        state: ReconciliationState | str,
        evidence: Mapping[str, object] | None = None,
    ) -> OrderResult:
        target = next(
            (result for result in self._results.values() if result.operation_id == operation_id),
            None,
        )
        if target is None:
            raise ValueError("unknown order operation")
        reconciliation = ReconciliationState(state)
        if reconciliation is ReconciliationState.CONFLICT:
            self._blocked_agents.add(target.request.agent_id)
            updated = replace(
                target,
                reconciliation_state=ReconciliationState.CONFLICT,
                error_code=SemanticExecutionError.CONFLICTING_EVIDENCE,
                updated_at=max(target.updated_at, target.submitted_at),
            )
        elif reconciliation is ReconciliationState.RESOLVED:
            self._blocked_agents.discard(target.request.agent_id)
            updated = replace(
                target,
                state=OrderState.CANCELLED,
                reconciliation_state=ReconciliationState.RESOLVED,
                remaining_units=ContractQuantity(0),
                cancelled_units=ContractQuantity(
                    target.requested_units - target.filled_units
                ),
                updated_at=max(target.updated_at, target.submitted_at),
            )
        else:
            updated = replace(target, reconciliation_state=reconciliation)
        del evidence
        key = (target.request.agent_id, target.request.idempotency_key)
        self._results[key] = updated
        return updated

    def recent_activity(self, agent_id: str) -> tuple[OrderResult, ...]:
        return tuple(
            result for result in self._results.values() if result.request.agent_id == agent_id
        )

    def _validate_context(
        self, request: OrderRequest, context: MarketContext, now: datetime
    ) -> None:
        if context.market.key != request.market_ref:
            raise ValueError("execution market does not match market_ref")
        if not context.market.tradeable:
            raise ValueError("market is not active and tradeable")
        if context.order_book.observed_at > context.order_book.cutoff:
            raise ValueError("order book is newer than its cutoff")
        if request.frozen_cutoff is not None and context.order_book.cutoff < request.frozen_cutoff:
            raise ValueError("execution context predates the frozen decision cutoff")
        if now < context.order_book.observed_at:
            raise ValueError("execution cannot precede the book observation")
        if now - context.order_book.observed_at > self.maximum_book_age:
            raise ValueError("execution order book is stale")
        if request.limit_price is not None:
            context.market.price_grid.require(
                request.limit_price_micros or 0,
                field="limit_price",
            )

    def _plan(
        self,
        request: OrderRequest,
        levels: Sequence[CanonicalLevel],
        position: ContractPosition | None,
    ) -> tuple[_PlannedFill, ...]:
        target = int(request.contract_units) if request.contract_units is not None else None
        owned = position.contract_units if position is not None else None
        if request.action is ContractSide.SELL and owned is not None:
            target = owned if target is None else min(target, owned)
        remaining = target
        cash_remaining = int(request.cash_amount_micros or 0)
        planned: list[_PlannedFill] = []
        for index, level in enumerate(levels):
            limit_price = request.limit_price_micros
            if limit_price is not None:
                if request.action is ContractSide.BUY and level.price > limit_price:
                    continue
                if request.action is ContractSide.SELL and level.price < limit_price:
                    continue
            quantity = int(level.quantity)
            if remaining is not None:
                quantity = min(quantity, remaining)
            if request.amount_type is ContractAmountType.CASH:
                if level.price == 0:
                    cash_quantity = quantity
                else:
                    cash_quantity = (cash_remaining * 100) // int(level.price)
                quantity = min(quantity, cash_quantity)
            if quantity <= 0:
                continue
            planned.append(_PlannedFill(index + 1, int(level.price), quantity))
            if remaining is not None:
                remaining -= quantity
            if request.amount_type is ContractAmountType.CASH:
                cash_remaining -= int(contract_gross_cash_micros(quantity, int(level.price)))
            if remaining == 0:
                break
        return tuple(planned)

    def _fit_cash_budget(
        self,
        request: OrderRequest,
        portfolio: ContractPortfolio,
        planned: tuple[_PlannedFill, ...],
        policy: FeePolicySnapshot,
    ) -> tuple[tuple[_PlannedFill, ...], tuple[FeeCalculation, ...]]:
        def calculate(items: tuple[_PlannedFill, ...]) -> tuple[FeeCalculation, ...]:
            return self.fee_calculator.calculate_fills(
                tuple((item.contract_units, item.price_micros) for item in items),
                policy=policy,
                action=request.action,
            )

        calculations = calculate(planned)
        if request.action is not ContractSide.BUY:
            return planned, calculations
        budget = int(portfolio.cash_micros)
        total = sum(
            int(item.gross_micros) + int(item.net_fee_micros)
            for item in calculations
        )
        if total <= budget:
            return planned, calculations
        items = list(planned)
        for index in range(len(items) - 1, -1, -1):
            original = items[index]
            low, high = 0, original.contract_units
            best: tuple[_PlannedFill, ...] | None = None
            while low <= high:
                middle = (low + high) // 2
                candidate = tuple(
                    items[:index]
                    + (
                        [_PlannedFill(original.level_index, original.price_micros, middle)]
                        if middle
                        else []
                    )
                    + items[index + 1 :]
                )
                candidate_calculations = calculate(candidate) if candidate else ()
                total = sum(
                    int(item.gross_micros) + int(item.net_fee_micros)
                    for item in candidate_calculations
                )
                if total <= budget:
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            items = list(best or tuple(items[:index] + items[index + 1 :]))
        final = tuple(item for item in items if item.contract_units)
        return final, calculate(final) if final else ()

    @staticmethod
    def _rejected(
        request: OrderRequest,
        portfolio: ContractPortfolio,
        code: SemanticExecutionError,
        now: datetime,
        *,
        operation_id: str,
        message: str | None = None,
        liquidity_audit: HaircutAudit | None = None,
        risk_check: object | None = None,
    ) -> OrderResult:
        requested_units = int(request.contract_units or 0)
        return OrderResult(
            request=request,
            operation_id=operation_id,
            state=OrderState.REJECTED,
            reconciliation_state=ReconciliationState.NOT_REQUIRED,
            requested_units=ContractQuantity(requested_units),
            filled_units=ContractQuantity(0),
            remaining_units=ContractQuantity(requested_units),
            cancelled_units=ContractQuantity(0),
            gross_cash_delta_micros=0,
            fee_micros=MoneyMicros(0),
            net_cash_delta_micros=0,
            submitted_at=request.created_at,
            updated_at=now,
            error_code=code,
            message=message,
            portfolio_before=portfolio,
            portfolio_after=portfolio,
            liquidity_audit=liquidity_audit,
            risk_check=risk_check,
        )

    @staticmethod
    def _pending(
        request: OrderRequest,
        portfolio: ContractPortfolio,
        now: datetime,
        *,
        operation_id: str,
        frozen_context_id: str | None,
    ) -> OrderResult:
        requested_units = int(request.contract_units or 0)
        return OrderResult(
            request=request,
            operation_id=operation_id,
            state=OrderState.PENDING,
            reconciliation_state=ReconciliationState.REQUIRED,
            requested_units=ContractQuantity(requested_units),
            filled_units=ContractQuantity(0),
            remaining_units=ContractQuantity(requested_units),
            cancelled_units=ContractQuantity(0),
            gross_cash_delta_micros=0,
            fee_micros=MoneyMicros(0),
            net_cash_delta_micros=0,
            frozen_context_id=frozen_context_id or request.frozen_context_id,
            submitted_at=request.created_at,
            updated_at=now,
            error_code=SemanticExecutionError.RECONCILIATION_REQUIRED,
            portfolio_before=portfolio,
            portfolio_after=portfolio,
        )


KalshiPaperBroker = BinaryPaperBroker
PaperExecutionBroker = BinaryPaperBroker


class SettlementBlockedError(ValueError):
    pass


class BinarySettlementEngine:
    """FINALIZED-only, idempotent binary settlement over contract positions."""

    def __init__(self) -> None:
        self._settlements: dict[
            tuple[str, str, datetime], tuple[ContractPortfolio, SettlementRecord]
        ] = {}
        self._terminal_by_market: dict[tuple[str, str], SettlementRecord] = {}
        self._conflicted_agents: set[str] = set()

    def settle(
        self,
        *,
        observation: ResolutionObservation,
        position: ContractPosition,
        portfolio: ContractPortfolio,
        as_of: datetime,
        settled_at: datetime,
    ) -> tuple[ContractPortfolio, SettlementRecord]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("settlement as_of must be timezone-aware")
        if settled_at.tzinfo is None or settled_at.utcoffset() is None:
            raise ValueError("settlement settled_at must be timezone-aware")
        if observation.market_key != position.market_ref:
            raise ValueError("resolution market does not match position")
        if (
            not observation.terminal
            or observation.result is None
            or observation.settlement_ts is None
        ):
            raise SettlementBlockedError(
                "only an unblocked FINALIZED binary observation can settle"
            )
        if observation.observed_at > as_of or observation.settlement_ts > as_of:
            raise SettlementBlockedError("finalized evidence is newer than the settlement cutoff")
        current = portfolio.position(position.market_ref, position.outcome)
        if current != position:
            raise ValueError("settlement position does not match current portfolio")
        key = (portfolio.agent_id, position.market_ref.canonical, observation.settlement_ts)
        market_key = (portfolio.agent_id, position.market_ref.canonical)
        prior_terminal = self._terminal_by_market.get(market_key)
        if prior_terminal is not None and (
            prior_terminal.settlement_ts != observation.settlement_ts
            or prior_terminal.outcome != observation.result
        ):
            self._conflicted_agents.add(portfolio.agent_id)
            raise SettlementBlockedError("conflicting terminal settlement evidence")
        existing = self._settlements.get(key)
        if existing is not None:
            _previous_portfolio, previous_record = existing
            if (
                previous_record.outcome != observation.result
                or previous_record.contract_units != position.contract_units
            ):
                self._conflicted_agents.add(portfolio.agent_id)
                raise SettlementBlockedError("conflicting terminal settlement evidence")
            return existing
        payout = (
            int(position.contract_units) * 10_000
            if position.outcome is observation.result
            else 0
        )
        realized = (
            payout
            - int(position.gross_cost_basis_micros)
            - int(position.entry_fees_micros)
        )
        positions = tuple(
            item
            for item in portfolio.positions
            if not (item.market_ref == position.market_ref and item.outcome is position.outcome)
        )
        updated = ContractPortfolio(
            portfolio.agent_id,
            MoneyMicros(int(portfolio.cash_micros) + payout),
            positions,
            portfolio.version + 1,
            portfolio.reconciliation_blocked,
            portfolio.realized_pnl_micros + realized,
        )
        postings = [
            Posting(LedgerAccount.CASH, MicroDollars(payout)),
            Posting(
                LedgerAccount.POSITION_COST,
                MicroDollars(-int(position.gross_cost_basis_micros)),
                market_ref=position.market_ref,
                outcome=position.outcome,
                contract_units_delta=-int(position.contract_units),
            ),
        ]
        if position.entry_fees_micros:
            postings.append(
                Posting(
                    LedgerAccount.FEES,
                    MicroDollars(-int(position.entry_fees_micros)),
                    market_ref=position.market_ref,
                    outcome=position.outcome,
                    entry_fees_delta_micros=-int(position.entry_fees_micros),
                )
            )
        postings.append(
            Posting(
                LedgerAccount.REALIZED_PNL,
                MicroDollars(
                    int(position.gross_cost_basis_micros)
                    + int(position.entry_fees_micros)
                    - payout
                ),
                market_ref=position.market_ref,
                outcome=position.outcome,
            )
        )
        ledger_entry = LedgerEntry(
            id=f"ledger-settlement-{portfolio.agent_id}-{observation.settlement_ts.isoformat()}",
            agent_id=portfolio.agent_id,
            idempotency_key=(
                f"settlement:{portfolio.agent_id}:{position.market_ref.canonical}:"
                f"{observation.settlement_ts.isoformat()}"
            ),
            event_type="binary_settlement",
            occurred_at=settled_at,
            postings=tuple(postings),
        )
        record = SettlementRecord(
            settlement_id=ledger_entry.id,
            market_ref=position.market_ref,
            outcome=position.outcome,
            resolution_id=str(observation.snapshot_id),
            settlement_ts=observation.settlement_ts,
            contract_units=position.contract_units,
            gross_payout_micros=MoneyMicros(payout),
            entry_fees_deducted_micros=position.entry_fees_micros,
            realized_pnl_micros=realized,
            ledger_entry=ledger_entry,
        )
        self._settlements[key] = (updated, record)
        self._terminal_by_market[market_key] = record
        return updated, record

    def is_blocked(self, agent_id: str) -> bool:
        return agent_id in self._conflicted_agents


FinalizedSettlementEngine = BinarySettlementEngine
ContractSettlementEngine = BinarySettlementEngine
BinaryOrderRequest = OrderRequest
SemanticOrderRequest = OrderRequest
BinaryOrderResult = OrderResult
FeeSnapshot = FeePolicySnapshot
ContractSettlement = SettlementRecord

