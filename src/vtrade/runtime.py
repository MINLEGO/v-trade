from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import Any, Protocol

from vtrade.deadline import check_deadline, run_with_deadline

JsonObject = dict[str, Any]
_LOGGER = logging.getLogger(__name__)

_REMOVED_STAGE_KEYS = frozenset(
    {
        "market_id",
        "outcome_id",
        "token_id",
        "venue_token_id",
        "condition_id",
        "shares",
        "order_id",
        "order_ids",
        "order_intent",
        "order_intents",
        "intent_id",
        "intent_ids",
    }
)


class RuntimeConfigurationError(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class CycleStage(StrEnum):
    MARKET_FREEZE = "market_freeze"
    PRE_SETTLEMENT = "pre_settlement"
    PROMPT = "prompt"
    HARNESS = "harness"
    BROKER = "broker"
    SETTLEMENT_VALUATION = "settlement_valuation"


STAGE_ORDER = (
    CycleStage.MARKET_FREEZE,
    CycleStage.PRE_SETTLEMENT,
    CycleStage.PROMPT,
    CycleStage.HARNESS,
    CycleStage.BROKER,
    CycleStage.SETTLEMENT_VALUATION,
)

MINIMUM_CYCLE_LEASE_DURATION = timedelta(seconds=3_300)
DEFAULT_CYCLE_LEASE_DURATION = timedelta(minutes=70)
HOURLY_SCHEDULER_LOCK_NAME = "vtrade:hourly-scheduler"


@dataclass(frozen=True, slots=True)
class CycleClaim:
    cycle_id: uuid.UUID
    agent_id: uuid.UUID
    scheduled_at: datetime
    data_cutoff: datetime | None
    lease_owner: str
    lease_expires_at: datetime
    recovery: bool = False
    attempt: int = 1

    def __post_init__(self) -> None:
        for value in (self.scheduled_at, self.lease_expires_at):
            _aware(value)
        if self.data_cutoff is not None:
            _aware(self.data_cutoff)
            if self.data_cutoff < self.scheduled_at:
                raise ValueError("data cutoff cannot precede the scheduled cycle instant")
        if not self.lease_owner:
            raise ValueError("lease owner is required")
        if self.attempt < 1:
            raise ValueError("cycle attempt must be positive")


@dataclass(frozen=True, slots=True)
class ArtifactRegistration:
    uri: str
    sha256: str
    byte_length: int
    retain_until: datetime

    def __post_init__(self) -> None:
        if not self.uri or len(self.sha256) != 64 or self.byte_length < 0:
            raise ValueError("artifact registration is incomplete")
        _aware(self.retain_until)


@dataclass(frozen=True, slots=True)
class StageResult:
    payload: JsonObject
    artifacts: tuple[ArtifactRegistration, ...] = ()

    def __post_init__(self) -> None:
        _reject_removed_stage_keys(self.payload)
        json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class MarketFreezeResult(StageResult):
    freshest_observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        StageResult.__post_init__(self)
        _aware(self.freshest_observed_at)


@dataclass(frozen=True, slots=True)
class PreSettlementResult(StageResult):
    settled_positions: int = 0

    def __post_init__(self) -> None:
        StageResult.__post_init__(self)
        if self.settled_positions < 0:
            raise ValueError("settled position count cannot be negative")


@dataclass(frozen=True, slots=True)
class PromptResult(StageResult):
    rendered_characters: int = 0

    def __post_init__(self) -> None:
        StageResult.__post_init__(self)
        if self.rendered_characters <= 0:
            raise ValueError("rendered prompt size must be positive")


@dataclass(frozen=True, slots=True)
class HarnessExecutionResult(StageResult):
    exa_searches: int = 0
    tool_calls: int = 0

    def __post_init__(self) -> None:
        StageResult.__post_init__(self)
        if self.exa_searches < 0 or self.tool_calls < 0:
            raise ValueError("harness counters cannot be negative")


@dataclass(frozen=True, slots=True)
class BrokerExecutionResult(StageResult):
    accepted_operations: int = 0

    def __post_init__(self) -> None:
        StageResult.__post_init__(self)
        if self.accepted_operations < 0:
            raise ValueError("accepted operation count cannot be negative")


@dataclass(frozen=True, slots=True)
class SettlementValuationResult(StageResult):
    account_value_micros: int = 0
    peak_account_value_micros: int = 0
    ledger_mismatch_micros: int = 0

    def __post_init__(self) -> None:
        StageResult.__post_init__(self)
        if self.account_value_micros < 0 or self.peak_account_value_micros < 0:
            raise ValueError("account values cannot be negative")


class MarketFreezePort(Protocol):
    def freeze(
        self, claim: CycleClaim, *, deadline: float | None = None
    ) -> MarketFreezeResult: ...


class PreSettlementPort(Protocol):
    def settle_before_prompt(
        self, claim: CycleClaim, frozen: JsonObject
    ) -> PreSettlementResult: ...


class PromptPort(Protocol):
    def render(self, claim: CycleClaim, frozen: JsonObject) -> PromptResult: ...


class HarnessPort(Protocol):
    def run(
        self, claim: CycleClaim, frozen: JsonObject, prompt: JsonObject
    ) -> HarnessExecutionResult: ...


class BrokerPort(Protocol):
    def execute(
        self, claim: CycleClaim, frozen: JsonObject, harness: JsonObject
    ) -> BrokerExecutionResult: ...


class SettlementValuationPort(Protocol):
    def settle_and_value(
        self, claim: CycleClaim, frozen: JsonObject, broker: JsonObject
    ) -> SettlementValuationResult: ...


class RuntimeRepository(Protocol):
    def claim_due_cycles(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        missed_grace: timedelta,
        limit: int,
    ) -> tuple[CycleClaim, ...]: ...

    def recover_expired_cycles(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int,
        stale_after: timedelta | None = None,
    ) -> tuple[CycleClaim, ...]: ...

    def renew_lease(self, claim: CycleClaim, *, now: datetime, duration: timedelta) -> None: ...

    def load_stage(self, cycle_id: uuid.UUID, stage: CycleStage) -> StageResult | None: ...

    def begin_stage(
        self, claim: CycleClaim, stage: CycleStage, input_fingerprint: str, *, now: datetime
    ) -> None: ...

    def complete_stage(
        self,
        claim: CycleClaim,
        stage: CycleStage,
        input_fingerprint: str,
        result: StageResult,
        *,
        now: datetime,
        deadline: float | None = None,
    ) -> None: ...

    def complete_cycle(self, claim: CycleClaim, *, now: datetime, summary: JsonObject) -> None: ...

    def fail_cycle(self, claim: CycleClaim, *, now: datetime, reason: str) -> int: ...

    def open_alert(self, alert: AlertEvent) -> None: ...

    def record_projection(self, projection: RuntimeProjection) -> None: ...

    def projection_inputs(self, *, since: datetime, until: datetime) -> ProjectionInputs: ...

    def claim_expired_artifacts(
        self, *, now: datetime, lease_owner: str, limit: int
    ) -> tuple[ExpiredArtifact, ...]: ...

    def purge_expired_payloads(self, *, now: datetime) -> int: ...

    def complete_artifact_deletion(
        self, artifact: ExpiredArtifact, *, now: datetime, error: str | None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AlertEvent:
    run_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    severity: str
    code: str
    details: JsonObject
    opened_at: datetime
    dedupe_key: str

    def __post_init__(self) -> None:
        if self.severity not in {"warning", "critical"} or not self.code or not self.dedupe_key:
            raise ValueError("alert metadata is invalid")
        _aware(self.opened_at)


@dataclass(frozen=True, slots=True)
class ProjectionInputs:
    artifact_bytes: int
    billed_cost_micros: int
    nominal_cost_micros: int
    completed_cycles: int

    def __post_init__(self) -> None:
        if (
            min(
                self.artifact_bytes,
                self.billed_cost_micros,
                self.nominal_cost_micros,
                self.completed_cycles,
            )
            < 0
        ):
            raise ValueError("projection inputs cannot be negative")


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    window_started_at: datetime
    window_ended_at: datetime
    projected_monthly_artifact_bytes: int
    projected_monthly_billed_cost_micros: int
    projected_monthly_nominal_cost_micros: int
    observed_cycles: int
    calculated_at: datetime


@dataclass(frozen=True, slots=True)
class ExpiredArtifact:
    inventory_id: uuid.UUID
    uri: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeTickResult:
    processed_cycle_ids: tuple[uuid.UUID, ...]
    failures: tuple[tuple[uuid.UUID, str], ...]


class ArtifactDeletionPort(Protocol):
    def delete(self, uri: str, sha256: str) -> None: ...


class AlertPauseScope(StrEnum):
    NONE = "none"
    AGENT = "agent"
    SYSTEM = "system"


CRITICAL_AGENT_ALERT_CODES = frozenset(
    {"stale_market_data", "ledger_mismatch", "consecutive_cycle_failures"}
)
CRITICAL_GLOBAL_ALERT_CODES = frozenset({"projected_budget_exceeded"})


@dataclass(frozen=True, slots=True)
class RuntimeAlertPolicy:
    maximum_data_age: timedelta = timedelta(minutes=5)
    failure_threshold: int = 3
    maximum_drawdown_fraction: Decimal = Decimal("0.25")
    monthly_budget_micros: int = 40_000_000
    storage_alert_bytes: int = 2_000_000_000

    @classmethod
    def pause_scope_for(cls, alert: AlertEvent) -> AlertPauseScope:
        """Return the one scheduler scope affected by an alert.

        Critical alerts with no agent are system-wide by construction, which keeps
        future system-wide alert codes fail-safe. Unknown critical alerts that carry
        an agent remain scoped to that agent; warnings never pause scheduling.
        """
        if alert.severity != "critical":
            return AlertPauseScope.NONE
        if alert.code in CRITICAL_GLOBAL_ALERT_CODES or alert.agent_id is None:
            return AlertPauseScope.SYSTEM
        if alert.code in CRITICAL_AGENT_ALERT_CODES or alert.agent_id is not None:
            return AlertPauseScope.AGENT
        return AlertPauseScope.NONE

    def pause_scope(self, alert: AlertEvent) -> AlertPauseScope:
        return self.pause_scope_for(alert)

    def cycle_alerts(
        self,
        claim: CycleClaim,
        *,
        now: datetime,
        freshest_observed_at: datetime,
        settlement: SettlementValuationResult,
    ) -> tuple[AlertEvent, ...]:
        alerts: list[AlertEvent] = []
        age = now - freshest_observed_at
        if age > self.maximum_data_age:
            alerts.append(
                _alert(
                    claim,
                    now,
                    "critical",
                    "stale_market_data",
                    {"age_seconds": age.total_seconds()},
                )
            )
        if settlement.ledger_mismatch_micros:
            alerts.append(
                _alert(
                    claim,
                    now,
                    "critical",
                    "ledger_mismatch",
                    {"mismatch_micros": settlement.ledger_mismatch_micros},
                )
            )
        peak = settlement.peak_account_value_micros
        if peak > 0:
            drawdown = Decimal(peak - settlement.account_value_micros) / Decimal(peak)
            if drawdown >= self.maximum_drawdown_fraction:
                alerts.append(
                    _alert(
                        claim,
                        now,
                        "warning",
                        "abnormal_drawdown",
                        {"fraction": str(drawdown)},
                    )
                )
        return tuple(alerts)

    def failure_alert(
        self, claim: CycleClaim, now: datetime, consecutive: int
    ) -> AlertEvent | None:
        if consecutive < self.failure_threshold:
            return None
        return _alert(
            claim,
            now,
            "critical",
            "consecutive_cycle_failures",
            {"count": consecutive},
        )

    def projection_alerts(self, projection: RuntimeProjection) -> tuple[AlertEvent, ...]:
        events: list[AlertEvent] = []
        if projection.projected_monthly_billed_cost_micros > self.monthly_budget_micros:
            events.append(
                AlertEvent(
                    None,
                    None,
                    "critical",
                    "projected_budget_exceeded",
                    {"projected_micros": projection.projected_monthly_billed_cost_micros},
                    projection.calculated_at,
                    f"projected-budget:{projection.calculated_at:%Y-%m}",
                )
            )
        if projection.projected_monthly_artifact_bytes > self.storage_alert_bytes:
            events.append(
                AlertEvent(
                    None,
                    None,
                    "warning",
                    "projected_storage_exceeded",
                    {"projected_bytes": projection.projected_monthly_artifact_bytes},
                    projection.calculated_at,
                    f"projected-storage:{projection.calculated_at:%Y-%m}",
                )
            )
        return tuple(events)


class CycleOrchestrator:
    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        market_freezer: MarketFreezePort,
        pre_settlement: PreSettlementPort,
        prompt: PromptPort,
        harness: HarnessPort,
        broker: BrokerPort,
        settlement_valuation: SettlementValuationPort,
        clock: Callable[[], datetime],
        alert_policy: RuntimeAlertPolicy | None = None,
        lease_duration: timedelta = DEFAULT_CYCLE_LEASE_DURATION,
        market_freeze_deadline_seconds: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if lease_duration < MINIMUM_CYCLE_LEASE_DURATION:
            raise ValueError("cycle lease must cover the configured 3300-second wall clock")
        if market_freeze_deadline_seconds <= 0:
            raise ValueError("market_freeze_deadline_seconds must be positive")
        self._repository = repository
        self._market_freezer = market_freezer
        self._pre_settlement = pre_settlement
        self._prompt = prompt
        self._harness = harness
        self._broker = broker
        self._settlement = settlement_valuation
        self._clock = clock
        self._alert_policy = alert_policy or RuntimeAlertPolicy()
        self._lease_duration = lease_duration
        self._market_freeze_deadline_seconds = market_freeze_deadline_seconds
        self._monotonic = monotonic

    @property
    def market_freeze_deadline_seconds(self) -> float:
        return self._market_freeze_deadline_seconds

    def run(self, claim: CycleClaim) -> JsonObject:
        active_claim = claim
        outputs: dict[CycleStage, JsonObject] = {}
        typed: dict[CycleStage, StageResult] = {}
        active_stage: CycleStage | None = None
        try:
            for stage in STAGE_ORDER:
                active_stage = stage
                stored = self._repository.load_stage(active_claim.cycle_id, stage)
                if stored is not None:
                    outputs[stage] = stored.payload
                    typed[stage] = stored
                    continue
                now = _aware(self._clock())
                self._repository.renew_lease(active_claim, now=now, duration=self._lease_duration)
                fingerprint = _fingerprint_inputs(active_claim, stage, outputs)
                stage_started = self._monotonic() if stage is CycleStage.MARKET_FREEZE else None
                deadline = (
                    stage_started + self._market_freeze_deadline_seconds
                    if stage_started is not None
                    else None
                )
                if deadline is not None:
                    _LOGGER.info(
                        "market_freeze stage_boundary event=start cycle_id=%s attempt=%s "
                        "lease_generation=%s deadline_remaining_ms=%.3f",
                        active_claim.cycle_id,
                        active_claim.attempt,
                        _lease_generation(active_claim.lease_owner),
                        max(0.0, deadline - self._monotonic()) * 1000,
                    )
                    run_with_deadline(
                        partial(
                            self._repository.begin_stage,
                            active_claim,
                            stage,
                            fingerprint,
                            now=now,
                        ),
                        deadline=deadline,
                        label="market_freeze stage begin",
                        clock=self._monotonic,
                    )
                    result = run_with_deadline(
                        partial(
                            self._invoke,
                            stage,
                            active_claim,
                            outputs,
                            deadline=deadline,
                        ),
                        deadline=deadline,
                        label="market_freeze stage",
                        clock=self._monotonic,
                    )
                else:
                    self._repository.begin_stage(active_claim, stage, fingerprint, now=now)
                    result = self._invoke(stage, active_claim, outputs)
                completed = _aware(self._clock())
                if deadline is not None:
                    check_deadline(deadline, "market_freeze checkpoint", clock=self._monotonic)
                    run_with_deadline(
                        partial(
                            self._repository.complete_stage,
                            active_claim,
                            stage,
                            fingerprint,
                            result,
                            now=completed,
                            deadline=deadline,
                        ),
                        deadline=deadline,
                        label="market_freeze checkpoint",
                        clock=self._monotonic,
                    )
                else:
                    self._repository.complete_stage(
                        active_claim, stage, fingerprint, result, now=completed
                    )
                if deadline is not None:
                    _LOGGER.info(
                        "market_freeze stage_boundary event=complete cycle_id=%s attempt=%s "
                        "lease_generation=%s elapsed_ms=%.3f deadline_remaining_ms=%.3f",
                        active_claim.cycle_id,
                        active_claim.attempt,
                        _lease_generation(active_claim.lease_owner),
                        (self._monotonic() - stage_started) * 1000
                        if stage_started is not None
                        else 0.0,
                        max(0.0, deadline - self._monotonic()) * 1000,
                    )
                if stage is CycleStage.MARKET_FREEZE:
                    active_claim = replace(
                        active_claim,
                        data_cutoff=_market_freeze_cutoff(result, fallback=completed),
                    )
                outputs[stage] = result.payload
                typed[stage] = result
            settlement = typed.get(CycleStage.SETTLEMENT_VALUATION)
            frozen = typed.get(CycleStage.MARKET_FREEZE)
            if isinstance(settlement, SettlementValuationResult) and isinstance(
                frozen, MarketFreezeResult
            ):
                for alert in self._alert_policy.cycle_alerts(
                    active_claim,
                    now=_aware(self._clock()),
                    freshest_observed_at=frozen.freshest_observed_at,
                    settlement=settlement,
                ):
                    self._repository.open_alert(alert)
            summary = {stage.value: outputs[stage] for stage in STAGE_ORDER}
            self._repository.complete_cycle(
                active_claim, now=_aware(self._clock()), summary=summary
            )
            return summary
        except Exception as exc:
            now = _aware(self._clock())
            safe_reason = redact_runtime_error(exc)
            consecutive = self._repository.fail_cycle(active_claim, now=now, reason=safe_reason)
            if active_stage is CycleStage.MARKET_FREEZE:
                _LOGGER.info(
                    "market_freeze stage_boundary event=failure cycle_id=%s attempt=%s "
                    "lease_generation=%s reason=%s",
                    active_claim.cycle_id,
                    active_claim.attempt,
                    _lease_generation(active_claim.lease_owner),
                    safe_reason,
                )
            failure_alert = self._alert_policy.failure_alert(active_claim, now, consecutive)
            if failure_alert is not None:
                self._repository.open_alert(failure_alert)
            raise

    def _invoke(
        self,
        stage: CycleStage,
        claim: CycleClaim,
        outputs: Mapping[CycleStage, JsonObject],
        *,
        deadline: float | None = None,
    ) -> StageResult:
        if stage is CycleStage.MARKET_FREEZE:
            return self._market_freezer.freeze(claim, deadline=deadline)
        frozen = _required(outputs, CycleStage.MARKET_FREEZE)
        if stage is CycleStage.PRE_SETTLEMENT:
            return self._pre_settlement.settle_before_prompt(claim, frozen)
        if stage is CycleStage.PROMPT:
            return self._prompt.render(claim, frozen)
        prompt = _required(outputs, CycleStage.PROMPT)
        if stage is CycleStage.HARNESS:
            return self._harness.run(claim, frozen, prompt)
        harness = _required(outputs, CycleStage.HARNESS)
        if stage is CycleStage.BROKER:
            return self._broker.execute(claim, frozen, harness)
        broker = _required(outputs, CycleStage.BROKER)
        return self._settlement.settle_and_value(claim, frozen, broker)


class HourlyRuntime:
    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        orchestrator: CycleOrchestrator,
        lease_owner: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = DEFAULT_CYCLE_LEASE_DURATION,
        missed_grace: timedelta = timedelta(minutes=10),
        batch_size: int = 10,
    ) -> None:
        if not lease_owner or batch_size <= 0:
            raise RuntimeConfigurationError("lease owner and positive batch size are required")
        if lease_duration < MINIMUM_CYCLE_LEASE_DURATION:
            raise RuntimeConfigurationError(
                "scheduler lease must cover the configured 3300-second wall clock"
            )
        self._repository = repository
        self._orchestrator = orchestrator
        self._lease_owner = lease_owner
        self._clock = clock
        self._lease_duration = lease_duration
        self._missed_grace = missed_grace
        self._batch_size = batch_size
        self._market_freeze_recovery_after = timedelta(
            seconds=orchestrator.market_freeze_deadline_seconds
        )

    def tick(self) -> RuntimeTickResult:
        now = _aware(self._clock())
        recovered = self._repository.recover_expired_cycles(
            now=now,
            lease_owner=self._lease_owner,
            lease_duration=self._lease_duration,
            limit=self._batch_size,
            stale_after=self._market_freeze_recovery_after,
        )
        remaining = max(0, self._batch_size - len(recovered))
        due = (
            self._repository.claim_due_cycles(
                now=now,
                lease_owner=self._lease_owner,
                lease_duration=self._lease_duration,
                missed_grace=self._missed_grace,
                limit=remaining,
            )
            if remaining
            else ()
        )
        claims = recovered + due
        processed: list[uuid.UUID] = []
        failures: list[tuple[uuid.UUID, str]] = []
        for claim in claims:
            try:
                self._orchestrator.run(claim)
                processed.append(claim.cycle_id)
            except Exception as exc:
                failures.append((claim.cycle_id, redact_runtime_error(exc)))
        return RuntimeTickResult(tuple(processed), tuple(failures))


class RetentionCleaner:
    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        deletion: ArtifactDeletionPort,
        lease_owner: str,
        clock: Callable[[], datetime],
        batch_size: int = 100,
    ) -> None:
        if not lease_owner or batch_size <= 0:
            raise RuntimeConfigurationError("retention worker requires an owner and batch size")
        self._repository = repository
        self._deletion = deletion
        self._lease_owner = lease_owner
        self._clock = clock
        self._batch_size = batch_size

    def run_once(self) -> tuple[uuid.UUID, ...]:
        now = _aware(self._clock())
        self._repository.purge_expired_payloads(now=now)
        expired = self._repository.claim_expired_artifacts(
            now=now, lease_owner=self._lease_owner, limit=self._batch_size
        )
        for artifact in expired:
            error: str | None = None
            try:
                self._deletion.delete(artifact.uri, artifact.sha256)
            except Exception as exc:
                error = redact_runtime_error(exc)
            self._repository.complete_artifact_deletion(
                artifact, now=_aware(self._clock()), error=error
            )
        return tuple(item.inventory_id for item in expired)


class ProjectionService:
    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        clock: Callable[[], datetime],
        alert_policy: RuntimeAlertPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._alerts = alert_policy or RuntimeAlertPolicy()

    def calculate(self, *, window: timedelta = timedelta(days=7)) -> RuntimeProjection:
        if window <= timedelta(0):
            raise ValueError("projection window must be positive")
        now = _aware(self._clock())
        since = now - window
        inputs = self._repository.projection_inputs(since=since, until=now)
        factor = Decimal(30 * 24 * 3600) / Decimal(window.total_seconds())
        projection = RuntimeProjection(
            since,
            now,
            int(Decimal(inputs.artifact_bytes) * factor),
            int(Decimal(inputs.billed_cost_micros) * factor),
            int(Decimal(inputs.nominal_cost_micros) * factor),
            inputs.completed_cycles,
            now,
        )
        self._repository.record_projection(projection)
        for alert in self._alerts.projection_alerts(projection):
            self._repository.open_alert(alert)
        return projection


def six_month_retain_until(created_at: datetime) -> datetime:
    created_at = _aware(created_at)
    year = created_at.year + (created_at.month + 5) // 12
    month = (created_at.month + 5) % 12 + 1
    day = min(created_at.day, _days_in_month(year, month))
    return created_at.replace(year=year, month=month, day=day)


def redact_runtime_error(error: BaseException) -> str:
    value = f"{type(error).__name__}: {error}"
    value = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(
        r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        value,
    )
    return value[:4000]


def stage_checkpoint(result: StageResult) -> JsonObject:
    metadata: JsonObject = {}
    kind = "stage"
    if isinstance(result, MarketFreezeResult):
        kind = CycleStage.MARKET_FREEZE.value
        metadata["freshest_observed_at"] = result.freshest_observed_at.isoformat()
    elif isinstance(result, PreSettlementResult):
        kind = CycleStage.PRE_SETTLEMENT.value
        metadata["settled_positions"] = result.settled_positions
    elif isinstance(result, PromptResult):
        kind = CycleStage.PROMPT.value
        metadata["rendered_characters"] = result.rendered_characters
    elif isinstance(result, HarnessExecutionResult):
        kind = CycleStage.HARNESS.value
        metadata.update(exa_searches=result.exa_searches, tool_calls=result.tool_calls)
    elif isinstance(result, BrokerExecutionResult):
        kind = CycleStage.BROKER.value
        metadata["accepted_operations"] = result.accepted_operations
    elif isinstance(result, SettlementValuationResult):
        kind = CycleStage.SETTLEMENT_VALUATION.value
        metadata.update(
            account_value_micros=result.account_value_micros,
            peak_account_value_micros=result.peak_account_value_micros,
            ledger_mismatch_micros=result.ledger_mismatch_micros,
        )
    return {"kind": kind, "payload": result.payload, "runtime": metadata}


def restore_stage(stage: CycleStage, checkpoint: Mapping[str, object]) -> StageResult:
    if checkpoint.get("kind") != stage.value:
        raise ValueError("runtime checkpoint kind does not match its stage")
    payload = checkpoint.get("payload")
    metadata = checkpoint.get("runtime")
    if not isinstance(payload, dict) or not isinstance(metadata, dict):
        raise ValueError("runtime checkpoint is malformed")
    artifacts: tuple[ArtifactRegistration, ...] = ()
    if stage is CycleStage.MARKET_FREEZE:
        observed = metadata.get("freshest_observed_at")
        if not isinstance(observed, str):
            raise ValueError("market checkpoint lacks freshness")
        return MarketFreezeResult(payload, artifacts, datetime.fromisoformat(observed))
    if stage is CycleStage.PRE_SETTLEMENT:
        return PreSettlementResult(payload, artifacts, int(metadata["settled_positions"]))
    if stage is CycleStage.PROMPT:
        return PromptResult(payload, artifacts, int(metadata["rendered_characters"]))
    if stage is CycleStage.HARNESS:
        return HarnessExecutionResult(
            payload, artifacts, int(metadata["exa_searches"]), int(metadata["tool_calls"])
        )
    if stage is CycleStage.BROKER:
        return BrokerExecutionResult(payload, artifacts, int(metadata["accepted_operations"]))
    return SettlementValuationResult(
        payload,
        artifacts,
        int(metadata["account_value_micros"]),
        int(metadata["peak_account_value_micros"]),
        int(metadata["ledger_mismatch_micros"]),
    )


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        following = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        following = datetime(year, month + 1, 1, tzinfo=UTC)
    return (following - timedelta(days=1)).day


def _alert(
    claim: CycleClaim, now: datetime, severity: str, code: str, details: JsonObject
) -> AlertEvent:
    return AlertEvent(
        None,
        claim.agent_id,
        severity,
        code,
        details,
        now,
        f"{code}:{claim.agent_id}",
    )


def _required(outputs: Mapping[CycleStage, JsonObject], stage: CycleStage) -> JsonObject:
    value = outputs.get(stage)
    if value is None:
        raise RuntimeError(f"required checkpoint is absent: {stage.value}")
    return value


def _fingerprint_inputs(
    claim: CycleClaim, stage: CycleStage, outputs: Mapping[CycleStage, JsonObject]
) -> str:
    raw = json.dumps(
        {
            "cycle_id": str(claim.cycle_id),
            "stage": stage.value,
            "prior": {key.value: value for key, value in outputs.items()},
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _reject_removed_stage_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in _REMOVED_STAGE_KEYS:
                raise ValueError(f"stage payload uses removed legacy key: {key}")
            _reject_removed_stage_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_removed_stage_keys(child)


def _market_freeze_cutoff(result: StageResult, *, fallback: datetime) -> datetime:
    """Use the freeze's immutable evidence cutoff when the port publishes it."""

    if not isinstance(result, MarketFreezeResult):
        return _aware(fallback)
    raw_cutoff = result.payload.get("data_cutoff")
    if raw_cutoff is None:
        return _aware(fallback)
    if not isinstance(raw_cutoff, str):
        raise ValueError("market freeze data_cutoff must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(raw_cutoff)
    except ValueError as exc:
        raise ValueError("market freeze data_cutoff is malformed") from exc
    return _aware(parsed)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _lease_generation(lease_owner: str) -> str:
    """Expose a stable, non-sensitive generation label in runtime telemetry."""

    return hashlib.sha256(lease_owner.encode("utf-8")).hexdigest()[:16]
