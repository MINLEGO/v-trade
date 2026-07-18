from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from vtrade.artifacts import SupabaseArtifactStore
from vtrade.broker import (
    ArchivedBid,
    ExecutionStatus,
    PaperOrder,
    PaperPolicy,
    PortfolioState,
    PositionState,
    PredictionArenaPaperBroker,
    SettlementEngine,
    SettlementObservation,
)
from vtrade.broker_repository import PostgresBrokerRepository
from vtrade.config import (
    ConfigurationError,
    ExperimentConfig,
    load_experiment_config,
    required_environment,
)
from vtrade.domain.ports import ArtifactStore, JsonObject
from vtrade.domain.types import (
    Market,
    MarketStatus,
    MicroDollars,
    OrderBookSnapshot,
    Outcome,
    PriceLevel,
    RawArtifact,
    Side,
)
from vtrade.harness import (
    BeliefRecord,
    BoundedToolHarness,
    HarnessLimits,
    HarnessResult,
    LearningEvent,
    PlanRecord,
    PlanType,
    PromptBuilder,
    deterministic_critical_learning,
)
from vtrade.harness_repository import PostgresBudgetGuard, PostgresHarnessRepository
from vtrade.market_data import PolymarketFreezeService, PostgresMarketDataRepository
from vtrade.polymarket import PolymarketVenue
from vtrade.postgres_runtime import PostgresRuntimeRepository
from vtrade.production_tools import ProductionToolRegistry, production_tool_context
from vtrade.providers import (
    ExaResearchProvider,
    OpenRouterModelGateway,
    ProviderTelemetry,
    canonical_redacted_json,
)
from vtrade.runtime import (
    ArtifactRegistration,
    BrokerExecutionResult,
    CycleClaim,
    CycleOrchestrator,
    HarnessExecutionResult,
    HourlyRuntime,
    PreSettlementResult,
    ProjectionService,
    PromptResult,
    RetentionCleaner,
    RuntimeAlertPolicy,
    RuntimeTickResult,
    SettlementValuationResult,
    six_month_retain_until,
)


class ProductionCompositionUnavailable(RuntimeError):
    """The frozen production graph cannot be built from the supplied resources."""


class _Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _PromptSource:
    prompt_version_id: uuid.UUID
    system_prompt: str
    model_config: JsonObject


class ProductionPromptPort:
    """Materialize and persist the private, immutable per-cycle prompt context."""

    def __init__(
        self,
        database_url: str,
        artifact_store: ArtifactStore,
        *,
        clock: Callable[[], datetime],
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._store = artifact_store
        self._clock = clock
        self._connect = connect or _default_connect
        self._memory = PostgresHarnessRepository(database_url, connect=connect)

    def render(self, claim: CycleClaim, frozen: JsonObject) -> PromptResult:
        cutoff = _cutoff(claim)
        source = self._prompt_source(claim.agent_id)
        beliefs = tuple(
            _belief(row, claim.agent_id)
            for row in self._memory.read_beliefs(
                actor_id=claim.agent_id, target_agent_id=claim.agent_id
            )
        )
        plans = tuple(
            _plan(row, claim.agent_id)
            for row in self._memory.read_plans(
                actor_id=claim.agent_id, target_agent_id=claim.agent_id
            )
        )
        learning_events = self._learning_events(claim.agent_id)
        learning = deterministic_critical_learning(learning_events)
        learning_input_sha256 = hashlib.sha256(
            json.dumps(
                [asdict(event) for event in learning_events],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        cycle_context: JsonObject = {
            "cycle_id": str(claim.cycle_id),
            "scheduled_at": claim.scheduled_at.isoformat(),
            "data_cutoff": cutoff.isoformat(),
            "market_snapshot_ids": _strings(frozen, "market_snapshot_ids"),
            "order_book_snapshot_ids": _strings(frozen, "order_book_snapshot_ids"),
            "account": self._account_context(claim.agent_id),
        }
        messages = PromptBuilder(source.system_prompt).build(
            agent_id=str(claim.agent_id),
            cycle_context=cycle_context,
            beliefs=beliefs,
            plans=plans,
            critical_learning=learning,
        )
        rendered = json.dumps(
            messages,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        artifact = self._store.put(canonical_redacted_json({"messages": messages}))
        now = _aware(self._clock())
        retained = six_month_retain_until(now)
        context: JsonObject = {
            "messages": list(messages),
            "model_config": source.model_config,
            "critical_learning": learning,
        }
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        market_ids = _uuids(frozen, "market_snapshot_ids")
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT rendered_prompt_sha256 FROM cycle_contexts WHERE agent_cycle_id = %s",
                (claim.cycle_id,),
            )
            existing = cursor.fetchone()
            if existing is not None and str(existing[0]) != digest:
                raise ValueError("cycle prompt idempotency fingerprint conflict")
            if existing is None:
                cursor.execute(
                    "INSERT INTO cycle_contexts "
                    "(id, agent_cycle_id, prompt_version_id, rendered_cycle_prompt, "
                    "rendered_prompt_sha256, context, market_snapshot_ids, artifact_uri, "
                    "artifact_sha256, retain_until, created_at) VALUES "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s::uuid[], %s, %s, %s, %s)",
                    (
                        uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:cycle-context:{claim.cycle_id}"),
                        claim.cycle_id,
                        source.prompt_version_id,
                        rendered,
                        digest,
                        json.dumps(context, sort_keys=True, default=str),
                        list(market_ids),
                        artifact.uri,
                        artifact.sha256,
                        retained,
                        now,
                    ),
                )
            cursor.execute(
                "INSERT INTO critical_learning_snapshots "
                "(id, agent_cycle_id, agent_id, summary, input_sha256, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (agent_cycle_id) DO NOTHING",
                (
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:learning:{claim.cycle_id}"),
                    claim.cycle_id,
                    claim.agent_id,
                    learning,
                    learning_input_sha256,
                    now,
                ),
            )
        return PromptResult(
            {
                "cycle_context_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:cycle-context:{claim.cycle_id}")
                ),
                "prompt_sha256": digest,
            },
            (_registration(artifact, retained),),
            len(rendered),
        )

    def _prompt_source(self, agent_id: uuid.UUID) -> _PromptSource:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pv.id, pv.body, mc.model_slug, mc.provider_policy, mc.parameters "
                "FROM agents a JOIN experiment_runs r ON r.id = a.run_id "
                "JOIN prompt_versions pv ON pv.definition_id = r.definition_id "
                "JOIN model_configs mc ON mc.id = a.model_config_id "
                "WHERE a.id = %s ORDER BY pv.created_at DESC, pv.id DESC LIMIT 1",
                (agent_id,),
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[3], Mapping) or not isinstance(row[4], Mapping):
            raise ProductionCompositionUnavailable("agent prompt/model registration is missing")
        config: JsonObject = {str(key): value for key, value in row[4].items()}
        config.update({str(key): value for key, value in row[3].items()})
        config["slug"] = str(row[2])
        return _PromptSource(uuid.UUID(str(row[0])), str(row[1]), config)

    def _learning_events(self, agent_id: uuid.UUID) -> tuple[LearningEvent, ...]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 'settlement', p.outcome_id::text, s.realized_pnl_micros, '' "
                "FROM settlements s JOIN positions p ON p.id = s.position_id "
                "WHERE s.agent_id = %s ORDER BY s.settled_at DESC LIMIT 50",
                (agent_id,),
            )
            rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT 'rejection', oi.market_id::text, 0, COALESCE(o.rejection_code, '') "
                "FROM orders o JOIN order_intents oi ON oi.id = o.intent_id "
                "JOIN agent_cycles ac ON ac.id = oi.agent_cycle_id "
                "WHERE ac.agent_id = %s AND o.status = 'rejected' "
                "ORDER BY o.created_at DESC LIMIT 50",
                (agent_id,),
            )
            rows.extend(cursor.fetchall())
        return tuple(
            LearningEvent(str(row[0]), str(row[1]), int(str(row[2])), str(row[3])) for row in rows
        )

    def _account_context(self, agent_id: uuid.UUID) -> JsonObject:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(sum(lp.amount_micros) FILTER "
                "(WHERE lp.account = 'cash'), 0), a.portfolio_version FROM agents a "
                "LEFT JOIN ledger_entries le ON le.agent_id = a.id "
                "LEFT JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
                "WHERE a.id = %s GROUP BY a.id",
                (agent_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ProductionCompositionUnavailable("agent account is missing")
        return {"cash_micros": int(str(row[0])), "portfolio_version": int(str(row[1]))}


class ProductionHarnessPort:
    """Execute all 29 tools through the bounded real model/provider harness."""

    def __init__(
        self,
        database_url: str,
        artifact_store: ArtifactStore,
        gateway: OpenRouterModelGateway,
        exa: ExaResearchProvider,
        limits: HarnessLimits,
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        schema_path: str | Path,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._store = artifact_store
        self._gateway = gateway
        self._exa = exa
        self._limits = limits
        self._clock = clock
        self._monotonic = monotonic
        self._schema_path = schema_path
        self._connect = connect or _default_connect
        self._repository = PostgresHarnessRepository(database_url, connect=connect)

    def run(
        self, claim: CycleClaim, frozen: JsonObject, prompt: JsonObject
    ) -> HarnessExecutionResult:
        del prompt
        if claim.recovery:
            return self._recover_completed_run(claim)
        messages, model_config = self._load_context(claim.cycle_id)
        context = production_tool_context(
            self._database_url,
            claim,
            self._exa,
            frozen=frozen,
            clock=self._clock,
        )
        registry = ProductionToolRegistry(context, schema_path=self._schema_path)
        result = BoundedToolHarness(
            self._gateway,
            registry.tool_specs(),
            self._limits,
            monotonic=self._monotonic,
        ).run(messages, model_config=model_config)
        transcript = canonical_redacted_json(
            {
                "messages": result.messages,
                "tool_calls": [asdict(item) for item in result.tool_calls],
                "termination_status": result.termination_status,
            }
        )
        artifact = self._store.put(transcript)
        completed = _aware(self._clock())
        retained = six_month_retain_until(completed)
        registrations = _harness_artifact_registrations(artifact, result.telemetry, retained)
        run_id = self._repository.persist_run(
            agent_cycle_id=claim.cycle_id,
            result=result,
            transcript_uri=artifact.uri,
            transcript_sha256=artifact.sha256,
            completed_at=completed,
            retain_until=retained,
            artifacts=registrations,
        )
        self._persist_detailed_audit(claim, result, retained, completed)
        searches = sum(1 for item in result.tool_calls if item.name == "web_search")
        intent_ids = self._cycle_intent_ids(claim.cycle_id)
        return HarnessExecutionResult(
            {
                "harness_run_id": str(run_id),
                "termination_status": result.termination_status,
                "intent_ids": [str(value) for value in intent_ids],
                "transcript_sha256": artifact.sha256,
            },
            registrations,
            searches,
            len(result.tool_calls),
        )

    def _recover_completed_run(self, claim: CycleClaim) -> HarnessExecutionResult:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, termination_status, total_tool_calls, total_web_searches, "
                "transcript_artifact_uri, transcript_sha256, retain_until "
                "FROM harness_runs WHERE agent_cycle_id = %s",
                (claim.cycle_id,),
            )
            run = cursor.fetchone()
            if run is None:
                raise ProductionCompositionUnavailable(
                    "recovery found no completed persisted harness run; "
                    "provider replay is forbidden"
                )
            cursor.execute(
                "SELECT uri, sha256, byte_length, retain_until FROM artifact_inventory "
                "WHERE status = 'active' AND (uri = %s OR uri IN "
                "(SELECT raw_artifact_uri FROM provider_usage WHERE agent_cycle_id = %s "
                "AND raw_artifact_uri IS NOT NULL)) "
                "ORDER BY created_at, id",
                (str(run[4]), claim.cycle_id),
            )
            artifact_rows = tuple(cursor.fetchall())
        registrations = tuple(
            ArtifactRegistration(
                str(row[0]),
                str(row[1]),
                int(str(row[2])),
                cast(datetime, row[3]),
            )
            for row in artifact_rows
        )
        if not registrations or not any(
            item.uri == str(run[4]) and item.sha256 == str(run[5]) for item in registrations
        ):
            raise ProductionCompositionUnavailable(
                "completed harness run lacks its atomic artifact inventory"
            )
        intent_ids = self._cycle_intent_ids(claim.cycle_id)
        return HarnessExecutionResult(
            {
                "harness_run_id": str(run[0]),
                "termination_status": str(run[1]),
                "intent_ids": [str(value) for value in intent_ids],
                "transcript_sha256": str(run[5]),
                "recovered_from_persisted_run": True,
            },
            registrations,
            int(str(run[3])),
            int(str(run[2])),
        )

    def _load_context(self, cycle_id: uuid.UUID) -> tuple[list[JsonObject], JsonObject]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT context FROM cycle_contexts WHERE agent_cycle_id = %s",
                (cycle_id,),
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[0], Mapping):
            raise ProductionCompositionUnavailable("persisted cycle context is missing")
        raw_messages = row[0].get("messages")
        raw_model = row[0].get("model_config")
        if not isinstance(raw_messages, list) or not isinstance(raw_model, Mapping):
            raise ProductionCompositionUnavailable("persisted prompt context is malformed")
        messages: list[JsonObject] = []
        for item in raw_messages:
            if not isinstance(item, Mapping):
                raise ProductionCompositionUnavailable("persisted prompt message is malformed")
            messages.append({str(key): value for key, value in item.items()})
        return messages, {str(key): value for key, value in raw_model.items()}

    def _persist_detailed_audit(
        self,
        claim: CycleClaim,
        result: HarnessResult,
        retained: datetime,
        completed: datetime,
    ) -> None:
        model_telemetry = [row for row in result.telemetry if row.usage_kind == "model"]
        search_telemetry = iter(row for row in result.telemetry if row.usage_kind == "web_search")
        records = {row.id: row for row in result.tool_calls}
        prefix: list[JsonObject] = []
        turn = 0
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            for message in result.messages:
                if message.get("role") != "assistant":
                    prefix.append(message)
                    continue
                telemetry = model_telemetry[turn] if turn < len(model_telemetry) else None
                turn_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"vtrade:model-turn:{claim.cycle_id}:{turn}"
                )
                cursor.execute(
                    "INSERT INTO model_turns "
                    "(id, agent_cycle_id, turn_index, request, response, provider_response_id, "
                    "termination_status, started_at, completed_at, raw_artifact_uri, "
                    "raw_sha256, retain_until) VALUES "
                    "(%s, %s, %s, %s::jsonb, %s::jsonb, NULL, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (agent_cycle_id, turn_index) DO NOTHING",
                    (
                        turn_id,
                        claim.cycle_id,
                        turn,
                        json.dumps({"messages": prefix}, sort_keys=True, default=str),
                        json.dumps(message, sort_keys=True, default=str),
                        "stop" if not message.get("tool_calls") else "tool_calls",
                        completed,
                        completed,
                        telemetry.artifact_uri if telemetry else None,
                        telemetry.raw_sha256 if telemetry else None,
                        retained,
                    ),
                )
                calls = message.get("tool_calls", [])
                if isinstance(calls, list):
                    for call_index, call in enumerate(calls):
                        if not isinstance(call, Mapping):
                            continue
                        call_id = str(call.get("id") or "")
                        record = records.get(call_id)
                        if record is None:
                            continue
                        tool_record_id = uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vtrade:tool-call:{turn_id}:{call_index}",
                        )
                        cursor.execute(
                            "INSERT INTO tool_calls "
                            "(id, model_turn_id, call_index, provider_call_id, category, "
                            "tool_name, display_name, arguments, output, success, "
                            "validation_status, error, called_at, completed_at, retain_until) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
                            "%s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (model_turn_id, call_index) DO NOTHING",
                            (
                                tool_record_id,
                                turn_id,
                                call_index,
                                record.id,
                                record.category,
                                record.name,
                                record.name,
                                json.dumps(record.arguments or {}, sort_keys=True),
                                json.dumps(record.output, sort_keys=True, default=str),
                                record.success,
                                "valid" if record.success else "rejected",
                                None if record.success else str(record.output.get("message", "")),
                                completed,
                                completed,
                                retained,
                            ),
                        )
                        if record.name == "web_search" and record.success:
                            telemetry = next(search_telemetry, None)
                            if telemetry is None:
                                raise ProductionCompositionUnavailable(
                                    "successful web search lacks provider telemetry"
                                )
                            self._persist_research(
                                cursor,
                                claim,
                                tool_record_id,
                                record.arguments or {},
                                record.output,
                                telemetry,
                                completed,
                            )
                prefix.append(message)
                turn += 1

    @staticmethod
    def _persist_research(
        cursor: _Cursor,
        claim: CycleClaim,
        tool_call_id: uuid.UUID,
        arguments: Mapping[str, object],
        output: Mapping[str, object],
        telemetry: ProviderTelemetry,
        completed: datetime,
    ) -> None:
        raw_results = output.get("results", [])
        if not isinstance(raw_results, list):
            raise ProductionCompositionUnavailable("web search result list is malformed")
        for row in raw_results:
            if not isinstance(row, Mapping):
                raise ProductionCompositionUnavailable("web search result is malformed")
            url = row.get("url")
            if not isinstance(url, str) or not url:
                raise ProductionCompositionUnavailable("web search result URL is missing")
            content = str(row.get("content") or "")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:research-document:{url}:{digest}")
            published = _optional_research_timestamp(row.get("published_at"))
            cursor.execute(
                "INSERT INTO research_documents "
                "(id, canonical_url, title, source_published_at, fetched_at, content_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (canonical_url, content_sha256) DO NOTHING",
                (
                    document_id,
                    url,
                    str(row.get("title") or ""),
                    published,
                    completed,
                    digest,
                ),
            )
            cursor.execute(
                "SELECT id FROM research_documents "
                "WHERE canonical_url = %s AND content_sha256 = %s",
                (url, digest),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise RuntimeError("research document disappeared after insert")
            document_id = uuid.UUID(str(existing[0]))
            cursor.execute(
                "INSERT INTO research_artifacts "
                "(id, tool_call_id, document_id, provider, query, artifact_uri, "
                "raw_sha256, source_cutoff, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vtrade:research-artifact:{tool_call_id}:{document_id}",
                    ),
                    tool_call_id,
                    document_id,
                    telemetry.provider,
                    str(arguments.get("query") or ""),
                    telemetry.artifact_uri,
                    telemetry.raw_sha256,
                    _cutoff(claim),
                    completed,
                ),
            )

    def _cycle_intent_ids(self, cycle_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM order_intents WHERE agent_cycle_id = %s ORDER BY created_at, id",
                (cycle_id,),
            )
            return tuple(uuid.UUID(str(row[0])) for row in cursor.fetchall())


@dataclass(frozen=True, slots=True)
class _TradingContext:
    intent_id: uuid.UUID
    market_id: uuid.UUID
    outcome_id: uuid.UUID
    order: PaperOrder
    market: Market
    outcome: Outcome
    book: OrderBookSnapshot
    book_snapshot_id: uuid.UUID


class _PostgresTradingState:
    def __init__(
        self,
        database_url: str,
        *,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._connect = connect or _default_connect

    def persisted_harness_intents(
        self, claim: CycleClaim, harness: Mapping[str, object]
    ) -> set[uuid.UUID]:
        raw_run_id = harness.get("harness_run_id")
        if not isinstance(raw_run_id, str):
            raise ProductionCompositionUnavailable("broker requires a persisted harness run")
        try:
            run_id = uuid.UUID(raw_run_id)
        except ValueError as exc:
            raise ProductionCompositionUnavailable("persisted harness run id is malformed") from exc
        payload_ids = set(_uuids(harness, "intent_ids"))
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM harness_runs WHERE id = %s AND agent_cycle_id = %s",
                (run_id, claim.cycle_id),
            )
            if cursor.fetchone() is None:
                raise ProductionCompositionUnavailable(
                    "broker cannot execute intents from an unpersisted harness"
                )
            cursor.execute(
                "SELECT id FROM order_intents WHERE agent_cycle_id = %s ORDER BY created_at, id",
                (claim.cycle_id,),
            )
            persisted_ids = {uuid.UUID(str(row[0])) for row in cursor.fetchall()}
        if payload_ids != persisted_ids:
            raise ProductionCompositionUnavailable(
                "harness checkpoint intent membership differs from persisted intents"
            )
        return persisted_ids

    def pending_intents(
        self, claim: CycleClaim, frozen: Mapping[str, object]
    ) -> tuple[_TradingContext, ...]:
        book_ids = _uuids(frozen, "order_book_snapshot_ids")
        market_snapshot_ids = _uuids(frozen, "market_snapshot_ids")
        if not book_ids or not market_snapshot_ids:
            raise ProductionCompositionUnavailable(
                "broker requires current-cycle market and order-book memberships"
            )
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT oi.id, oi.market_id, oi.outcome_id, oi.side, oi.shares, "
                "oi.created_at, ms.payload, ms.volume_micros, ms.liquidity_micros, "
                "ms.status, obs.id, obs.cutoff, obs.source_created_at, obs.bids, obs.asks, "
                "obs.raw_artifact_uri, obs.raw_sha256, o.venue_token_id "
                "FROM order_intents oi JOIN markets m ON m.id = oi.market_id "
                "JOIN outcomes o ON o.id = oi.outcome_id "
                "JOIN market_snapshots ms ON ms.market_id = m.id "
                "JOIN order_book_snapshots obs ON obs.outcome_id = o.id "
                "LEFT JOIN orders existing ON existing.intent_id = oi.id "
                "WHERE oi.agent_cycle_id = %s AND existing.id IS NULL "
                "AND ms.id = ANY(%s::uuid[]) AND obs.id = ANY(%s::uuid[]) "
                "AND ms.cutoff <= %s AND obs.cutoff <= %s "
                "AND ms.status = 'open' "
                "AND COALESCE((ms.payload->>'tradeable')::boolean, false) "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(ms.payload->'outcomes') frozen "
                "WHERE frozen->>'venue_token_id' = o.venue_token_id "
                "AND COALESCE((frozen->>'tradeable')::boolean, false)) "
                "ORDER BY oi.created_at, oi.id",
                (
                    claim.cycle_id,
                    list(market_snapshot_ids),
                    list(book_ids),
                    _cutoff(claim),
                    _cutoff(claim),
                ),
            )
            rows = cursor.fetchall()
        return tuple(self._trading_context(row, claim.agent_id) for row in rows)

    @staticmethod
    def _trading_context(row: Sequence[object], agent_id: uuid.UUID) -> _TradingContext:
        intent_id = uuid.UUID(str(row[0]))
        market_id = uuid.UUID(str(row[1]))
        outcome_id = uuid.UUID(str(row[2]))
        if row[4] is None:
            raise ProductionCompositionUnavailable("order intent lacks normalized shares")
        payload = _mapping(row[6])
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, list):
            raise ProductionCompositionUnavailable("market snapshot outcomes are malformed")
        token_id = str(row[17])
        outcome_payload: dict[str, Any] | None = None
        for candidate in raw_outcomes:
            if isinstance(candidate, Mapping) and candidate.get("venue_token_id") == token_id:
                outcome_payload = _mapping(candidate)
                break
        if outcome_payload is None:
            raise ProductionCompositionUnavailable(
                "intent outcome is absent from its current-cycle market snapshot"
            )
        metadata = _mapping(payload.get("metadata"))
        outcome = Outcome(
            str(outcome_id),
            str(market_id),
            _required_payload_string(outcome_payload, "name"),
            token_id,
            None,
            None,
            MicroDollars(int(Decimal(str(outcome_payload["tick_size"])) * Decimal(1_000_000))),
            MicroDollars(
                int(Decimal(str(outcome_payload["minimum_order_size"])) * Decimal(1_000_000))
            ),
            (
                int(str(outcome_payload["outcome_index"]))
                if outcome_payload.get("outcome_index") is not None
                else None
            ),
            (
                Decimal(str(outcome_payload["indicative_price"]))
                if outcome_payload.get("indicative_price") is not None
                else None
            ),
            bool(outcome_payload.get("tradeable")),
            _mapping(outcome_payload.get("metadata")),
        )
        market = Market(
            str(market_id),
            _required_payload_string(payload, "venue_market_id"),
            _required_payload_string(payload, "event_id"),
            _required_payload_string(payload, "question"),
            str(payload.get("resolution_rules") or ""),
            _optional_payload_timestamp(payload.get("opens_at")),
            _optional_payload_timestamp(payload.get("closes_at")),
            MarketStatus(str(row[9])),
            str(payload["category"]) if payload.get("category") is not None else None,
            MicroDollars(int(str(row[7]))),
            MicroDollars(int(str(row[8]))),
            metadata,
            _required_payload_string(payload, "slug"),
            (
                str(payload["resolution_source"])
                if payload.get("resolution_source") is not None
                else None
            ),
            bool(payload.get("tradeable")),
            (outcome,),
            _optional_payload_timestamp(payload.get("observed_at")) or cast(datetime, row[11]),
            _optional_payload_timestamp(payload.get("source_updated_at")),
        )
        bids = _levels(row[13])
        asks = _levels(row[14])
        artifact = RawArtifact(str(row[16]), 0, str(row[15]))
        book = OrderBookSnapshot(
            token_id,
            str(metadata.get("condition_id") or ""),
            cast(datetime, row[11]),
            cast(datetime | None, row[12]),
            bids,
            asks,
            Decimal(str(outcome_payload["tick_size"])),
            Decimal(str(outcome_payload["minimum_order_size"])),
            bool(_mapping(outcome_payload.get("metadata")).get("negative_risk", False)),
            artifact,
        )
        order = PaperOrder(
            str(intent_id),
            str(agent_id),
            str(market_id),
            str(outcome_id),
            Side(str(row[3])),
            Decimal(str(row[4])),
            cast(datetime, row[5]),
        )
        return _TradingContext(
            intent_id,
            market_id,
            outcome_id,
            order,
            market,
            outcome,
            book,
            uuid.UUID(str(row[10])),
        )

    def portfolio(self, agent_id: uuid.UUID) -> PortfolioState:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(sum(lp.amount_micros) FILTER "
                "(WHERE lp.account = 'cash'), 0), a.portfolio_version FROM agents a "
                "LEFT JOIN ledger_entries le ON le.agent_id = a.id "
                "LEFT JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
                "WHERE a.id = %s GROUP BY a.id",
                (agent_id,),
            )
            account = cursor.fetchone()
            cursor.execute(
                "SELECT m.id, p.outcome_id, p.shares, p.average_cost, "
                "p.cost_basis_micros, p.realized_pnl_micros FROM positions p "
                "JOIN outcomes o ON o.id = p.outcome_id "
                "JOIN markets m ON m.id = o.market_id "
                "WHERE p.agent_id = %s AND p.shares > 0 ORDER BY p.outcome_id",
                (agent_id,),
            )
            positions = cursor.fetchall()
        if account is None:
            raise ProductionCompositionUnavailable("agent portfolio is missing")
        return PortfolioState(
            str(agent_id),
            MicroDollars(int(str(account[0]))),
            tuple(
                PositionState(
                    str(row[0]),
                    str(row[1]),
                    Decimal(str(row[2])),
                    Decimal(str(row[3])),
                    MicroDollars(int(str(row[4]))),
                    MicroDollars(int(str(row[5]))),
                )
                for row in positions
            ),
            (),
            int(str(account[1])),
        )

    def executable_bids(
        self,
        portfolio: PortfolioState,
        *,
        cutoff: datetime,
        order_book_snapshot_ids: Sequence[uuid.UUID],
    ) -> dict[str, ArchivedBid | None]:
        if not portfolio.positions:
            return {}
        outcomes = [uuid.UUID(row.outcome_id) for row in portfolio.positions]
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT ON (obs.outcome_id) obs.outcome_id, obs.best_bid, obs.cutoff "
                "FROM order_book_snapshots obs WHERE obs.outcome_id = ANY(%s::uuid[]) "
                "AND obs.id = ANY(%s::uuid[]) AND obs.cutoff <= %s "
                "ORDER BY obs.outcome_id, obs.cutoff DESC, obs.id DESC",
                (outcomes, list(order_book_snapshot_ids), cutoff),
            )
            rows = cursor.fetchall()
        found = {
            str(row[0]): (
                ArchivedBid(Decimal(str(row[1])), cast(datetime, row[2]))
                if row[1] is not None
                else None
            )
            for row in rows
        }
        return {
            position.outcome_id: found.get(position.outcome_id) for position in portfolio.positions
        }

    def archived_executable_bids(
        self,
        portfolio: PortfolioState,
        *,
        cutoff: datetime,
        maximum_bid_age: timedelta,
    ) -> tuple[dict[str, ArchivedBid | None], tuple[uuid.UUID, ...]]:
        if not portfolio.positions:
            return {}, ()
        outcomes = [uuid.UUID(row.outcome_id) for row in portfolio.positions]
        oldest = cutoff - maximum_bid_age
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT ON (obs.outcome_id) obs.outcome_id, obs.best_bid, "
                "obs.cutoff, obs.id FROM order_book_snapshots obs "
                "WHERE obs.outcome_id = ANY(%s::uuid[]) AND obs.best_bid IS NOT NULL "
                "AND obs.cutoff <= %s AND obs.cutoff >= %s "
                "AND (obs.source_created_at IS NULL OR obs.source_created_at <= %s) "
                "ORDER BY obs.outcome_id, obs.cutoff DESC, obs.id DESC",
                (outcomes, cutoff, oldest, cutoff),
            )
            rows = cursor.fetchall()
        found = {
            str(row[0]): ArchivedBid(Decimal(str(row[1])), cast(datetime, row[2])) for row in rows
        }
        return (
            {
                position.outcome_id: found.get(position.outcome_id)
                for position in portfolio.positions
            },
            tuple(uuid.UUID(str(row[3])) for row in rows),
        )


class ProductionBrokerPort:
    def __init__(
        self,
        database_url: str,
        market_repository: PostgresMarketDataRepository,
        *,
        clock: Callable[[], datetime],
        maximum_market_fraction: Decimal,
        maximum_bid_age: timedelta,
        connect: _Connect | None = None,
    ) -> None:
        self._database_url = database_url
        self._market_repository = market_repository
        self._clock = clock
        self._state = _PostgresTradingState(database_url, connect=connect)
        self._repository = PostgresBrokerRepository(database_url, connect=connect)
        self._broker = PredictionArenaPaperBroker(
            policy=PaperPolicy.PREDICTIONARENA_UNCONDITIONAL,
            maximum_market_cost_basis_fraction=maximum_market_fraction,
            maximum_book_age=maximum_bid_age,
            maximum_valuation_bid_age=maximum_bid_age,
        )

    def execute(
        self, claim: CycleClaim, frozen: JsonObject, harness: JsonObject
    ) -> BrokerExecutionResult:
        allowed_intents = self._state.persisted_harness_intents(claim, harness)
        if not allowed_intents:
            return BrokerExecutionResult({"order_ids": [], "rejections": []}, (), 0)
        fee_ids = _uuids(frozen, "fee_rate_snapshot_ids")
        book_ids = _uuids(frozen, "order_book_snapshot_ids")
        if not fee_ids:
            raise ProductionCompositionUnavailable("cycle has no frozen fee-rate membership")
        created: list[str] = []
        rejected: list[JsonObject] = []
        accepted = 0
        for item in self._state.pending_intents(claim, frozen):
            if item.intent_id not in allowed_intents:
                raise ProductionCompositionUnavailable("broker encountered a foreign cycle intent")
            portfolio = self._state.portfolio(claim.agent_id)
            bids = self._state.executable_bids(
                portfolio,
                cutoff=_cutoff(claim),
                order_book_snapshot_ids=book_ids,
            )
            fee = self._market_repository.frozen_fee_policy(
                item.outcome.venue_token_id,
                cutoff=_cutoff(claim),
                fee_rate_snapshot_ids=fee_ids,
            )
            result = self._broker.place(
                item.order,
                market=item.market,
                outcome=item.outcome,
                snapshot=item.book,
                portfolio=portfolio,
                executable_bids=bids,
                fee_policy=fee,
                now=_aware(self._clock()),
            )
            persisted = self._repository.persist_execution(
                result,
                agent_id=claim.agent_id,
                intent_id=item.intent_id,
                market_id=item.market_id,
                outcome_id=item.outcome_id,
                snapshot_id=item.book_snapshot_id,
            )
            created.append(str(persisted.record_id))
            if result.status is ExecutionStatus.REJECTED:
                rejected.append(
                    {
                        "intent_id": str(item.intent_id),
                        "code": result.rejection_code.value if result.rejection_code else None,
                    }
                )
            else:
                accepted += 1
        return BrokerExecutionResult(
            {"order_ids": created, "rejections": rejected},
            (),
            accepted,
        )


class ProductionSettlementValuationPort:
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
        self._state = _PostgresTradingState(database_url, connect=connect)
        self._repository = PostgresBrokerRepository(database_url, connect=connect)

    def settle_before_prompt(self, claim: CycleClaim, frozen: JsonObject) -> PreSettlementResult:
        cutoff = _cutoff(claim)
        settled_ids = self._settle_eligible(claim.agent_id, frozen, cutoff)
        return PreSettlementResult(
            {
                "settlement_ids": settled_ids,
                "settlement_cutoff": cutoff.isoformat(),
            },
            (),
            len(settled_ids),
        )

    def settle_and_value(
        self, claim: CycleClaim, frozen: JsonObject, broker: JsonObject
    ) -> SettlementValuationResult:
        del broker
        cutoff = _cutoff(claim)
        settled_ids = self._settle_eligible(claim.agent_id, frozen, cutoff)
        portfolio = self._state.portfolio(claim.agent_id)
        bids, valuation_book_ids = self._state.archived_executable_bids(
            portfolio,
            cutoff=cutoff,
            maximum_bid_age=self._maximum_bid_age,
        )
        account_value = int(
            portfolio.account_value_micros(
                bids,
                as_of=cutoff,
                maximum_bid_age=self._maximum_bid_age,
            )
        )
        liquidation = account_value - int(portfolio.cash_micros)
        basis = sum(int(position.cost_basis_micros) for position in portfolio.positions)
        realized = self._realized_pnl(claim.agent_id)
        unrealized = liquidation - basis
        mismatch = self._ledger_mismatch(claim.agent_id)
        calculated = _aware(self._clock())
        self._persist_performance(
            claim,
            cash=int(portfolio.cash_micros),
            liquidation=liquidation,
            account_value=account_value,
            realized=realized,
            unrealized=unrealized,
            calculated=calculated,
            settlement_ids=settled_ids,
            bid_ids=valuation_book_ids,
        )
        peak = self._peak_account_value(claim.agent_id, account_value)
        return SettlementValuationResult(
            {
                "settlement_ids": settled_ids,
                "performance_snapshot_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:performance:{claim.cycle_id}")
                ),
                "valuation_cutoff": cutoff.isoformat(),
            },
            (),
            account_value,
            peak,
            mismatch,
        )

    def _settle_eligible(
        self,
        agent_id: uuid.UUID,
        frozen: Mapping[str, object],
        cutoff: datetime,
    ) -> list[str]:
        settled_ids: list[str] = []
        for row in self._settlement_candidates(agent_id, frozen, cutoff):
            portfolio = self._state.portfolio(agent_id)
            outcome_id = str(row[2])
            position = portfolio.position(outcome_id)
            if position is None:
                continue
            observation = SettlementObservation(
                str(row[0]),
                str(row[1]),
                str(row[3]) if row[3] is not None else None,
                cast(datetime, row[4]),
                cast(datetime, row[5]),
                cast(datetime, row[6]),
            )
            result = SettlementEngine().settle(
                resolution=observation,
                position=position,
                portfolio=portfolio,
                as_of=cutoff,
                settled_at=_aware(self._clock()),
            )
            persisted = self._repository.persist_settlement(
                result,
                agent_id=agent_id,
                position_id=uuid.UUID(str(row[7])),
                resolution_id=uuid.UUID(str(row[0])),
                market_id=uuid.UUID(str(row[1])),
                outcome_id=uuid.UUID(str(row[2])),
            )
            settled_ids.append(str(persisted.record_id))
        return settled_ids

    def _settlement_candidates(
        self,
        agent_id: uuid.UUID,
        frozen: Mapping[str, object],
        cutoff: datetime,
    ) -> Sequence[Sequence[object]]:
        resolution_ids = _uuids(frozen, "resolution_ids")
        if not resolution_ids:
            return ()
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT r.id, r.market_id, p.outcome_id, r.winning_outcome_id, "
                "r.source_created_at, r.observed_at, r.eligible_after, p.id "
                "FROM resolutions r JOIN outcomes o ON o.market_id = r.market_id "
                "JOIN positions p ON p.outcome_id = o.id "
                "LEFT JOIN settlements s ON s.position_id = p.id AND s.resolution_id = r.id "
                "WHERE p.agent_id = %s AND p.shares > 0 AND s.id IS NULL "
                "AND r.id = ANY(%s::uuid[]) AND r.observed_at <= %s "
                "AND r.source_created_at <= %s AND r.eligible_after <= %s "
                "ORDER BY r.observed_at, r.id, p.id",
                (agent_id, list(resolution_ids), cutoff, cutoff, cutoff),
            )
            return tuple(cursor.fetchall())

    def _realized_pnl(self, agent_id: uuid.UUID) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(sum(realized_pnl_micros), 0) FROM positions WHERE agent_id = %s",
                (agent_id,),
            )
            row = cursor.fetchone()
        return int(str(row[0])) if row else 0

    def _ledger_mismatch(self, agent_id: uuid.UUID) -> int:
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "WITH ledger AS (SELECT lp.outcome_id, sum(lp.amount_micros) AS basis "
                "FROM ledger_postings lp JOIN ledger_entries le ON le.id = lp.ledger_entry_id "
                "WHERE le.agent_id = %s AND lp.account = 'position_cost' "
                "GROUP BY lp.outcome_id), cached AS (SELECT outcome_id, cost_basis_micros "
                "FROM positions WHERE agent_id = %s) "
                "SELECT COALESCE(sum(abs(COALESCE(ledger.basis, 0) - "
                "COALESCE(cached.cost_basis_micros, 0))), 0) FROM ledger FULL JOIN cached "
                "USING (outcome_id)",
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
        calculated: datetime,
        settlement_ids: Sequence[str],
        bid_ids: Sequence[uuid.UUID],
    ) -> None:
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:performance:{claim.cycle_id}")
        calculation = {
            "valuation_policy": "latest_archived_executable_bid_max_age_300_seconds",
            "valuation_cutoff": _cutoff(claim).isoformat(),
            "settlement_ids": list(settlement_ids),
            "eligible_order_book_snapshot_ids": [str(value) for value in bid_ids],
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
                    identifier,
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


@dataclass(frozen=True, slots=True)
class ProductionWorker:
    runtime: HourlyRuntime
    retention: RetentionCleaner
    projection: ProjectionService
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    sleeper: Callable[[float], None]

    def run_once(self) -> RuntimeTickResult:
        result = self.runtime.tick()
        self.retention.run_once()
        return result

    def run_forever(
        self,
        *,
        poll_seconds: float = 30.0,
        projection_seconds: float = 3_600.0,
    ) -> None:
        if poll_seconds <= 0 or projection_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        last_maintenance = self.monotonic() - projection_seconds
        while True:
            self.runtime.tick()
            now = self.monotonic()
            if now - last_maintenance >= projection_seconds:
                self.retention.run_once()
                self.projection.calculate()
                last_maintenance = now
            self.sleeper(poll_seconds)


def build_production_worker(
    config: ExperimentConfig,
    *,
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProductionWorker:
    config.assert_runnable()
    _verify_frozen_artifact(config.raw, "tool_schemas")
    try:
        values = (
            dict(environment)
            if environment is not None
            else required_environment(
                (
                    "VTRADE_DATABASE_URL",
                    "VTRADE_SUPABASE_URL",
                    "VTRADE_SUPABASE_BUCKET",
                    "VTRADE_SUPABASE_SERVICE_ROLE_KEY",
                    "VTRADE_OPENROUTER_API_KEY",
                    "VTRADE_EXA_API_KEY",
                )
            )
        )
    except ConfigurationError as exc:
        raise ProductionCompositionUnavailable(str(exc)) from exc
    missing = [
        name
        for name in (
            "VTRADE_DATABASE_URL",
            "VTRADE_SUPABASE_URL",
            "VTRADE_SUPABASE_BUCKET",
            "VTRADE_SUPABASE_SERVICE_ROLE_KEY",
            "VTRADE_OPENROUTER_API_KEY",
            "VTRADE_EXA_API_KEY",
        )
        if not values.get(name) or values.get(name) == "REQUIRED"
    ]
    if missing:
        raise ProductionCompositionUnavailable(
            f"missing required production resources: {', '.join(missing)}"
        )
    database_url = values["VTRADE_DATABASE_URL"]
    store = SupabaseArtifactStore(
        values["VTRADE_SUPABASE_URL"],
        values["VTRADE_SUPABASE_BUCKET"],
        values["VTRADE_SUPABASE_SERVICE_ROLE_KEY"],
    )
    limits = _harness_limits(config.raw)
    budget = PostgresBudgetGuard(
        database_url,
        limit_micros=_integer(config.raw["limits"], "monthly_external_api_budget_micros"),
        thresholds=cast(
            tuple[int, int, int],
            tuple(int(value) for value in config.raw["limits"]["budget_alert_micros"]),
        ),
        clock=clock,
    )
    gateway = OpenRouterModelGateway(
        values["VTRADE_OPENROUTER_API_KEY"], store, budget, clock=clock
    )
    exa = ExaResearchProvider(values["VTRADE_EXA_API_KEY"], store, budget, clock=clock)
    source_skew = float(config.raw["limits"]["maximum_source_clock_skew_seconds"])
    venue = PolymarketVenue(
        store,
        clock=clock,
        maximum_source_clock_skew_seconds=source_skew,
    )
    market_repository = PostgresMarketDataRepository(database_url)
    repository = PostgresRuntimeRepository(database_url)
    maximum_bid_age = timedelta(
        seconds=_integer(config.raw["limits"], "maximum_archived_bid_age_seconds")
    )
    settlement_valuation = ProductionSettlementValuationPort(
        database_url,
        clock=clock,
        maximum_bid_age=maximum_bid_age,
    )
    orchestrator = CycleOrchestrator(
        repository=repository,
        market_freezer=PolymarketFreezeService(
            venue,
            market_repository,
            clock=clock,
        ),
        pre_settlement=settlement_valuation,
        prompt=ProductionPromptPort(database_url, store, clock=clock),
        harness=ProductionHarnessPort(
            database_url,
            store,
            gateway,
            exa,
            limits,
            clock=clock,
            monotonic=monotonic,
            schema_path=str(config.raw["artifacts"]["tool_schemas"]["path"]),
        ),
        broker=ProductionBrokerPort(
            database_url,
            market_repository,
            clock=clock,
            maximum_market_fraction=Decimal(
                str(config.raw["limits"]["maximum_market_cost_basis_fraction"])
            ),
            maximum_bid_age=maximum_bid_age,
        ),
        settlement_valuation=settlement_valuation,
        clock=clock,
        alert_policy=RuntimeAlertPolicy(
            maximum_data_age=maximum_bid_age,
            monthly_budget_micros=_integer(
                config.raw["limits"], "monthly_external_api_budget_micros"
            ),
        ),
    )
    lease_owner = values.get("VTRADE_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
    runtime = HourlyRuntime(
        repository=repository,
        orchestrator=orchestrator,
        lease_owner=lease_owner,
        clock=clock,
        batch_size=1,
    )
    return ProductionWorker(
        runtime,
        RetentionCleaner(
            repository=repository,
            deletion=store,
            lease_owner=f"{lease_owner}:retention",
            clock=clock,
        ),
        ProjectionService(repository=repository, clock=clock),
        clock,
        monotonic,
        sleeper,
    )


def run_worker(
    config_path: str | Path,
    *,
    worker: ProductionWorker | None = None,
    environment: Mapping[str, str] | None = None,
    forever: bool = False,
) -> RuntimeTickResult | None:
    config = load_experiment_config(config_path)
    config.assert_runnable()
    application = worker or build_production_worker(config, environment=environment)
    if forever:
        application.run_forever(
            poll_seconds=float(os.getenv("VTRADE_WORKER_POLL_SECONDS", "30")),
            projection_seconds=float(os.getenv("VTRADE_WORKER_PROJECTION_SECONDS", "3600")),
        )
        return None
    return application.run_once()


def main() -> None:
    config_path = os.getenv(
        "VTRADE_EXPERIMENT_CONFIG", "config/experiments/predictionarena-polymarket-v1.json"
    )
    try:
        run_worker(config_path, forever=True)
    except KeyboardInterrupt:
        return


def _harness_limits(raw: Mapping[str, Any]) -> HarnessLimits:
    limits = raw.get("limits")
    if not isinstance(limits, Mapping):
        raise ProductionCompositionUnavailable("experiment limits are missing")
    return HarnessLimits(
        _integer(limits, "maximum_model_turns"),
        _integer(limits, "maximum_total_tool_calls"),
        _integer(limits, "maximum_web_searches_per_cycle"),
        float(limits["maximum_cycle_wall_clock_seconds"]),
        _integer(limits, "maximum_model_context_tokens"),
        _integer(limits, "maximum_assembled_input_tokens"),
        _integer(limits, "reserved_model_output_tokens"),
        _integer(limits, "maximum_tool_call_argument_tokens"),
        _integer(limits, "default_maximum_tool_result_tokens"),
        _integer(limits, "get_portfolio_maximum_tool_result_tokens"),
    )


def _verify_frozen_artifact(raw: Mapping[str, object], name: str) -> None:
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get(name), Mapping):
        raise ProductionCompositionUnavailable(f"frozen artifact {name} is missing")
    definition = cast(Mapping[str, object], artifacts[name])
    path = definition.get("path")
    expected = definition.get("sha256")
    if not isinstance(path, str) or not isinstance(expected, str) or len(expected) != 64:
        raise ProductionCompositionUnavailable(f"frozen artifact {name} is malformed")
    try:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ProductionCompositionUnavailable(f"cannot read frozen artifact {name}") from exc
    if actual != expected:
        raise ProductionCompositionUnavailable(f"frozen artifact {name} hash mismatch")


def _integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ProductionCompositionUnavailable(f"configuration field {key} must be integer")
    return result


def _cutoff(claim: CycleClaim) -> datetime:
    if claim.data_cutoff is None:
        raise ProductionCompositionUnavailable("cycle cutoff is not finalized")
    return _aware(claim.data_cutoff)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _strings(value: Mapping[str, object], key: str) -> list[str]:
    rows = value.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
        raise ProductionCompositionUnavailable(f"stage payload lacks string list {key}")
    return cast(list[str], rows)


def _uuids(value: Mapping[str, object], key: str) -> tuple[uuid.UUID, ...]:
    try:
        rows = tuple(uuid.UUID(item) for item in _strings(value, key))
    except ValueError as exc:
        raise ProductionCompositionUnavailable(f"stage payload has malformed {key}") from exc
    if len(set(rows)) != len(rows):
        raise ProductionCompositionUnavailable(f"stage payload has duplicate {key}")
    return rows


def _mapping(value: object) -> dict[str, Any]:
    return {str(key): child for key, child in value.items()} if isinstance(value, Mapping) else {}


def _levels(value: object) -> tuple[PriceLevel, ...]:
    if not isinstance(value, list):
        raise ProductionCompositionUnavailable("frozen order-book levels are malformed")
    levels: list[PriceLevel] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ProductionCompositionUnavailable("frozen order-book level is malformed")
        levels.append(PriceLevel(Decimal(str(row.get("price"))), Decimal(str(row.get("size")))))
    return tuple(levels)


def _belief(row: Mapping[str, object], agent_id: uuid.UUID) -> BeliefRecord:
    evidence = row.get("evidence", [])
    return BeliefRecord(
        str(row["id"]),
        str(agent_id),
        Decimal(str(row["probability"])),
        str(row["content"]),
        str(row["category"]),
        tuple(str(value) for value in evidence) if isinstance(evidence, list) else (),
        _parse_timestamp(row["created_at"]),
    )


def _required_payload_string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProductionCompositionUnavailable(f"market snapshot lacks {key}")
    return result


def _optional_payload_timestamp(value: object) -> datetime | None:
    return _parse_timestamp(value) if value is not None else None


def _optional_research_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _parse_timestamp(value)
    except ProductionCompositionUnavailable:
        return None


def _plan(row: Mapping[str, object], agent_id: uuid.UUID) -> PlanRecord:
    due = row.get("due_at")
    return PlanRecord(
        str(row["id"]),
        str(agent_id),
        PlanType(str(row["plan_type"])),
        str(row["content"]),
        _parse_timestamp(due) if due is not None else None,
        _parse_timestamp(row["created_at"]),
    )


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as exc:
        raise ProductionCompositionUnavailable("persisted timestamp is malformed") from exc


def _registration(reference: Any, retained: datetime) -> ArtifactRegistration:
    return ArtifactRegistration(
        str(reference.uri),
        str(reference.sha256),
        int(reference.byte_length),
        retained,
    )


def _deduplicated_registrations(
    registrations: Sequence[ArtifactRegistration],
) -> tuple[ArtifactRegistration, ...]:
    unique: dict[tuple[str, str], ArtifactRegistration] = {}
    for registration in registrations:
        unique[(registration.uri, registration.sha256)] = registration
    return tuple(unique.values())


def _harness_artifact_registrations(
    transcript: Any,
    telemetry: Sequence[ProviderTelemetry],
    retained: datetime,
) -> tuple[ArtifactRegistration, ...]:
    return _deduplicated_registrations(
        (
            _registration(transcript, retained),
            *(
                ArtifactRegistration(
                    row.artifact_uri,
                    row.raw_sha256,
                    row.artifact_byte_length,
                    retained,
                )
                for row in telemetry
            ),
        )
    )


if __name__ == "__main__":
    main()
