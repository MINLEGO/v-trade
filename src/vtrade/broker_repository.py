from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast

from vtrade.broker import ExecutionResult, SettlementResult
from vtrade.ledger import LedgerEntry
from vtrade.order_execution import LiveExecutionAudit


class _Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class PersistenceResult:
    record_id: uuid.UUID
    created: bool
    fingerprint: str


class PostgresBrokerRepository:
    """Atomic, agent-serialized persistence for broker and settlement results.

    The database idempotency key is checked under an agent advisory transaction lock.
    Reuse with a different payload fails closed instead of silently returning stale state.
    """

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def persist_execution(
        self,
        result: ExecutionResult,
        *,
        agent_id: uuid.UUID,
        intent_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
        live_audit: LiveExecutionAudit | None = None,
    ) -> PersistenceResult:
        if str(agent_id) != result.order.agent_id:
            raise ValueError("database agent does not own the execution result")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            return self.persist_execution_locked(
                cursor,
                result,
                agent_id=agent_id,
                intent_id=intent_id,
                market_id=market_id,
                outcome_id=outcome_id,
                snapshot_id=snapshot_id,
                live_audit=live_audit,
            )

    def persist_execution_locked(
        self,
        cursor: _Cursor,
        result: ExecutionResult,
        *,
        agent_id: uuid.UUID,
        intent_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
        live_audit: LiveExecutionAudit | None = None,
    ) -> PersistenceResult:
        """Persist using a caller-owned transaction that already holds the agent lock."""
        if str(agent_id) != result.order.agent_id:
            raise ValueError("database agent does not own the execution result")
        fingerprint = _fingerprint(result)
        order_id = _stable_database_uuid("order", result.order.id)
        idempotency_key = f"paper-order:{result.order.id}"
        _validate_execution_relations(
            cursor,
            result,
            agent_id=agent_id,
            intent_id=intent_id,
            market_id=market_id,
            outcome_id=outcome_id,
            snapshot_id=snapshot_id,
            live_audit=live_audit,
        )
        if live_audit is not None:
            _persist_live_audit(cursor, intent_id, live_audit)
        existing = _existing(cursor, "orders", idempotency_key)
        if existing is not None:
            _assert_same_fingerprint(existing, fingerprint)
            return PersistenceResult(existing[0], False, fingerprint)
        if result.status.value != "rejected":
            _assert_portfolio_version(cursor, agent_id, result.portfolio_before.version)
        executed_at = result.executed_at or result.order.created_at
        rejected_at = executed_at if result.rejection_code is not None else None
        accepted_at = None if rejected_at is not None else executed_at
        cursor.execute(
            "INSERT INTO orders "
            "(id, intent_id, policy, status, requested_shares, accepted_at, "
            "rejected_at, rejection_code, idempotency_key, created_at, "
            "liquidity_time_in_force, executed_at, execution_fingerprint) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                order_id,
                intent_id,
                result.policy.value,
                result.status.value,
                result.order.shares,
                accepted_at,
                rejected_at,
                result.rejection_code.value if result.rejection_code else None,
                idempotency_key,
                result.order.created_at,
                result.order.liquidity_time_in_force.value,
                executed_at,
                fingerprint,
            ),
        )
        for fill in result.fills:
            if snapshot_id is None or result.fee_policy is None:
                raise ValueError("accepted execution requires persisted market context")
            cursor.execute(
                "INSERT INTO fills "
                "(id, order_id, fill_index, shares, price, gross_micros, fee_micros, "
                "snapshot_id, idempotency_key, filled_at, fee_rate, fee_exponent, "
                "fee_taker_only, fee_formula_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    _stable_database_uuid("fill", fill.id),
                    order_id,
                    fill.fill_index,
                    fill.shares,
                    fill.price,
                    int(fill.gross_micros),
                    int(fill.fee_micros),
                    snapshot_id,
                    f"paper-fill:{fill.id}",
                    fill.filled_at,
                    result.fee_policy.rate,
                    result.fee_policy.exponent,
                    result.fee_policy.taker_only,
                    result.fee_policy.formula_version,
                ),
            )
        for entry in result.ledger_entries:
            _insert_ledger(
                cursor,
                entry,
                agent_id=agent_id,
                source_table="orders",
                source_id=order_id,
                market_id=market_id,
                outcome_id=outcome_id,
            )
        if result.status.value != "rejected":
            _upsert_position(
                cursor,
                result,
                agent_id=agent_id,
                market_id=market_id,
                outcome_id=outcome_id,
            )
            _advance_portfolio_version(cursor, agent_id, result.portfolio.version)
        return PersistenceResult(order_id, True, fingerprint)

    def persist_settlement(
        self,
        result: SettlementResult,
        *,
        agent_id: uuid.UUID,
        position_id: uuid.UUID,
        resolution_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
    ) -> PersistenceResult:
        if str(agent_id) != result.portfolio.agent_id:
            raise ValueError("database agent does not own the settlement result")
        fingerprint = _fingerprint(result)
        settlement_id = _stable_database_uuid("settlement", result.settlement_id)
        idempotency_key = result.ledger_entry.idempotency_key
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            _validate_settlement_relations(
                cursor,
                result,
                agent_id=agent_id,
                position_id=position_id,
                resolution_id=resolution_id,
                market_id=market_id,
                outcome_id=outcome_id,
            )
            existing = _existing(cursor, "settlements", idempotency_key)
            if existing is not None:
                _assert_same_fingerprint(existing, fingerprint)
                return PersistenceResult(existing[0], False, fingerprint)
            _assert_portfolio_version(cursor, agent_id, result.portfolio_before.version)
            cursor.execute(
                "INSERT INTO settlements "
                "(id, agent_id, position_id, resolution_id, shares, payout_micros, "
                "realized_pnl_micros, idempotency_key, settled_at, as_of_cutoff, "
                "execution_fingerprint) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    settlement_id,
                    agent_id,
                    position_id,
                    resolution_id,
                    result.position.shares,
                    int(result.payout_micros),
                    int(result.realized_pnl_micros),
                    idempotency_key,
                    result.settled_at,
                    result.as_of,
                    fingerprint,
                ),
            )
            _advance_portfolio_version(cursor, agent_id, result.portfolio.version)
            _insert_ledger(
                cursor,
                result.ledger_entry,
                agent_id=agent_id,
                source_table="settlements",
                source_id=settlement_id,
                market_id=market_id,
                outcome_id=outcome_id,
            )
            cursor.execute(
                "UPDATE positions SET shares = 0, average_cost = 0, cost_basis_micros = 0, "
                "realized_pnl_micros = realized_pnl_micros + %s, updated_at = %s "
                "WHERE id = %s AND agent_id = %s AND outcome_id = %s",
                (
                    int(result.realized_pnl_micros),
                    result.settled_at,
                    position_id,
                    agent_id,
                    outcome_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("settlement position update did not match its locked owner")
        return PersistenceResult(settlement_id, True, fingerprint)


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _lock_agent(cursor: _Cursor, agent_id: uuid.UUID) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(agent_id),))


def _validate_execution_relations(
    cursor: _Cursor,
    result: ExecutionResult,
    *,
    agent_id: uuid.UUID,
    intent_id: uuid.UUID,
    market_id: uuid.UUID,
    outcome_id: uuid.UUID,
    snapshot_id: uuid.UUID | None,
    live_audit: LiveExecutionAudit | None,
) -> None:
    if snapshot_id is None:
        if (
            result.status.value != "rejected"
            or result.snapshot is not None
            or (live_audit is not None and live_audit.context is not None)
        ):
            raise ValueError("context-less execution must be a rejected result")
        cursor.execute(
            "SELECT ac.agent_id, oi.market_id, oi.outcome_id, oi.side "
            "FROM order_intents oi JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id "
            "WHERE oi.id = %s FOR UPDATE OF oi",
            (intent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("execution intent does not exist")
        if not (
            uuid.UUID(str(row[0])) == agent_id
            and uuid.UUID(str(row[1])) == market_id
            and uuid.UUID(str(row[2])) == outcome_id
            and str(row[3]) == result.order.side.value
        ):
            raise ValueError("execution intent ownership or dimensions differ")
        return
    cursor.execute(
        "SELECT ac.agent_id, oi.market_id, oi.outcome_id, oi.side, o.venue_token_id, "
        "obs.outcome_id, obs.cutoff, obs.raw_sha256 FROM order_intents oi "
        "JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id "
        "JOIN outcomes o ON o.id = oi.outcome_id "
        "JOIN order_book_snapshots obs ON obs.id = %s "
        "WHERE oi.id = %s FOR UPDATE OF oi",
        (snapshot_id, intent_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("execution intent or order-book snapshot does not exist")
    snapshot = result.snapshot
    if snapshot is None:
        raise ValueError("execution relation validation requires a market snapshot")
    matches = (
        uuid.UUID(str(row[0])) == agent_id
        and uuid.UUID(str(row[1])) == market_id
        and uuid.UUID(str(row[2])) == outcome_id
        and str(row[3]) == result.order.side.value
        and str(row[4]) == snapshot.token_id
        and uuid.UUID(str(row[5])) == outcome_id
        and cast(datetime, row[6]) == snapshot.observed_at
        and str(row[7]) == snapshot.artifact.sha256
    )
    if not matches:
        raise ValueError("execution intent/snapshot ownership or dimensions differ")
    if live_audit is None or live_audit.context is None:
        return
    context = live_audit.context
    if context.book_snapshot_id != snapshot_id:
        raise ValueError("live audit book snapshot differs from execution snapshot")
    if (
        context.market.id != str(market_id)
        or context.outcome.id != str(outcome_id)
        or context.outcome.market_id != str(market_id)
        or context.outcome.venue_token_id != snapshot.token_id
        or context.book.token_id != snapshot.token_id
        or context.book_observed_at != snapshot.observed_at
    ):
        raise ValueError("live context domain identifiers differ from the execution intent")
    cursor.execute(
        "SELECT market_id, cutoff, payload FROM market_snapshots WHERE id = %s FOR UPDATE",
        (context.market_snapshot_id,),
    )
    market_row = cursor.fetchone()
    if (
        market_row is None
        or uuid.UUID(str(market_row[0])) != market_id
        or cast(datetime, market_row[1]) != context.market_observed_at
    ):
        raise ValueError("live market snapshot does not belong to the execution market")
    market_payload = market_row[2]
    if isinstance(market_payload, str):
        market_payload = json.loads(market_payload)
    raw_outcomes = (
        market_payload.get("outcomes") if isinstance(market_payload, Mapping) else None
    )
    if not isinstance(raw_outcomes, list) or not any(
        isinstance(outcome, Mapping)
        and str(outcome.get("venue_token_id") or "") == snapshot.token_id
        for outcome in raw_outcomes
    ):
        raise ValueError("live market snapshot does not contain the execution outcome")
    cursor.execute(
        "SELECT outcome_id, token_id, condition_id, fee_rate, fee_exponent, fee_taker_only, "
        "observed_at "
        "FROM fee_rate_snapshots WHERE id = %s FOR UPDATE",
        (context.fee_rate_snapshot_id,),
    )
    fee_row = cursor.fetchone()
    if fee_row is None or not (
        uuid.UUID(str(fee_row[0])) == outcome_id
        and str(fee_row[1]) == snapshot.token_id
        and str(fee_row[2]) == snapshot.condition_id
        and Decimal(str(fee_row[3])) == context.fee_policy.rate
        and (
            (fee_row[4] is None and context.fee_policy.exponent is None)
            or (
                fee_row[4] is not None
                and context.fee_policy.exponent is not None
                and Decimal(str(fee_row[4])) == context.fee_policy.exponent
            )
        )
        and bool(fee_row[5]) == context.fee_policy.taker_only
        and cast(datetime, fee_row[6]) == context.fee_observed_at
    ):
        raise ValueError("live fee snapshot does not belong to the execution outcome")


def _persist_live_audit(
    cursor: _Cursor,
    intent_id: uuid.UUID,
    audit: LiveExecutionAudit,
) -> None:
    for attempt in audit.attempts:
        cursor.execute(
            "SELECT intent_id, attempt, requested_at, started_at, completed_at, status, "
            "error_code FROM order_execution_attempts WHERE intent_id = %s AND attempt = %s "
            "FOR UPDATE",
            (intent_id, attempt.attempt),
        )
        existing = cursor.fetchone()
        expected = (
            intent_id,
            attempt.attempt,
            audit.requested_at,
            attempt.started_at,
            attempt.completed_at,
            attempt.status,
            attempt.error_code,
        )
        if existing is not None:
            actual = (
                uuid.UUID(str(existing[0])),
                int(str(existing[1])),
                existing[2],
                existing[3],
                existing[4],
                str(existing[5]),
                existing[6],
            )
            if actual != expected:
                raise ValueError("live execution attempt idempotency key was reused")
            continue
        cursor.execute(
            "INSERT INTO order_execution_attempts "
            "(id, intent_id, attempt, requested_at, started_at, completed_at, "
            "status, error_code) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                _stable_database_uuid("execution-attempt", f"{intent_id}:{attempt.attempt}"),
                intent_id,
                attempt.attempt,
                audit.requested_at,
                attempt.started_at,
                attempt.completed_at,
                attempt.status,
                attempt.error_code,
            ),
        )
    if audit.context is None:
        return
    context = audit.context
    artifact_hashes = dict(context.artifact_hashes)
    cursor.execute(
        "SELECT id, intent_id, market_snapshot_id, order_book_snapshot_id, "
        "fee_rate_snapshot_id, requested_at, validated_at, executed_at, "
        "market_observed_at, order_book_observed_at, fee_observed_at, artifact_hashes "
        "FROM live_order_contexts WHERE intent_id = %s FOR UPDATE",
        (intent_id,),
    )
    existing_context = cursor.fetchone()
    if existing_context is not None:
        stored_hashes = existing_context[11]
        if isinstance(stored_hashes, str):
            stored_hashes = json.loads(stored_hashes)
        expected_context = (
            intent_id,
            context.market_snapshot_id,
            context.book_snapshot_id,
            context.fee_rate_snapshot_id,
            audit.requested_at,
            context.validated_at,
            audit.executed_at,
            context.market_observed_at,
            context.book_observed_at,
            context.fee_observed_at,
            artifact_hashes,
        )
        actual_context = (
            uuid.UUID(str(existing_context[1])),
            uuid.UUID(str(existing_context[2])),
            uuid.UUID(str(existing_context[3])),
            uuid.UUID(str(existing_context[4])),
            existing_context[5],
            existing_context[6],
            existing_context[7],
            existing_context[8],
            existing_context[9],
            existing_context[10],
            stored_hashes,
        )
        if actual_context != expected_context:
            raise ValueError("live order context idempotency key was reused")
        return
    cursor.execute(
        "INSERT INTO live_order_contexts "
        "(id, intent_id, market_snapshot_id, order_book_snapshot_id, "
        "fee_rate_snapshot_id, requested_at, validated_at, executed_at, "
        "market_observed_at, order_book_observed_at, fee_observed_at, artifact_hashes) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            _stable_database_uuid("live-order-context", str(intent_id)),
            intent_id,
            context.market_snapshot_id,
            context.book_snapshot_id,
            context.fee_rate_snapshot_id,
            audit.requested_at,
            context.validated_at,
            audit.executed_at,
            context.market_observed_at,
            context.book_observed_at,
            context.fee_observed_at,
            json.dumps(artifact_hashes, sort_keys=True),
        ),
    )


def _validate_settlement_relations(
    cursor: _Cursor,
    result: SettlementResult,
    *,
    agent_id: uuid.UUID,
    position_id: uuid.UUID,
    resolution_id: uuid.UUID,
    market_id: uuid.UUID,
    outcome_id: uuid.UUID,
) -> None:
    cursor.execute(
        "SELECT p.agent_id, o.market_id, p.outcome_id, p.shares, p.average_cost, "
        "p.cost_basis_micros, p.realized_pnl_micros, r.market_id, "
        "r.winning_outcome_id, r.source_created_at, r.observed_at, r.eligible_after "
        "FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
        "JOIN resolutions r ON r.id = %s WHERE p.id = %s FOR UPDATE OF p",
        (resolution_id, position_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("settlement position or resolution does not exist")
    expected_winner = (
        _stable_database_uuid("outcome", result.resolution.winning_outcome_id)
        if result.resolution.winning_outcome_id is not None
        else None
    )
    matches = (
        uuid.UUID(str(row[0])) == agent_id
        and uuid.UUID(str(row[1])) == market_id
        and uuid.UUID(str(row[2])) == outcome_id
        and Decimal(str(row[3])) == result.position.shares
        and Decimal(str(row[4])) == result.position.average_cost
        and int(str(row[5])) == int(result.position.cost_basis_micros)
        and int(str(row[6])) == int(result.position.realized_pnl_micros)
        and uuid.UUID(str(row[7])) == market_id
        and (
            (row[8] is None and expected_winner is None)
            or (
                row[8] is not None
                and expected_winner is not None
                and uuid.UUID(str(row[8])) == expected_winner
            )
        )
        and cast(datetime, row[9]) == result.resolution.source_created_at
        and cast(datetime, row[10]) == result.resolution.observed_at
        and cast(datetime, row[11]) == result.resolution.eligible_after
    )
    if not matches:
        raise ValueError("settlement ownership, resolution, or position dimensions differ")


def _assert_portfolio_version(cursor: _Cursor, agent_id: uuid.UUID, expected: int) -> None:
    cursor.execute("SELECT portfolio_version FROM agents WHERE id = %s FOR UPDATE", (agent_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("agent does not exist")
    if int(str(row[0])) != expected:
        raise ValueError("stale portfolio projection; reload and revalidate the order")


def _advance_portfolio_version(cursor: _Cursor, agent_id: uuid.UUID, version: int) -> None:
    cursor.execute(
        "UPDATE agents SET portfolio_version = %s WHERE id = %s",
        (version, agent_id),
    )


def _existing(cursor: _Cursor, table: str, idempotency_key: str) -> tuple[uuid.UUID, str] | None:
    if table not in {"orders", "settlements"}:
        raise ValueError("unsupported idempotency table")
    cursor.execute(
        f"SELECT id, execution_fingerprint FROM {table} WHERE idempotency_key = %s",
        (idempotency_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return uuid.UUID(str(row[0])), str(row[1])


def _assert_same_fingerprint(existing: tuple[uuid.UUID, str], fingerprint: str) -> None:
    if existing[1] != fingerprint:
        raise ValueError("idempotency key reused with a different financial payload")


def _insert_ledger(
    cursor: _Cursor,
    entry: LedgerEntry,
    *,
    agent_id: uuid.UUID,
    source_table: str,
    source_id: uuid.UUID,
    market_id: uuid.UUID,
    outcome_id: uuid.UUID,
) -> None:
    ledger_id = _stable_database_uuid("ledger", entry.id)
    cursor.execute(
        "INSERT INTO ledger_entries "
        "(id, agent_id, event_type, source_table, source_id, idempotency_key, "
        "reversal_of, occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            ledger_id,
            agent_id,
            entry.event_type,
            source_table,
            source_id,
            entry.idempotency_key,
            uuid.UUID(entry.reversal_of) if entry.reversal_of else None,
            entry.occurred_at,
        ),
    )
    for posting in entry.postings:
        cursor.execute(
            "INSERT INTO ledger_postings "
            "(ledger_entry_id, account, amount_micros, market_id, outcome_id, shares_delta) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                ledger_id,
                posting.account.value,
                int(posting.amount_micros),
                market_id if posting.market_id is not None else None,
                outcome_id if posting.outcome_id is not None else None,
                posting.shares_delta,
            ),
        )


def _upsert_position(
    cursor: _Cursor,
    result: ExecutionResult,
    *,
    agent_id: uuid.UUID,
    market_id: uuid.UUID,
    outcome_id: uuid.UUID,
) -> None:
    position = result.portfolio.position(result.order.outcome_id)
    shares = position.shares if position else Decimal(0)
    average_cost = position.average_cost if position else Decimal(0)
    cost_basis = int(position.cost_basis_micros) if position else 0
    realized = int(position.realized_pnl_micros) if position else 0
    cursor.execute(
        "INSERT INTO positions "
        "(id, agent_id, outcome_id, shares, average_cost, cost_basis_micros, "
        "realized_pnl_micros, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (agent_id, outcome_id) DO UPDATE SET shares = EXCLUDED.shares, "
        "average_cost = EXCLUDED.average_cost, cost_basis_micros = EXCLUDED.cost_basis_micros, "
        "realized_pnl_micros = EXCLUDED.realized_pnl_micros, updated_at = EXCLUDED.updated_at",
        (
            _stable_database_uuid("position", f"{agent_id}:{market_id}:{outcome_id}"),
            agent_id,
            outcome_id,
            shares,
            average_cost,
            cost_basis,
            realized,
            result.order.created_at,
        ),
    )


def _stable_database_uuid(kind: str, value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:{kind}:{value}")


def _fingerprint(value: ExecutionResult | SettlementResult) -> str:
    payload = json.dumps(
        asdict(value),
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, (datetime, Decimal, Enum, uuid.UUID)):
        return str(value)
    raise TypeError(f"unsupported fingerprint value: {type(value).__name__}")
