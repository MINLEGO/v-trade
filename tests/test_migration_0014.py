from pathlib import Path


def test_exa_cost_semantics_migration_preserves_usage_and_clears_false_halt() -> None:
    sql = Path("migrations/0014_exa_estimated_cost_semantics.sql").read_text(
        encoding="utf-8"
    )

    assert "SET billed_cost_micros = 0" in sql
    assert "SET unexpected_billed_cost_micros = 0" in sql
    assert "reservations.actual_credit_count > reservations.reserved_credit_count" in sql
    assert "request_count = 0" not in sql
    assert "credit_count = 0" not in sql
    assert "WHERE code = 'exa_unexpected_billed_cost'" in sql
    assert "resolved_at = now()" in sql
