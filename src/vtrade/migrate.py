from __future__ import annotations

import hashlib
import os
from pathlib import Path

from vtrade.config import required_environment


def apply_migrations(directory: Path = Path("migrations")) -> None:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is REQUIRED to apply PostgreSQL migrations") from exc
    database_url = required_environment(("VTRADE_DATABASE_URL",))["VTRADE_DATABASE_URL"]
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (918_520_024,))
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, sha256 text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for source in files:
            body = source.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            cursor.execute(
                "SELECT sha256 FROM schema_migrations WHERE version = %s", (source.name,)
            )
            row = cursor.fetchone()
            if row:
                if row[0] != digest:
                    raise RuntimeError(f"applied migration changed: {source.name}")
                continue
            cursor.execute(body.decode("utf-8"))
            cursor.execute(
                "INSERT INTO schema_migrations(version, sha256) VALUES (%s, %s)",
                (source.name, digest),
            )


if __name__ == "__main__":
    apply_migrations(Path(os.getenv("VTRADE_MIGRATIONS", "migrations")))
