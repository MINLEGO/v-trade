from __future__ import annotations

from pathlib import Path

MIGRATION = Path("migrations/0011_private_runtime_and_strict_exa.sql")


def test_phase11_migration_has_bounded_stage_security_and_exa_changes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "'market_freeze', 'pre_settlement', 'prompt'" in sql
    assert "reserved_credit_count BETWEEN 1 AND 10" in sql
    assert "ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY" in sql
    assert "REVOKE ALL PRIVILEGES ON TABLE public.%I FROM anon" in sql
    assert "REVOKE ALL PRIVILEGES ON TABLE public.%I FROM authenticated" in sql
    assert "GRANT ALL PRIVILEGES ON TABLE public.%I TO service_role" in sql
    assert "REVOKE ALL PRIVILEGES ON FUNCTION %s FROM PUBLIC" in sql


def test_phase11_does_not_change_shared_public_schema_defaults() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "REVOKE USAGE ON SCHEMA public" not in sql
    assert "ALTER DEFAULT PRIVILEGES" not in sql
    assert "ON ALL TABLES IN SCHEMA public" not in sql
    assert "ON ALL FUNCTIONS IN SCHEMA public" not in sql
