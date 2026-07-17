from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vtrade.bootstrap import BootstrapError, PostgresExperimentBootstrap, _parse_timestamp
from vtrade.config import config_hash, load_experiment_config


class Cursor:
    def __init__(self, rows: list[tuple[object, ...] | None]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 1

    def execute(self, query: str, params: tuple[object, ...] = ()) -> object:
        self.queries.append((query, params))
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0)


class Connection:
    def __init__(self, cursor: Cursor) -> None:
        self.value = cursor

    @contextmanager
    def cursor(self) -> Any:
        yield self.value


def connector(cursor: Cursor) -> Any:
    @contextmanager
    def connect(_database_url: str) -> Any:
        yield Connection(cursor)

    return connect


def ready_config(tmp_path: Path) -> tuple[Path, str]:
    source = Path("config/experiments/predictionarena-polymarket-v1.json")
    raw = __import__("json").loads(source.read_text(encoding="utf-8"))
    raw["status"] = "ready"
    for decision in raw["owner_decisions"].values():
        decision["status"] = "resolved"
    prompt_body = Path(raw["artifacts"]["prompt"]["path"]).read_text(encoding="utf-8")
    raw["artifacts"]["prompt"]["sha256"] = hashlib.sha256(
        prompt_body.encode("utf-8")
    ).hexdigest()
    path = tmp_path / "experiment.json"
    path.write_text(__import__("json").dumps(raw), encoding="utf-8")
    assert config_hash(raw) == load_experiment_config(path).sha256
    return path, prompt_body


def test_register_is_inert_and_inserts_all_frozen_records(tmp_path: Path) -> None:
    path, prompt = ready_config(tmp_path)
    cursor = Cursor([None, None, None, None, None])
    service = PostgresExperimentBootstrap(
        "postgres://test",
        connect=connector(cursor),
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    )
    result = service.register(
        config=load_experiment_config(path),
        prompt_body=prompt,
        code_version="commit-abc",
        run_label="shadow-1",
        starts_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    sql = "\n".join(query for query, _ in cursor.queries)
    assert result.run_id
    assert len(result.model_ids) == 2
    assert "INSERT INTO experiment_definitions" in sql
    assert "INSERT INTO prompt_versions" in sql
    assert sql.count("INSERT INTO model_configs") == 2
    assert "INSERT INTO experiment_runs" in sql
    assert "INSERT INTO agents" not in sql
    assert "'ready'" in sql
    assert "'running'" not in sql


def test_register_rejects_prompt_fingerprint_before_database(tmp_path: Path) -> None:
    path, _ = ready_config(tmp_path)
    service = PostgresExperimentBootstrap("postgres://test", connect=connector(Cursor([])))
    with pytest.raises(BootstrapError, match="prompt bytes"):
        service.register(
            config=load_experiment_config(path),
            prompt_body="changed",
            code_version="commit-abc",
            run_label="shadow-1",
            starts_at=datetime(2026, 7, 17, tzinfo=UTC),
        )


def test_agent_add_is_paused_and_start_remove_only_touch_selected_agent() -> None:
    run_id = "00000000-0000-0000-0000-000000000001"
    model_id = "00000000-0000-0000-0000-000000000002"
    add_cursor = Cursor([(run_id, model_id, 10_000_000_000), None])
    service = PostgresExperimentBootstrap(
        "postgres://test",
        connect=connector(add_cursor),
        clock=lambda: datetime(2026, 7, 16, tzinfo=UTC),
    )
    agent_id = service.add_agent(
        experiment_version="predictionarena-polymarket-v1",
        run_label="baseline",
        model_label="DeepSeek V4 Flash",
        name="deepseek-1",
        initial_cash_micros=10_000_000_000,
    )
    add_sql = "\n".join(query for query, _ in add_cursor.queries)
    assert agent_id
    assert "INSERT INTO agents" in add_sql
    assert "enabled, created_at" in add_sql
    assert "false" in add_sql

    start_cursor = Cursor([(str(agent_id), run_id, "ready")])
    service._connect = connector(start_cursor)
    service.start_agent(
        experiment_version="predictionarena-polymarket-v1",
        run_label="baseline",
        name="deepseek-1",
        starts_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    start_sql = "\n".join(query for query, _ in start_cursor.queries)
    assert "WHERE id = %s" in start_sql
    assert "WHERE agent_id = %s" in start_sql
    assert "SET status = 'running'" in start_sql

    remove_cursor = Cursor([(str(agent_id),)])
    service._connect = connector(remove_cursor)
    service.remove_agent(
        experiment_version="predictionarena-polymarket-v1",
        run_label="baseline",
        name="deepseek-1",
    )
    remove_sql = "\n".join(query for query, _ in remove_cursor.queries)
    assert "SET paused_at" in remove_sql
    assert "SET enabled = false" in remove_sql
    assert "DELETE" not in remove_sql


def test_cli_timestamp_requires_timezone() -> None:
    assert _parse_timestamp("2026-07-16T00:00:00Z").tzinfo is UTC
    with pytest.raises(Exception, match="timezone-aware"):
        _parse_timestamp("2026-07-16T00:00:00")
