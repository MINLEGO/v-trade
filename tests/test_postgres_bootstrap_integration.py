from __future__ import annotations

import copy
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vtrade.bootstrap import PostgresExperimentBootstrap
from vtrade.config import ExperimentConfig, config_hash, load_experiment_config

pytestmark = pytest.mark.skipif(
    os.getenv("VTRADE_RUN_POSTGRES_INTEGRATION") != "1",
    reason="set VTRADE_RUN_POSTGRES_INTEGRATION=1 for rollback-only PostgreSQL verification",
)


def test_real_postgres_bootstrap_is_explicit_and_agent_isolated() -> None:
    import psycopg

    database_url = os.environ["VTRADE_DATABASE_URL"]
    original = load_experiment_config(
        "config/experiments/predictionarena-polymarket-v1.json"
    )
    original.assert_runnable()
    raw = copy.deepcopy(original.raw)
    suffix = uuid.uuid4().hex
    raw["experiment_version"] = f"bootstrap-integration-{suffix}"
    config = ExperimentConfig(raw, config_hash(raw))
    prompt = Path(str(raw["artifacts"]["prompt"]["path"])).read_text(encoding="utf-8")
    now = datetime.now(UTC)
    with psycopg.connect(database_url) as connection:

        @contextmanager
        def connect(_database_url: str) -> Any:
            yield connection

        service = PostgresExperimentBootstrap(database_url, connect=connect, clock=lambda: now)
        registration = service.register(
            config=config,
            prompt_body=prompt,
            code_version=f"integration-{suffix}",
            run_label=f"shadow-{suffix}",
            starts_at=now,
        )
        first = service.add_agent(
            experiment_version=raw["experiment_version"],
            run_label=f"shadow-{suffix}",
            model_label="DeepSeek V4 Flash",
            name=f"deepseek-{suffix}",
            initial_cash_micros=10_000_000_000,
        )
        second = service.add_agent(
            experiment_version=raw["experiment_version"],
            run_label=f"shadow-{suffix}",
            model_label="MiMo V2.5 Pro",
            name=f"mimo-{suffix}",
            initial_cash_micros=10_000_000_000,
        )
        service.start_agent(
            experiment_version=raw["experiment_version"],
            run_label=f"shadow-{suffix}", name=f"deepseek-{suffix}", starts_at=now
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT agent_id, enabled FROM agent_runtime_schedules "
                "WHERE agent_id IN (%s, %s) ORDER BY agent_id",
                (first, second),
            )
            enabled = {uuid.UUID(str(row[0])): bool(row[1]) for row in cursor.fetchall()}
            assert enabled == {first: True, second: False}
            cursor.execute(
                "SELECT le.agent_id, sum(lp.amount_micros), "
                "sum(lp.amount_micros) FILTER (WHERE lp.account = 'cash') "
                "FROM ledger_entries le JOIN ledger_postings lp ON lp.ledger_entry_id = le.id "
                "WHERE le.agent_id IN (%s, %s) AND le.event_type = 'initial_capital' "
                "GROUP BY le.agent_id",
                (first, second),
            )
            capital = {
                uuid.UUID(str(row[0])): (int(str(row[1])), int(str(row[2])))
                for row in cursor.fetchall()
            }
            assert capital == {
                first: (0, 10_000_000_000),
                second: (0, 10_000_000_000),
            }
            cursor.execute(
                "SELECT status FROM experiment_runs WHERE id = %s", (registration.run_id,)
            )
            assert cursor.fetchone() == ("running",)
        service.remove_agent(
            experiment_version=raw["experiment_version"],
            run_label=f"shadow-{suffix}",
            name=f"deepseek-{suffix}",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT enabled FROM agent_runtime_schedules WHERE agent_id = %s", (first,)
            )
            assert cursor.fetchone() == (False,)
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM experiment_definitions WHERE id = %s",
                (registration.definition_id,),
            )
            assert cursor.fetchone() == (0,)
