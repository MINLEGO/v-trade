from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol, cast

from vtrade.domain.execution import (
    EconomicFill,
    FeeParticipantRole,
    FeePolicySnapshot,
    OrderRequest,
    OrderResult,
    OrderState,
    ReconciliationState,
    SettlementRecord,
    SubmissionState,
    operation_uuid,
)
from vtrade.domain.types import (
    CanonicalLevel,
    ContractQuantity,
    MarketContext,
    MarketKey,
    MoneyMicros,
    OutcomeSide,
    PriceMicros,
    RawArtifact,
    ResolutionObservation,
)
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


@dataclass(frozen=True, slots=True)
class OrderIntentReservation:
    operation_id: uuid.UUID
    market_id: uuid.UUID | None
    outcome_id: uuid.UUID | None
    created: bool
    existing_result: OrderResult | None = None
    conflict: bool = False
    blocked: bool = False
    in_flight: bool = False


class PostgresBrokerRepository:
    """Atomic persistence for semantic orders, accounting, and settlement evidence."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect

    def prepare_order_intent(
        self,
        request: OrderRequest,
        *,
        agent_id: uuid.UUID,
        agent_cycle_id: uuid.UUID,
    ) -> OrderIntentReservation:
        """Record an order intention under the agent lock before venue I/O.

        The intent table is deliberately separate from the append-only operation
        table.  This lets a refresh happen outside the transaction while keeping
        strict idempotency and a durable request timestamp.
        """

        if request.agent_id != str(agent_id):
            raise ValueError("database agent does not own the semantic order")
        if request.frozen_cutoff is None:
            raise ValueError("order intent requires a frozen decision cutoff")
        if request.created_at < request.frozen_cutoff:
            raise ValueError("order intent timestamp predates the frozen decision cutoff")
        operation_id = _stable_database_uuid(
            "order-operation", operation_uuid(request.agent_id, request.idempotency_key)
        )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            _lock_agent(cursor, agent_id)
            cursor.execute(
                "SELECT id, request_fingerprint, market_id, outcome_id FROM order_operations "
                "WHERE agent_id = %s AND idempotency_key = %s FOR UPDATE",
                (agent_id, request.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                existing_id = uuid.UUID(str(existing[0]))
                if str(existing[1]) != request.fingerprint:
                    return OrderIntentReservation(
                        existing_id,
                        uuid.UUID(str(existing[2])),
                        uuid.UUID(str(existing[3])),
                        False,
                        conflict=True,
                    )
                return OrderIntentReservation(
                    existing_id,
                    uuid.UUID(str(existing[2])),
                    uuid.UUID(str(existing[3])),
                    False,
                    existing_result=_load_order_result_cursor(cursor, request, existing_id),
                )
            cursor.execute(
                "SELECT id, request_fingerprint, operation_id, status, market_id, outcome_id "
                "FROM order_operation_intents WHERE agent_id = %s AND idempotency_key = %s "
                "FOR UPDATE",
                (agent_id, request.idempotency_key),
            )
            intent = cursor.fetchone()
            if intent is not None:
                if str(intent[1]) != request.fingerprint:
                    return OrderIntentReservation(
                        operation_id,
                        uuid.UUID(str(intent[4])),
                        uuid.UUID(str(intent[5])),
                        False,
                        conflict=True,
                    )
                return OrderIntentReservation(
                    operation_id,
                    uuid.UUID(str(intent[4])),
                    uuid.UUID(str(intent[5])),
                    False,
                    in_flight=intent[2] is None and str(intent[3]) == "OPEN",
                )
            cursor.execute(
                "SELECT paused_at FROM agents WHERE id = %s FOR UPDATE",
                (agent_id,),
            )
            agent_row = cursor.fetchone()
            if agent_row is None:
                raise ValueError("agent does not exist")
            if agent_row[0] is not None:
                raise ValueError("agent is paused")
            cursor.execute(
                "SELECT data_cutoff FROM agent_cycles WHERE id = %s AND agent_id = %s",
                (agent_cycle_id, agent_id),
            )
            cycle_row = cursor.fetchone()
            if cycle_row is None or cycle_row[0] is None:
                raise ValueError("order intent cycle cutoff is not finalized")
            if _aware(cast(datetime, cycle_row[0])) != request.frozen_cutoff:
                raise ValueError("order intent cutoff differs from the cycle cutoff")
            cursor.execute(
                "SELECT 1 FROM order_operation_current WHERE agent_id = %s "
                "AND reconciliation_state IN ('REQUIRED', 'CONFLICT') LIMIT 1",
                (agent_id,),
            )
            if cursor.fetchone() is not None:
                return OrderIntentReservation(
                    operation_id,
                    None,
                    None,
                    False,
                    blocked=True,
                )
            cursor.execute(
                "SELECT m.id, o.id FROM markets m JOIN outcomes o "
                "ON o.market_id = m.id AND o.outcome_side = %s "
                "WHERE m.venue = 'kalshi' AND m.market_ref = %s",
                (OutcomeSide(request.outcome).value, request.market_key.market_ref),
            )
            identity = cursor.fetchone()
            if identity is None:
                raise ValueError("semantic order market identity is not persisted")
            market_id = uuid.UUID(str(identity[0]))
            outcome_id = uuid.UUID(str(identity[1]))
            intent_id = _stable_database_uuid(
                "order-intent", f"{request.agent_id}:{request.idempotency_key}"
            )
            cursor.execute(
                "INSERT INTO order_operation_intents "
                "(id, agent_id, agent_cycle_id, market_id, outcome_id, outcome_side, "
                "order_side, amount_kind, cash_amount_micros, contract_units, "
                "limit_price_micros, time_in_force, frozen_context_id, frozen_cutoff, "
                "requested_at, idempotency_key, request_fingerprint, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'OPEN', %s)",
                (
                    intent_id,
                    agent_id,
                    agent_cycle_id,
                    market_id,
                    outcome_id,
                    OutcomeSide(request.outcome).value,
                    _enum_value(request.action),
                    _enum_value(request.amount_type),
                    request.cash_amount_micros,
                    request.contract_units,
                    request.limit_price_micros,
                    _enum_value(request.time_in_force),
                    request.frozen_context_id,
                    request.frozen_cutoff,
                    request.created_at,
                    request.idempotency_key,
                    request.fingerprint,
                    request.created_at,
                ),
            )
        return OrderIntentReservation(operation_id, market_id, outcome_id, True)

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
        execution_context: MarketContext | None = None,
        fee_policy: FeePolicySnapshot | None = None,
        execution_context_refreshed_at: datetime | None = None,
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
            cursor.execute(
                "SELECT paused_at FROM agents WHERE id = %s FOR UPDATE",
                (agent_id,),
            )
            agent_row = cursor.fetchone()
            if agent_row is None:
                raise ValueError("agent does not exist")
            if agent_row[0] is not None:
                raise ValueError("agent is paused")
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
                    json.dumps(_reconciliation_evidence(result), sort_keys=True),
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
            persisted_book_snapshot_id = _persist_execution_context_cursor(
                cursor,
                result,
                execution_context,
                fee_policy=fee_policy,
                execution_context_refreshed_at=execution_context_refreshed_at,
                operation_id=operation_uuid_value,
                agent_id=agent_id,
                market_id=market_id,
            )
            cursor.execute(
                "UPDATE order_operation_intents SET status = 'FINALIZED', operation_id = %s "
                "WHERE agent_id = %s AND idempotency_key = %s "
                "AND request_fingerprint = %s AND status = 'OPEN'",
                (
                    operation_uuid_value,
                    agent_id,
                    result.request.idempotency_key,
                    result.request.fingerprint,
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
                persisted_fee_policy_id: uuid.UUID | None = None
                if fee_policy is not None:
                    cursor.execute(
                        "SELECT id FROM fee_policy_snapshots WHERE market_id = %s "
                        "AND policy_fingerprint = %s ORDER BY id DESC LIMIT 1",
                        (market_id, fee_policy.fingerprint),
                    )
                    policy_row = cursor.fetchone()
                    if policy_row is None:
                        raise ValueError("fee policy snapshot is not persisted")
                    persisted_fee_policy_id = uuid.UUID(str(policy_row[0]))
                for fill_index, fill in enumerate(result.fills):
                    calculation = (
                        result.fee_calculations[fill_index]
                        if fill_index < len(result.fee_calculations)
                        else None
                    )
                    fill_policy_fingerprint = (
                        fill.fee_policy_fingerprint or result.fee_policy_fingerprint
                    )
                    fill_evidence = {
                        "authoritative": fill.authoritative,
                        "fee_policy_fingerprint": fill_policy_fingerprint,
                        "fee_policy_evidence_references": [
                            dict(reference) for reference in result.fee_policy_evidence_references
                        ],
                    }
                    cursor.execute(
                        "INSERT INTO fills "
                        "(id, operation_id, fill_id, fill_fingerprint, contract_units, "
                        "price_micros, "
                        "gross_cash_micros, authoritative_fee_micros, net_cash_delta_micros, "
                        "frozen_context_id, execution_context_id, adapter_evidence, filled_at, "
                        "trade_fee_micros, rounding_fee_micros, rebate_micros, "
                        "fee_policy_snapshot_id) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, "
                        "%s, %s, %s, %s)",
                        (
                            _stable_database_uuid("fill", f"{result.operation_id}:{fill.fill_id}"),
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
                            json.dumps(fill_evidence, sort_keys=True),
                            fill.filled_at,
                            (
                                calculation.trade_fee_micros
                                if calculation is not None
                                else fill.trade_fee_micros
                            ),
                            (
                                calculation.rounding_fee_micros
                                if calculation is not None
                                else fill.rounding_fee_micros
                            ),
                            (
                                calculation.rebate_micros
                                if calculation is not None
                                else fill.rebate_micros
                            ),
                            persisted_fee_policy_id,
                        ),
                    )
                audit = result.liquidity_audit
                audit_snapshot_id = persisted_book_snapshot_id or book_snapshot_id
                if audit is not None and audit_snapshot_id is not None:
                    cursor.execute(
                        "INSERT INTO liquidity_haircut_audits "
                        "(snapshot_id, outcome_side, rule_version, captured_raw_levels, "
                        "effective_levels, raw_depth_units, ignored_quantity_units, "
                        "effective_depth_units, consumed_quantity_units, "
                        "cancelled_quantity_units, remaining_quantity_units, executable) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (snapshot_id, outcome_side) DO NOTHING",
                        (
                            audit_snapshot_id,
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
            for source_artifact in snapshot.source_artifacts:
                _persist_raw_artifact_cursor(cursor, source_artifact)
            inserted_id = _persist_fee_policy_cursor(
                cursor,
                snapshot,
                market_id=market_id,
                raw_artifact_id=raw_artifact_id,
            )
            for reference in snapshot.evidence_references:
                role = reference.get("role")
                sha256 = reference.get("sha256")
                if not isinstance(role, str) or not isinstance(sha256, str):
                    raise ValueError("fee policy evidence reference is malformed")
                cursor.execute(
                    "INSERT INTO fee_policy_snapshot_artifacts "
                    "(fee_policy_snapshot_id, raw_artifact_id, evidence_role) "
                    "SELECT %s, id, %s FROM raw_artifacts WHERE sha256 = %s "
                    "ON CONFLICT DO NOTHING",
                    (inserted_id, role, sha256),
                )
        return PersistenceResult(inserted_id, True, snapshot.fingerprint)

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

    def reconcile_not_submitted_operations(
        self, *, now: datetime
    ) -> tuple[uuid.UUID, ...]:
        """Cancel only pending operations explicitly proven not to be submitted."""

        now = _aware(now)
        unresolved_agents: set[uuid.UUID] = set()
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT operation.id, operation.agent_id "
                "FROM order_operations operation "
                "JOIN order_operation_current current_state "
                "ON current_state.operation_id = operation.id "
                "WHERE current_state.reconciliation_state IN ('REQUIRED', 'CONFLICT') "
                "ORDER BY operation.agent_id, operation.id "
            )
            candidates = tuple(cursor.fetchall())
            for candidate in candidates:
                operation_id = uuid.UUID(str(candidate[0]))
                agent_id = uuid.UUID(str(candidate[1]))
                _lock_agent(cursor, agent_id)
                cursor.execute(
                    "SELECT operation.id, operation.agent_id, operation.contract_units, "
                    "operation.cash_amount_micros, current_state.state, "
                    "current_state.reconciliation_state, latest.evidence, "
                    "EXISTS (SELECT 1 FROM fills WHERE fills.operation_id = operation.id) "
                    "AS has_fills "
                    "FROM order_operations operation "
                    "JOIN order_operation_current current_state "
                    "ON current_state.operation_id = operation.id "
                    "LEFT JOIN LATERAL (SELECT evidence FROM order_reconciliation_events event "
                    "WHERE event.operation_id = operation.id ORDER BY event.sequence_number DESC, "
                    "event.id DESC LIMIT 1) latest ON true "
                    "WHERE operation.id = %s AND operation.agent_id = %s "
                    "AND current_state.agent_id = %s FOR UPDATE OF operation, current_state",
                    (operation_id, agent_id, agent_id),
                )
                current = cursor.fetchone()
                if current is None or str(current[5]) not in {"REQUIRED", "CONFLICT"}:
                    continue
                if str(current[4]) != "PENDING" or str(current[5]) != "REQUIRED":
                    unresolved_agents.add(agent_id)
                    continue
                if bool(current[7]):
                    unresolved_agents.add(agent_id)
                    continue
                evidence = _json_mapping(current[6])
                if evidence.get("submission_state") != SubmissionState.NOT_SUBMITTED.value:
                    unresolved_agents.add(agent_id)
                    continue
                cursor.execute(
                    "SELECT COALESCE(max(sequence_number), -1) FROM "
                    "order_reconciliation_events WHERE operation_id = %s",
                    (operation_id,),
                )
                reconciliation_row = cursor.fetchone()
                reconciliation_sequence = (
                    int(str(reconciliation_row[0])) + 1 if reconciliation_row is not None else 0
                )
                cancellation_evidence = {
                    "automatic": True,
                    "submission_state": SubmissionState.NOT_SUBMITTED.value,
                    "venue_submission_occurred": False,
                    "cancelled_contract_units": (
                        None if current[2] is None else int(str(current[2]))
                    ),
                    "cancelled_cash_amount_micros": (
                        None if current[3] is None else int(str(current[3]))
                    ),
                }
                cursor.execute(
                    "INSERT INTO order_reconciliation_events "
                    "(id, operation_id, sequence_number, reconciliation_state, evidence, "
                    "observed_at, idempotency_key) VALUES "
                    "(%s, %s, %s, 'RESOLVED', %s::jsonb, %s, %s)",
                    (
                        _stable_database_uuid(
                            "reconciliation-event", f"{operation_id}:{reconciliation_sequence}"
                        ),
                        operation_id,
                        reconciliation_sequence,
                        json.dumps(cancellation_evidence, sort_keys=True),
                        now,
                        f"reconciliation:{operation_id}:{reconciliation_sequence}",
                    ),
                )
                cursor.execute(
                    "SELECT COALESCE(max(sequence_number), -1) FROM order_lifecycle_events "
                    "WHERE operation_id = %s",
                    (operation_id,),
                )
                lifecycle_row = cursor.fetchone()
                lifecycle_sequence = (
                    int(str(lifecycle_row[0])) + 1 if lifecycle_row is not None else 0
                )
                cursor.execute(
                    "INSERT INTO order_lifecycle_events "
                    "(id, operation_id, sequence_number, state, reason, observed_at, "
                    "idempotency_key) VALUES (%s, %s, %s, 'CANCELLED', %s, %s, %s)",
                    (
                        _stable_database_uuid(
                            "lifecycle-event", f"{operation_id}:cancelled:{lifecycle_sequence}"
                        ),
                        operation_id,
                        lifecycle_sequence,
                        "automatic reconciliation: NOT_SUBMITTED",
                        now,
                        f"lifecycle:{operation_id}:cancelled:{lifecycle_sequence}",
                    ),
                )
                cursor.execute(
                    "UPDATE order_operation_current SET state = 'CANCELLED', "
                    "reconciliation_state = 'RESOLVED', state_version = state_version + 1, "
                    "updated_at = %s WHERE operation_id = %s AND agent_id = %s",
                    (now, operation_id, agent_id),
                )
        return tuple(sorted(unresolved_agents, key=str))

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
        "fee_policy_fingerprint": result.fee_policy_fingerprint,
        "fee_components": [
            {
                "trade_fee_micros": int(fill.trade_fee_micros),
                "rounding_fee_micros": int(fill.rounding_fee_micros),
                "rebate_micros": int(fill.rebate_micros),
                "fee_policy_fingerprint": fill.fee_policy_fingerprint,
            }
            for fill in result.fills
        ],
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


def _load_order_result_cursor(
    cursor: _Cursor, request: OrderRequest, operation_id: uuid.UUID
) -> OrderResult:
    cursor.execute(
        "SELECT operation.amount_kind, operation.contract_units, operation.created_at, "
        "current_state.state, current_state.reconciliation_state, current_state.updated_at "
        "FROM order_operations operation JOIN order_operation_current current_state "
        "ON current_state.operation_id = operation.id WHERE operation.id = %s",
        (operation_id,),
    )
    operation = cursor.fetchone()
    if operation is None:
        raise ValueError("semantic order operation disappeared")
    submitted_at = _aware(cast(datetime, operation[2]))
    replay_request = replace(request, created_at=submitted_at)
    cursor.execute(
        "SELECT fill_id, fill_fingerprint, contract_units, price_micros, gross_cash_micros, "
        "authoritative_fee_micros, net_cash_delta_micros, frozen_context_id, "
        "execution_context_id, filled_at, trade_fee_micros, rounding_fee_micros, "
        "rebate_micros, fee_policy_snapshot_id, fps.policy_fingerprint "
        "FROM fills f LEFT JOIN fee_policy_snapshots fps "
        "ON fps.id = f.fee_policy_snapshot_id WHERE f.operation_id = %s "
        "ORDER BY fill_id",
        (operation_id,),
    )
    fills = tuple(
        EconomicFill(
            fill_id=str(row[0]),
            contract_units=ContractQuantity(int(str(row[2]))),
            price_micros=PriceMicros(int(str(row[3]))),
            gross_cash_micros=MoneyMicros(int(str(row[4]))),
            fee_micros=MoneyMicros(int(str(row[5]))),
            net_cash_delta_micros=int(str(row[6])),
            filled_at=_aware(cast(datetime, row[9])),
            frozen_context_id=None if row[7] is None else str(row[7]),
            execution_context_id=None if row[8] is None else str(row[8]),
            fingerprint=str(row[1]),
            trade_fee_micros=MoneyMicros(int(str(row[10] or 0))),
            rounding_fee_micros=MoneyMicros(int(str(row[11] or 0))),
            rebate_micros=MoneyMicros(int(str(row[12] or 0))),
            fee_policy_fingerprint=None if row[14] is None else str(row[14]),
        )
        for row in cursor.fetchall()
    )
    cursor.execute(
        "SELECT evidence FROM order_reconciliation_events WHERE operation_id = %s "
        "ORDER BY sequence_number DESC, id DESC LIMIT 1",
        (operation_id,),
    )
    reconciliation_row = cursor.fetchone()
    evidence = _json_mapping(reconciliation_row[0]) if reconciliation_row is not None else {}
    cursor.execute(
        "SELECT reason FROM order_lifecycle_events WHERE operation_id = %s "
        "ORDER BY sequence_number DESC, id DESC LIMIT 1",
        (operation_id,),
    )
    lifecycle_row = cursor.fetchone()
    reason = None if lifecycle_row is None or lifecycle_row[0] is None else str(lifecycle_row[0])
    cursor.execute(
        "SELECT id FROM execution_contexts WHERE operation_id = %s",
        (operation_id,),
    )
    context_row = cursor.fetchone()
    state = OrderState(str(operation[3]))
    filled_units = sum(int(fill.contract_units) for fill in fills)
    requested_units = (
        int(str(operation[1]))
        if operation[1] is not None
        else filled_units
    )
    cancelled_units = (
        requested_units - filled_units if state is OrderState.CANCELLED else 0
    )
    remaining_units = (
        0
        if state is OrderState.CANCELLED
        else max(0, requested_units - filled_units)
    )
    gross_delta = sum(
        int(fill.gross_cash_micros)
        if replay_request.side.value == "SELL"
        else -int(fill.gross_cash_micros)
        for fill in fills
    )
    fee = sum(int(fill.fee_micros) for fill in fills)
    raw_error_code = evidence.get("error_code")
    if raw_error_code is None and state is OrderState.REJECTED:
        raw_error_code = reason
    error_code = None if raw_error_code is None else str(raw_error_code)
    raw_submission_state = evidence.get("submission_state")
    submission_state = (
        SubmissionState(str(raw_submission_state))
        if raw_submission_state in {item.value for item in SubmissionState}
        else None
    )
    fee_policy_fingerprint = evidence.get("fee_policy_fingerprint")
    if not isinstance(fee_policy_fingerprint, str):
        fee_policy_fingerprint = next(
            (
                fill.fee_policy_fingerprint
                for fill in fills
                if fill.fee_policy_fingerprint is not None
            ),
            None,
        )
    fee_evidence_value = evidence.get("fee_policy_evidence_references", ())
    fee_evidence_references = (
        tuple(item for item in fee_evidence_value if isinstance(item, Mapping))
        if isinstance(fee_evidence_value, (list, tuple))
        else ()
    )
    return OrderResult(
        request=replay_request,
        operation_id=operation_uuid(request.agent_id, request.idempotency_key),
        state=state,
        reconciliation_state=ReconciliationState(str(operation[4])),
        requested_units=ContractQuantity(requested_units),
        filled_units=ContractQuantity(filled_units),
        remaining_units=ContractQuantity(remaining_units),
        cancelled_units=ContractQuantity(cancelled_units),
        fills=fills,
        gross_cash_delta_micros=gross_delta,
        fee_micros=MoneyMicros(fee),
        net_cash_delta_micros=gross_delta - fee,
        frozen_context_id=request.frozen_context_id,
        execution_context_id=(
            str(context_row[0])
            if context_row is not None
            else (fills[0].execution_context_id if fills else None)
        ),
        submitted_at=submitted_at,
        updated_at=_aware(cast(datetime, operation[5])),
        error_code=error_code,
        message=(
            str(evidence["message"])
            if isinstance(evidence.get("message"), str)
            else None
        ),
        submission_state=submission_state,
        reconciliation_evidence=evidence,
        fee_policy_fingerprint=fee_policy_fingerprint,
        fee_policy_evidence_references=fee_evidence_references,
    )


def _persist_execution_context_cursor(
    cursor: _Cursor,
    result: OrderResult,
    context: MarketContext | None,
    *,
    fee_policy: FeePolicySnapshot | None,
    execution_context_refreshed_at: datetime | None,
    operation_id: uuid.UUID,
    agent_id: uuid.UUID,
    market_id: uuid.UUID,
) -> uuid.UUID | None:
    if context is None:
        return None
    if result.execution_context_id is None:
        raise ValueError("fresh execution context id is required for context persistence")
    try:
        execution_context_id = uuid.UUID(result.execution_context_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("fresh execution context id must be a UUID") from exc
    if context.market.key.stable_id != market_id:
        raise ValueError("fresh execution market identity differs from the operation")
    execution_cutoff = _aware(result.updated_at)
    refreshed_at = _aware(execution_context_refreshed_at or execution_cutoff)
    frozen_cutoff = _aware(result.request.frozen_cutoff or result.submitted_at)
    if frozen_cutoff > refreshed_at or refreshed_at > execution_cutoff:
        raise ValueError("fresh execution context timestamps are not ordered")
    if context.market.observed_at > execution_cutoff:
        raise ValueError("fresh execution market is newer than its execution cutoff")
    if (
        context.market.source_updated_at is not None
        and context.market.source_updated_at > execution_cutoff
    ):
        raise ValueError("fresh execution market update is newer than its execution cutoff")
    if (
        context.market.source_updated_at is not None
        and context.market.source_updated_at > context.market.observed_at
    ):
        raise ValueError("fresh execution market update postdates its observation")
    if context.order_book.observed_at > execution_cutoff:
        raise ValueError("fresh execution book is newer than its execution cutoff")
    if context.order_book.cutoff > execution_cutoff:
        raise ValueError("fresh execution book cutoff is newer than its execution cutoff")
    if context.order_book.cutoff < frozen_cutoff:
        raise ValueError("fresh execution book predates the frozen decision cutoff")
    if (
        context.order_book.source_timestamp is not None
        and context.order_book.source_timestamp > context.order_book.observed_at
    ):
        raise ValueError("fresh execution book timestamp postdates its observation")
    market_artifact_id = _persist_raw_artifact_cursor(cursor, context.market.audit)
    book_artifact_id = _persist_raw_artifact_cursor(cursor, context.order_book.artifact)
    fee_policy_id: uuid.UUID | None = None
    if fee_policy is not None:
        cursor.execute(
            "SELECT id FROM fee_policy_snapshots WHERE market_id = %s "
            "AND policy_fingerprint = %s ORDER BY id DESC LIMIT 1",
            (market_id, fee_policy.fingerprint),
        )
        policy_row = cursor.fetchone()
        if policy_row is None:
            primary_artifact = fee_policy.raw_artifact or context.market.audit
            primary_artifact_id = _persist_raw_artifact_cursor(cursor, primary_artifact)
            for source_artifact in fee_policy.source_artifacts:
                _persist_raw_artifact_cursor(cursor, source_artifact)
            fee_policy_id = _persist_fee_policy_cursor(
                cursor,
                fee_policy,
                market_id=market_id,
                raw_artifact_id=primary_artifact_id,
            )
            for reference in fee_policy.evidence_references:
                role = reference.get("role")
                sha256 = reference.get("sha256")
                if not isinstance(role, str) or not isinstance(sha256, str):
                    raise ValueError("fee policy evidence reference is malformed")
                cursor.execute(
                    "INSERT INTO fee_policy_snapshot_artifacts "
                    "(fee_policy_snapshot_id, raw_artifact_id, evidence_role) "
                    "SELECT %s, id, %s FROM raw_artifacts WHERE sha256 = %s "
                    "ON CONFLICT DO NOTHING",
                    (fee_policy_id, role, sha256),
                )
        else:
            fee_policy_id = uuid.UUID(str(policy_row[0]))
    cursor.execute(
        "INSERT INTO execution_contexts "
        "(id, operation_id, agent_id, market_id, frozen_context_id, frozen_cutoff, "
        "refreshed_at, execution_cutoff, fee_policy_snapshot_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (
            execution_context_id,
            operation_id,
            agent_id,
            market_id,
            result.frozen_context_id,
            frozen_cutoff,
            refreshed_at,
            execution_cutoff,
            fee_policy_id,
            execution_cutoff,
        ),
    )
    market_snapshot_id = _stable_database_uuid(
        "execution-market-snapshot", f"{execution_context_id}:{context.market.snapshot_id}"
    )
    cursor.execute(
        "INSERT INTO execution_market_snapshots "
        "(id, execution_context_id, market_id, market_ref, lifecycle_status, eligible, "
        "tradeable, question, resolution_rules, resolution_source, open_time, close_time, "
        "expected_expiration_time, latest_expiration_time, fee_waiver_expiration_time, "
        "volume_units, liquidity_micros, "
        "observed_at, source_updated_at, cutoff, raw_artifact_id) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            market_snapshot_id,
            execution_context_id,
            market_id,
            context.market.market_ref,
            context.market.status.value,
            context.market.eligible,
            context.market.tradeable,
            context.market.question,
            context.market.resolution_rules,
            context.market.resolution_source,
            context.market.open_time,
            context.market.close_time,
            context.market.expected_expiration_time,
            context.market.latest_expiration_time,
            context.market.fee_waiver_expiration_time,
            int(context.market.volume),
            int(context.market.liquidity_micros),
            context.market.observed_at,
            context.market.source_updated_at,
            execution_cutoff,
            market_artifact_id,
        ),
    )
    book_snapshot_id = _stable_database_uuid(
        "execution-order-book", f"{execution_context_id}:{context.order_book.snapshot_id}"
    )
    cursor.execute(
        "INSERT INTO order_book_snapshots "
        "(id, freeze_id, execution_context_id, market_id, observed_at, source_timestamp, "
        "cutoff, raw_artifact_id) VALUES (%s, NULL, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            book_snapshot_id,
            execution_context_id,
            market_id,
            context.order_book.observed_at,
            context.order_book.source_timestamp,
            execution_cutoff,
            book_artifact_id,
        ),
    )
    for outcome_side, book_side, levels in _book_levels(context):
        for level_index, level in enumerate(levels):
            cursor.execute(
                "INSERT INTO order_book_levels "
                "(snapshot_id, outcome_side, book_side, level_index, price_micros, contract_units) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    book_snapshot_id,
                    outcome_side,
                    book_side,
                    level_index,
                    int(level.price),
                    int(level.quantity),
                ),
            )
    return book_snapshot_id


def _book_levels(
    context: MarketContext,
) -> tuple[tuple[str, str, tuple[CanonicalLevel, ...]], ...]:
    book = context.order_book
    return (
        ("YES", "bid", book.yes_bids),
        ("YES", "ask", book.yes_asks),
        ("NO", "bid", book.no_bids),
        ("NO", "ask", book.no_asks),
    )


def _persist_raw_artifact_cursor(cursor: _Cursor, artifact: RawArtifact) -> uuid.UUID:
    artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:raw-artifact:{artifact.sha256}")
    cursor.execute(
        "INSERT INTO raw_artifacts "
        "(id, sha256, uri, byte_length, source_endpoint, request_identity, source_timestamp, "
        "observed_at, captured_cutoff, schema_version, audit_metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, '{}'::jsonb) "
        "ON CONFLICT (sha256) DO NOTHING",
        (
            artifact_id,
            artifact.sha256,
            artifact.uri,
            artifact.byte_length,
            artifact.source_endpoint,
            artifact.request_identity,
            artifact.source_timestamp,
            artifact.observed_at,
            artifact.schema_version,
        ),
    )
    cursor.execute(
        "SELECT id, byte_length, uri, request_identity FROM raw_artifacts "
        "WHERE sha256 = %s FOR UPDATE",
        (artifact.sha256,),
    )
    existing = cursor.fetchone()
    if existing is None:
        raise ValueError("raw artifact disappeared during persistence")
    if (
        int(str(existing[1])) != artifact.byte_length
        or str(existing[2]) != artifact.uri
        or (existing[3] or None) != artifact.request_identity
    ):
        raise ValueError("raw artifact digest was reused with different metadata")
    return uuid.UUID(str(existing[0]))


def _persist_fee_policy_cursor(
    cursor: _Cursor,
    snapshot: FeePolicySnapshot,
    *,
    market_id: uuid.UUID,
    raw_artifact_id: uuid.UUID,
) -> uuid.UUID:
    """Insert an exact execution fee snapshot in the caller's transaction."""

    policy_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:fee-policy:{snapshot.fingerprint}")
    cursor.execute(
        "SELECT id, policy_fingerprint FROM fee_policy_snapshots "
        "WHERE market_id = %s AND policy_fingerprint = %s FOR UPDATE",
        (market_id, snapshot.fingerprint),
    )
    existing = cursor.fetchone()
    if existing is not None:
        if str(existing[1]) != snapshot.fingerprint:
            raise ValueError("fee policy fingerprint conflict")
        return uuid.UUID(str(existing[0]))
    resolved_num, resolved_den = snapshot.resolved_multiplier
    cursor.execute(
        "INSERT INTO fee_policy_snapshots "
        "(id, market_id, policy_version, formula_version, schedule_identity, fee_type, "
        "participant_role, multiplier_numerator, multiplier_denominator, "
        "series_multiplier_numerator, series_multiplier_denominator, "
        "event_override_micros, event_override_numerator, event_override_denominator, "
        "event_override_fee_type, event_override_cleared, rate_numerator, rate_denominator, "
        "waiver, waiver_evidence, exact_inputs, effective_at, as_of_at, scheduled_ts, "
        "observed_at, cutoff, source_tier, raw_artifact_id, schedule_sha256, "
        "settlement_fee_micros, policy_fingerprint) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            policy_id,
            market_id,
            snapshot.contract_version,
            snapshot.formula_version,
            snapshot.schedule_version,
            snapshot.fee_type,
            FeeParticipantRole(snapshot.participant_role).value.lower(),
            resolved_num,
            resolved_den,
            snapshot.series_multiplier_numerator,
            snapshot.series_multiplier_denominator,
            snapshot.event_override_numerator
            if snapshot.event_override_denominator == 1_000_000
            else None,
            snapshot.event_override_numerator,
            snapshot.event_override_denominator,
            snapshot.event_override_fee_type,
            snapshot.event_override_cleared,
            snapshot.rate_numerator,
            snapshot.rate_denominator,
            snapshot.waiver,
                (
                    None
                    if snapshot.waiver_evidence is None
                    else json.dumps(dict(snapshot.waiver_evidence))
                ),
            json.dumps(dict(snapshot.exact_inputs), sort_keys=True),
            snapshot.effective_from,
            snapshot.as_of,
            snapshot.scheduled_ts,
            snapshot.source_observed_at,
            snapshot.cutoff,
            snapshot.source_tier,
            raw_artifact_id,
            snapshot.schedule_sha256,
            snapshot.settlement_fee_micros,
            snapshot.fingerprint,
        ),
    )
    return policy_id


def _json_mapping(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _reconciliation_evidence(result: OrderResult) -> dict[str, object]:
    evidence: dict[str, object] = {
        "contract_version": result.contract_version,
        "error_code": (
            _enum_value(result.error_code) if result.error_code is not None else None
        ),
        "message": result.message,
    }
    if result.submission_state is not None:
        evidence["submission_state"] = _enum_value(result.submission_state)
    if result.fee_policy_fingerprint is not None:
        evidence["fee_policy_fingerprint"] = result.fee_policy_fingerprint
        evidence["fee_policy_evidence_references"] = [
            dict(reference) for reference in result.fee_policy_evidence_references
        ]
    if result.fills:
        evidence["fee_components"] = {
            "trade_fee_micros": sum(int(fill.trade_fee_micros) for fill in result.fills),
            "rounding_fee_micros": sum(
                int(fill.rounding_fee_micros) for fill in result.fills
            ),
            "rebate_micros": sum(int(fill.rebate_micros) for fill in result.fills),
            "net_fee_micros": int(result.fee_micros),
        }
    evidence.update(dict(result.reconciliation_evidence))
    return evidence


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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)
