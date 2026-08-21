# Kalshi fixture corpus

The active fixture surface is `spec/fixtures/kalshi/manifest.json`. It records exact
credential-free public REST captures, request identity, response metadata, cutoff
causality, and SHA-256 values. Raw response bytes are never hand-written, rounded,
normalized, or replaced by a fake provider.

The checked-in manifest is intentionally `owner_pending` until the intended French
production host supplies a reviewed capture. That state is valid repository evidence
but is not valid production-composition evidence: fixture-dependent startup must fail
closed until the manifest is complete.
