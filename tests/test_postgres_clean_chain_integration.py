from __future__ import annotations

import os

import pytest

from vtrade.migrate import (
    EXPECTED_MIGRATIONS,
    MigrationError,
    apply_migrations,
    load_migration_sources,
)

pytestmark = pytest.mark.skipif(
    os.getenv("VTRADE_RUN_POSTGRES_INTEGRATION") != "1",
    reason="set VTRADE_RUN_POSTGRES_INTEGRATION=1 for real PostgreSQL verification",
)


def test_fresh_database_applies_reruns_and_rejects_checksum_drift() -> None:
    import psycopg

    database_url = os.environ["VTRADE_DATABASE_URL"]
    sources = load_migration_sources()
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.schema_migrations')")
        if cursor.fetchone()[0] is not None:
            cursor.execute("SELECT count(*) FROM schema_migrations")
            if int(cursor.fetchone()[0]) != 0:
                pytest.skip("requires a dedicated empty staging database")

    assert apply_migrations(database_url) == EXPECTED_MIGRATIONS
    assert apply_migrations(database_url) == EXPECTED_MIGRATIONS

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT version, sha256, position FROM schema_migrations ORDER BY position"
        )
        rows = tuple(cursor.fetchall())
        assert tuple(str(row[0]) for row in rows) == EXPECTED_MIGRATIONS
        assert int(rows[-1][2]) == len(EXPECTED_MIGRATIONS)
        cursor.execute(
            "UPDATE schema_migrations SET sha256 = %s WHERE version = %s",
            ("0" * 64, EXPECTED_MIGRATIONS[0]),
        )
        connection.commit()

    try:
        with pytest.raises(MigrationError, match="applied migration changed"):
            apply_migrations(database_url)
    finally:
        with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE schema_migrations SET sha256 = %s WHERE version = %s",
                (sources[0].sha256, EXPECTED_MIGRATIONS[0]),
            )
            connection.commit()
