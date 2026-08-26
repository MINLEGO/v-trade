from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from vtrade.migrate import (
    EXPECTED_MIGRATIONS,
    MigrationError,
    apply_migrations,
    load_migration_sources,
)


class FakeCursor:
    def __init__(self, rows: list[tuple[str, str, int]]) -> None:
        self.rows = rows
        self.executed: list[str] = []
        self.inserted_sql: list[str] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append(query)
        if query.startswith("SELECT version, sha256, position"):
            return
        if query.startswith("INSERT INTO schema_migrations"):
            self.rows.append((str(params[0]), str(params[1]), int(params[2])))
            return
        if query.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            return
        if query.startswith("SELECT pg_advisory_xact_lock"):
            return
        self.inserted_sql.append(query)

    def fetchall(self) -> list[tuple[str, str, int]]:
        return list(self.rows)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_instance = cursor

    def cursor(self) -> AbstractContextManager[FakeCursor]:
        return self.cursor_instance

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _copy_chain(tmp_path: Path) -> Path:
    source = Path("migrations")
    destination = tmp_path / "migrations"
    destination.mkdir()
    for name in EXPECTED_MIGRATIONS:
        (destination / name).write_bytes((source / name).read_bytes())
    return destination


def test_load_migration_sources_requires_exact_canonical_chain(tmp_path: Path) -> None:
    directory = _copy_chain(tmp_path)
    sources = load_migration_sources(directory)
    assert tuple(source.name for source in sources) == EXPECTED_MIGRATIONS
    assert tuple(source.position for source in sources) == tuple(
        range(1, len(EXPECTED_MIGRATIONS) + 1)
    )
    assert all(
        source.sha256 == hashlib.sha256(source.body).hexdigest() for source in sources
    )

    (directory / "0005_unexpected.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="exactly the ordered clean chain"):
        load_migration_sources(directory)


def test_apply_migrations_records_a_prefix_and_is_idempotent(tmp_path: Path) -> None:
    directory = _copy_chain(tmp_path)
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)

    assert apply_migrations(directory, database_url="unused", connect=lambda _url: connection)
    applied_sql_count = len(cursor.inserted_sql)
    assert [row[0] for row in cursor.rows] == list(EXPECTED_MIGRATIONS)
    assert [row[2] for row in cursor.rows] == list(range(1, len(EXPECTED_MIGRATIONS) + 1))

    apply_migrations(directory, database_url="unused", connect=lambda _url: connection)
    assert len(cursor.inserted_sql) == applied_sql_count


def test_apply_migrations_rejects_checksum_drift(tmp_path: Path) -> None:
    directory = _copy_chain(tmp_path)
    sources = load_migration_sources(directory)
    cursor = FakeCursor([(sources[0].name, "0" * 64, 1)])
    connection = FakeConnection(cursor)

    with pytest.raises(MigrationError, match="applied migration changed"):
        apply_migrations(directory, database_url="unused", connect=lambda _url: connection)


def test_apply_migrations_rejects_missing_or_reordered_prefix(tmp_path: Path) -> None:
    directory = _copy_chain(tmp_path)
    sources = load_migration_sources(directory)
    cursor = FakeCursor([(sources[1].name, sources[1].sha256, 2)])
    connection = FakeConnection(cursor)

    with pytest.raises(MigrationError, match="ordered prefix"):
        apply_migrations(directory, database_url="unused", connect=lambda _url: connection)
