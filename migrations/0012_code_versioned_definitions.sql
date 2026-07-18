-- Permit the same frozen experiment configuration to be rerun under a distinct,
-- explicitly recorded code version. Configuration bytes remain immutable; the pair
-- (configuration fingerprint, code version) identifies an executable definition.

ALTER TABLE experiment_definitions
  DROP CONSTRAINT IF EXISTS experiment_definitions_config_sha256_key;

ALTER TABLE experiment_definitions
  ADD CONSTRAINT experiment_definitions_config_sha256_code_version_key
  UNIQUE (config_sha256, code_version);
