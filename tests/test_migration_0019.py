from pathlib import Path

MIGRATION = Path("migrations/0019_virtual_liquidity.sql")


def test_virtual_liquidity_migration_is_private_versioned_and_auditable() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "CREATE TABLE virtual_liquidity_levels",
        "CREATE TABLE virtual_liquidity_executions",
        "CREATE TABLE virtual_liquidity_execution_levels",
        "agent_id uuid",
        "agent_cycle_id uuid",
        "snapshot_id uuid",
        "displayed_shares",
        "consumed_shares",
        "cancelled_shares",
        "remaining_shares",
        "CHECK (remaining_shares = available_shares - consumed_shares)",
        "DEFERRABLE INITIALLY DEFERRED",
        "ON DELETE CASCADE",
    ):
        assert required in sql
    assert "UPDATE order_book_snapshots" not in sql
