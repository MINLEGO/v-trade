from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from vtrade.domain.execution import (
    FeeParticipantRole,
    FeePolicySnapshot,
    OrderResult,
    ReconciliationState,
    SettlementRecord,
)
from vtrade.domain.types import MarketKey, OutcomeSide, ResolutionObservation
from vtrade.ledger import LedgerEntry
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
    """Atomic persistence for semantic orders, accounting, and settlement evidence."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

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



def _stable_database_uuid(kind: str, value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:{kind}:{value}")
