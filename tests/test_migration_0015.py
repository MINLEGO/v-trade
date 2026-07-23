from pathlib import Path


MIGRATION = Path("migrations/0015_belief_confidence_and_singleton_plans.sql")


def test_belief_confidence_and_singleton_plan_migration() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "SET confidence = probability" in sql
    assert "ALTER COLUMN confidence SET NOT NULL" in sql
    assert "DROP COLUMN probability" in sql
    for category in (
        "event_analysis",
        "trading_strategy",
        "market_sentiment",
        "market_structure",
        "risk_assessment",
    ):
        assert f"'{category}'" in sql
    assert "plans_one_active_per_type_idx" in sql
    assert "WHERE status = 'active'" in sql
