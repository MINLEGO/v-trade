# Historical PredictionArena archive

archive: historical-read-only

This directory contains read-only historical evidence and analysis retained for
provenance and audit context. It is not an active experiment, implementation, or
fixture source.

The runtime, tests, Docker image, migrations, and active fixture discovery must not
import, load, execute, or resolve files from this directory. Historical files are
not imported or loaded by runtime, tests, image builds, migrations, or fixture
discovery. The archive does not establish venue equivalence, schema compatibility,
execution equivalence, or performance equivalence with the active Kalshi paper
release. It is not loaded by any active runtime path.

Changes to the active release must be made under the active `src/`, `config/`,
`migrations/`, `spec/`, and test surfaces and must create a new frozen experiment
version when a contract changes.
