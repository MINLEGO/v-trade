from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

from vtrade.broker import BinaryPaperBroker, BinarySettlementEngine, SettlementBlockedError
from vtrade.broker_repository import PostgresBrokerRepository
from vtrade.deadline import DeadlineExceeded, run_with_deadline
from vtrade.domain.execution import (
    FeeParticipantRole,
    FeePolicySnapshot,
    OrderRequest,
    SemanticExecutionError,
    SubmissionState,
    operation_uuid,
)
from vtrade.domain.ports import FreshExecutionContextError, FreshExecutionContextPort
from vtrade.domain.types import (
    BinaryMarket,
    BinaryOutcome,
    CanonicalLevel,
    CanonicalOrderBook,
    ContractQuantity,
    EventKey,
    MarketContext,
    MarketKey,
    MarketStatus,
    MoneyMicros,
    OutcomeKey,
    OutcomeSide,
    PriceGrid,
    PriceMicros,
    RawArtifact,
    ResolutionObservation,
    SeriesKey,
)
from vtrade.portfolio import ContractPortfolio, ContractPosition
from vtrade.postgres_runtime import PostgresRuntimeRepository
from vtrade.runtime import (
    AlertEvent,
    BrokerExecutionResult,
    CycleClaim,
    PreSettlementResult,
    SettlementValuationResult,
)


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("semantic runtime timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _cutoff(claim: CycleClaim) -> datetime:
    if claim.data_cutoff is None:
        raise RuntimeError("cycle cutoff is not finalized")
    return _aware(claim.data_cutoff)


def _mapping(value: object) -> dict[str, Any]:
    return {str(key): child for key, child in value.items()} if isinstance(value, Mapping) else {}


def _timestamp(value: object, field: str, *, required: bool = True) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"freeze payload lacks {field}")
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError as exc:
        raise RuntimeError(f"freeze payload has malformed {field}") from exc


def _artifact(value: object, *, observed_at: datetime | None = None) -> RawArtifact:
    payload = _mapping(value)
    sha256 = payload.get("sha256")
    uri = payload.get("uri")
    byte_length = payload.get("byte_length")
    if (
        not isinstance(sha256, str)
        or not isinstance(uri, str)
        or isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
    ):
        raise RuntimeError("freeze payload contains malformed artifact evidence")
    return RawArtifact(sha256, byte_length, uri, observed_at=observed_at)


def _level(value: object) -> CanonicalLevel:
    payload = _mapping(value)
    price = payload.get("price_micros")
    units = payload.get("contract_units")
    if (
        isinstance(price, bool)
        or not isinstance(price, int)
        or isinstance(units, bool)
        or not isinstance(units, int)
    ):
        raise RuntimeError("freeze payload contains malformed price levels")
    return CanonicalLevel(PriceMicros(price), ContractQuantity(units))


def frozen_context(frozen: Mapping[str, object], market_ref: str) -> MarketContext:
    contexts = frozen.get("contexts")
    if not isinstance(contexts, list):
        raise RuntimeError("published freeze lacks typed market contexts")
    selected: Mapping[str, object] | None = None
    for item in contexts:
        candidate = _mapping(item)
        if candidate.get("market_ref") == market_ref:
            selected = candidate
            break
    if selected is None:
        raise RuntimeError(f"market {market_ref} is absent from the published freeze")

    market_payload = _mapping(selected.get("market"))
    market_key = MarketKey(market_ref)
    series_key = SeriesKey(str(market_payload.get("series_ref") or ""))
    event_key = EventKey(str(market_payload.get("event_ref") or ""))
    raw_ranges = market_payload.get("price_ranges")
    if not isinstance(raw_ranges, list):
        raise RuntimeError("market price grid is missing from the freeze")
    grid = PriceGrid.from_ranges(
        [
            {
                "start": str(
                    Decimal(int(str(_mapping(item).get("start")))) / Decimal(1_000_000)
                ),
                "end": str(
                    Decimal(int(str(_mapping(item).get("end")))) / Decimal(1_000_000)
                ),
                "step": str(
                    Decimal(int(str(_mapping(item).get("step")))) / Decimal(1_000_000)
                ),
            }
            for item in raw_ranges
        ]
    )
    raw_outcomes = market_payload.get("outcomes")
    if not isinstance(raw_outcomes, list):
        raise RuntimeError("binary outcomes are missing from the freeze")
    outcomes = tuple(
        BinaryOutcome(
            OutcomeKey(market_key, str(_mapping(item).get("outcome") or "")),
            str(_mapping(item).get("label") or ""),
            bool(_mapping(item).get("eligible")),
        )
        for item in raw_outcomes
    )
    if len(outcomes) != 2:
        raise RuntimeError("published market does not contain YES and NO")
    market_observed_at = _timestamp(market_payload.get("observed_at"), "market observed_at")
    open_time = _timestamp(market_payload.get("open_time"), "market open_time")
    assert market_observed_at is not None and open_time is not None
    volume_24h_units = market_payload.get("volume_24h_units")
    if (
        isinstance(volume_24h_units, bool)
        or not isinstance(volume_24h_units, int)
        or volume_24h_units < 0
    ):
        raise RuntimeError("freeze payload lacks valid volume_24h_units metric")
    market = BinaryMarket(
        key=market_key,
        series_key=series_key,
        event_key=event_key,
        question=str(market_payload.get("question") or ""),
        resolution_rules=str(market_payload.get("resolution_rules") or ""),
        resolution_source=(
            None
            if market_payload.get("resolution_source") is None
            else str(market_payload.get("resolution_source"))
        ),
        open_time=open_time,
        close_time=_timestamp(
            market_payload.get("close_time"), "market close_time", required=False
        ),
        expected_expiration_time=_timestamp(
            market_payload.get("expected_expiration_time"),
            "market expected_expiration_time",
            required=False,
        ),
        latest_expiration_time=_timestamp(
            market_payload.get("latest_expiration_time"),
            "market latest_expiration_time",
            required=False,
        ),
        status=MarketStatus(str(market_payload.get("status") or "").lower()),
        eligible=bool(market_payload.get("eligible")),
        price_grid=grid,
        outcomes=outcomes,
        observed_at=market_observed_at,
        audit=_artifact(market_payload.get("audit"), observed_at=market_observed_at),
        volume=ContractQuantity(int(str(market_payload.get("volume_units") or 0))),
        liquidity_micros=MoneyMicros(int(str(market_payload.get("liquidity_micros") or 0))),
        volume_24h=ContractQuantity(volume_24h_units),
    )

    book_payload = _mapping(selected.get("order_book"))
    book_observed_at = _timestamp(book_payload.get("observed_at"), "book observed_at")
    book_cutoff = _timestamp(book_payload.get("cutoff"), "book cutoff")
    assert book_observed_at is not None and book_cutoff is not None
    book = CanonicalOrderBook(
        market_key,
        tuple(_level(item) for item in cast(list[object], book_payload.get("yes_bids", []))),
        tuple(_level(item) for item in cast(list[object], book_payload.get("yes_asks", []))),
        tuple(_level(item) for item in cast(list[object], book_payload.get("no_bids", []))),
        tuple(_level(item) for item in cast(list[object], book_payload.get("no_asks", []))),
        book_observed_at,
        book_cutoff,
        _artifact(book_payload.get("audit"), observed_at=book_observed_at),
        _timestamp(book_payload.get("source_timestamp"), "book source_timestamp", required=False),
    )
    return MarketContext(market, book)


def load_contract_portfolio(
    database_url: str, agent_id: uuid.UUID, *, connect: _Connect
) -> ContractPortfolio:
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE((SELECT sum(lp.amount_micros) FROM ledger_postings lp "
            "JOIN ledger_entries le ON le.id = lp.ledger_entry_id "
            "WHERE le.agent_id = a.id AND lp.account = 'cash'), 0), "
            "a.portfolio_version, EXISTS (SELECT 1 FROM order_operation_current current_state "
            "JOIN order_operations operation ON operation.id = current_state.operation_id "
            "WHERE operation.agent_id = a.id "
            "AND current_state.reconciliation_state IN ('REQUIRED', 'CONFLICT')), "
            "COALESCE((SELECT sum(position.realized_pnl_micros) FROM positions position "
            "WHERE position.agent_id = a.id), 0) FROM agents a WHERE a.id = %s",
            (agent_id,),
        )
        account = cursor.fetchone()
        if account is None:
            raise RuntimeError("agent account is missing")
        cursor.execute(
            "SELECT m.market_ref, p.outcome_side, p.contract_units, "
            "p.gross_cost_basis_micros, p.entry_fees_micros, p.realized_pnl_micros, "
            "p.updated_at FROM positions p JOIN markets m ON m.id = p.market_id "
            "WHERE p.agent_id = %s AND p.contract_units > 0 "
            "ORDER BY m.market_ref, p.outcome_side",
            (agent_id,),
        )
        rows = cursor.fetchall()
    positions = tuple(
        ContractPosition(
            MarketKey(str(row[0])),
            OutcomeSide(str(row[1])),
            ContractQuantity(int(str(row[2]))),
            MoneyMicros(int(str(row[3]))),
            MoneyMicros(int(str(row[4]))),
            int(str(row[5])),
            _aware(cast(datetime, row[6])),
        )
        for row in rows
    )
    return ContractPortfolio(
        str(agent_id),
        MoneyMicros(int(str(account[0]))),
        positions,
        int(str(account[1])),
        bool(account[2]),
        int(str(account[3])),
    )


class ProductionSemanticOrderExecutor:
    """Execute a tool order through the binary paper contract and clean ledger."""

    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], datetime],
        maximum_book_age: timedelta,
        maximum_market_fraction: Decimal,
        connect: _Connect | None = None,
        execution_context_provider: FreshExecutionContextPort | None = None,
        fresh_execution_deadline_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._database_url = database_url
        self._clock = clock
        self._maximum_book_age = maximum_book_age
        self._connect = connect or _default_connect
        if fresh_execution_deadline_seconds <= 0:
            raise ValueError("fresh_execution_deadline_seconds must be positive")
        self._execution_context_provider = execution_context_provider
        self._fresh_execution_deadline_seconds = fresh_execution_deadline_seconds
        self._monotonic = monotonic
        self._repository = PostgresBrokerRepository(
            database_url, connect=cast(Any, self._connect)
        )
        if not maximum_market_fraction.is_finite() or maximum_market_fraction <= 0:
            raise ValueError("maximum market fraction must be between zero and one")
        numerator, denominator = maximum_market_fraction.as_integer_ratio()
        if numerator > denominator:
            raise ValueError("maximum market fraction must be between zero and one")
        self._broker = BinaryPaperBroker(
            maximum_market_concentration_numerator=numerator,
            maximum_market_concentration_denominator=denominator,
            maximum_book_age=maximum_book_age,
        )

    def submit_and_execute(
        self, claim: CycleClaim, frozen: dict[str, Any], request: OrderRequest
    ) -> Any:
        cutoff = _cutoff(claim)
        if request.agent_id != str(claim.agent_id):
            raise RuntimeError("semantic order agent does not own the cycle")
        if request.frozen_cutoff != cutoff or request.created_at < cutoff:
            raise RuntimeError("semantic order must retain the immutable cycle cutoff")
        raw_contexts = frozen.get("contexts")
        if not isinstance(raw_contexts, list):
            raise RuntimeError("published freeze lacks typed market contexts")
        domain_operation_id = operation_uuid(request.agent_id, request.idempotency_key)
        contexts: dict[MarketKey | str, MarketContext] = {
            MarketKey(str(_mapping(item).get("market_ref") or "")): frozen_context(
                frozen, str(_mapping(item).get("market_ref") or "")
            )
            for item in raw_contexts
        }
        reservation = self._repository.prepare_order_intent(
            request,
            agent_id=claim.agent_id,
            agent_cycle_id=claim.cycle_id,
        )
        if reservation.existing_result is not None:
            return reservation.existing_result
        portfolio = load_contract_portfolio(
            self._database_url, claim.agent_id, connect=self._connect
        )
        if reservation.conflict:
            return BinaryPaperBroker._rejected(
                request,
                portfolio,
                SemanticExecutionError.IDEMPOTENCY_CONFLICT,
                self._execution_now(request.created_at),
                operation_id=domain_operation_id,
                message="idempotency key was reused with different request evidence",
            )
        if reservation.blocked:
            return BinaryPaperBroker._rejected(
                request,
                portfolio,
                SemanticExecutionError.RECONCILIATION_REQUIRED,
                self._execution_now(request.created_at),
                operation_id=domain_operation_id,
                message="agent has an unresolved reconciliation operation",
            )
        if reservation.in_flight:
            return replace(
                BinaryPaperBroker._pending(
                    request,
                    portfolio,
                    self._execution_now(request.created_at),
                    operation_id=domain_operation_id,
                    frozen_context_id=request.frozen_context_id,
                ),
                submission_state=SubmissionState.UNKNOWN,
                reconciliation_evidence={"submission_state": SubmissionState.UNKNOWN.value},
            )
        if reservation.market_id is None or reservation.outcome_id is None:
            raise RuntimeError("order intent identity is incomplete")
        fresh: MarketContext | None = None
        execution_cutoff = self._execution_now(request.created_at)
        try:
            fresh = self._refresh_execution_context(request.market_key)
            execution_cutoff = self._execution_now(request.created_at)
            self._validate_fresh_context(request, fresh, execution_cutoff)
        except FreshExecutionContextError as exc:
            result = self._refresh_failure_result(
                request, portfolio, exc, domain_operation_id
            )
            self._persist_order_result(
                result,
                claim=claim,
                reservation=reservation,
            )
            return result
        except (TimeoutError, OSError, ConnectionError) as exc:
            result = self._pending_refresh_result(
                request, portfolio, exc, domain_operation_id
            )
            self._persist_order_result(
                result,
                claim=claim,
                reservation=reservation,
            )
            return result
        except ValueError as exc:
            result = BinaryPaperBroker._rejected(
                request,
                portfolio,
                _context_error_code(str(exc)),
                execution_cutoff,
                operation_id=domain_operation_id,
                message=str(exc),
            )
            persistable_fresh = self._persistable_fresh_context(
                request, fresh, execution_cutoff
            )
            result = replace(
                result,
                frozen_context_id=request.frozen_context_id,
                submission_state=SubmissionState.NOT_SUBMITTED,
                reconciliation_evidence={
                    "submission_state": SubmissionState.NOT_SUBMITTED.value,
                    "venue_submission_occurred": False,
                    "refresh_failure": True,
                    "failure_type": type(exc).__name__,
                },
            )
            if persistable_fresh is not None:
                result = replace(result, execution_context_id=str(reservation.operation_id))
            self._persist_order_result(
                result,
                claim=claim,
                reservation=reservation,
                execution_context=persistable_fresh,
                execution_context_refreshed_at=(
                    execution_cutoff if persistable_fresh is not None else None
                ),
            )
            return result

        policy = self._fee_policy(request.market_key.market_ref, cutoff)
        result = self._broker.execute(
            request,
            context=fresh,
            portfolio=portfolio,
            fee_policy=policy,
            frozen_context_id=request.frozen_context_id or str(claim.cycle_id),
            execution_context_id=str(reservation.operation_id),
            now=execution_cutoff,
            account_value_micros=(
                int(portfolio.account_value_micros(contexts))
                if portfolio.positions
                and all(
                    any(position.market_ref == context_key for context_key in contexts)
                    for position in portfolio.positions
                )
                else None
            ),
            valuation_contexts=contexts,
        )
        result = replace(
            result,
            frozen_context_id=request.frozen_context_id or str(claim.cycle_id),
            execution_context_id=str(reservation.operation_id),
        )
        self._persist_order_result(
            result,
            claim=claim,
            reservation=reservation,
            execution_context=fresh,
            fee_policy=policy,
            execution_context_refreshed_at=execution_cutoff,
        )
        return result

    def _refresh_execution_context(self, market_key: MarketKey) -> MarketContext:
        provider = self._execution_context_provider
        if provider is None:
            raise FreshExecutionContextError(
                "fresh execution context provider is unavailable", retryable=False
            )
        deadline = self._monotonic() + self._fresh_execution_deadline_seconds

        def invoke() -> MarketContext:
            try:
                result = provider.get_fresh_execution_context(market_key, deadline=deadline)
            except TypeError as first_error:
                try:
                    result = provider.get_fresh_execution_context(market_key)
                except TypeError as second_error:
                    raise FreshExecutionContextError(
                        "fresh execution context provider has an invalid signature",
                        retryable=False,
                    ) from second_error
                del first_error
            if not isinstance(result, MarketContext):
                raise FreshExecutionContextError(
                    "fresh execution context provider returned an invalid context",
                    retryable=False,
                )
            return result

        try:
            return run_with_deadline(
                invoke,
                deadline=deadline,
                label="fresh execution context",
                clock=self._monotonic,
            )
        except DeadlineExceeded as exc:
            raise FreshExecutionContextError(
                "fresh execution context refresh exceeded its ten-second deadline",
                retryable=True,
            ) from exc

    def _validate_fresh_context(
        self, request: OrderRequest, context: MarketContext, execution_cutoff: datetime
    ) -> None:
        if context.market.key != request.market_key:
            raise ValueError("fresh execution market differs from market_ref")
        if not context.market.tradeable:
            raise ValueError("fresh execution market is not active and tradeable")
        selected_outcome = (
            context.market.yes
            if OutcomeSide(request.outcome) is OutcomeSide.YES
            else context.market.no
        )
        if not selected_outcome.eligible:
            raise ValueError("fresh execution outcome is not eligible")
        if request.frozen_cutoff is not None and context.order_book.cutoff < request.frozen_cutoff:
            raise ValueError("fresh execution context predates the frozen decision cutoff")
        if context.market.observed_at > execution_cutoff:
            raise ValueError("fresh market metadata is from the future")
        if (
            context.market.source_updated_at is not None
            and context.market.source_updated_at > execution_cutoff
        ):
            raise ValueError("fresh market metadata timestamp is from the future")
        if (
            context.market.source_updated_at is not None
            and context.market.source_updated_at > context.market.observed_at
        ):
            raise ValueError("fresh market metadata timestamp postdates its observation")
        if context.order_book.observed_at > execution_cutoff:
            raise ValueError("fresh execution order book is from the future")
        if context.order_book.cutoff > execution_cutoff:
            raise ValueError("fresh execution order book cutoff is from the future")
        if (
            context.order_book.source_timestamp is not None
            and context.order_book.source_timestamp > execution_cutoff
        ):
            raise ValueError("fresh order book timestamp is from the future")
        if (
            context.order_book.source_timestamp is not None
            and context.order_book.source_timestamp > context.order_book.observed_at
        ):
            raise ValueError("fresh order book timestamp postdates its observation")
        maximum_age = min(self._maximum_book_age, timedelta(seconds=300))
        if execution_cutoff - context.order_book.observed_at > maximum_age:
            raise ValueError("fresh execution order book is stale")

    @staticmethod
    def _persistable_fresh_context(
        request: OrderRequest,
        context: MarketContext | None,
        execution_cutoff: datetime,
    ) -> MarketContext | None:
        if context is None or context.market.key != request.market_key:
            return None
        if (
            request.frozen_cutoff is not None
            and context.order_book.cutoff < request.frozen_cutoff
        ):
            return None
        if context.market.observed_at > execution_cutoff:
            return None
        if (
            context.market.source_updated_at is not None
            and (
                context.market.source_updated_at > execution_cutoff
                or context.market.source_updated_at > context.market.observed_at
            )
        ):
            return None
        if context.order_book.observed_at > execution_cutoff:
            return None
        if context.order_book.cutoff > execution_cutoff:
            return None
        if (
            context.order_book.source_timestamp is not None
            and (
                context.order_book.source_timestamp > execution_cutoff
                or context.order_book.source_timestamp > context.order_book.observed_at
            )
        ):
            return None
        return context

    def _execution_now(self, minimum: datetime) -> datetime:
        return max(_aware(self._clock()), _aware(minimum))

    def _pending_refresh_result(
        self,
        request: OrderRequest,
        portfolio: ContractPortfolio,
        message: BaseException | str,
        operation_id: str,
    ) -> Any:
        result = BinaryPaperBroker._pending(
            request,
            portfolio,
            self._execution_now(request.created_at),
            operation_id=str(operation_id),
            frozen_context_id=request.frozen_context_id,
        )
        return replace(
            result,
            reconciliation_evidence={
                "submission_state": SubmissionState.NOT_SUBMITTED.value,
                "venue_submission_occurred": False,
                "refresh_failure": True,
                "failure_type": (
                    type(message).__name__
                    if isinstance(message, BaseException)
                    else "refresh_error"
                ),
            },
        )

    def _refresh_failure_result(
        self,
        request: OrderRequest,
        portfolio: ContractPortfolio,
        error: FreshExecutionContextError,
        operation_id: str,
    ) -> Any:
        if error.retryable:
            return self._pending_refresh_result(request, portfolio, error, operation_id)
        code_value = error.error_code or _context_error_code(str(error))
        try:
            code = SemanticExecutionError(code_value)
        except ValueError:
            code = SemanticExecutionError.INVALID_CONTEXT
        result = BinaryPaperBroker._rejected(
            request,
            portfolio,
            code,
            self._execution_now(request.created_at),
            operation_id=str(operation_id),
            message=str(error),
        )
        return replace(
            result,
            frozen_context_id=request.frozen_context_id,
            submission_state=SubmissionState.NOT_SUBMITTED,
            reconciliation_evidence={
                "submission_state": SubmissionState.NOT_SUBMITTED.value,
                "venue_submission_occurred": False,
                "refresh_failure": True,
                "failure_type": type(error).__name__,
            },
        )

    def _persist_order_result(
        self,
        result: Any,
        *,
        claim: CycleClaim,
        reservation: Any,
        execution_context: MarketContext | None = None,
        fee_policy: FeePolicySnapshot | None = None,
        execution_context_refreshed_at: datetime | None = None,
    ) -> None:
        if reservation.market_id is None or reservation.outcome_id is None:
            raise RuntimeError("order intent identity is incomplete")
        self._repository.persist_order_result(
            result,
            agent_id=claim.agent_id,
            agent_cycle_id=claim.cycle_id,
            market_id=reservation.market_id,
            outcome_id=reservation.outcome_id,
            operation_id=reservation.operation_id,
            execution_context=execution_context,
            fee_policy=fee_policy,
            execution_context_refreshed_at=execution_context_refreshed_at,
        )

    def _fee_policy(self, market_ref: str, cutoff: datetime) -> FeePolicySnapshot | None:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT fps.policy_version, fps.formula_version, fps.schedule_identity, "
                "fps.participant_role, fps.multiplier_numerator, fps.multiplier_denominator, "
                "fps.event_override_micros, fps.event_override_cleared, fps.effective_at, "
                "fps.as_of_at, fps.observed_at, fps.cutoff, fps.source_tier, "
                "fps.exact_inputs, fps.waiver_evidence, fps.policy_fingerprint, "
                "ra.sha256, ra.byte_length, ra.uri, ra.source_endpoint, ra.request_identity, "
                "ra.source_timestamp, ra.observed_at, ra.captured_cutoff, ra.schema_version "
                "FROM fee_policy_snapshots fps JOIN markets m ON m.id = fps.market_id "
                "JOIN raw_artifacts ra ON ra.id = fps.raw_artifact_id "
                "WHERE m.market_ref = %s AND fps.observed_at <= %s AND fps.as_of_at <= %s "
                "AND fps.cutoff <= %s ORDER BY fps.observed_at DESC, fps.id DESC LIMIT 1",
                (market_ref, cutoff, cutoff, cutoff),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        exact_inputs = row[13] if isinstance(row[13], Mapping) else {}
        waiver_evidence = row[14] if isinstance(row[14], Mapping) else None
        raw_artifact = RawArtifact(
            str(row[16]),
            int(str(row[17])),
            str(row[18]),
            source_endpoint=None if row[19] is None else str(row[19]),
            request_identity=None if row[20] is None else str(row[20]),
            source_timestamp=(None if row[21] is None else _aware(cast(datetime, row[21]))),
            observed_at=_aware(cast(datetime, row[22])),
            historical_cutoff=(None if row[23] is None else _aware(cast(datetime, row[23]))),
            schema_version=str(row[24]),
        )
        override = row[6]
        snapshot = FeePolicySnapshot(
            contract_version=str(row[0]),
            formula_version=str(row[1]),
            schedule_version=str(row[2]),
            participant_role=FeeParticipantRole(str(row[3]).upper()),
            series_multiplier_numerator=int(str(row[4])),
            series_multiplier_denominator=int(str(row[5])),
            event_override_numerator=(None if override is None else int(str(override))),
            event_override_denominator=(None if override is None else 1_000_000),
            event_override_cleared=bool(row[7]),
            effective_at=_aware(cast(datetime, row[8])),
            as_of_at=_aware(cast(datetime, row[9])),
            observed_at=_aware(cast(datetime, row[10])),
            cutoff=_aware(cast(datetime, row[11])),
            source_tier=str(row[12]),
            raw_artifact=raw_artifact,
            waiver_evidence=cast(Mapping[str, object] | None, waiver_evidence),
            exact_inputs=cast(Mapping[str, object], exact_inputs),
        )
        if snapshot.fingerprint != str(row[15]):
            raise RuntimeError("fee policy fingerprint is inconsistent")
        return snapshot


class ProductionSemanticReconciliationPort:
    """Resolve only pre-submission ambiguous order attempts before a new cycle."""

    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], datetime],
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._clock = clock
        connector = connect or _default_connect
        self._broker_repository = PostgresBrokerRepository(
            database_url, connect=cast(Any, connector)
        )
        self._runtime_repository = PostgresRuntimeRepository(
            database_url, connect=cast(Any, connector)
        )

    def reconcile_before_cycle(self, *, now: datetime | None = None) -> None:
        observed_at = _aware(now or self._clock())
        try:
            unresolved = self._broker_repository.reconcile_not_submitted_operations(
                now=observed_at
            )
        except Exception as exc:
            self._runtime_repository.open_alert(
                AlertEvent(
                    None,
                    None,
                    "critical",
                    "order_reconciliation_failed",
                    {"failure_type": type(exc).__name__},
                    observed_at,
                    "order-reconciliation-failed",
                )
            )
            raise RuntimeError("automatic order reconciliation failed") from exc
        if not unresolved:
            return
        for agent_id in unresolved:
            self._runtime_repository.open_alert(
                AlertEvent(
                    None,
                    agent_id,
                    "critical",
                    "order_reconciliation_required",
                    {"operation_state": "RECONCILIATION_REQUIRED"},
                    observed_at,
                    f"order-reconciliation-required:{agent_id}",
                )
            )
        raise RuntimeError("unresolved order reconciliation blocks the cycle")


class ProductionSemanticBrokerPort:
    """Reconcile already-persisted semantic operations; never execute twice."""

    def __init__(self, database_url: str, *, connect: _Connect | None = None) -> None:
        self._database_url = database_url
        self._connect = connect or _default_connect

    def execute(
        self, claim: CycleClaim, _frozen: dict[str, Any], harness: dict[str, Any]
    ) -> BrokerExecutionResult:
        run_id = harness.get("harness_run_id")
        if not isinstance(run_id, str):
            raise RuntimeError("broker requires a persisted harness run")
        try:
            parsed_run_id = uuid.UUID(run_id)
        except ValueError as exc:
            raise RuntimeError("broker harness run id is malformed") from exc
        raw_operation_ids = harness.get("operation_ids", [])
        if not isinstance(raw_operation_ids, list):
            raise RuntimeError("broker operation membership is malformed")
        try:
            operation_ids = {uuid.UUID(str(value)) for value in raw_operation_ids}
        except ValueError as exc:
            raise RuntimeError("broker operation membership is malformed") from exc
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM harness_runs WHERE id = %s AND agent_cycle_id = %s",
                (parsed_run_id, claim.cycle_id),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("broker cannot use an unpersisted harness")
            cursor.execute(
                "SELECT operation.id, current_state.state, current_state.reconciliation_state "
                "FROM order_operations operation JOIN order_operation_current current_state "
                "ON current_state.operation_id = operation.id "
                "WHERE operation.agent_cycle_id = %s ORDER BY operation.created_at, operation.id",
                (claim.cycle_id,),
            )
            rows = tuple(cursor.fetchall())
        persisted_ids = {uuid.UUID(str(row[0])) for row in rows}
        if persisted_ids != operation_ids:
            raise RuntimeError(
                "harness operation membership differs from persisted semantic operations"
            )
        operations = [
            {
                "operation_id": str(row[0]),
                "state": str(row[1]),
                "reconciliation_state": str(row[2]),
            }
            for row in rows
        ]
        accepted = sum(1 for row in rows if str(row[1]) in {"FILLED", "PARTIALLY_FILLED"})
        return BrokerExecutionResult(
            {
                "operation_ids": [str(value) for value in sorted(persisted_ids)],
                "operations": operations,
                "accepted_operations": accepted,
            },
            (),
            accepted,
        )


class ProductionSemanticSettlementPort:
    """Apply only validated FINALIZED observations and value open contracts."""

    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], datetime],
        maximum_bid_age: timedelta,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._clock = clock
        self._maximum_bid_age = maximum_bid_age
        self._connect = connect or _default_connect
        self._repository = PostgresBrokerRepository(
            database_url, connect=cast(Any, self._connect)
        )

    def settle_before_prompt(
        self, claim: CycleClaim, frozen: dict[str, Any]
    ) -> PreSettlementResult:
        cutoff = _cutoff(claim)
        settled_ids = self._settle_eligible(claim, frozen, cutoff)
        return PreSettlementResult(
            {"settlement_ids": settled_ids, "settlement_cutoff": cutoff.isoformat()},
            (),
            len(settled_ids),
        )

    def settle_and_value(
        self, claim: CycleClaim, frozen: dict[str, Any], _broker: dict[str, Any]
    ) -> SettlementValuationResult:
        cutoff = _cutoff(claim)
        settled_ids = self._settle_eligible(claim, frozen, cutoff)
        portfolio = load_contract_portfolio(
            self._database_url, claim.agent_id, connect=self._connect
        )
        contexts: dict[MarketKey | str, MarketContext] = {
            (
                position.market_ref
                if isinstance(position.market_ref, MarketKey)
                else MarketKey(position.market_ref)
            ): frozen_context(
                frozen,
                (
                    position.market_ref.market_ref
                    if isinstance(position.market_ref, MarketKey)
                    else str(position.market_ref)
                ),
            )
            for position in portfolio.positions
        }
        liquidation = 0
        book_ids: list[uuid.UUID] = []
        for position in portfolio.positions:
            market_key = (
                position.market_ref
                if isinstance(position.market_ref, MarketKey)
                else MarketKey(position.market_ref)
            )
            context = contexts.get(market_key)
            if context is None:
                raise RuntimeError("valuation context is missing for an open position")
            if cutoff - context.order_book.observed_at > self._maximum_bid_age:
                raise RuntimeError("valuation order book is stale")
            level = context.order_book.best_bid(position.outcome)
            if level is None:
                raise RuntimeError("valuation bid is missing for an open position")
            liquidation += (int(position.contract_units) * int(level.price) + 50) // 100
            book_ids.append(context.order_book.snapshot_id)
        cash = int(portfolio.cash_micros)
        basis = sum(int(position.gross_cost_basis_micros) for position in portfolio.positions)
        entry_fees = sum(int(position.entry_fees_micros) for position in portfolio.positions)
        account_value = cash + liquidation
        unrealized = liquidation - basis - entry_fees
        mismatch = self._ledger_mismatch(claim.agent_id)
        calculated = _aware(self._clock())
        self._persist_performance(
            claim,
            cash=cash,
            liquidation=liquidation,
            account_value=account_value,
            realized=portfolio.realized_pnl_micros,
            unrealized=unrealized,
            entry_fees=entry_fees,
            calculated=calculated,
            settlement_ids=settled_ids,
            book_ids=book_ids,
        )
        peak = self._peak_account_value(claim.agent_id, account_value)
        return SettlementValuationResult(
            {
                "settlement_ids": settled_ids,
                "performance_snapshot_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:performance:{claim.cycle_id}")
                ),
                "valuation_cutoff": cutoff.isoformat(),
                "cash_micros": cash,
                "position_liquidation_micros": liquidation,
                "account_value_micros": account_value,
            },
            (),
            account_value,
            peak,
            mismatch,
        )

    def _settle_eligible(
        self, claim: CycleClaim, frozen: Mapping[str, object], cutoff: datetime
    ) -> list[str]:
        raw_ids = frozen.get("resolution_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids:
            return []
        try:
            resolution_ids = tuple(uuid.UUID(str(value)) for value in raw_ids)
        except ValueError as exc:
            raise RuntimeError("freeze resolution membership is malformed") from exc
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT ro.id, ro.market_id, m.market_ref, ro.lifecycle_status, ro.result, "
                "ro.observed_at, ro.source_timestamp, ro.settlement_ts, ro.blocked, "
                "p.id, p.outcome_id, p.outcome_side, p.contract_units, "
                "p.gross_cost_basis_micros, p.entry_fees_micros, p.realized_pnl_micros, "
                "p.updated_at, ra.sha256, ra.byte_length, ra.uri, ra.source_endpoint, "
                "ra.request_identity, ra.source_timestamp, ra.observed_at, "
                "ra.captured_cutoff, ra.schema_version "
                "FROM resolution_observations ro JOIN markets m ON m.id = ro.market_id "
                "JOIN positions p ON p.market_id = ro.market_id "
                "JOIN raw_artifacts ra ON ra.id = ro.raw_artifact_id "
                "LEFT JOIN settlements settled ON settled.position_id = p.id "
                "AND settled.resolution_id = ro.id "
                "WHERE p.agent_id = %s AND p.contract_units > 0 "
                "AND ro.id = ANY(%s::uuid[]) AND ro.lifecycle_status = 'finalized' "
                "AND ro.result IS NOT NULL AND ro.settlement_ts IS NOT NULL "
                "AND ro.blocked = false AND ro.observed_at <= %s "
                "AND ro.settlement_ts <= %s AND settled.id IS NULL "
                "ORDER BY ro.observed_at, ro.id, p.id",
                (claim.agent_id, list(resolution_ids), cutoff, cutoff),
            )
            rows = tuple(cursor.fetchall())
        engine = BinarySettlementEngine()
        portfolio = load_contract_portfolio(
            self._database_url, claim.agent_id, connect=self._connect
        )
        settled: list[str] = []
        for row in rows:
            market_ref = MarketKey(str(row[2]))
            outcome = OutcomeSide(str(row[11]))
            position = portfolio.position(market_ref, outcome)
            if position is None or int(position.contract_units) != int(str(row[12])):
                raise RuntimeError("settlement position changed before persistence")
            observation = ResolutionObservation(
                market_ref,
                MarketStatus.FINALIZED,
                OutcomeSide(str(row[4])),
                _aware(cast(datetime, row[5])),
                None if row[6] is None else _aware(cast(datetime, row[6])),
                _aware(cast(datetime, row[7])),
                RawArtifact(
                    str(row[17]),
                    int(str(row[18])),
                    str(row[19]),
                    source_endpoint=None if row[20] is None else str(row[20]),
                    request_identity=None if row[21] is None else str(row[21]),
                    source_timestamp=None if row[22] is None else _aware(cast(datetime, row[22])),
                    observed_at=_aware(cast(datetime, row[23])),
                    historical_cutoff=None if row[24] is None else _aware(cast(datetime, row[24])),
                    schema_version=str(row[25]),
                ),
                False,
            )
            try:
                after, record = engine.settle(
                    observation=observation,
                    position=position,
                    portfolio=portfolio,
                    as_of=cutoff,
                    settled_at=max(_aware(self._clock()), cutoff),
                )
            except SettlementBlockedError:
                raise
            persisted = self._repository.persist_settlement_record(
                record,
                agent_id=claim.agent_id,
                position_id=uuid.UUID(str(row[9])),
                resolution_id=uuid.UUID(str(row[0])),
                market_id=uuid.UUID(str(row[1])),
                outcome_id=uuid.UUID(str(row[10])),
                portfolio_before=portfolio,
                portfolio_after=after,
            )
            portfolio = after
            settled.append(str(persisted.record_id))
        return settled

    def _ledger_mismatch(self, agent_id: uuid.UUID) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "WITH ledger AS (SELECT lp.market_id, lp.outcome_side, "
                "sum(lp.amount_micros) AS basis FROM ledger_postings lp "
                "JOIN ledger_entries le ON le.id = lp.ledger_entry_id "
                "WHERE le.agent_id = %s AND lp.account = 'position_cost' "
                "GROUP BY lp.market_id, lp.outcome_side), cached AS (" 
                "SELECT market_id, outcome_side, gross_cost_basis_micros AS basis "
                "FROM positions WHERE agent_id = %s) SELECT COALESCE(sum(abs(" 
                "COALESCE(ledger.basis, 0) - COALESCE(cached.basis, 0))), 0) "
                "FROM ledger FULL JOIN cached USING (market_id, outcome_side)",
                (agent_id, agent_id),
            )
            row = cursor.fetchone()
        return int(str(row[0])) if row else 0

    def _persist_performance(
        self,
        claim: CycleClaim,
        *,
        cash: int,
        liquidation: int,
        account_value: int,
        realized: int,
        unrealized: int,
        entry_fees: int,
        calculated: datetime,
        settlement_ids: Sequence[str],
        book_ids: Sequence[uuid.UUID],
    ) -> None:
        snapshot_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:performance:{claim.cycle_id}")
        calculation = {
            "valuation_policy": (
                "latest_frozen_executable_bid_max_age_"
                f"{int(self._maximum_bid_age.total_seconds())}_seconds"
            ),
            "valuation_max_age_seconds": int(self._maximum_bid_age.total_seconds()),
            "valuation_cutoff": _cutoff(claim).isoformat(),
            "entry_fees_micros": entry_fees,
            "settlement_ids": list(settlement_ids),
            "eligible_order_book_snapshot_ids": [str(value) for value in book_ids],
        }
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO performance_snapshots "
                "(id, agent_cycle_id, cash_micros, position_liquidation_micros, "
                "account_value_micros, realized_pnl_micros, unrealized_pnl_micros, "
                "calculated_at, calculation) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (agent_cycle_id) DO NOTHING",
                (
                    snapshot_id,
                    claim.cycle_id,
                    cash,
                    liquidation,
                    account_value,
                    realized,
                    unrealized,
                    calculated,
                    json.dumps(calculation, sort_keys=True),
                ),
            )

    def _peak_account_value(self, agent_id: uuid.UUID, current: int) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(max(ps.account_value_micros), %s) "
                "FROM performance_snapshots ps JOIN agent_cycles ac "
                "ON ac.id = ps.agent_cycle_id WHERE ac.agent_id = %s",
                (current, agent_id),
            )
            row = cursor.fetchone()
        return max(current, int(str(row[0])) if row else current)


def _context_error_code(message: str) -> SemanticExecutionError:
    lowered = message.casefold()
    if "stale" in lowered:
        return SemanticExecutionError.STALE_BOOK
    if "tradeable" in lowered or "active" in lowered:
        return SemanticExecutionError.MARKET_NOT_TRADEABLE
    return SemanticExecutionError.INVALID_CONTEXT


__all__ = [
    "ProductionSemanticBrokerPort",
    "ProductionSemanticOrderExecutor",
    "ProductionSemanticReconciliationPort",
    "ProductionSemanticSettlementPort",
    "frozen_context",
    "load_contract_portfolio",
]
