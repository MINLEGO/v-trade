# Kalshi fixture corpus

The active fixture surface is `spec/fixtures/kalshi/manifest.json`. It records exact
credential-free public REST captures, request identity, response metadata, cutoff
causality, and SHA-256 values. Raw response bytes are never hand-written, rounded,
normalized, or replaced by a fake provider.

The reviewed capture from the intended French production host is integrated under
`spec/fixtures/kalshi/`. The active manifest is `ready`; the original probe audit is
preserved as `probe-manifest.json`, and raw response bytes remain under `responses/`.
