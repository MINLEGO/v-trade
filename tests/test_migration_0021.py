from pathlib import Path

MIGRATION = Path("migrations/0021_position_entry_fees.sql")


def test_position_entry_fees_migration_adds_a_non_negative_defaulted_field() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ALTER TABLE positions" in sql
    assert "ADD COLUMN entry_fees_micros bigint NOT NULL DEFAULT 0" in sql
    assert "CHECK (entry_fees_micros >= 0)" in sql
