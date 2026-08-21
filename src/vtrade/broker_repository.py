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
from vtrade.domain.execution import (
    FeeParticipantRole,
    FeePolicySnapshot,
    OrderResult,
    ReconciliationState,
    SettlementRecord,
)
from vtrade.domain.types import MarketKey, OutcomeSide, ResolutionObservation
from vtrade.ledger import LedgerEntry
from vtrade.order_execution import LiveExecutionAudit
from vtrade.portfolio import ContractPortfolio
from vtrade.risk import ConcentrationCheck


class _Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


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
                "entry_fees_micros = 0, realized_pnl_micros = realized_pnl_micros + %s, "
                "updated_at = %s "
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

    def persist_order_result(
        self,
        result: OrderResult,
        *,
        agent_id: uuid.UUID,
        agent_cycle_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
        operation_id: uuid.UUID | None = None,
        book_snapshot_id: uuid.UUID | None = None,
    ) -> PersistenceResult:
        """Persist one semantic order and its fill/accounting evidence atomically."""

        if result.request.agent_id != str(agent_id):
            raise ValueError("database agent does not own the semantic order")
        fingerprint = _semantic_fingerprint(result)
        operation_uuid_value = operation_id or _stable_database_uuid(
            "order-operation", result.operation_id
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            cursor.execute(
                "SELECT id, request_fingerprint FROM order_operations "
                "WHERE agent_id = %s AND idempotency_key = %s FOR UPDATE",
                (agent_id, result.request.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                existing_id = uuid.UUID(str(existing[0]))
                if str(existing[1]) != result.request.fingerprint:
                    raise ValueError("semantic idempotency key was reused with different evidence")
                _assert_semantic_replay(cursor, existing_id, result)
                return PersistenceResult(existing_id, False, fingerprint)
            frozen_cutoff = result.request.frozen_cutoff or result.submitted_at
            execution_cutoff = result.updated_at
            cursor.execute(
                "INSERT INTO order_operations "
                "(id, agent_id, agent_cycle_id, market_id, outcome_side, order_side, "
                "amount_kind, cash_amount_micros, contract_units, limit_price_micros, "
                "time_in_force, frozen_cutoff, execution_cutoff, idempotency_key, "
                "request_fingerprint, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    operation_uuid_value,
                    agent_id,
                    agent_cycle_id,
                    market_id,
                    _enum_value(result.request.outcome),
                    _enum_value(result.request.action),
                    _enum_value(result.request.amount_type),
                    result.request.cash_amount_micros,
                    result.request.contract_units,
                    result.request.limit_price_micros,
                    _enum_value(result.request.time_in_force),
                    frozen_cutoff,
                    execution_cutoff,
                    result.request.idempotency_key,
                    result.request.fingerprint,
                    result.submitted_at,
                ),
            )
            lifecycle_rows = (
                (("PENDING", None, result.submitted_at, 0, "validated"),)
                if _enum_value(result.state) == "PENDING"
                else (
                    ("PENDING", None, result.submitted_at, 0, "validated"),
                    (
                        _enum_value(result.state),
                        _enum_value(result.error_code)
                        if result.error_code is not None
                        else None,
                        result.updated_at,
                        1,
                        "terminal",
                    ),
                )
            )
            for state, reason, observed_at, sequence_number, label in lifecycle_rows:
                cursor.execute(
                    "INSERT INTO order_lifecycle_events "
                    "(operation_id, sequence_number, state, reason, observed_at, idempotency_key) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        operation_uuid_value,
                        sequence_number,
                        state,
                        reason,
                        observed_at,
                        f"lifecycle:{result.operation_id}:{label}",
                    ),
                )
            cursor.execute(
                "INSERT INTO order_reconciliation_events "
                "(operation_id, sequence_number, reconciliation_state, evidence, "
                "observed_at, idempotency_key) VALUES (%s, 0, %s, %s::jsonb, %s, %s)",
                (
                    operation_uuid_value,
                    _enum_value(result.reconciliation_state),
                    json.dumps(
                        {
                            "contract_version": result.contract_version,
                            "error_code": (
                                _enum_value(result.error_code)
                                if result.error_code is not None
                                else None
                            ),
                        },
                        sort_keys=True,
                    ),
                    result.updated_at,
                    f"reconciliation:{result.operation_id}:0",
                ),
            )
            cursor.execute(
                "UPDATE order_operation_current SET state = %s, reconciliation_state = %s, "
                "state_version = state_version + 1, updated_at = %s WHERE operation_id = %s",
                (
                    _enum_value(result.state),
                    _enum_value(result.reconciliation_state),
                    result.updated_at,
                    operation_uuid_value,
                ),
            )
            if result.fills:
                if not isinstance(result.portfolio_before, ContractPortfolio) or not isinstance(
                    result.portfolio_after, ContractPortfolio
                ):
                    raise ValueError("semantic fills require contract portfolio projections")
                _assert_contract_portfolio_version(
                    cursor, agent_id, result.portfolio_before.version
                )
                for fill in result.fills:
                    cursor.execute(
                        "INSERT INTO fills "
                        "(operation_id, fill_id, fill_fingerprint, contract_units, price_micros, "
                        "gross_cash_micros, authoritative_fee_micros, net_cash_delta_micros, "
                        "frozen_context_id, execution_context_id, adapter_evidence, filled_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                        (
                            operation_uuid_value,
                            fill.fill_id,
                            fill.fingerprint,
                            fill.contract_units,
                            fill.price_micros,
                            fill.gross_cash_micros,
                            fill.fee_micros,
                            fill.net_cash_delta_micros,
                            _uuid_or_none(fill.frozen_context_id),
                            _uuid_or_none(fill.execution_context_id),
                            json.dumps({"authoritative": fill.authoritative}),
                            fill.filled_at,
                        ),
                    )
                audit = result.liquidity_audit
                if audit is not None and book_snapshot_id is not None:
                    cursor.execute(
                        "INSERT INTO liquidity_haircut_audits "
                        "(snapshot_id, outcome_side, rule_version, captured_raw_levels, "
                        "effective_levels, raw_depth_units, ignored_quantity_units, "
                        "effective_depth_units, consumed_quantity_units, "
                        "cancelled_quantity_units, remaining_quantity_units, executable) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (snapshot_id, outcome_side) DO NOTHING",
                        (
                            book_snapshot_id,
                            audit.outcome.value,
                            audit.rule_version,
                            audit.captured_level_count,
                            audit.effective_level_count,
                            audit.raw_quantity_units,
                            audit.ignored_quantity_units,
                            audit.effective_quantity_units,
                            audit.consumed_quantity_units,
                            audit.cancelled_quantity_units,
                            audit.remaining_quantity_units,
                            bool(audit.effective_quantity_units),
                        ),
                    )
                for entry in result.ledger_entries:
                    _insert_contract_ledger(
                        cursor,
                        entry,
                        agent_id=agent_id,
                        source_table="order_operations",
                        source_id=operation_uuid_value,
                        market_id=market_id,
                        outcome_side=OutcomeSide(result.request.outcome),
                    )
                _upsert_contract_position(
                    cursor,
                    result.portfolio_after,
                    agent_id=agent_id,
                    market_id=market_id,
                    outcome_id=outcome_id,
                    market_ref=result.request.market_ref,
                    outcome=OutcomeSide(result.request.outcome),
                    updated_at=result.updated_at,
                )
                position_id = _stable_database_uuid(
                    "contract-position", f"{agent_id}:{market_id}:{outcome_id}"
                )
                for fill in result.fills:
                    if _enum_value(result.request.action) == "BUY":
                        cursor.execute(
                            "INSERT INTO position_fee_allocations "
                            "(position_id, fill_id, contract_units, fee_micros, allocation_kind) "
                            "VALUES (%s, %s, %s, %s, 'entry')",
                            (
                                position_id,
                                _stable_database_uuid(
                                    "fill", f"{result.operation_id}:{fill.fill_id}"
                                ),
                                fill.contract_units,
                                fill.fee_micros,
                            ),
                        )
                if _enum_value(result.request.action) == "SELL":
                    before_position = result.portfolio_before.position(
                        result.request.market_ref,
                        OutcomeSide(result.request.outcome),
                    )
                    after_position = result.portfolio_after.position(
                        result.request.market_ref,
                        OutcomeSide(result.request.outcome),
                    )
                    removed_entry_fees = (
                        int(before_position.entry_fees_micros)
                        - (
                            int(after_position.entry_fees_micros)
                            if after_position is not None
                            else 0
                        )
                        if before_position is not None
                        else 0
                    )
                    if removed_entry_fees > 0:
                        cursor.execute(
                            "INSERT INTO position_fee_allocations "
                            "(position_id, fill_id, contract_units, fee_micros, allocation_kind) "
                            "VALUES (%s, %s, %s, %s, 'sell_release')",
                            (
                                position_id,
                                _stable_database_uuid(
                                    "fill", f"{result.operation_id}:{result.fills[0].fill_id}"
                                ),
                                sum(int(fill.contract_units) for fill in result.fills),
                                removed_entry_fees,
                            ),
                        )
                cursor.execute(
                    "INSERT INTO portfolio_versions "
                    "(id, agent_id, version, reason, created_at) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (agent_id, version) DO NOTHING",
                    (
                        _stable_database_uuid(
                            "portfolio-version", f"{agent_id}:{result.portfolio_after.version}"
                        ),
                        agent_id,
                        result.portfolio_after.version,
                        "semantic order fill",
                        result.updated_at,
                    ),
                )
                _advance_contract_portfolio_version(
                    cursor, agent_id, result.portfolio_after.version
                )
            return PersistenceResult(operation_uuid_value, True, fingerprint)

    def persist_fee_policy_snapshot(
        self,
        snapshot: FeePolicySnapshot,
        *,
        market_id: uuid.UUID,
        raw_artifact_id: uuid.UUID,
    ) -> PersistenceResult:
        """Store one immutable policy selection before it can authorize a fill."""

        required_times = (
            snapshot.effective_from,
            snapshot.as_of,
            snapshot.source_observed_at,
            snapshot.cutoff,
        )
        if any(value is None for value in required_times):
            raise ValueError("fee policy persistence requires complete source timestamps")
        policy_id = _stable_database_uuid("fee-policy", snapshot.fingerprint)
        multiplier_num, multiplier_den = snapshot.resolved_multiplier
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, policy_fingerprint FROM fee_policy_snapshots "
                "WHERE market_id = %s AND policy_fingerprint = %s FOR UPDATE",
                (market_id, snapshot.fingerprint),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[1]) != snapshot.fingerprint:
                    raise ValueError("fee policy fingerprint conflict")
                return PersistenceResult(uuid.UUID(str(existing[0])), False, snapshot.fingerprint)
            cursor.execute(
                "INSERT INTO fee_policy_snapshots "
                "(id, market_id, policy_version, formula_version, schedule_identity, "
                "participant_role, multiplier_numerator, multiplier_denominator, "
                "event_override_micros, event_override_cleared, waiver_evidence, exact_inputs, "
                "effective_at, as_of_at, observed_at, cutoff, source_tier, raw_artifact_id, "
                "policy_fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                "%s::jsonb, %s, %s, %s, %s, %s, %s, %s)",
                (
                    policy_id,
                    market_id,
                    snapshot.contract_version,
                    snapshot.formula_version,
                    snapshot.schedule_version,
                    FeeParticipantRole(
                        snapshot.role or snapshot.participant_role
                    ).value.lower(),
                    multiplier_num,
                    multiplier_den,
                    snapshot.event_override_numerator,
                    snapshot.event_override_cleared,
                    json.dumps(
                        dict(snapshot.waiver_evidence)
                        if snapshot.waiver_evidence is not None
                        else {"waived": snapshot.waiver}
                    ),
                    json.dumps(dict(snapshot.exact_inputs), sort_keys=True),
                    snapshot.effective_from,
                    snapshot.as_of,
                    snapshot.source_observed_at,
                    snapshot.cutoff,
                    snapshot.source_tier,
                    raw_artifact_id,
                    snapshot.fingerprint,
                ),
            )
        return PersistenceResult(policy_id, True, snapshot.fingerprint)

    def persist_resolution_observation(
        self,
        observation: ResolutionObservation,
        *,
        market_id: uuid.UUID,
        raw_artifact_id: uuid.UUID,
        cutoff: datetime,
    ) -> PersistenceResult:
        """Append one resolution observation; terminal conflicts remain evidence."""

        resolution_id = _stable_database_uuid("resolution", str(observation.snapshot_id))
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "market": observation.market_key.canonical,
                    "status": observation.status.value,
                    "result": observation.result.value if observation.result else None,
                    "observed_at": observation.observed_at.isoformat(),
                    "source_timestamp": (
                        observation.source_timestamp.isoformat()
                        if observation.source_timestamp
                        else None
                    ),
                    "settlement_ts": (
                        observation.settlement_ts.isoformat()
                        if observation.settlement_ts
                        else None
                    ),
                    "blocked": observation.blocked,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, raw_artifact_id FROM resolution_observations "
                "WHERE market_id = %s AND observed_at = %s AND raw_artifact_id = %s FOR UPDATE",
                (market_id, observation.observed_at, raw_artifact_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                return PersistenceResult(uuid.UUID(str(existing[0])), False, fingerprint)
            cursor.execute(
                "INSERT INTO resolution_observations "
                "(id, market_id, lifecycle_status, result, observed_at, source_timestamp, "
                "settlement_ts, cutoff, raw_artifact_id, blocked) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    resolution_id,
                    market_id,
                    observation.status.value,
                    observation.result.value if observation.result else None,
                    observation.observed_at,
                    observation.source_timestamp,
                    observation.settlement_ts,
                    cutoff,
                    raw_artifact_id,
                    observation.blocked,
                ),
            )
        return PersistenceResult(resolution_id, True, fingerprint)

    def persist_risk_check(
        self,
        check: ConcentrationCheck,
        *,
        operation_id: uuid.UUID,
        policy_snapshot_id: uuid.UUID,
        checked_at: datetime,
    ) -> PersistenceResult:
        """Append the exact rational concentration decision for one operation."""

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "account_value_micros": check.account_value_micros,
                    "existing_market_basis_micros": check.existing_market_basis_micros,
                    "proposed_market_basis_micros": check.proposed_market_basis_micros,
                    "numerator": check.numerator,
                    "denominator": check.denominator,
                    "approved": check.approved,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        check_id = _stable_database_uuid("risk-check", str(operation_id))
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, account_value_micros, existing_market_basis_micros, "
                "proposed_market_basis_micros, decision FROM risk_checks "
                "WHERE operation_id = %s FOR UPDATE",
                (operation_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                actual = (
                    int(str(existing[1])),
                    int(str(existing[2])),
                    int(str(existing[3])),
                    str(existing[4]) == "approved",
                )
                expected = (
                    check.account_value_micros,
                    check.existing_market_basis_micros,
                    check.proposed_market_basis_micros,
                    check.approved,
                )
                if actual != expected:
                    raise ValueError("risk-check operation was reused with divergent evidence")
                return PersistenceResult(uuid.UUID(str(existing[0])), False, fingerprint)
            cursor.execute(
                "INSERT INTO risk_checks "
                "(id, operation_id, policy_snapshot_id, account_value_micros, "
                "existing_market_basis_micros, proposed_market_basis_micros, "
                "concentration_numerator, concentration_denominator, decision, "
                "rejection_reason, checked_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    check_id,
                    operation_id,
                    policy_snapshot_id,
                    check.account_value_micros,
                    check.existing_market_basis_micros,
                    check.proposed_market_basis_micros,
                    check.numerator,
                    check.denominator,
                    "approved" if check.approved else "rejected",
                    None if check.approved else "CONCENTRATION_LIMIT",
                    checked_at,
                ),
            )
        return PersistenceResult(check_id, True, fingerprint)

    def persist_fill_evidence(
        self,
        *,
        operation_id: uuid.UUID,
        agent_id: uuid.UUID,
        fill_id: str,
        fill_fingerprint: str,
        fill_values: Sequence[object],
    ) -> bool:
        """Insert one fill once; return False for an identical replay."""

        if not fill_id or len(fill_fingerprint) != 64:
            raise ValueError("fill evidence identity is malformed")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            cursor.execute(
                "SELECT fill_fingerprint FROM fills WHERE operation_id = %s AND fill_id = %s "
                "FOR UPDATE",
                (operation_id, fill_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[0]) != fill_fingerprint:
                    raise ValueError("fill id was reused with divergent evidence")
                return False
            cursor.execute(
                "INSERT INTO fills "
                "(operation_id, fill_id, fill_fingerprint, contract_units, price_micros, "
                "gross_cash_micros, authoritative_fee_micros, net_cash_delta_micros, filled_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (operation_id, fill_id, fill_fingerprint, *fill_values),
            )
        return True

    def persist_reconciliation_event(
        self,
        *,
        operation_id: uuid.UUID,
        agent_id: uuid.UUID,
        sequence_number: int,
        state: ReconciliationState | str,
        evidence: Mapping[str, object],
        observed_at: datetime,
    ) -> PersistenceResult:
        """Append and project one later terminal reconciliation observation."""

        if sequence_number < 0:
            raise ValueError("reconciliation sequence cannot be negative")
        normalized_state = ReconciliationState(state)
        evidence_fingerprint = hashlib.sha256(
            json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        event_id = _stable_database_uuid(
            "reconciliation-event", f"{operation_id}:{sequence_number}"
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            cursor.execute(
                "SELECT id, evidence FROM order_reconciliation_events "
                "WHERE operation_id = %s AND sequence_number = %s FOR UPDATE",
                (operation_id, sequence_number),
            )
            existing = cursor.fetchone()
            if existing is not None:
                stored = existing[1]
                if isinstance(stored, str):
                    stored = json.loads(stored)
                if not isinstance(stored, Mapping):
                    raise ValueError("stored reconciliation evidence is not an object")
                actual = hashlib.sha256(
                    json.dumps(dict(stored), sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if actual != evidence_fingerprint:
                    raise ValueError("reconciliation sequence was reused with divergent evidence")
                return PersistenceResult(uuid.UUID(str(existing[0])), False, evidence_fingerprint)
            cursor.execute(
                "INSERT INTO order_reconciliation_events "
                "(id, operation_id, sequence_number, reconciliation_state, evidence, "
                "observed_at, idempotency_key) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    event_id,
                    operation_id,
                    sequence_number,
                    normalized_state.value,
                    json.dumps(dict(evidence), sort_keys=True),
                    observed_at,
                    f"reconciliation:{operation_id}:{sequence_number}",
                ),
            )
            cursor.execute(
                "UPDATE order_operation_current SET reconciliation_state = %s, "
                "state_version = state_version + 1, updated_at = %s WHERE operation_id = %s "
                "AND agent_id = %s",
                (normalized_state.value, observed_at, operation_id, agent_id),
            )
        return PersistenceResult(event_id, True, evidence_fingerprint)

    def persist_settlement_record(
        self,
        record: SettlementRecord,
        *,
        agent_id: uuid.UUID,
        position_id: uuid.UUID,
        resolution_id: uuid.UUID,
        market_id: uuid.UUID,
        outcome_id: uuid.UUID,
        portfolio_before: ContractPortfolio,
        portfolio_after: ContractPortfolio,
    ) -> PersistenceResult:
        """Persist one validated FINALIZED payout and its atomic projection."""

        if portfolio_before.agent_id != str(agent_id) or portfolio_after.agent_id != str(agent_id):
            raise ValueError("settlement portfolio is owned by another agent")
        if record.ledger_entry is None:
            raise ValueError("settlement requires a balanced ledger entry")
        fingerprint = _settlement_record_fingerprint(record)
        settlement_id = _stable_database_uuid("contract-settlement", record.settlement_id)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            cursor.execute(
                "SELECT id, settlement_fingerprint FROM settlements "
                "WHERE idempotency_key = %s OR (position_id = %s AND resolution_id = %s) "
                "FOR UPDATE",
                (record.ledger_entry.idempotency_key, position_id, resolution_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if str(existing[1]) != fingerprint:
                    raise ValueError("terminal settlement evidence conflicts with prior evidence")
                return PersistenceResult(uuid.UUID(str(existing[0])), False, fingerprint)
            _assert_contract_portfolio_version(cursor, agent_id, portfolio_before.version)
            _insert_contract_ledger(
                cursor,
                record.ledger_entry,
                agent_id=agent_id,
                source_table="settlements",
                source_id=settlement_id,
                market_id=market_id,
                outcome_side=OutcomeSide(record.outcome),
            )
            cursor.execute(
                "INSERT INTO settlements "
                "(id, agent_id, position_id, resolution_id, market_id, settlement_ts, "
                "outcome_side, contract_units, gross_payout_micros, entry_fees_deducted_micros, "
                "realized_pnl_micros, ledger_entry_id, idempotency_key, settlement_fingerprint, "
                "settled_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    settlement_id,
                    agent_id,
                    position_id,
                    resolution_id,
                    market_id,
                    record.settlement_ts,
                    OutcomeSide(record.outcome).value,
                    record.contract_units,
                    record.gross_payout_micros,
                    record.entry_fees_deducted_micros,
                    record.realized_pnl_micros,
                    _stable_database_uuid("ledger", record.ledger_entry.id),
                    record.ledger_entry.idempotency_key,
                    fingerprint,
                    record.settlement_ts,
                ),
            )
            cursor.execute(
                "INSERT INTO position_fee_allocations "
                "(position_id, settlement_id, contract_units, fee_micros, allocation_kind) "
                "VALUES (%s, %s, %s, %s, 'settlement')",
                (
                    position_id,
                    settlement_id,
                    record.contract_units,
                    record.entry_fees_deducted_micros,
                ),
            )
            cursor.execute(
                "UPDATE positions SET contract_units = 0, gross_cost_basis_micros = 0, "
                "entry_fees_micros = 0, realized_pnl_micros = realized_pnl_micros + %s, "
                "portfolio_version = %s, updated_at = %s "
                "WHERE id = %s AND agent_id = %s AND market_id = %s AND outcome_id = %s",
                (
                    record.realized_pnl_micros,
                    portfolio_after.version,
                    record.settlement_ts,
                    position_id,
                    agent_id,
                    market_id,
                    outcome_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("settlement position update did not match its owner")
            _advance_contract_portfolio_version(cursor, agent_id, portfolio_after.version)
            return PersistenceResult(settlement_id, True, fingerprint)


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("execution context reference must be a UUID") from exc


def _semantic_fingerprint(result: OrderResult) -> str:
    payload = {
        "request": result.request.fingerprint,
        "operation_id": result.operation_id,
        "state": _enum_value(result.state),
        "reconciliation_state": _enum_value(result.reconciliation_state),
        "fills": [fill.fingerprint for fill in result.fills],
        "gross_cash_delta_micros": result.gross_cash_delta_micros,
        "fee_micros": int(result.fee_micros),
        "net_cash_delta_micros": result.net_cash_delta_micros,
        "updated_at": result.updated_at.isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _assert_semantic_replay(
    cursor: _Cursor, operation_id: uuid.UUID, result: OrderResult
) -> None:
    cursor.execute(
        "SELECT state, reconciliation_state FROM order_operation_current "
        "WHERE operation_id = %s FOR UPDATE",
        (operation_id,),
    )
    current = cursor.fetchone()
    if current is None:
        raise ValueError("semantic order projection is missing")
    if str(current[0]) != _enum_value(result.state) or str(current[1]) != _enum_value(
        result.reconciliation_state
    ):
        raise ValueError("semantic order replay has divergent lifecycle evidence")
    cursor.execute(
        "SELECT fill_id, fill_fingerprint FROM fills WHERE operation_id = %s "
        "ORDER BY fill_id",
        (operation_id,),
    )
    actual_fills = tuple((str(row[0]), str(row[1])) for row in cursor.fetchall())
    expected_fills = tuple(sorted((fill.fill_id, str(fill.fingerprint)) for fill in result.fills))
    if actual_fills != expected_fills:
        raise ValueError("semantic order replay has divergent fill evidence")


def _settlement_record_fingerprint(record: SettlementRecord) -> str:
    market_ref = (
        record.market_ref
        if isinstance(record.market_ref, MarketKey)
        else MarketKey(record.market_ref)
    )
    payload = {
        "settlement_id": record.settlement_id,
        "market_ref": market_ref.canonical,
        "outcome": OutcomeSide(record.outcome).value,
        "resolution_id": record.resolution_id,
        "settlement_ts": record.settlement_ts.isoformat(),
        "contract_units": int(record.contract_units),
        "gross_payout_micros": int(record.gross_payout_micros),
        "entry_fees_deducted_micros": int(record.entry_fees_deducted_micros),
        "realized_pnl_micros": record.realized_pnl_micros,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _assert_contract_portfolio_version(
    cursor: _Cursor, agent_id: uuid.UUID, expected: int
) -> None:
    cursor.execute("SELECT portfolio_version FROM agents WHERE id = %s FOR UPDATE", (agent_id,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("agent does not exist")
    if int(str(row[0])) != expected:
        raise ValueError("stale contract portfolio projection")


def _advance_contract_portfolio_version(
    cursor: _Cursor, agent_id: uuid.UUID, version: int
) -> None:
    cursor.execute(
        "UPDATE agents SET portfolio_version = %s WHERE id = %s",
        (version, agent_id),
    )


def _insert_contract_ledger(
    cursor: _Cursor,
    entry: LedgerEntry,
    *,
    agent_id: uuid.UUID,
    source_table: str,
    source_id: uuid.UUID,
    market_id: uuid.UUID | None = None,
    outcome_side: OutcomeSide | None = None,
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
        has_dimensions = posting.contract_units_delta is not None
        cursor.execute(
            "INSERT INTO ledger_postings "
            "(ledger_entry_id, account, amount_micros, market_id, outcome_side, "
            "contract_units_delta) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                ledger_id,
                posting.account.value,
                int(posting.amount_micros),
                market_id if has_dimensions else None,
                outcome_side.value if has_dimensions and outcome_side is not None else None,
                posting.contract_units_delta if has_dimensions else None,
            ),
        )


def _upsert_contract_position(
    cursor: _Cursor,
    portfolio: ContractPortfolio,
    *,
    agent_id: uuid.UUID,
    market_id: uuid.UUID,
    outcome_id: uuid.UUID,
    market_ref: MarketKey | str,
    outcome: OutcomeSide,
    updated_at: datetime,
) -> None:
    position = portfolio.position(market_ref, outcome)
    contract_units = position.contract_units if position is not None else 0
    basis = int(position.gross_cost_basis_micros) if position is not None else 0
    fees = int(position.entry_fees_micros) if position is not None else 0
    realized = int(position.realized_pnl_micros) if position is not None else 0
    cursor.execute(
        "INSERT INTO positions "
        "(id, agent_id, market_id, outcome_id, outcome_side, contract_units, "
        "gross_cost_basis_micros, entry_fees_micros, realized_pnl_micros, "
        "portfolio_version, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (agent_id, outcome_id) DO UPDATE SET "
        "contract_units = EXCLUDED.contract_units, "
        "gross_cost_basis_micros = EXCLUDED.gross_cost_basis_micros, "
        "entry_fees_micros = EXCLUDED.entry_fees_micros, "
        "realized_pnl_micros = EXCLUDED.realized_pnl_micros, "
        "portfolio_version = EXCLUDED.portfolio_version, updated_at = EXCLUDED.updated_at",
        (
            _stable_database_uuid("contract-position", f"{agent_id}:{market_id}:{outcome_id}"),
            agent_id,
            market_id,
            outcome_id,
            outcome.value,
            contract_units,
            basis,
            fees,
            realized,
            portfolio.version,
            updated_at,
        ),
    )


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
        "p.cost_basis_micros, p.realized_pnl_micros, p.entry_fees_micros, r.market_id, "
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
        and int(str(row[7])) == int(result.position.entry_fees_micros)
        and uuid.UUID(str(row[8])) == market_id
        and (
            (row[9] is None and expected_winner is None)
            or (
                row[9] is not None
                and expected_winner is not None
                and uuid.UUID(str(row[9])) == expected_winner
            )
        )
        and cast(datetime, row[10]) == result.resolution.source_created_at
        and cast(datetime, row[11]) == result.resolution.observed_at
        and cast(datetime, row[12]) == result.resolution.eligible_after
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
    entry_fees = int(position.entry_fees_micros) if position else 0
    cursor.execute(
        "INSERT INTO positions "
        "(id, agent_id, outcome_id, shares, average_cost, cost_basis_micros, "
        "realized_pnl_micros, entry_fees_micros, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (agent_id, outcome_id) DO UPDATE SET shares = EXCLUDED.shares, "
        "average_cost = EXCLUDED.average_cost, cost_basis_micros = EXCLUDED.cost_basis_micros, "
        "realized_pnl_micros = EXCLUDED.realized_pnl_micros, "
        "entry_fees_micros = EXCLUDED.entry_fees_micros, updated_at = EXCLUDED.updated_at",
        (
            _stable_database_uuid("position", f"{agent_id}:{market_id}:{outcome_id}"),
            agent_id,
            outcome_id,
            shares,
            average_cost,
            cost_basis,
            realized,
            entry_fees,
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
