"""Cutoff-bound Kalshi fee-policy resolution.

The public Kalshi API exposes fee inputs in several independent resources.  This
module keeps the resolution rules pure and deliberately refuses to manufacture a
zero-fee policy when one of those inputs is missing or contradictory.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from vtrade.domain.execution import (
    FEE_FORMULA_VERSION,
    FEE_SETTLEMENT_CONTRACT_VERSION,
    FeeParticipantRole,
    FeePolicySnapshot,
)
from vtrade.domain.types import MoneyMicros, RawArtifact

FEE_SCHEDULE_SCHEMA_VERSION = "vtrade-kalshi-fee-schedule-v1"
DEFAULT_FEE_SCHEDULE_PATH = Path("spec/fee-schedules/kalshi-predictions-v1.json")
OFFICIAL_FEE_SCHEDULE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"


class FeePolicyStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class FeePolicyReason(StrEnum):
    UNSUPPORTED_FEE_TYPE = "UNSUPPORTED_FEE_TYPE"
    UNSUPPORTED_PARTICIPANT_ROLE = "UNSUPPORTED_PARTICIPANT_ROLE"
    FEE_POLICY_INVALID = "FEE_POLICY_INVALID"
    FEE_POLICY_UNAVAILABLE = "FEE_POLICY_UNAVAILABLE"
    FEE_SCHEDULE_INVALID = "FEE_SCHEDULE_INVALID"
    FEE_SCHEDULE_HASH_DRIFT = "FEE_SCHEDULE_HASH_DRIFT"
    FEE_SOURCE_INCOMPLETE = "FEE_SOURCE_INCOMPLETE"
    FEE_SOURCE_CONFLICT = "FEE_SOURCE_CONFLICT"


class FeeEvidenceRole(StrEnum):
    OFFICIAL_SCHEDULE = "official_schedule"
    SERIES_METADATA = "series_metadata"
    SERIES_FEE_CHANGE = "series_fee_change"
    EVENT_FEE_CHANGE = "event_fee_change"
    MARKET_WAIVER = "market_waiver"


class FeePolicyError(RuntimeError):
    """A global fee-policy source or schedule cannot be trusted."""


class FeeScheduleDriftError(FeePolicyError):
    """Legacy error retained for callers that used PDF verification."""


class FeePolicySourceConflictError(FeePolicyError):
    """Two applicable source records disagree at the same scheduled timestamp."""


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _exact_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{field_name} must be an exact decimal string or integer")
    try:
        parsed = Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def exact_rational(
    value: object,
    field_name: str,
    *,
    allow_zero: bool = True,
) -> tuple[int, int]:
    """Convert an API decimal to an exact, reduced integer ratio."""

    parsed = _exact_decimal(value, field_name)
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{field_name} must be non-negative")
    numerator, denominator = parsed.as_integer_ratio()
    if denominator <= 0 or (numerator == 0 and not allow_zero):
        raise ValueError(f"{field_name} has an invalid rational value")
    return int(numerator), int(denominator)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    if isinstance(value, datetime):
        return _aware(value, "timestamp").isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    return value


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FeeEvidence:
    role: FeeEvidenceRole | str
    artifact: RawArtifact

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", FeeEvidenceRole(self.role))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """The verified local representation of the canonical JSON fee schedule."""

    schedule_version: str
    official_url: str | None
    effective_at: datetime
    pdf_sha256: str | None
    formula_version: str = FEE_FORMULA_VERSION
    fee_type: str = "quadratic"
    taker_rate_numerator: int = 7
    taker_rate_denominator: int = 100
    maker_rate_numerator: int = 175
    maker_rate_denominator: int = 10_000
    default_taker_multiplier_numerator: int = 1
    default_taker_multiplier_denominator: int = 1
    default_maker_multiplier_numerator: int = 0
    default_maker_multiplier_denominator: int = 1
    settlement_fee_micros: int = 0
    rounding_rule: str = "fee_plus_position_cost_to_nearest_centicent"
    captured_at: datetime | None = None
    artifact: RawArtifact | None = None
    provenance: str = "official_public_schedule"

    def __post_init__(self) -> None:
        _required_text(self.schedule_version, "schedule_version")
        if self.official_url is not None:
            _required_text(self.official_url, "official_url")
        object.__setattr__(self, "effective_at", _aware(self.effective_at, "effective_at"))
        if self.captured_at is not None:
            object.__setattr__(self, "captured_at", _aware(self.captured_at, "captured_at"))
        if self.pdf_sha256 is not None:
            if len(self.pdf_sha256) != 64 or self.pdf_sha256.lower() != self.pdf_sha256:
                raise ValueError("pdf_sha256 must be a lowercase SHA-256")
            if any(character not in "0123456789abcdef" for character in self.pdf_sha256):
                raise ValueError("pdf_sha256 must be a lowercase SHA-256")
        if self.formula_version != FEE_FORMULA_VERSION:
            raise ValueError("unsupported fee schedule formula")
        if self.fee_type != "quadratic":
            raise ValueError("unsupported fee schedule fee type")
        if self.settlement_fee_micros < 0:
            raise ValueError("settlement fee cannot be negative")
        for name, value in (
            ("taker_rate_numerator", self.taker_rate_numerator),
            ("taker_rate_denominator", self.taker_rate_denominator),
            ("maker_rate_numerator", self.maker_rate_numerator),
            ("maker_rate_denominator", self.maker_rate_denominator),
            ("default_taker_multiplier_numerator", self.default_taker_multiplier_numerator),
            ("default_taker_multiplier_denominator", self.default_taker_multiplier_denominator),
            ("default_maker_multiplier_numerator", self.default_maker_multiplier_numerator),
            ("default_maker_multiplier_denominator", self.default_maker_multiplier_denominator),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "taker_rate_denominator",
            "maker_rate_denominator",
            "default_taker_multiplier_denominator",
            "default_maker_multiplier_denominator",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.taker_rate_numerator <= 0 or self.maker_rate_numerator <= 0:
            raise ValueError("fee rates must be positive")

    @property
    def verified(self) -> bool:
        return self.artifact is not None

    def verify_pdf(self, content: bytes) -> None:
        """Verify a legacy PDF provenance record; new runtime never calls this."""

        if self.pdf_sha256 is None:
            raise FeeScheduleDriftError("official fee schedule has no captured PDF hash")
        actual = hashlib.sha256(content).hexdigest()
        if actual != self.pdf_sha256:
            raise FeeScheduleDriftError(
                "official Kalshi fee schedule hash differs from the canonical artifact"
            )

    def rate(self, role: FeeParticipantRole | str) -> tuple[int, int]:
        selected = FeeParticipantRole(role)
        if selected is FeeParticipantRole.MAKER:
            return self.maker_rate_numerator, self.maker_rate_denominator
        return self.taker_rate_numerator, self.taker_rate_denominator

    def default_multiplier(self, role: FeeParticipantRole | str) -> tuple[int, int]:
        selected = FeeParticipantRole(role)
        if selected is FeeParticipantRole.MAKER:
            return (
                self.default_maker_multiplier_numerator,
                self.default_maker_multiplier_denominator,
            )
        return self.default_taker_multiplier_numerator, self.default_taker_multiplier_denominator

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        artifact: RawArtifact | None = None,
    ) -> FeeSchedule:
        if value.get("schema_version") not in (None, FEE_SCHEDULE_SCHEMA_VERSION):
            raise ValueError("unsupported fee schedule artifact version")
        rates = value.get("rates")
        rate_mapping = rates if isinstance(rates, Mapping) else {}
        taker = rate_mapping.get("taker")
        maker = rate_mapping.get("maker")
        taker_mapping = taker if isinstance(taker, Mapping) else {}
        maker_mapping = maker if isinstance(maker, Mapping) else {}
        multipliers = value.get("default_multipliers")
        multiplier_mapping = multipliers if isinstance(multipliers, Mapping) else {}

        def ratio(
            mapping: Mapping[str, object], name: str, fallback: tuple[int, int]
        ) -> tuple[int, int]:
            numerator = mapping.get(f"{name}_numerator", mapping.get("numerator"))
            denominator = mapping.get(f"{name}_denominator", mapping.get("denominator"))
            if numerator is not None or denominator is not None:
                if isinstance(numerator, bool) or not isinstance(numerator, int):
                    raise ValueError(f"{name}_numerator must be an integer")
                if isinstance(denominator, bool) or not isinstance(denominator, int):
                    raise ValueError(f"{name}_denominator must be an integer")
                return numerator, denominator
            raw = mapping.get(name)
            if isinstance(raw, Mapping):
                return ratio(raw, name, fallback)
            return exact_rational(raw, name) if raw is not None else fallback

        taker_rate = ratio(taker_mapping, "rate", (7, 100))
        maker_rate = ratio(maker_mapping, "rate", (175, 10_000))
        taker_multiplier = ratio(
            multiplier_mapping,
            "taker",
            (1, 1),
        )
        maker_multiplier = ratio(
            multiplier_mapping,
            "maker",
            (0, 1),
        )
        effective_raw = value.get("effective_at", value.get("effective_date"))
        if not isinstance(effective_raw, str):
            raise ValueError("fee schedule effective_at is required")
        try:
            effective_at = _aware(
                datetime.fromisoformat(effective_raw.replace("Z", "+00:00")),
                "effective_at",
            )
        except ValueError as exc:
            raise ValueError("fee schedule effective_at is malformed") from exc
        captured_raw = value.get("captured_at")
        captured_at = None
        if captured_raw is not None:
            if not isinstance(captured_raw, str):
                raise ValueError("fee schedule captured_at is malformed")
            captured_at = _aware(
                datetime.fromisoformat(captured_raw.replace("Z", "+00:00")), "captured_at"
            )
        official_url = value.get("official_url")
        if official_url is not None and not isinstance(official_url, str):
            raise ValueError("fee schedule official_url is malformed")
        pdf_hash = value.get("pdf_sha256")
        if pdf_hash is not None and not isinstance(pdf_hash, str):
            raise ValueError("fee schedule pdf_sha256 is malformed")
        settlement_raw = value.get("settlement_fee_micros", 0)
        if isinstance(settlement_raw, bool) or not isinstance(settlement_raw, int):
            raise ValueError("fee schedule settlement_fee_micros must be an integer")
        return cls(
            schedule_version=_required_text(value.get("schedule_version"), "schedule_version"),
            official_url=official_url,
            effective_at=effective_at,
            pdf_sha256=pdf_hash,
            formula_version=str(value.get("formula_version", FEE_FORMULA_VERSION)),
            fee_type=str(value.get("fee_type", "quadratic")),
            taker_rate_numerator=taker_rate[0],
            taker_rate_denominator=taker_rate[1],
            maker_rate_numerator=maker_rate[0],
            maker_rate_denominator=maker_rate[1],
            default_taker_multiplier_numerator=taker_multiplier[0],
            default_taker_multiplier_denominator=taker_multiplier[1],
            default_maker_multiplier_numerator=maker_multiplier[0],
            default_maker_multiplier_denominator=maker_multiplier[1],
            settlement_fee_micros=settlement_raw,
            rounding_rule=str(
                value.get("rounding_rule", "fee_plus_position_cost_to_nearest_centicent")
            ),
            captured_at=captured_at,
            artifact=artifact,
            provenance=str(value.get("provenance", "official_public_schedule")),
        )


def load_fee_schedule(
    path: str | Path = DEFAULT_FEE_SCHEDULE_PATH,
    *,
    allow_unverified: bool = False,
    observed_at: datetime | None = None,
) -> FeeSchedule:
    """Load the canonical local JSON schedule and verify its archived evidence."""

    source = Path(path)
    try:
        content = source.read_bytes()
        raw = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeePolicyError(f"cannot load canonical fee schedule {source}") from exc
    if not isinstance(raw, Mapping):
        raise FeePolicyError("canonical fee schedule must be a JSON object")
    required_fields = {
        "schema_version",
        "schedule_version",
        "effective_at",
        "formula_version",
        "fee_type",
        "rates",
        "default_multipliers",
        "rounding_rule",
        "settlement_fee_micros",
    }
    missing_fields = sorted(field for field in required_fields if field not in raw)
    if missing_fields:
        raise FeePolicyError(
            "canonical fee schedule is incomplete: " + ", ".join(missing_fields)
        )
    artifact = RawArtifact(
        hashlib.sha256(content).hexdigest(),
        len(content),
        str(source).replace("\\", "/"),
        observed_at=(
            datetime.now(UTC)
            if observed_at is None
            else _aware(observed_at, "fee schedule observation")
        ),
        schema_version=FEE_SCHEDULE_SCHEMA_VERSION,
    )
    try:
        schedule = FeeSchedule.from_mapping(raw, artifact=artifact)
    except (TypeError, ValueError) as exc:
        raise FeePolicyError("canonical fee schedule is invalid") from exc
    if not allow_unverified and not schedule.verified:
        raise FeePolicyError("canonical fee schedule has no archived JSON artifact")
    return schedule


@dataclass(frozen=True, slots=True)
class FeeChange:
    """Normalized series or event fee-change evidence."""

    identity: str
    scope_ref: str
    scheduled_ts: datetime
    fee_type: str | None = None
    multiplier_numerator: int | None = None
    multiplier_denominator: int | None = None
    fee_type_override: str | None = None
    multiplier_override_numerator: int | None = None
    multiplier_override_denominator: int | None = None
    explicit_clear: bool = False
    raw: Mapping[str, object] = field(default_factory=dict)
    artifact: RawArtifact | None = None

    def __post_init__(self) -> None:
        _required_text(self.identity, "fee change identity")
        _required_text(self.scope_ref, "fee change scope")
        object.__setattr__(self, "scheduled_ts", _aware(self.scheduled_ts, "scheduled_ts"))
        for name in (
            "multiplier_numerator",
            "multiplier_denominator",
            "multiplier_override_numerator",
            "multiplier_override_denominator",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name} must be an integer")
        if self.multiplier_numerator is not None and self.multiplier_numerator < 0:
            raise ValueError("fee change multiplier cannot be negative")
        if self.multiplier_denominator is not None and self.multiplier_denominator <= 0:
            raise ValueError("fee change multiplier denominator must be positive")
        if (
            self.multiplier_override_numerator is not None
            and self.multiplier_override_numerator < 0
        ):
            raise ValueError("fee change override cannot be negative")
        if (
            self.multiplier_override_denominator is not None
            and self.multiplier_override_denominator <= 0
        ):
            raise ValueError("fee change override denominator must be positive")
        if (self.multiplier_numerator is None) != (self.multiplier_denominator is None):
            raise ValueError("fee change multiplier numerator and denominator are both required")
        if (self.multiplier_override_numerator is None) != (
            self.multiplier_override_denominator is None
        ):
            raise ValueError("fee change override numerator and denominator are both required")
        if self.explicit_clear and (
            self.fee_type is not None
            or self.multiplier_numerator is not None
            or self.multiplier_denominator is not None
            or self.fee_type_override is not None
            or self.multiplier_override_numerator is not None
            or self.multiplier_override_denominator is not None
        ):
            raise ValueError("a cleared fee change cannot carry fee values")
        for name, value in (
            ("fee_type", self.fee_type),
            ("fee_type_override", self.fee_type_override),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string")

    @property
    def semantic_key(self) -> tuple[object, ...]:
        return (
            self.scope_ref,
            self.scheduled_ts,
            self.fee_type,
            self.multiplier_numerator,
            self.multiplier_denominator,
            self.fee_type_override,
            self.multiplier_override_numerator,
            self.multiplier_override_denominator,
            self.explicit_clear,
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        scope_ref: str,
        event_override: bool,
        index: int = 0,
        artifact: RawArtifact | None = None,
    ) -> FeeChange:
        identity = value.get("id", value.get("change_id", f"change-{index}"))
        scheduled_raw = value.get("scheduled_ts")
        if not isinstance(scheduled_raw, (str, int, Decimal)) or isinstance(
            scheduled_raw, bool
        ):
            raise ValueError("fee change scheduled_ts is required")
        if isinstance(scheduled_raw, (int, Decimal)) or (
            isinstance(scheduled_raw, str) and scheduled_raw.isdigit()
        ):
            numeric = _exact_decimal(scheduled_raw, "scheduled_ts")
            if numeric < 0:
                raise ValueError("fee change scheduled_ts cannot be negative")
            if numeric > Decimal("100000000000"):
                numeric /= Decimal(1000)
            try:
                scheduled_ts = datetime.fromtimestamp(float(numeric), tz=UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise ValueError(
                    "fee change scheduled_ts is outside the supported range"
                ) from exc
        else:
            scheduled_ts = datetime.fromisoformat(scheduled_raw.replace("Z", "+00:00"))
        if not isinstance(identity, str):
            raise ValueError("fee change id must be a string")
        if event_override:
            type_keys = ("fee_type_override", "event_fee_type", "fee_type")
            multiplier_keys = (
                "fee_multiplier_override",
                "event_fee_multiplier",
                "fee_multiplier",
            )
            type_values = [value[key] for key in type_keys if key in value]
            multiplier_values = [value[key] for key in multiplier_keys if key in value]
            if any(item != type_values[0] for item in type_values[1:]):
                raise ValueError("event fee type aliases conflict")
            if any(item != multiplier_values[0] for item in multiplier_values[1:]):
                raise ValueError("event fee multiplier aliases conflict")
            type_present = bool(type_values)
            multiplier_present = bool(multiplier_values)
            if not type_present or not multiplier_present:
                raise ValueError("event fee override fields are incomplete")
            fee_type = type_values[0]
            raw_multiplier = multiplier_values[0]
            has_type = fee_type is not None
            has_multiplier = raw_multiplier is not None
            if type_present != multiplier_present or has_type != has_multiplier:
                raise ValueError("event fee override is partially null")
            if has_type and not isinstance(fee_type, str):
                raise ValueError("event fee_type_override must be a string")
            override_num, override_den = (
                exact_rational(raw_multiplier, "fee_multiplier_override")
                if has_multiplier
                else (None, None)
            )
            return cls(
                identity,
                scope_ref,
                scheduled_ts,
                fee_type_override=fee_type if isinstance(fee_type, str) else None,
                multiplier_override_numerator=override_num,
                multiplier_override_denominator=override_den,
                explicit_clear=not has_type,
                raw=dict(value),
                artifact=artifact,
            )
        fee_type = value.get("fee_type")
        if not isinstance(fee_type, str) or not fee_type.strip():
            raise ValueError("series fee change fee_type is required")
        raw_multiplier = value.get("fee_multiplier")
        multiplier_num, multiplier_den = exact_rational(raw_multiplier, "fee_multiplier")
        return cls(
            identity,
            scope_ref,
            scheduled_ts,
            fee_type=fee_type,
            multiplier_numerator=multiplier_num,
            multiplier_denominator=multiplier_den,
            raw=dict(value),
            artifact=artifact,
        )


@dataclass(frozen=True, slots=True)
class FeePolicyResolution:
    market_ref: str
    status: FeePolicyStatus
    reason: FeePolicyReason | None = None
    policy: FeePolicySnapshot | None = None
    evidence: tuple[FeeEvidence, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.market_ref, "fee policy market reference")
        object.__setattr__(self, "status", FeePolicyStatus(self.status))
        if self.reason is not None:
            object.__setattr__(self, "reason", FeePolicyReason(self.reason))
        if self.status is FeePolicyStatus.AVAILABLE and self.policy is None:
            raise ValueError("available fee resolution requires a policy")
        if self.status is not FeePolicyStatus.AVAILABLE and self.policy is not None:
            raise ValueError("unavailable fee resolution cannot carry a policy")
        if self.status is FeePolicyStatus.AVAILABLE and self.reason is not None:
            raise ValueError("available fee resolution cannot carry a closed reason")
        if self.status is not FeePolicyStatus.AVAILABLE and self.reason is None:
            raise ValueError("closed fee resolution requires an explicit reason")

    @property
    def tradeable(self) -> bool:
        return self.status is FeePolicyStatus.AVAILABLE and self.policy is not None


def _change_values(
    values: Sequence[FeeChange | Mapping[str, object]],
    *,
    scope_ref: str,
    event_override: bool,
) -> tuple[FeeChange, ...]:
    parsed: list[FeeChange] = []
    for index, value in enumerate(values):
        parsed.append(
            value
            if isinstance(value, FeeChange)
            else FeeChange.from_mapping(
                value,
                scope_ref=scope_ref,
                event_override=event_override,
                index=index,
            )
        )
    return tuple(parsed)


def _select_change(
    values: Sequence[FeeChange],
    *,
    as_of: datetime,
    scope_ref: str,
) -> FeeChange | None:
    applicable = [
        value
        for value in values
        if value.scope_ref == scope_ref and value.scheduled_ts <= as_of
    ]
    if not applicable:
        return None
    by_timestamp: dict[datetime, list[FeeChange]] = defaultdict(list)
    for value in applicable:
        by_timestamp[value.scheduled_ts].append(value)
    for timestamp, group in by_timestamp.items():
        semantic = {value.semantic_key for value in group}
        if len(semantic) > 1:
            raise FeePolicySourceConflictError(
                f"fee changes conflict at {timestamp.isoformat()} for {scope_ref}"
            )
    return max(applicable, key=lambda value: (value.scheduled_ts, value.identity))


class FeePolicyResolver:
    """Resolve an immutable policy for one market and one causal cutoff."""

    def __init__(
        self,
        schedule: FeeSchedule,
        *,
        participant_role: FeeParticipantRole | str = FeeParticipantRole.TAKER,
    ) -> None:
        self.schedule = schedule
        self.participant_role = FeeParticipantRole(participant_role)

    def resolve(
        self,
        *,
        market_ref: str,
        series_ref: str,
        event_ref: str,
        series_fee_type: str | None,
        series_multiplier: object | None = None,
        series_multiplier_numerator: int | None = None,
        series_multiplier_denominator: int | None = None,
        series_changes: Sequence[FeeChange | Mapping[str, object]] = (),
        event_changes: Sequence[FeeChange | Mapping[str, object]] = (),
        waiver_expiration_time: datetime | str | None = None,
        as_of: datetime,
        cutoff: datetime,
        evidence: Mapping[FeeEvidenceRole | str, RawArtifact] | Sequence[FeeEvidence] = (),
    ) -> FeePolicyResolution:
        try:
            as_of = _aware(as_of, "fee policy as_of")
            cutoff = _aware(cutoff, "fee policy cutoff")
            if as_of > cutoff:
                raise ValueError("fee policy as_of cannot be after cutoff")
            normalized_evidence = list(self._evidence(evidence))
            if self.schedule.artifact is not None and not any(
                item.role is FeeEvidenceRole.OFFICIAL_SCHEDULE
                and item.artifact.sha256 == self.schedule.artifact.sha256
                for item in normalized_evidence
            ):
                normalized_evidence.insert(
                    0,
                    FeeEvidence(FeeEvidenceRole.OFFICIAL_SCHEDULE, self.schedule.artifact),
                )
            self._validate_evidence_cutoff(normalized_evidence, cutoff)
            schedule_artifact = self.schedule.artifact
            if not self.schedule.verified or schedule_artifact is None:
                raise FeePolicyError(
                    "local JSON fee schedule lacks archived evidence"
                )
            if as_of < self.schedule.effective_at:
                return FeePolicyResolution(
                    market_ref,
                    FeePolicyStatus.UNAVAILABLE,
                    FeePolicyReason.FEE_POLICY_UNAVAILABLE,
                    evidence=tuple(normalized_evidence),
                )
            if series_fee_type is None or (
                series_multiplier is None
                and (series_multiplier_numerator is None or series_multiplier_denominator is None)
            ):
                return FeePolicyResolution(
                    market_ref,
                    FeePolicyStatus.UNAVAILABLE,
                    FeePolicyReason.FEE_SOURCE_INCOMPLETE,
                    evidence=tuple(normalized_evidence),
                )
            if not isinstance(series_fee_type, str) or not series_fee_type.strip():
                raise ValueError("series fee type is malformed")
            if series_multiplier_numerator is None or series_multiplier_denominator is None:
                parsed_num, parsed_den = exact_rational(
                    series_multiplier, "series_multiplier"
                )
                series_multiplier_numerator, series_multiplier_denominator = (
                    parsed_num,
                    parsed_den,
                )
            elif series_multiplier is not None:
                parsed_num, parsed_den = exact_rational(series_multiplier, "series_multiplier")
                if (parsed_num, parsed_den) != (
                    series_multiplier_numerator,
                    series_multiplier_denominator,
                ):
                    raise ValueError("series multiplier representations conflict")
            if series_multiplier_numerator < 0 or series_multiplier_denominator <= 0:
                raise ValueError("series multiplier is invalid")
            metadata_series_num = series_multiplier_numerator
            metadata_series_den = series_multiplier_denominator
            series_history = _change_values(
                series_changes, scope_ref=series_ref, event_override=False
            )
            event_history = _change_values(
                event_changes, scope_ref=event_ref, event_override=True
            )
            for change, role in (
                *((item, FeeEvidenceRole.SERIES_FEE_CHANGE) for item in series_history),
                *((item, FeeEvidenceRole.EVENT_FEE_CHANGE) for item in event_history),
            ):
                if change.artifact is None:
                    continue
                candidate = FeeEvidence(role, change.artifact)
                if not any(
                    item.role == candidate.role
                    and item.artifact.sha256 == candidate.artifact.sha256
                    for item in normalized_evidence
                ):
                    normalized_evidence.append(candidate)
            self._validate_evidence_cutoff(normalized_evidence, cutoff)
            selected_series = _select_change(
                series_history, as_of=as_of, scope_ref=series_ref
            )
            fee_type = series_fee_type
            effective_from = self.schedule.effective_at
            selected_scheduled_ts: datetime | None = None
            if selected_series is not None:
                if selected_series.fee_type is None or selected_series.multiplier_numerator is None:
                    raise ValueError("selected series fee change is incomplete")
                fee_type = selected_series.fee_type
                series_multiplier_numerator = selected_series.multiplier_numerator
                series_multiplier_denominator = selected_series.multiplier_denominator or 1
                effective_from = selected_series.scheduled_ts
                selected_scheduled_ts = selected_series.scheduled_ts
            selected_event = _select_change(event_history, as_of=as_of, scope_ref=event_ref)
            event_override_num: int | None = None
            event_override_den: int | None = None
            event_override_type: str | None = None
            event_override_cleared = False
            if selected_event is not None:
                selected_scheduled_ts = max(
                    selected_scheduled_ts or selected_event.scheduled_ts,
                    selected_event.scheduled_ts,
                )
                effective_from = max(effective_from, selected_event.scheduled_ts)
                if selected_event.explicit_clear:
                    event_override_cleared = True
                else:
                    event_override_num = selected_event.multiplier_override_numerator
                    event_override_den = selected_event.multiplier_override_denominator
                    event_override_type = selected_event.fee_type_override
                    if event_override_num is None or event_override_den is None:
                        raise ValueError("selected event fee change is incomplete")
                    fee_type = event_override_type or fee_type
            waiver = False
            waiver_evidence: dict[str, object] | None = None
            if waiver_expiration_time is not None:
                if isinstance(waiver_expiration_time, str):
                    waiver_expiration_time = datetime.fromisoformat(
                        waiver_expiration_time.replace("Z", "+00:00")
                    )
                expiration = _aware(waiver_expiration_time, "fee waiver expiration")
                waiver = expiration > as_of
                waiver_evidence = {
                    "expiration_time": expiration.isoformat(),
                    "waived": waiver,
                    "as_of": as_of.isoformat(),
                }
            if fee_type != "quadratic":
                return FeePolicyResolution(
                    market_ref,
                    FeePolicyStatus.UNSUPPORTED,
                    FeePolicyReason.UNSUPPORTED_FEE_TYPE,
                    evidence=tuple(normalized_evidence),
                )
            if self.participant_role is not FeeParticipantRole.TAKER:
                return FeePolicyResolution(
                    market_ref,
                    FeePolicyStatus.UNSUPPORTED,
                    FeePolicyReason.UNSUPPORTED_PARTICIPANT_ROLE,
                    evidence=tuple(normalized_evidence),
                )
            source_artifacts = tuple(item.artifact for item in normalized_evidence)
            primary_artifact = source_artifacts[0] if source_artifacts else schedule_artifact
            exact_inputs = {
                "schedule": {
                    "schedule_version": self.schedule.schedule_version,
                    "official_url": self.schedule.official_url,
                    "effective_at": self.schedule.effective_at.isoformat(),
                    "artifact_sha256": schedule_artifact.sha256,
                    "pdf_sha256": self.schedule.pdf_sha256,
                    "formula_version": self.schedule.formula_version,
                },
                "series": {
                    "series_ref": series_ref,
                    "fee_type": series_fee_type,
                    "metadata_multiplier_numerator": metadata_series_num,
                    "metadata_multiplier_denominator": metadata_series_den,
                    "selected_change_id": (
                        selected_series.identity if selected_series is not None else None
                    ),
                    "selected_scheduled_ts": (
                        selected_series.scheduled_ts.isoformat()
                        if selected_series is not None
                        else None
                    ),
                    "selected_fee_type": fee_type,
                    "selected_multiplier_numerator": series_multiplier_numerator,
                    "selected_multiplier_denominator": series_multiplier_denominator,
                },
                "event": {
                    "event_ref": event_ref,
                    "selected_change_id": (
                        selected_event.identity if selected_event is not None else None
                    ),
                    "selected_scheduled_ts": (
                        selected_event.scheduled_ts.isoformat()
                        if selected_event is not None
                        else None
                    ),
                    "fee_type_override": event_override_type,
                    "fee_multiplier_override_numerator": event_override_num,
                    "fee_multiplier_override_denominator": event_override_den,
                    "cleared": event_override_cleared,
                },
                "waiver": waiver_evidence,
            }
            rate_num, rate_den = self.schedule.rate(self.participant_role)
            policy = FeePolicySnapshot(
                contract_version=FEE_SETTLEMENT_CONTRACT_VERSION,
                schedule_version=self.schedule.schedule_version,
                formula_version=self.schedule.formula_version,
                participant_role=self.participant_role,
                fee_type=fee_type,
                series_multiplier_numerator=series_multiplier_numerator,
                series_multiplier_denominator=series_multiplier_denominator,
                event_override_numerator=event_override_num,
                event_override_denominator=event_override_den,
                event_override_fee_type=event_override_type,
                event_override_cleared=event_override_cleared,
                rate_numerator=rate_num,
                rate_denominator=rate_den,
                waiver=waiver,
                waiver_evidence=waiver_evidence,
                as_of=as_of,
                effective_from=effective_from,
                scheduled_ts=selected_scheduled_ts,
                source_observed_at=max(
                    (
                        _aware(item.artifact.observed_at, "fee evidence observation")
                        for item in normalized_evidence
                        if item.artifact.observed_at is not None
                    ),
                    default=as_of,
                ),
                cutoff=cutoff,
                source_tier="official",
                raw_artifact=primary_artifact,
                source_artifacts=source_artifacts,
                evidence_references=tuple(
                    {
                        "role": FeeEvidenceRole(item.role).value,
                        "sha256": item.artifact.sha256,
                    }
                    for item in normalized_evidence
                ),
                schedule_sha256=schedule_artifact.sha256,
                settlement_fee_micros=MoneyMicros(self.schedule.settlement_fee_micros),
                exact_inputs=exact_inputs,
            )
            return FeePolicyResolution(
                market_ref,
                FeePolicyStatus.AVAILABLE,
                policy=policy,
                evidence=tuple(normalized_evidence),
            )
        except (TypeError, ValueError, InvalidOperation):
            return FeePolicyResolution(
                market_ref, FeePolicyStatus.INVALID, FeePolicyReason.FEE_POLICY_INVALID
            )

    @staticmethod
    def _evidence(
        evidence: Mapping[FeeEvidenceRole | str, RawArtifact] | Sequence[FeeEvidence],
    ) -> tuple[FeeEvidence, ...]:
        if isinstance(evidence, Mapping):
            return tuple(
                FeeEvidence(FeeEvidenceRole(role), artifact)
                for role, artifact in evidence.items()
            )
        return tuple(evidence)

    @staticmethod
    def _validate_evidence_cutoff(
        evidence: Sequence[FeeEvidence], cutoff: datetime
    ) -> None:
        for item in evidence:
            observed = item.artifact.observed_at
            if observed is None:
                if item.role is FeeEvidenceRole.OFFICIAL_SCHEDULE:
                    raise FeePolicyError("official fee schedule evidence has no observation time")
                raise ValueError("fee evidence observation is required")
            if _aware(observed, "fee evidence observation") > cutoff:
                if item.role is FeeEvidenceRole.OFFICIAL_SCHEDULE:
                    raise FeePolicyError("official fee schedule evidence is newer than its cutoff")
                raise ValueError("fee evidence is newer than the requested cutoff")
            for field_name, timestamp in (
                ("fee evidence source timestamp", item.artifact.source_timestamp),
                ("fee evidence historical cutoff", item.artifact.historical_cutoff),
            ):
                if timestamp is not None and _aware(timestamp, field_name) > cutoff:
                    if item.role is FeeEvidenceRole.OFFICIAL_SCHEDULE:
                        raise FeePolicyError(
                            "official fee schedule evidence is newer than its cutoff"
                        )
                    raise ValueError("fee evidence is newer than the requested cutoff")


def resolve_fee_policy(**kwargs: object) -> FeePolicyResolution:
    """Convenience wrapper for callers that do not need to retain a resolver."""

    schedule = kwargs.pop("schedule")
    if not isinstance(schedule, FeeSchedule):
        raise TypeError("schedule must be a FeeSchedule")
    participant_role = kwargs.pop("participant_role", FeeParticipantRole.TAKER)
    if not isinstance(participant_role, (FeeParticipantRole, str)):
        raise TypeError("participant_role must be a fee participant role")
    resolver = FeePolicyResolver(schedule, participant_role=participant_role)
    return resolver.resolve(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "DEFAULT_FEE_SCHEDULE_PATH",
    "FEE_SCHEDULE_SCHEMA_VERSION",
    "OFFICIAL_FEE_SCHEDULE_URL",
    "FeeChange",
    "FeeEvidence",
    "FeeEvidenceRole",
    "FeePolicyError",
    "FeePolicyReason",
    "FeePolicyResolution",
    "FeePolicyResolver",
    "FeePolicySourceConflictError",
    "FeePolicyStatus",
    "FeeSchedule",
    "FeeScheduleDriftError",
    "exact_rational",
    "load_fee_schedule",
    "resolve_fee_policy",
]
