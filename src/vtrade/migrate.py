from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from vtrade.config import required_environment

EXPECTED_MIGRATIONS: tuple[str, ...] = (
    "0001_foundation_agent_state_ledger.sql",
    "0002_kalshi_catalogue_and_freezes.sql",
    "0003_execution_portfolio_and_settlement.sql",
    "0004_runtime_audit_and_admin.sql",
    "0005_runtime_pre_settlement_stage.sql",
    "0006_monthly_budget_alert_flags.sql",
    "0007_exa_quota_contract.sql",
)
MIGRATION_LOCK_NAME = "vtrade:clean-migrations:v1"


class MigrationError(RuntimeError):
    """Raised when the migration chain or its persisted evidence is unsafe."""


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]


@dataclass(frozen=True, slots=True)
class MigrationSource:
    position: int
    name: str
    body: bytes
    sha256: str


def load_migration_sources(
    directory: Path = Path("migrations"),
) -> tuple[MigrationSource, ...]:
    """Load the canonical migration files as their original bytes."""

    if not directory.is_dir():
        raise MigrationError(f"migration directory does not exist: {directory}")
    sql_files = tuple(
        sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == ".sql"
        )
    )
    names = tuple(path.name for path in sql_files)
    expected = EXPECTED_MIGRATIONS
    if names != expected:
        missing = tuple(name for name in expected if name not in names)
        unexpected = tuple(name for name in names if name not in expected)
        raise MigrationError(
            "migration directory must contain exactly the ordered clean chain; "
            f"missing={missing!r}, unexpected={unexpected!r}, observed={names!r}"
        )

    sources: list[MigrationSource] = []
    for position, path in enumerate(sql_files, start=1):
        body = path.read_bytes()
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration is not valid UTF-8: {path.name}") from exc
        if text.startswith("\ufeff"):
            raise MigrationError(f"migration contains a UTF-8 BOM: {path.name}")
        if "\r" in text:
            raise MigrationError(f"migration must use LF line endings: {path.name}")
        sources.append(
            MigrationSource(position, path.name, body, hashlib.sha256(body).hexdigest())
        )
    return tuple(sources)


def apply_migrations(
    directory: Path = Path("migrations"),
    *,
    database_url: str | None = None,
    connect: _Connect | None = None,
) -> tuple[str, ...]:
    """Apply the clean chain once, rejecting every non-prefix database state.

    The runner owns both the table and the ordering. A migration is recorded only
    after its SQL has completed in the same transaction, so a failed statement cannot
    leave a false-success marker behind.
    """

    sources = load_migration_sources(directory)
    if database_url is None:
        database_url = required_environment(("VTRADE_DATABASE_URL",))["VTRADE_DATABASE_URL"]
    resolved_url = database_url
    connection_factory = connect or _default_connect
    expected_by_name = {source.name: source for source in sources}

    with connection_factory(resolved_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (MIGRATION_LOCK_NAME,),
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, "
            "sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'), "
            "position integer NOT NULL UNIQUE CHECK (position > 0), "
            "applied_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )
        cursor.execute(
            "SELECT version, sha256, position FROM schema_migrations ORDER BY position"
        )
        applied = tuple(cursor.fetchall())
        if len(applied) > len(sources):
            raise MigrationError("database contains more migrations than the clean chain")

        for expected_position, row in enumerate(applied, start=1):
            if len(row) < 3:
                raise MigrationError("schema_migrations row is missing its runner position")
            version, digest, position = str(row[0]), str(row[1]), int(str(row[2]))
            expected = sources[expected_position - 1]
            if position != expected_position or version != expected.name:
                raise MigrationError(
                    "schema_migrations is not an ordered prefix of the clean chain: "
                    f"position={position}, version={version!r}, expected={expected.name!r}"
                )
            if digest != expected.sha256:
                raise MigrationError(f"applied migration changed: {version}")
            if version not in expected_by_name:
                raise MigrationError(f"unexpected applied migration: {version}")

        for source in sources[len(applied) :]:
            cursor.execute(source.body.decode("utf-8"))
            cursor.execute(
                "INSERT INTO schema_migrations(version, sha256, position) VALUES (%s, %s, %s)",
                (source.name, source.sha256, source.position),
            )

    return tuple(source.name for source in sources)


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - guarded by the production dependency
        raise MigrationError("psycopg is required to apply PostgreSQL migrations") from exc
    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply the clean V-Trade PostgreSQL chain")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(os.getenv("VTRADE_MIGRATIONS", "migrations")),
    )
    parser.add_argument("--database-url-env", default="VTRADE_DATABASE_URL")
    args = parser.parse_args()
    values = required_environment((args.database_url_env,))
    apply_migrations(args.directory, database_url=values[args.database_url_env])


if __name__ == "__main__":
    main()
