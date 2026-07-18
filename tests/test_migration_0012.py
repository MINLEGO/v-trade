from pathlib import Path


def test_code_versioned_definition_migration_preserves_config_immutability() -> None:
    sql = Path("migrations/0012_code_versioned_definitions.sql").read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS experiment_definitions_config_sha256_key" in sql
    assert "UNIQUE (config_sha256, code_version)" in sql
