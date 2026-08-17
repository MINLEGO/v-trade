from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

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
