from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from vtrade.domain.types import MarketKey, MicroDollars, OutcomeSide


class LedgerAccount(StrEnum):
    CASH = "cash"
    OWNER_EQUITY = "owner_equity"
    POSITION_COST = "position_cost"
    FEES = "fees"
    PROCEEDS = "proceeds"
    REALIZED_PNL = "realized_pnl"
    SETTLEMENT_PAYOUT = "settlement_payout"


@dataclass(frozen=True, slots=True)
class Posting:
    account: LedgerAccount
    amount_micros: MicroDollars
    market_id: str | None = None
    outcome_id: str | None = None
    shares_delta: Decimal | None = None
    market_ref: MarketKey | str | None = None
    outcome: OutcomeSide | str | None = None
    contract_units_delta: int | None = None
    entry_fees_delta_micros: int | None = None

    def __post_init__(self) -> None:
        if (
            int(self.amount_micros) == 0
            and self.shares_delta is None
            and self.contract_units_delta is None
            and self.entry_fees_delta_micros is None
        ):
            raise ValueError("ledger postings require money or a share delta")
        if (self.market_id is None) != (self.outcome_id is None):
            raise ValueError("ledger posting market and outcome dimensions are atomic")
        if self.shares_delta is not None:
            if not self.shares_delta.is_finite() or self.shares_delta == 0:
                raise ValueError("ledger share deltas must be finite and non-zero")
            if self.account is not LedgerAccount.POSITION_COST:
                raise ValueError("only position-cost postings may change shares")
            if self.outcome_id is None:
                raise ValueError("share deltas require market and outcome dimensions")
        if (self.market_ref is None) != (self.outcome is None):
            raise ValueError("ledger posting market_ref and outcome dimensions are atomic")
        if self.market_ref is not None:
            if isinstance(self.market_ref, str):
                object.__setattr__(self, "market_ref", MarketKey(self.market_ref))
            assert self.outcome is not None
            if not isinstance(self.outcome, OutcomeSide):
                object.__setattr__(self, "outcome", OutcomeSide(self.outcome))
        if self.contract_units_delta is not None:
            if isinstance(self.contract_units_delta, bool) or self.contract_units_delta == 0:
                raise ValueError("contract unit deltas must be non-zero integers")
            if self.market_ref is None or self.outcome is None:
                raise ValueError("contract unit deltas require market_ref and outcome")
            if self.account is not LedgerAccount.POSITION_COST:
                raise ValueError("only position-cost postings may change contract units")
        if self.entry_fees_delta_micros is not None:
            if (
                isinstance(self.entry_fees_delta_micros, bool)
                or not isinstance(self.entry_fees_delta_micros, int)
                or self.entry_fees_delta_micros == 0
            ):
                raise ValueError("entry-fee deltas must be non-zero integers")
            if self.account is not LedgerAccount.FEES:
                raise ValueError("only fee postings may change entry-fee allocation")
            if self.market_ref is None or self.outcome is None:
                raise ValueError("entry-fee deltas require market_ref and outcome")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: str
    agent_id: str
    idempotency_key: str
    event_type: str
    occurred_at: datetime
    postings: tuple[Posting, ...]
    reversal_of: str | None = None

    def __post_init__(self) -> None:
        if not self.postings:
            raise ValueError("ledger entry requires postings")
        if sum(int(posting.amount_micros) for posting in self.postings) != 0:
            raise ValueError("ledger entry postings must balance to zero")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("ledger entry timestamps must be timezone-aware")


class AppendOnlyLedger:
    """Deterministic domain ledger; persistence enforces the same invariants in PostgreSQL."""

    def __init__(self, entries: Iterable[LedgerEntry] = ()) -> None:
        self._entries: list[LedgerEntry] = []
        self._idempotency: dict[str, LedgerEntry] = {}
        for entry in entries:
            self.append(entry)

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        existing = self._idempotency.get(entry.idempotency_key)
        if existing is not None:
            if existing != entry:
                raise ValueError("idempotency key reused with different ledger event")
            return existing
        self._entries.append(entry)
        self._idempotency[entry.idempotency_key] = entry
        return entry

    def balances(self, agent_id: str) -> dict[LedgerAccount, MicroDollars]:
        balances = {account: 0 for account in LedgerAccount}
        for entry in self._entries:
            if entry.agent_id == agent_id:
                for posting in entry.postings:
                    balances[posting.account] += int(posting.amount_micros)
        return {account: MicroDollars(value) for account, value in balances.items()}


ContractPosting = Posting
ContractLedgerEntry = LedgerEntry
