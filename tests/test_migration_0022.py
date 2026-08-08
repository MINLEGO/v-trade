from pathlib import Path

MIGRATION = Path("migrations/0022_conservative_liquidity_haircut.sql")


def test_conservative_haircut_migration_versions_private_effective_depth() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for required in (
        "ignored_shares",
        "effective_shares",
        "executable",
        "rule_version",
        "ignored_best_levels",
        "maximum_ignored_depth_fraction",
        "raw_depth_shares",
        "best_level_fraction",
        "ignored_depth_shares",
        "ignored_fraction",
        "effective_depth_shares",
        "best_level_price",
        "virtual_liquidity_levels_effective_shares_check",
        "virtual_liquidity_execution_levels_available_effective_check",
    ):
        assert required in sql
    assert "UPDATE order_book_snapshots" not in sql
