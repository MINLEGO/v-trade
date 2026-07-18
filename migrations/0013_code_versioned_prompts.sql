-- A byte-identical frozen prompt may be reused by a new code-versioned experiment
-- definition. It remains unique within each definition and cannot be changed in place.

ALTER TABLE prompt_versions
  DROP CONSTRAINT IF EXISTS prompt_versions_body_sha256_key;

ALTER TABLE prompt_versions
  ADD CONSTRAINT prompt_versions_definition_body_sha256_key
  UNIQUE (definition_id, body_sha256);
