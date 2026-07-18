from pathlib import Path


def test_code_versioned_prompt_migration_scopes_fingerprint_to_definition() -> None:
    sql = Path("migrations/0013_code_versioned_prompts.sql").read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS prompt_versions_body_sha256_key" in sql
    assert "UNIQUE (definition_id, body_sha256)" in sql
