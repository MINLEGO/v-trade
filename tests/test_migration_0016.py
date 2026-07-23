from pathlib import Path

MIGRATION = Path("migrations/0016_market_fee_policy_parameters.sql")


def test_market_fee_policy_migration_preserves_legacy_audit_and_adds_fd_parameters() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "condition_id",
        "fee_rate numeric(30, 18)",
        "fee_exponent numeric(30, 18)",
        "fee_taker_only boolean",
        "ALTER COLUMN base_fee_bps DROP NOT NULL",
        "GET /clob-markets/{condition_id}",
        "fee_rate_snapshots_fee_rate_check",
    ):
        assert required in sql
