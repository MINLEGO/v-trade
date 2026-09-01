from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from vtrade.artifacts import ContentAddressedArtifactStore
from vtrade.domain.execution import FeePolicySnapshot
from vtrade.domain.types import (
    BinaryMarket,
    BinaryOutcome,
    EventKey,
    MarketKey,
    MarketStatus,
    OutcomeKey,
    OutcomeSide,
    PriceGrid,
    RawArtifact,
    SeriesKey,
)
from vtrade.fee_policy import (
    FeeChange,
    FeeEvidence,
    FeeEvidenceRole,
    FeePolicyError,
    FeePolicyReason,
    FeePolicyResolver,
    FeePolicySourceConflictError,
    FeePolicyStatus,
    FeeSchedule,
    load_fee_schedule,
)
from vtrade.kalshi import KalshiPublicRestAdapter
from vtrade.semantic_runtime import _fee_policy_from_payload

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
ARTIFACT = RawArtifact(
    "a" * 64,
    1,
    "fixture://fee-policy",
    observed_at=NOW - timedelta(minutes=1),
)


def _schedule() -> FeeSchedule:
    return FeeSchedule(
        schedule_version="test-schedule",
        official_url="https://example.test/fee-schedule.pdf",
        effective_at=NOW - timedelta(days=1),
        pdf_sha256="b" * 64,
        artifact=ARTIFACT,
    )


def _market() -> BinaryMarket:
    key = MarketKey("KXTEST-1")
    return BinaryMarket(
        key=key,
        series_key=SeriesKey("SERIES-1"),
        event_key=EventKey("EVENT-1"),
        question="Will this resolve YES?",
        resolution_rules="Resolve from the official source.",
        resolution_source="https://example.test/source",
        open_time=NOW - timedelta(days=1),
        close_time=NOW + timedelta(days=1),
        expected_expiration_time=NOW + timedelta(days=1),
        latest_expiration_time=NOW + timedelta(days=1),
        status=MarketStatus.ACTIVE,
        eligible=True,
        price_grid=PriceGrid.from_ranges(
            [{"start": "0.00", "end": "1.00", "step": "0.01"}]
        ),
        outcomes=(
            BinaryOutcome(OutcomeKey(key, OutcomeSide.YES), "YES", True),
            BinaryOutcome(OutcomeKey(key, OutcomeSide.NO), "NO", True),
        ),
        observed_at=NOW - timedelta(minutes=1),
        audit=ARTIFACT,
        fee_waiver_expiration_time=NOW + timedelta(hours=1),
    )


def test_canonical_schedule_is_hash_pinned_and_exact() -> None:
    schedule = load_fee_schedule()

    assert schedule.verified
    assert schedule.artifact is not None
    assert schedule.artifact.sha256 == (
        "1fd0e6826a9515f50841449a0a6f7c8f33bcb5cfb646495f323e64cd9bed8583"
    )
    assert schedule.pdf_sha256 == (
        "c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601"
    )
    assert schedule.rate("TAKER") == (7, 100)
    assert schedule.default_multiplier("TAKER") == (1, 1)


def test_fee_policy_accepts_an_exact_zero_series_multiplier() -> None:
    policy = FeePolicySnapshot(series_multiplier="0")

    assert policy.series_multiplier_numerator == 0
    assert policy.series_multiplier_denominator == 1


def test_missing_archived_json_schedule_is_a_global_failure() -> None:
    schedule = FeeSchedule(
        schedule_version="unverified",
        official_url="https://example.test/fee-schedule.pdf",
        effective_at=NOW - timedelta(days=1),
        pdf_sha256=None,
        artifact=None,
    )

    with pytest.raises(FeePolicyError, match="archived evidence"):
        FeePolicyResolver(schedule).resolve(
            market_ref="KXTEST-1",
            series_ref="SERIES-1",
            event_ref="EVENT-1",
            series_fee_type="quadratic",
            series_multiplier="1",
            as_of=NOW,
            cutoff=NOW,
        )


def test_local_json_schedule_does_not_require_pdf_provenance(tmp_path: Path) -> None:
    raw = json.loads(Path("spec/fee-schedules/kalshi-predictions-v1.json").read_text())
    raw.pop("official_url")
    raw.pop("pdf_sha256")
    path = tmp_path / "fee-schedule.json"
    content = json.dumps(raw, indent=2).encode("utf-8")
    path.write_bytes(content)

    schedule = load_fee_schedule(path, observed_at=NOW)

    assert schedule.verified
    assert schedule.official_url is None
    assert schedule.pdf_sha256 is None
    assert schedule.artifact is not None
    assert schedule.artifact.sha256 == hashlib.sha256(content).hexdigest()


def test_adapter_loads_the_local_json_schedule_without_a_pdf_request(tmp_path: Path) -> None:
    source = Path("spec/fee-schedules/kalshi-predictions-v1.json")
    schedule_path = tmp_path / source.name
    content = source.read_bytes()
    schedule_path.write_bytes(content)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/series/fee_changes"):
            payload = {"series_fee_change_arr": []}
        elif request.url.path.endswith("/events/fee_changes"):
            payload = {"event_fee_change_arr": []}
        else:
            raise AssertionError(f"unexpected network request: {request.url}")
        return httpx.Response(200, request=request, content=json.dumps(payload).encode())

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    adapter = KalshiPublicRestAdapter(
        ContentAddressedArtifactStore(tmp_path / "artifacts"),
        client=client,
        clock=lambda: NOW,
        fee_schedule_path=schedule_path,
    )

    resolution = adapter.resolve_fee_policy_for_market(
        _market(),
        series_metadata={
            "ticker": "SERIES-1",
            "fee_type": "quadratic",
            "fee_multiplier": "1",
        },
        as_of=NOW,
        cutoff=NOW,
    )

    assert resolution.tradeable
    assert all("kalshi-fee-schedule.pdf" not in call for call in calls)
    assert resolution.policy is not None
    assert resolution.policy.schedule_sha256 == hashlib.sha256(content).hexdigest()
    client.close()


@pytest.mark.parametrize("schedule_content", [None, b"not-json"])
def test_adapter_fails_closed_when_the_local_json_schedule_is_missing_or_invalid(
    tmp_path: Path, schedule_content: bytes | None
) -> None:
    schedule_path = tmp_path / "fee-schedule.json"
    if schedule_content is not None:
        schedule_path.write_bytes(schedule_content)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError(f"unexpected network request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    adapter = KalshiPublicRestAdapter(
        ContentAddressedArtifactStore(tmp_path / "artifacts"),
        client=client,
        clock=lambda: NOW,
        fee_schedule_path=schedule_path,
    )

    with pytest.raises(FeePolicyError, match="canonical fee schedule"):
        adapter._fee_schedule_for_operation(adapter._deadline())

    assert calls == []
    client.close()


def test_resolver_applies_latest_series_and_event_precedence_with_exact_rationals() -> None:
    resolver = FeePolicyResolver(_schedule())
    series_change = FeeChange(
        identity="series-change",
        scope_ref="SERIES-1",
        scheduled_ts=NOW - timedelta(hours=2),
        fee_type="quadratic",
        multiplier_numerator=1,
        multiplier_denominator=8,
    )
    event_change = FeeChange(
        identity="event-change",
        scope_ref="EVENT-1",
        scheduled_ts=NOW - timedelta(hours=1),
        fee_type_override="quadratic",
        multiplier_override_numerator=1,
        multiplier_override_denominator=4,
    )
    result = resolver.resolve(
        market_ref="KXTEST-1",
        series_ref="SERIES-1",
        event_ref="EVENT-1",
        series_fee_type="quadratic",
        series_multiplier="1",
        series_changes=(series_change,),
        event_changes=(event_change,),
        waiver_expiration_time=NOW + timedelta(minutes=1),
        as_of=NOW,
        cutoff=NOW,
        evidence=(FeeEvidence(FeeEvidenceRole.SERIES_METADATA, ARTIFACT),),
    )

    assert result.status is FeePolicyStatus.AVAILABLE
    assert result.policy is not None
    assert result.policy.series_multiplier_numerator == 1
    assert result.policy.series_multiplier_denominator == 8
    assert result.policy.event_override_numerator == 1
    assert result.policy.event_override_denominator == 4
    assert result.policy.waiver is True
    assert result.policy.exact_inputs["series"]["selected_change_id"] == "series-change"
    assert result.policy.schedule_sha256 == ARTIFACT.sha256
    assert result.policy.exact_inputs["schedule"]["artifact_sha256"] == ARTIFACT.sha256


def test_resolver_supports_explicit_event_clear_and_strict_waiver_expiration() -> None:
    resolver = FeePolicyResolver(_schedule())
    clear = FeeChange(
        identity="clear",
        scope_ref="EVENT-1",
        scheduled_ts=NOW - timedelta(hours=1),
        explicit_clear=True,
    )
    result = resolver.resolve(
        market_ref="KXTEST-1",
        series_ref="SERIES-1",
        event_ref="EVENT-1",
        series_fee_type="quadratic",
        series_multiplier="1",
        event_changes=(clear,),
        waiver_expiration_time=NOW,
        as_of=NOW,
        cutoff=NOW,
    )

    assert result.tradeable
    assert result.policy is not None
    assert result.policy.event_override_cleared is True
    assert result.policy.event_override_numerator is None
    assert result.policy.waiver is False
    assert result.policy.waiver_evidence is not None
    assert result.policy.waiver_evidence["waived"] is False


def test_resolver_keeps_local_unsupported_or_invalid_reasons_and_global_conflicts() -> None:
    resolver = FeePolicyResolver(_schedule())
    unsupported = resolver.resolve(
        market_ref="KXTEST-1",
        series_ref="SERIES-1",
        event_ref="EVENT-1",
        series_fee_type="flat",
        series_multiplier="1",
        as_of=NOW,
        cutoff=NOW,
    )
    invalid = resolver.resolve(
        market_ref="KXTEST-1",
        series_ref="SERIES-1",
        event_ref="EVENT-1",
        series_fee_type="quadratic",
        series_multiplier="1",
        event_changes=(
            {"id": "partial", "scheduled_ts": NOW.isoformat(), "fee_type": "quadratic"},
        ),
        as_of=NOW,
        cutoff=NOW,
    )

    assert unsupported.status is FeePolicyStatus.UNSUPPORTED
    assert unsupported.reason is FeePolicyReason.UNSUPPORTED_FEE_TYPE
    assert invalid.status is FeePolicyStatus.INVALID
    assert invalid.reason is FeePolicyReason.FEE_POLICY_INVALID

    first = FeeChange(
        identity="a",
        scope_ref="SERIES-1",
        scheduled_ts=NOW - timedelta(hours=1),
        fee_type="quadratic",
        multiplier_numerator=1,
        multiplier_denominator=1,
    )
    second = FeeChange(
        identity="b",
        scope_ref="SERIES-1",
        scheduled_ts=first.scheduled_ts,
        fee_type="quadratic",
        multiplier_numerator=2,
        multiplier_denominator=1,
    )
    with pytest.raises(FeePolicySourceConflictError):
        resolver.resolve(
            market_ref="KXTEST-1",
            series_ref="SERIES-1",
            event_ref="EVENT-1",
            series_fee_type="quadratic",
            series_multiplier="1",
            series_changes=(first, second),
            as_of=NOW,
            cutoff=NOW,
        )


def test_adapter_reads_all_event_fee_change_pages_and_keeps_each_evidence_artifact(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/series/fee_changes"):
            payload = {
                "series_fee_change_arr": [
                    {
                        "id": "series-1",
                        "series_ticker": "SERIES-1",
                        "fee_type": "quadratic",
                        "fee_multiplier": "1",
                        "scheduled_ts": "2026-08-20T00:00:00Z",
                    }
                ]
            }
        elif request.url.path.endswith("/events/fee_changes"):
            if request.url.params.get("cursor") is None:
                payload = {
                    "event_fee_change_arr": [
                        {
                            "id": "event-1",
                            "event_ticker": "EVENT-1",
                            "fee_type": "quadratic",
                            "fee_multiplier": "0.5",
                            "scheduled_ts": "2026-08-20T01:00:00Z",
                        }
                    ],
                    "cursor": "page-2",
                }
            else:
                payload = {
                    "event_fee_change_arr": [
                        {
                            "id": "event-2",
                            "event_ticker": "EVENT-1",
                            "fee_type": None,
                            "fee_multiplier": None,
                            "scheduled_ts": "2026-08-20T02:00:00Z",
                        }
                    ],
                    "cursor": None,
                }
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        return httpx.Response(200, request=request, content=json.dumps(payload).encode())

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=5,
    )
    schedule = _schedule()
    adapter = KalshiPublicRestAdapter(
        ContentAddressedArtifactStore(tmp_path),
        client=client,
        clock=lambda: NOW - timedelta(minutes=1),
        fee_schedule=schedule,
    )
    resolution = adapter.resolve_fee_policy_for_market(
        _market(),
        series_metadata={
            "ticker": "SERIES-1",
            "fee_type": "quadratic",
            "fee_multiplier": "1",
        },
        as_of=NOW,
        cutoff=NOW,
    )

    assert resolution.tradeable
    assert resolution.policy is not None
    assert resolution.policy.event_override_cleared is True
    event_evidence = [
        item for item in resolution.evidence if item.role is FeeEvidenceRole.EVENT_FEE_CHANGE
    ]
    assert len(event_evidence) == 2
    assert any("cursor=page-2" in call for call in calls)


def test_fee_policy_payload_matches_the_closed_agent_schema() -> None:
    resolver = FeePolicyResolver(_schedule())
    result = resolver.resolve(
        market_ref="KXTEST-1",
        series_ref="SERIES-1",
        event_ref="EVENT-1",
        series_fee_type="quadratic",
        series_multiplier="1",
        as_of=NOW,
        cutoff=NOW,
        evidence=(FeeEvidence(FeeEvidenceRole.SERIES_METADATA, ARTIFACT),),
    )
    assert result.policy is not None
    document = json.loads(Path("spec/tool-schemas-vtrade-kalshi-v1.json").read_text())
    schema = {"$ref": "#/$defs/fee_policy", "$defs": document["$defs"]}
    Draft202012Validator(schema).validate(result.policy.to_payload())


def test_fee_policy_checkpoint_payload_round_trips_its_fingerprint() -> None:
    resolver = FeePolicyResolver(_schedule())
    result = resolver.resolve(
        market_ref="KXTEST-1",
        series_ref="SERIES-1",
        event_ref="EVENT-1",
        series_fee_type="quadratic",
        series_multiplier="1",
        as_of=NOW,
        cutoff=NOW,
        evidence=(FeeEvidence(FeeEvidenceRole.SERIES_METADATA, ARTIFACT),),
    )
    assert result.policy is not None
    evidence = [
        {
            "role": item.role.value,
            "artifact": {
                "uri": item.artifact.uri,
                "sha256": item.artifact.sha256,
                "byte_length": item.artifact.byte_length,
            },
        }
        for item in result.evidence
    ]

    restored = _fee_policy_from_payload(result.policy.to_payload(), evidence)

    assert restored.fingerprint == result.policy.fingerprint
    assert restored.evidence_references == result.policy.evidence_references


def test_fresh_execution_fee_refresh_uses_a_completed_order_time_cutoff(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    current = NOW

    def clock() -> datetime:
        nonlocal current
        current += timedelta(microseconds=1)
        return current

    market_payload = {
        "ticker": "KXTEST-1",
        "event_ticker": "EVENT-1",
        "series_ticker": "SERIES-1",
        "title": "Will this resolve YES?",
        "rules_primary": "Resolve from the official source.",
        "settlement_sources": ["https://example.test/source"],
        "status": "active",
        "open_time": (NOW - timedelta(days=1)).isoformat(),
        "close_time": (NOW + timedelta(days=1)).isoformat(),
        "expected_expiration_time": (NOW + timedelta(days=1)).isoformat(),
        "latest_expiration_time": (NOW + timedelta(days=1)).isoformat(),
        "updated_time": (NOW - timedelta(minutes=1)).isoformat(),
        "price_ranges": [{"start": "0.00", "end": "1.00", "step": "0.01"}],
        "yes_sub_title": "YES",
        "no_sub_title": "NO",
        "volume_fp": "1.00",
        "volume_24h_fp": "1.00",
        "liquidity_dollars": "1.00",
        "result": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        calls.append(path)
        if path == "/historical/cutoff":
            payload: object = {"market_settled_ts": "2026-08-01T00:00:00Z"}
        elif path == "/markets/KXTEST-1":
            payload = market_payload
        elif path == "/markets/KXTEST-1/orderbook":
            payload = {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "1.00"]],
                    "no_dollars": [["0.50", "1.00"]],
                }
            }
        elif path == "/series/SERIES-1":
            payload = {
                "series": {
                    "ticker": "SERIES-1",
                    "title": "Test series",
                    "fee_type": "quadratic",
                    "fee_multiplier": "1",
                }
            }
        elif path == "/series/fee_changes":
            payload = {"series_fee_change_arr": []}
        elif path == "/events/fee_changes":
            payload = {"event_fee_change_arr": []}
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        return httpx.Response(200, request=request, content=json.dumps(payload).encode())

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    adapter = KalshiPublicRestAdapter(
        ContentAddressedArtifactStore(tmp_path),
        client=client,
        clock=clock,
        fee_schedule=_schedule(),
        require_fee_policy=True,
    )
    try:
        context = adapter.get_fresh_execution_context(MarketKey("KXTEST-1"))
    finally:
        adapter.close()

    assert context.market.tradeable
    assert context.market.fee_policy is not None
    assert context.market.fee_policy.cutoff == context.order_book.cutoff
    assert context.order_book.cutoff >= context.order_book.observed_at
    assert "/series/fee_changes" in calls
    assert "/events/fee_changes" in calls
