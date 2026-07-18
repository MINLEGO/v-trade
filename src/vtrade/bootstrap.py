from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from vtrade.config import ExperimentConfig, canonical_json, load_experiment_config


class BootstrapError(RuntimeError):
    """Raised when persisted experiment state differs from the frozen inputs."""


class _Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class FrozenRegistration:
    definition_id: uuid.UUID
    run_id: uuid.UUID
    prompt_id: uuid.UUID
    model_ids: tuple[uuid.UUID, ...]


class PostgresExperimentBootstrap:
    """Idempotently register and explicitly activate a frozen experiment cohort."""

    def __init__(
        self,
        database_url: str,
        *,
        connect: _Connect | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._database_url = database_url
        self._connect = connect or _default_connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        config: ExperimentConfig,
        prompt_body: str,
        code_version: str,
        run_label: str,
        starts_at: datetime,
        version_number: int = 1,
    ) -> FrozenRegistration:
        config.assert_runnable()
        starts_at = _aware(starts_at)
        if not code_version or not run_label or version_number <= 0:
            raise ValueError("code version, run label, and positive version number are required")
        prompt = config.raw["artifacts"]["prompt"]
        prompt_sha256 = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()
        if prompt_sha256 != str(prompt["sha256"]):
            raise BootstrapError("prompt bytes differ from the hash frozen in experiment config")
        now = _aware(self._clock())
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"vtrade:bootstrap:{config.version}",),
            )
            definition_id = self._definition(
                cursor, config, code_version, version_number, now
            )
            prompt_id = self._prompt(cursor, definition_id, prompt_body, prompt, now)
            model_ids = tuple(
                self._model(cursor, definition_id, model, now)
                for model in config.raw["models"]
            )
            run_id = self._run(cursor, definition_id, run_label, starts_at, now)
        return FrozenRegistration(definition_id, run_id, prompt_id, model_ids)

    def add_agent(
        self,
        *,
        experiment_version: str,
        run_label: str,
        model_label: str,
        name: str,
        initial_cash_micros: int,
    ) -> uuid.UUID:
        if (
            not experiment_version
            or not run_label
            or not model_label
            or not name
            or initial_cash_micros <= 0
        ):
            raise ValueError("run, model, name, and positive initial cash are required")
        now = _aware(self._clock())
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT runs.id, configs.id, "
                "(definitions.definition #>> '{capital,initial_balance_micros}')::bigint "
                "FROM experiment_runs runs "
                "JOIN experiment_definitions definitions ON definitions.id = runs.definition_id "
                "JOIN model_configs configs ON configs.definition_id = runs.definition_id "
                "WHERE definitions.experiment_version = %s AND runs.run_label = %s "
                "AND configs.label = %s FOR UPDATE OF runs",
                (experiment_version, run_label, model_label),
            )
            row = cursor.fetchone()
            if row is None:
                raise BootstrapError("unknown run/model pair")
            run_id, model_id = uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))
            if int(str(row[2])) != initial_cash_micros:
                raise BootstrapError("initial cash differs from the frozen experiment config")
            cursor.execute(
                "SELECT id, model_config_id, initial_cash_micros, created_at FROM agents "
                "WHERE run_id = %s AND name = %s FOR UPDATE",
                (run_id, name),
            )
            existing = cursor.fetchone()
            if existing is not None:
                mismatched = (
                    uuid.UUID(str(existing[1])) != model_id
                    or int(str(existing[2])) != initial_cash_micros
                )
                if mismatched:
                    raise BootstrapError(
                        "existing agent differs from requested frozen registration"
                    )
                agent_id = uuid.UUID(str(existing[0]))
                self._ensure_initial_capital(
                    cursor,
                    agent_id=agent_id,
                    initial_cash_micros=initial_cash_micros,
                    occurred_at=_aware(cast(datetime, existing[3])),
                )
                return agent_id
            agent_id = uuid.uuid4()
            cursor.execute(
                "INSERT INTO agents "
                "(id, run_id, model_config_id, name, initial_cash_micros, paused_at, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (agent_id, run_id, model_id, name, initial_cash_micros, now, now),
            )
            self._ensure_initial_capital(
                cursor,
                agent_id=agent_id,
                initial_cash_micros=initial_cash_micros,
                occurred_at=now,
            )
            cursor.execute(
                "INSERT INTO agent_runtime_schedules "
                "(agent_id, next_scheduled_at, enabled, created_at, updated_at) "
                "VALUES (%s, %s, false, %s, %s)",
                (agent_id, now, now, now),
            )
            return agent_id

    @staticmethod
    def _ensure_initial_capital(
        cursor: _Cursor,
        *,
        agent_id: uuid.UUID,
        initial_cash_micros: int,
        occurred_at: datetime,
    ) -> None:
        """Create or verify the balanced owner-equity event for ledger-only replay."""
        key = f"initial-capital:{agent_id}"
        cursor.execute(
            "SELECT le.agent_id, le.event_type, count(lp.id), "
            "COALESCE(sum(lp.amount_micros), 0), "
            "COALESCE(sum(lp.amount_micros) FILTER (WHERE lp.account = 'cash'), 0), "
            "COALESCE(sum(lp.amount_micros) FILTER (WHERE lp.account = 'owner_equity'), 0) "
            "FROM ledger_entries le JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
            "WHERE le.idempotency_key = %s GROUP BY le.id, le.agent_id, le.event_type",
            (key,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            matches = (
                uuid.UUID(str(existing[0])) == agent_id
                and str(existing[1]) == "initial_capital"
                and int(str(existing[2])) == 2
                and int(str(existing[3])) == 0
                and int(str(existing[4])) == initial_cash_micros
                and int(str(existing[5])) == -initial_cash_micros
            )
            if not matches:
                raise BootstrapError("existing initial-capital ledger event differs")
            return
        ledger_id = uuid.uuid5(uuid.NAMESPACE_URL, f"vtrade:ledger:{key}")
        cursor.execute(
            "INSERT INTO ledger_entries "
            "(id, agent_id, event_type, source_table, source_id, idempotency_key, occurred_at) "
            "VALUES (%s, %s, 'initial_capital', 'agents', %s, %s, %s)",
            (ledger_id, agent_id, agent_id, key, occurred_at),
        )
        cursor.execute(
            "INSERT INTO ledger_postings (ledger_entry_id, account, amount_micros) "
            "VALUES (%s, 'cash', %s), (%s, 'owner_equity', %s)",
            (ledger_id, initial_cash_micros, ledger_id, -initial_cash_micros),
        )

    def start_agent(
        self, *, experiment_version: str, run_label: str, name: str, starts_at: datetime
    ) -> None:
        starts_at = _aware(starts_at)
        now = _aware(self._clock())
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT agents.id, runs.id, runs.status FROM agents "
                "JOIN experiment_runs runs ON runs.id = agents.run_id "
                "JOIN experiment_definitions definitions ON definitions.id = runs.definition_id "
                "WHERE definitions.experiment_version = %s AND runs.run_label = %s "
                "AND agents.name = %s FOR UPDATE OF agents, runs",
                (experiment_version, run_label, name),
            )
            row = cursor.fetchone()
            if row is None:
                raise BootstrapError("unknown run/agent pair")
            if str(row[2]) in {"completed", "failed"}:
                raise BootstrapError("a terminal experiment run cannot be restarted")
            agent_id, run_id = uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))
            cursor.execute("UPDATE agents SET paused_at = NULL WHERE id = %s", (agent_id,))
            cursor.execute(
                "UPDATE agent_runtime_schedules SET enabled = true, next_scheduled_at = %s, "
                "updated_at = %s WHERE agent_id = %s",
                (starts_at, now, agent_id),
            )
            if cursor.rowcount != 1:
                raise BootstrapError("agent schedule is missing")
            cursor.execute(
                "UPDATE experiment_runs SET status = 'running' "
                "WHERE id = %s AND status IN ('ready', 'paused', 'running')",
                (run_id,),
            )

    def remove_agent(self, *, experiment_version: str, run_label: str, name: str) -> None:
        """Soft-remove an agent so its immutable history remains auditable."""
        now = _aware(self._clock())
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT agents.id FROM agents JOIN experiment_runs runs ON runs.id = agents.run_id "
                "JOIN experiment_definitions definitions ON definitions.id = runs.definition_id "
                "WHERE definitions.experiment_version = %s AND runs.run_label = %s "
                "AND agents.name = %s FOR UPDATE OF agents",
                (experiment_version, run_label, name),
            )
            row = cursor.fetchone()
            if row is None:
                raise BootstrapError("unknown run/agent pair")
            agent_id = uuid.UUID(str(row[0]))
            cursor.execute("UPDATE agents SET paused_at = %s WHERE id = %s", (now, agent_id))
            cursor.execute(
                "UPDATE agent_runtime_schedules SET enabled = false, updated_at = %s "
                "WHERE agent_id = %s",
                (now, agent_id),
            )

    @staticmethod
    def _definition(
        cursor: _Cursor,
        config: ExperimentConfig,
        code_version: str,
        version_number: int,
        now: datetime,
    ) -> uuid.UUID:
        cursor.execute(
            "SELECT id, config_sha256, code_version FROM experiment_definitions "
            "WHERE experiment_version = %s AND version_number = %s FOR UPDATE",
            (config.version, version_number),
        )
        row = cursor.fetchone()
        if row is not None:
            if str(row[1]) != config.sha256 or str(row[2]) != code_version:
                raise BootstrapError("existing experiment definition fingerprint differs")
            return uuid.UUID(str(row[0]))
        definition_id = uuid.uuid4()
        cursor.execute(
            "INSERT INTO experiment_definitions "
            "(id, experiment_version, version_number, status, definition, config_sha256, "
            "code_version, created_at) VALUES (%s, %s, %s, 'ready', %s::jsonb, %s, %s, %s)",
            (
                definition_id,
                config.version,
                version_number,
                canonical_json(config.raw).decode("utf-8"),
                config.sha256,
                code_version,
                now,
            ),
        )
        return definition_id

    @staticmethod
    def _prompt(
        cursor: _Cursor,
        definition_id: uuid.UUID,
        prompt_body: str,
        prompt: dict[str, object],
        now: datetime,
    ) -> uuid.UUID:
        name = Path(str(prompt["path"])).name
        sha256 = str(prompt["sha256"])
        cursor.execute(
            "SELECT id, body_sha256 FROM prompt_versions "
            "WHERE definition_id = %s AND name = %s FOR UPDATE",
            (definition_id, name),
        )
        row = cursor.fetchone()
        if row is not None:
            if str(row[1]) != sha256:
                raise BootstrapError("existing prompt fingerprint differs")
            return uuid.UUID(str(row[0]))
        prompt_id = uuid.uuid4()
        cursor.execute(
            "INSERT INTO prompt_versions "
            "(id, definition_id, name, body, body_sha256, classification, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)",
            (
                prompt_id,
                definition_id,
                name,
                prompt_body,
                sha256,
                json.dumps({"source": prompt["classification"]}),
                now,
            ),
        )
        return prompt_id

    @staticmethod
    def _model(
        cursor: _Cursor,
        definition_id: uuid.UUID,
        model: dict[str, object],
        now: datetime,
    ) -> uuid.UUID:
        label, slug = str(model["label"]), str(model["slug"])
        sha256 = hashlib.sha256(canonical_json(model)).hexdigest()
        cursor.execute(
            "SELECT id, model_slug, config_sha256 FROM model_configs "
            "WHERE definition_id = %s AND label = %s FOR UPDATE",
            (definition_id, label),
        )
        row = cursor.fetchone()
        if row is not None:
            if str(row[1]) != slug or str(row[2]) != sha256:
                raise BootstrapError("existing model config fingerprint differs")
            return uuid.UUID(str(row[0]))
        model_id = uuid.uuid4()
        policy_keys = {
            "provider_allowlist",
            "provider_selection",
            "allow_provider_fallbacks",
            "cross_model_fallback",
            "provider_max_price",
            "allowed_quantizations",
        }
        provider_policy = {key: model[key] for key in sorted(policy_keys) if key in model}
        parameters = {key: value for key, value in model.items() if key not in policy_keys}
        cursor.execute(
            "INSERT INTO model_configs "
            "(id, definition_id, label, model_slug, provider_policy, parameters, "
            "config_sha256, created_at) VALUES "
            "(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)",
            (
                model_id,
                definition_id,
                label,
                slug,
                canonical_json(provider_policy).decode("utf-8"),
                canonical_json(parameters).decode("utf-8"),
                sha256,
                now,
            ),
        )
        return model_id

    @staticmethod
    def _run(
        cursor: _Cursor,
        definition_id: uuid.UUID,
        run_label: str,
        starts_at: datetime,
        now: datetime,
    ) -> uuid.UUID:
        cursor.execute(
            "SELECT id, starts_at FROM experiment_runs "
            "WHERE definition_id = %s AND run_label = %s FOR UPDATE",
            (definition_id, run_label),
        )
        row = cursor.fetchone()
        if row is not None:
            if _aware(cast(datetime, row[1])) != starts_at:
                raise BootstrapError("existing experiment run start timestamp differs")
            return uuid.UUID(str(row[0]))
        run_id = uuid.uuid4()
        cursor.execute(
            "INSERT INTO experiment_runs "
            "(id, definition_id, run_label, status, starts_at, created_at) "
            "VALUES (%s, %s, %s, 'ready', %s, %s)",
            (run_id, definition_id, run_label, starts_at, now),
        )
        return run_id


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def main() -> None:
    parser = argparse.ArgumentParser(description="Register and explicitly operate V-Trade agents")
    parser.add_argument("--database-url-env", default="VTRADE_DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--config", required=True)
    register.add_argument("--prompt", required=True)
    register.add_argument("--code-version", required=True)
    register.add_argument("--run-label", required=True)
    register.add_argument("--starts-at", required=True, type=_parse_timestamp)
    register.add_argument("--version-number", type=int, default=1)
    add = sub.add_parser("add-agent")
    add.add_argument("--experiment-version", required=True)
    add.add_argument("--run-label", required=True)
    add.add_argument("--model-label", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--initial-cash-micros", type=int, default=10_000_000_000)
    start = sub.add_parser("start-agent")
    start.add_argument("--experiment-version", required=True)
    start.add_argument("--run-label", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--starts-at", required=True, type=_parse_timestamp)
    remove = sub.add_parser("remove-agent")
    remove.add_argument("--experiment-version", required=True)
    remove.add_argument("--run-label", required=True)
    remove.add_argument("--name", required=True)
    args = parser.parse_args()
    database_url = os.getenv(args.database_url_env)
    if not database_url:
        parser.error(f"environment variable {args.database_url_env} is required")
    service = PostgresExperimentBootstrap(database_url)
    if args.command == "register":
        config = load_experiment_config(args.config)
        body = Path(args.prompt).read_text(encoding="utf-8")
        service.register(
            config=config,
            prompt_body=body,
            code_version=args.code_version,
            run_label=args.run_label,
            starts_at=args.starts_at,
            version_number=args.version_number,
        )
    elif args.command == "add-agent":
        service.add_agent(
            experiment_version=args.experiment_version,
            run_label=args.run_label,
            model_label=args.model_label,
            name=args.name,
            initial_cash_micros=args.initial_cash_micros,
        )
    elif args.command == "start-agent":
        service.start_agent(
            experiment_version=args.experiment_version,
            run_label=args.run_label,
            name=args.name,
            starts_at=args.starts_at,
        )
    else:
        service.remove_agent(
            experiment_version=args.experiment_version,
            run_label=args.run_label,
            name=args.name,
        )


if __name__ == "__main__":
    main()
