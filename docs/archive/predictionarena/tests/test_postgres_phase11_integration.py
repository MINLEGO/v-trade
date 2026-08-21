from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("VTRADE_RUN_POSTGRES_INTEGRATION") != "1",
    reason="set VTRADE_RUN_POSTGRES_INTEGRATION=1 for real PostgreSQL verification",
)


def _migration_objects() -> tuple[list[str], list[str]]:
    sql = Path("migrations/0011_private_runtime_and_strict_exa.sql").read_text(
        encoding="utf-8"
    )
    table_block = re.search(
        r"vtrade_tables constant text\[\] := ARRAY\[(.*?)\];", sql, re.DOTALL
    )
    function_block = re.search(
        r"vtrade_functions constant text\[\] := ARRAY\[(.*?)\];", sql, re.DOTALL
    )
    assert table_block is not None and function_block is not None
    tables = re.findall(r"'([a-z_]+)'", table_block.group(1))
    functions = re.findall(r"'(public\.[a-z_]+\(\))'", function_block.group(1))
    return tables, functions


def test_real_postgres_private_roles_stages_and_exa_constraint() -> None:
    import psycopg

    database_url = os.environ["VTRADE_DATABASE_URL"]
    tables, functions = _migration_objects()
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, c.relrowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = ANY(%s) ORDER BY c.relname",
            (tables,),
        )
        rows = cursor.fetchall()
        assert {str(row[0]) for row in rows} == set(tables)
        assert all(bool(row[1]) for row in rows)

        cursor.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                       (["anon", "authenticated", "service_role"],))
        roles = {str(row[0]) for row in cursor.fetchall()}
        for role in roles & {"anon", "authenticated"}:
            cursor.execute(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND grantee = %s AND table_name = ANY(%s)",
                (role, tables),
            )
            assert cursor.fetchall() == []
            for function in functions:
                cursor.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, function)
                )
                assert cursor.fetchone() == (False,)

        if "service_role" in roles:
            for table in tables:
                cursor.execute(
                    "SELECT has_table_privilege('service_role', %s, "
                    "'SELECT,INSERT,UPDATE,DELETE')",
                    (f"public.{table}",),
                )
                assert cursor.fetchone() == (True,)

        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'runtime_cycle_steps_stage_check'"
        )
        assert "pre_settlement" in str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'artifact_inventory_stage_check'"
        )
        assert "pre_settlement" in str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'exa_quota_reservations_reserved_credit_count_check'"
        )
        definition = str(cursor.fetchone()[0])
        assert ">= (1)::numeric" in definition
        assert "<= (10)::numeric" in definition
