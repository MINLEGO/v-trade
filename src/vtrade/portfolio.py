from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, cast

from vtrade.domain.execution import EconomicFill, OrderRequest
from vtrade.domain.ports import JsonObject
from vtrade.domain.types import (
    ContractQuantity,
    MarketContext,
    MarketKey,
    MicroDollars,
    MoneyMicros,
    OutcomeSide,
    Side,
    utc_now,
)
from vtrade.ledger import AppendOnlyLedger, LedgerAccount, LedgerEntry, Posting

DEFAULT_PAGE_LIMIT = 100
MAXIMUM_PAGE_LIMIT = 200
MAXIMUM_RESULT_TOKENS = 24_000


class PortfolioPaginationError(ValueError):
    pass


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]
_CursorFactory = Callable[[], str]


class PostgresPortfolioHandler:
    """Bound, snapshot-backed implementation of the get_portfolio tool."""

    def __init__(
        self,
        database_url: str,
        *,
        agent_id: uuid.UUID,
        agent_cycle_id: uuid.UUID,
        connect: _Connect | None = None,
        cursor_factory: _CursorFactory | None = None,
        maximum_result_tokens: int = MAXIMUM_RESULT_TOKENS,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        if maximum_result_tokens <= 0 or maximum_result_tokens > MAXIMUM_RESULT_TOKENS:
            raise ValueError("portfolio result limit must be between 1 and 24000")
        self._database_url = database_url
        self._agent_id = agent_id
        self._agent_cycle_id = agent_cycle_id
        self._connect = connect or _default_connect
        self._cursor_factory = cursor_factory or (lambda: secrets.token_urlsafe(32))
        self._maximum_result_tokens = maximum_result_tokens

    def __call__(self, arguments: JsonObject) -> JsonObject:
        return self.handle(arguments)

    def handle(self, arguments: JsonObject) -> JsonObject:
        cursor_token, limit = _validate_arguments(arguments)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            if cursor_token is None:
                snapshot_id, after_position_id = self._load_or_create_snapshot(cursor)
            else:
                snapshot_id, after_position_id = self._resolve_cursor(cursor, cursor_token)

            rows = self._read_rows(cursor, snapshot_id, after_position_id, limit + 1)
            items, has_more = _bounded_items(
                rows,
                requested_limit=limit,
                maximum_result_tokens=self._maximum_result_tokens,
            )
            next_cursor: str | None = None
            if has_more:
                if not items:
                    raise PortfolioPaginationError(
                        "one portfolio item exceeds the 24000-token result ceiling"
                    )
                next_cursor = self._cursor_factory()
                _validate_cursor_token(next_cursor)
                last_position_id = uuid.UUID(str(items[-1][0]))
                self._store_cursor(
                    cursor,
                    token=next_cursor,
                    snapshot_id=snapshot_id,
                    after_position_id=last_position_id,
                )
            output: JsonObject = {
                "items": [item[1] for item in items],
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
            if _token_upper_bound(output) > self._maximum_result_tokens:
                raise RuntimeError("portfolio page bound invariant violated")
            return output

    def _load_or_create_snapshot(self, cursor: _Cursor) -> tuple[uuid.UUID, None]:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"portfolio-snapshot:{self._agent_cycle_id}",),
        )
        cursor.execute(
            "SELECT ac.agent_id, ac.data_cutoff, a.portfolio_version "
            "FROM agent_cycles ac JOIN agents a ON a.id = ac.agent_id "
            "WHERE ac.id = %s FOR SHARE",
            (self._agent_cycle_id,),
        )
        cycle = cursor.fetchone()
        if cycle is None or uuid.UUID(str(cycle[0])) != self._agent_id:
            raise PortfolioPaginationError("agent cycle is not authorized for this agent")
        cursor.execute(
            "SELECT id FROM portfolio_query_snapshots "
            "WHERE agent_cycle_id = %s AND agent_id = %s AND portfolio_version = %s",
            (self._agent_cycle_id, self._agent_id, cycle[2]),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return uuid.UUID(str(existing[0])), None

        snapshot_id = uuid.uuid4()
        cursor.execute(
            "INSERT INTO portfolio_query_snapshots "
            "(id, agent_cycle_id, agent_id, data_cutoff, portfolio_version) "
            "VALUES (%s, %s, %s, %s, %s)",
            (snapshot_id, self._agent_cycle_id, self._agent_id, cycle[1], cycle[2]),
        )
        cursor.execute(
            "INSERT INTO portfolio_snapshot_positions (snapshot_id, position_id, item) "
            "SELECT %s, p.id, jsonb_build_object(" 
            "'position_id', p.id::text, 'market_id', m.id::text, "
            "'market_question', m.question, 'outcome_id', o.id::text, "
            "'venue_token_id', o.venue_token_id, "
            "'outcome', o.name, 'shares', p.shares::text, "
            "'average_cost', p.average_cost::text, "
            "'cost_basis_micros', p.cost_basis_micros, "
            "'entry_fees_micros', p.entry_fees_micros, "
            "'realized_pnl_micros', p.realized_pnl_micros, "
            "'updated_at', to_char(p.updated_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')) "
            "FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
            "JOIN markets m ON m.id = o.market_id "
            "WHERE p.agent_id = %s AND p.shares > 0",
            (snapshot_id, self._agent_id),
        )
        return snapshot_id, None

    def _resolve_cursor(self, cursor: _Cursor, token: str) -> tuple[uuid.UUID, uuid.UUID]:
        cursor.execute(
            "SELECT snapshot_id, after_position_id FROM portfolio_page_cursors "
            "WHERE cursor_hash = %s AND agent_id = %s AND agent_cycle_id = %s",
            (_cursor_hash(token), self._agent_id, self._agent_cycle_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise PortfolioPaginationError("invalid or foreign portfolio cursor")
        return uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))

    def _read_rows(
        self,
        cursor: _Cursor,
        snapshot_id: uuid.UUID,
        after_position_id: uuid.UUID | None,
        limit: int,
    ) -> list[tuple[uuid.UUID, JsonObject]]:
        cursor.execute(
            "SELECT position_id, item FROM portfolio_snapshot_positions "
            "WHERE snapshot_id = %s AND (%s::uuid IS NULL OR position_id > %s::uuid) "
            "ORDER BY position_id ASC LIMIT %s",
            (snapshot_id, after_position_id, after_position_id, limit),
        )
        rows: list[tuple[uuid.UUID, JsonObject]] = []
        for row in cursor.fetchall():
            item = row[1]
            if not isinstance(item, Mapping):
                raise RuntimeError("portfolio snapshot item is not a JSON object")
            rows.append((uuid.UUID(str(row[0])), cast(JsonObject, dict(item))))
        return rows

    def _store_cursor(
        self,
        cursor: _Cursor,
        *,
        token: str,
        snapshot_id: uuid.UUID,
        after_position_id: uuid.UUID,
    ) -> None:
        cursor.execute(
            "INSERT INTO portfolio_page_cursors "
            "(cursor_hash, snapshot_id, agent_id, agent_cycle_id, after_position_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                _cursor_hash(token),
                snapshot_id,
                self._agent_id,
                self._agent_cycle_id,
                after_position_id,
            ),
        )


class PostgresContractPortfolioHandler:
    """Read the Kalshi portfolio projection in the semantic vocabulary."""

    def __init__(
        self,
        database_url: str,
        *,
        agent_id: uuid.UUID,
        connect: _Connect | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._agent_id = agent_id
        self._connect = connect or _default_connect

    def __call__(self, _arguments: JsonObject | None = None) -> JsonObject:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.id, m.market_ref, p.outcome_side, p.contract_units, "
                "p.gross_cost_basis_micros, p.entry_fees_micros, p.realized_pnl_micros, "
                "p.updated_at FROM positions p JOIN markets m ON m.id = p.market_id "
                "WHERE p.agent_id = %s AND p.contract_units > 0 "
                "ORDER BY m.market_ref, p.outcome_side",
                (self._agent_id,),
            )
            rows = cursor.fetchall()
        items: list[JsonObject] = []
        for row in rows:
            updated_at = row[7]
            items.append(
                {
                    "position_id": str(row[0]),
                    "market_ref": str(row[1]),
                    "outcome": str(row[2]),
                    "contract_units": int(str(row[3])),
                    "gross_cost_basis_micros": int(str(row[4])),
                    "entry_fees_micros": int(str(row[5])),
                    "realized_pnl_micros": int(str(row[6])),
                    "updated_at": (
                        updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
                        if isinstance(updated_at, datetime)
                        else str(updated_at)
                    ),
                }
            )
        return {"items": items, "next_cursor": None, "has_more": False}


ContractPortfolioHandler = PostgresContractPortfolioHandler


def _validate_arguments(arguments: JsonObject) -> tuple[str | None, int]:
    unknown = set(arguments) - {"cursor", "limit"}
    if unknown:
        raise PortfolioPaginationError(f"unknown get_portfolio arguments: {sorted(unknown)}")
    cursor = arguments.get("cursor")
    if cursor is not None:
        if not isinstance(cursor, str):
            raise PortfolioPaginationError("cursor must be a string")
        _validate_cursor_token(cursor)
    limit = arguments.get("limit", DEFAULT_PAGE_LIMIT)
    valid_limit = isinstance(limit, int) and not isinstance(limit, bool)
    if not valid_limit or not 1 <= limit <= MAXIMUM_PAGE_LIMIT:
        raise PortfolioPaginationError("limit must be an integer between 1 and 200")
    return cursor, limit


def _validate_cursor_token(token: str) -> None:
    if not token or len(token.encode("utf-8")) > 64:
        raise PortfolioPaginationError("cursor must be a non-empty opaque string")


def _bounded_items(
    rows: Sequence[tuple[uuid.UUID, JsonObject]],
    *,
    requested_limit: int,
    maximum_result_tokens: int,
) -> tuple[list[tuple[uuid.UUID, JsonObject]], bool]:
    selected: list[tuple[uuid.UUID, JsonObject]] = []
    for row in rows[:requested_limit]:
        candidate = [*selected, row]
        candidate_has_more = len(candidate) < len(rows)
        probe: JsonObject = {
            "items": [item[1] for item in candidate],
            "next_cursor": "x" * 64 if candidate_has_more else None,
            "has_more": candidate_has_more,
        }
        if _token_upper_bound(probe) > maximum_result_tokens:
            break
        selected = candidate
    has_more = len(selected) < len(rows)
    return selected, has_more


def _token_upper_bound(value: object) -> int:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return max(1, len(raw.encode("utf-8")))


def _cursor_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# vtrade-binary-order-v1 portfolio projection
# ---------------------------------------------------------------------------


def _market_key(value: MarketKey | str) -> MarketKey:
    return value if isinstance(value, MarketKey) else MarketKey(value)


def _pro_rata(total: int, part: int, whole: int) -> int:
    if total < 0 or part < 0 or whole <= 0 or part > whole:
        raise ValueError("invalid proportional accounting input")
    if part == whole:
        return total
    return int(
        (Decimal(total) * Decimal(part) / Decimal(whole)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


@dataclass(frozen=True, slots=True)
class ContractPosition:
    """One long YES/NO position measured in hundredths of a contract."""

    market_ref: MarketKey | str
    outcome: OutcomeSide | str
    contract_units: ContractQuantity
    gross_cost_basis_micros: MoneyMicros
    entry_fees_micros: MoneyMicros = field(default=MoneyMicros(0))
    realized_pnl_micros: int = 0
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_ref", _market_key(self.market_ref))
        object.__setattr__(self, "outcome", OutcomeSide(self.outcome))
        if self.contract_units < 0:
            raise ValueError("position contract units cannot be negative")
        if self.gross_cost_basis_micros < 0 or self.entry_fees_micros < 0:
            raise ValueError("position cost and entry fees cannot be negative")
        if self.contract_units == 0 and (
            self.gross_cost_basis_micros != 0 or self.entry_fees_micros != 0
        ):
            raise ValueError("an empty position cannot retain basis or entry fees")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("position updated_at must be timezone-aware")

    @property
    def quantity_units(self) -> ContractQuantity:
        return self.contract_units

    @property
    def cost_basis_micros(self) -> MoneyMicros:
        return self.gross_cost_basis_micros

    @property
    def contracts(self) -> Decimal:
        return Decimal(self.contract_units) / Decimal(100)

    @property
    def average_price_micros(self) -> int:
        if self.contract_units == 0:
            return 0
        return int(
            (Decimal(self.gross_cost_basis_micros) * Decimal(100) / Decimal(self.contract_units))
            .to_integral_value(rounding=ROUND_HALF_UP)
        )


@dataclass(frozen=True, slots=True)
class ContractPortfolio:
    """Immutable portfolio snapshot used by the semantic paper broker."""

    agent_id: str
    cash_micros: MoneyMicros
    positions: tuple[ContractPosition, ...] = ()
    version: int = 0
    reconciliation_blocked: bool = False
    realized_pnl_micros: int = 0

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("portfolio agent_id is required")
        if self.cash_micros < 0:
            raise ValueError("portfolio cash cannot be negative")
        if self.version < 0:
            raise ValueError("portfolio version cannot be negative")
        keys = {(position.market_ref, position.outcome) for position in self.positions}
        if len(keys) != len(self.positions):
            raise ValueError("portfolio cannot contain duplicate market outcomes")

    def position(
        self, market_ref: MarketKey | str, outcome: OutcomeSide | str
    ) -> ContractPosition | None:
        key = (_market_key(market_ref), OutcomeSide(outcome))
        return next(
            (
                position
                for position in self.positions
                if (_market_key(position.market_ref), OutcomeSide(position.outcome)) == key
            ),
            None,
        )

    def market_cost_basis_micros(self, market_ref: MarketKey | str) -> MoneyMicros:
        key = _market_key(market_ref)
        return MoneyMicros(
            sum(
                int(position.gross_cost_basis_micros)
                for position in self.positions
                if position.market_ref == key
            )
        )

    def account_value_micros(
        self,
        contexts: Mapping[MarketKey | str, MarketContext] | None = None,
    ) -> MoneyMicros:
        """Value open positions at a cutoff-compatible executable bid.

        A missing bid is a hard error.  The caller may choose to reject the order
        rather than silently valuing an exposure at zero.
        """

        total = int(self.cash_micros)
        for position in self.positions:
            if contexts is None:
                total += int(position.gross_cost_basis_micros)
                continue
            key = _market_key(position.market_ref)
            context = contexts.get(key) or contexts.get(key.market_ref)
            if context is None:
                raise ValueError("missing context for held position valuation")
            level = context.order_book.best_bid(position.outcome)
            if level is None:
                raise ValueError("held position has no cutoff-compatible bid")
            total += _position_value(position.contract_units, int(level.price))
        return MoneyMicros(total)

    def with_reconciliation_block(self, blocked: bool = True) -> ContractPortfolio:
        return ContractPortfolio(
            self.agent_id,
            self.cash_micros,
            self.positions,
            self.version,
            blocked,
            self.realized_pnl_micros,
        )


@dataclass(frozen=True, slots=True)
class FillAccounting:
    fill: EconomicFill
    removed_basis_micros: int
    removed_entry_fees_micros: int
    realized_pnl_micros: int


def apply_order_fills(
    portfolio: ContractPortfolio,
    request: OrderRequest,
    fills: Sequence[EconomicFill],
    *,
    operation_id: str,
    occurred_at: datetime,
) -> tuple[ContractPortfolio, LedgerEntry, tuple[FillAccounting, ...]]:
    """Apply all confirmed fills atomically and build one balanced ledger event."""

    if request.agent_id != portfolio.agent_id:
        raise ValueError("order agent does not own the portfolio")
    if not fills:
        raise ValueError("financial accounting requires at least one fill")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("accounting timestamp must be timezone-aware")
    working = portfolio
    accounting: list[FillAccounting] = []
    cash_delta = 0
    position_cost_delta = 0
    units_delta = 0
    fee_expense_delta = 0
    realized_posting = 0
    for fill in fills:
        if request.action is Side.BUY:
            current = working.position(request.market_ref, request.outcome)
            current_units = current.contract_units if current else 0
            current_basis = int(current.gross_cost_basis_micros) if current else 0
            current_fees = int(current.entry_fees_micros) if current else 0
            new_units = current_units + int(fill.contract_units)
            new_basis = current_basis + int(fill.gross_cash_micros)
            new_fees = current_fees + int(fill.fee_micros)
            updated = ContractPosition(
                request.market_ref,
                request.outcome,
                ContractQuantity(new_units),
                MoneyMicros(new_basis),
                MoneyMicros(new_fees),
                current.realized_pnl_micros if current else 0,
                fill.filled_at,
            )
            cash_change = int(fill.net_cash_delta_micros)
            if cash_change >= 0:
                raise ValueError("BUY fill must have a negative cash delta")
            working = _replace_position(
                working,
                updated,
                cash_micros=int(working.cash_micros) + cash_change,
                realized_pnl_micros=working.realized_pnl_micros,
            )
            removed_basis = 0
            removed_fees = 0
            realized = 0
            fee_expense_delta += int(fill.fee_micros)
            position_cost_delta += int(fill.gross_cash_micros)
            units_delta += int(fill.contract_units)
            cash_delta += cash_change
        else:
            current = working.position(request.market_ref, request.outcome)
            if current is None or current.contract_units < int(fill.contract_units):
                raise ValueError("SELL fill exceeds the owned contract position")
            removed_basis = _pro_rata(
                int(current.gross_cost_basis_micros),
                int(fill.contract_units),
                current.contract_units,
            )
            removed_fees = _pro_rata(
                int(current.entry_fees_micros),
                int(fill.contract_units),
                current.contract_units,
            )
            remaining_units = current.contract_units - int(fill.contract_units)
            remaining_basis = int(current.gross_cost_basis_micros) - removed_basis
            remaining_fees = int(current.entry_fees_micros) - removed_fees
            realized = (
                int(fill.gross_cash_micros)
                - removed_basis
                - removed_fees
                - int(fill.fee_micros)
            )
            updated = ContractPosition(
                request.market_ref,
                request.outcome,
                ContractQuantity(remaining_units),
                MoneyMicros(remaining_basis),
                MoneyMicros(remaining_fees),
                current.realized_pnl_micros + realized,
                fill.filled_at,
            )
            cash_change = int(fill.net_cash_delta_micros)
            if cash_change <= 0:
                raise ValueError("SELL fill must have a positive cash delta")
            working = _replace_position(
                working,
                updated,
                cash_micros=int(working.cash_micros) + cash_change,
                realized_pnl_micros=working.realized_pnl_micros + realized,
            )
            fee_expense_delta += int(fill.fee_micros) - removed_fees
            position_cost_delta -= removed_basis
            units_delta -= int(fill.contract_units)
            realized_posting += removed_basis + removed_fees - int(fill.gross_cash_micros)
            cash_delta += cash_change
        if working.cash_micros < 0:
            raise ValueError("fill accounting would make cash negative")
        accounting.append(FillAccounting(fill, removed_basis, removed_fees, realized))

    postings = [
        Posting(LedgerAccount.CASH, MicroDollars(cash_delta)),
        Posting(
            LedgerAccount.POSITION_COST,
            MicroDollars(position_cost_delta),
            market_ref=request.market_ref,
            outcome=request.outcome,
            contract_units_delta=units_delta,
        ),
    ]
    if fee_expense_delta:
        postings.append(
            Posting(
                LedgerAccount.FEES,
                MicroDollars(fee_expense_delta),
                market_ref=request.market_ref,
                outcome=request.outcome,
                entry_fees_delta_micros=(
                    sum(item.fill.fee_micros for item in accounting)
                    if request.action is Side.BUY
                    else -sum(item.removed_entry_fees_micros for item in accounting)
                ),
            )
        )
    if realized_posting:
        postings.append(
            Posting(
                LedgerAccount.REALIZED_PNL,
                MicroDollars(realized_posting),
                market_ref=request.market_ref,
                outcome=request.outcome,
            )
        )
    entry = LedgerEntry(
        id=f"ledger-order-{operation_id}",
        agent_id=portfolio.agent_id,
        idempotency_key=f"order-accounting:{operation_id}",
        event_type="paper_order_fill",
        occurred_at=occurred_at,
        postings=tuple(postings),
    )
    result = ContractPortfolio(
        working.agent_id,
        working.cash_micros,
        tuple(
            sorted(
                working.positions,
                key=lambda item: _market_key(item.market_ref).canonical
                + OutcomeSide(item.outcome).value,
            )
        ),
        portfolio.version + 1,
        portfolio.reconciliation_blocked,
        working.realized_pnl_micros,
    )
    return result, entry, tuple(accounting)


def _replace_position(
    portfolio: ContractPortfolio,
    position: ContractPosition,
    *,
    cash_micros: int,
    realized_pnl_micros: int,
) -> ContractPortfolio:
    positions = [
        item
        for item in portfolio.positions
        if not (item.market_ref == position.market_ref and item.outcome is position.outcome)
    ]
    if position.contract_units:
        positions.append(position)
    return ContractPortfolio(
        portfolio.agent_id,
        MoneyMicros(cash_micros),
        tuple(
            sorted(
                positions,
                key=lambda item: _market_key(item.market_ref).canonical
                + OutcomeSide(item.outcome).value,
            )
        ),
        portfolio.version,
        portfolio.reconciliation_blocked,
        realized_pnl_micros,
    )


def _position_value(contract_units: int, price_micros: int) -> int:
    numerator = contract_units * price_micros
    quotient, remainder = divmod(numerator, 100)
    return quotient + (1 if remainder >= 50 else 0)


def replay_contract_portfolio(
    initial: ContractPortfolio, entries: Sequence[LedgerEntry]
) -> ContractPortfolio:
    """Replay contract-dimension postings for an audit/checksum verification."""

    ledger = AppendOnlyLedger()
    cash = int(initial.cash_micros)
    positions = {
        (position.market_ref, position.outcome): position for position in initial.positions
    }
    realized_pnl_micros = initial.realized_pnl_micros
    for entry in entries:
        if entry.agent_id != initial.agent_id:
            raise ValueError("ledger replay cannot mix agents")
        ledger.append(entry)
        cash += sum(
            int(posting.amount_micros)
            for posting in entry.postings
            if posting.account is LedgerAccount.CASH
        )
        for posting in entry.postings:
            if posting.contract_units_delta is None:
                continue
            if posting.market_ref is None or posting.outcome is None:
                raise ValueError("contract posting dimensions are incomplete")
            key = (_market_key(posting.market_ref), OutcomeSide(posting.outcome))
            current = positions.get(key)
            units = (current.contract_units if current else 0) + posting.contract_units_delta
            basis = (int(current.gross_cost_basis_micros) if current else 0) + int(
                posting.amount_micros
            )
            entry_fee_delta = sum(
                int(item.entry_fees_delta_micros)
                for item in entry.postings
                if item.entry_fees_delta_micros is not None
                and item.market_ref is not None
                and _market_key(item.market_ref) == key[0]
                and item.outcome is not None
                and OutcomeSide(item.outcome) is key[1]
            )
            fee_expense_delta = sum(
                int(item.amount_micros)
                for item in entry.postings
                if item.account is LedgerAccount.FEES
                and item.market_ref is not None
                and _market_key(item.market_ref) == key[0]
                and item.outcome is not None
                and OutcomeSide(item.outcome) is key[1]
            )
            entry_fees = (int(current.entry_fees_micros) if current else 0) + entry_fee_delta
            realized_delta = -sum(
                int(item.amount_micros)
                for item in entry.postings
                if item.account is LedgerAccount.REALIZED_PNL
                and item.market_ref is not None
                and _market_key(item.market_ref) == key[0]
                and item.outcome is not None
                and OutcomeSide(item.outcome) is key[1]
            )
            if posting.contract_units_delta < 0:
                realized_delta -= fee_expense_delta - entry_fee_delta
            realized = (current.realized_pnl_micros if current else 0) + realized_delta
            realized_pnl_micros += realized_delta
            if units < 0 or basis < 0 or entry_fees < 0:
                raise ValueError("ledger replay produced negative position state")
            if units == 0:
                if basis != 0 or entry_fees != 0:
                    raise ValueError("ledger replay left an empty position with accounting state")
                positions.pop(key, None)
            else:
                positions[key] = ContractPosition(
                    key[0],
                    key[1],
                    ContractQuantity(units),
                    MoneyMicros(basis),
                    MoneyMicros(entry_fees),
                    realized,
                    entry.occurred_at,
                )
    if cash < 0:
        raise ValueError("ledger replay produced negative cash")
    return ContractPortfolio(
        initial.agent_id,
        MoneyMicros(cash),
        tuple(positions.values()),
        initial.version + len(entries),
        initial.reconciliation_blocked,
        realized_pnl_micros,
    )


# Discoverable aliases for callers migrating to the binary vocabulary.
Position = ContractPosition
Portfolio = ContractPortfolio
BinaryPosition = ContractPosition
BinaryPortfolio = ContractPortfolio
apply_fills = apply_order_fills
apply_fill = apply_order_fills


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))
