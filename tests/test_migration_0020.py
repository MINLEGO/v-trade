from pathlib import Path

MIGRATION = Path("migrations/0020_live_order_execution.sql")


def test_live_order_execution_migration_records_context_and_restart_state() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for required in (
        "requested_at",
        "executed_at",
        "order_execution_attempts",
        "live_order_contexts",
        "live_order_contexts_append_only",
    ):
        assert required in sql
    assert "DISABLE TRIGGER order_intents_append_only" in sql
    assert "ENABLE TRIGGER order_intents_append_only" in sql
    assert "DISABLE TRIGGER orders_append_only" in sql
    assert "ENABLE TRIGGER orders_append_only" in sql
