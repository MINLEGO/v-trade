# Kalshi public REST captures

This directory is reserved for exact captures produced by
`scripts/probe_kalshi_public_rest.py` from the intended French production host.

The probe writes the raw UTF-8 response bytes under `responses/` and a separate
`manifest.json` containing request identity, status, timing, secret-free headers,
cutoff data, and SHA-256 values. Response JSON must never be hand-written,
rounded, normalized, or replaced with a fake provider.

The reviewed French-host probe captured 125 HTTP-200 responses at
`2026-08-24T17:15:11.246281Z`. The active `manifest.json` is the runtime projection
with status `ready`; `probe-manifest.json` preserves the probe's pagination,
concurrency, cutoff, and response-audit metadata. Raw response bytes remain exact
and are validated by SHA-256 and byte length before composition.
