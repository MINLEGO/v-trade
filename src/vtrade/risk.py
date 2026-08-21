from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from vtrade.domain.execution import SemanticExecutionError
from vtrade.domain.types import MicroDollars


@dataclass(frozen=True, slots=True)
class MarketCapacity:
    """Cost-basis capacity for one market in a coherent account snapshot."""

    held_cost_basis_micros: MicroDollars
    pending_buy_reserved_cost_basis_micros: MicroDollars
    market_limit_micros: MicroDollars
    remaining_capacity_micros: MicroDollars


def calculate_market_capacity(
    account_value_micros: int,
    maximum_market_cost_basis_fraction: Decimal,
    *,
    held_cost_basis_micros: int = 0,
    pending_buy_reserved_cost_basis_micros: int = 0,
) -> MarketCapacity:
    """Calculate the executable BUY capacity for one market.

    The returned capacity is a snapshot calculation. It does not reserve anything or
    guarantee that a later execution will be accepted; the broker must revalidate it
    under its execution lock.
    """

    if account_value_micros < 0:
        raise ValueError("account value cannot be negative")
    if (
        not maximum_market_cost_basis_fraction.is_finite()
        or not Decimal(0) < maximum_market_cost_basis_fraction <= Decimal(1)
    ):
        raise ValueError("maximum market cost-basis fraction must be between zero and one")
    if held_cost_basis_micros < 0:
        raise ValueError("held cost basis cannot be negative")
    if pending_buy_reserved_cost_basis_micros < 0:
        raise ValueError("pending BUY reserved cost basis cannot be negative")

    market_limit_micros = int(
        (Decimal(account_value_micros) * maximum_market_cost_basis_fraction).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    remaining_capacity_micros = max(
        0,
        market_limit_micros
        - held_cost_basis_micros
        - pending_buy_reserved_cost_basis_micros,
    )
    return MarketCapacity(
        held_cost_basis_micros=MicroDollars(held_cost_basis_micros),
        pending_buy_reserved_cost_basis_micros=MicroDollars(
            pending_buy_reserved_cost_basis_micros
        ),
        market_limit_micros=MicroDollars(market_limit_micros),
        remaining_capacity_micros=MicroDollars(remaining_capacity_micros),
    )


@dataclass(frozen=True, slots=True)
class ConcentrationCheck:
    """Exact rational concentration decision recorded with an order."""

    account_value_micros: int
    existing_market_basis_micros: int
    proposed_market_basis_micros: int
    numerator: int
    denominator: int
    approved: bool
    rejection_code: SemanticExecutionError | None = None

    @property
    def resulting_market_basis_micros(self) -> int:
        return self.existing_market_basis_micros + self.proposed_market_basis_micros


def check_market_concentration(
    account_value_micros: int,
    existing_market_basis_micros: int,
    proposed_market_basis_micros: int,
    *,
    numerator: int = 15,
    denominator: int = 100,
) -> ConcentrationCheck:
    """Apply the 15% rule without Decimal rounding or an implicit reservation.

    Equality is accepted.  The proposed value is the gross BUY cost that will be
    committed by the fill; a pending or ambiguous order is intentionally not added
    to this snapshot.
    """

    values = (
        account_value_micros,
        existing_market_basis_micros,
        proposed_market_basis_micros,
        numerator,
        denominator,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("concentration inputs must be integers")
    if account_value_micros < 0:
        raise ValueError("account value cannot be negative")
    if existing_market_basis_micros < 0 or proposed_market_basis_micros < 0:
        raise ValueError("market basis cannot be negative")
    if numerator <= 0 or denominator <= 0 or numerator > denominator:
        raise ValueError("concentration fraction must be between zero and one")
    approved = (
        (existing_market_basis_micros + proposed_market_basis_micros) * denominator
        <= account_value_micros * numerator
    )
    return ConcentrationCheck(
        account_value_micros,
        existing_market_basis_micros,
        proposed_market_basis_micros,
        numerator,
        denominator,
        approved,
        None if approved else SemanticExecutionError.CONCENTRATION_LIMIT,
    )


def concentration_allowed(
    account_value_micros: int,
    market_basis_micros: int,
    *,
    numerator: int = 15,
    denominator: int = 100,
) -> bool:
    """Small pure predicate used by risk and broker tests."""

    return check_market_concentration(
        account_value_micros,
        0,
        market_basis_micros,
        numerator=numerator,
        denominator=denominator,
    ).approved


# Vocabulary aliases used by the execution boundary.
RiskCheck = ConcentrationCheck
evaluate_concentration = check_market_concentration
check_concentration = check_market_concentration
