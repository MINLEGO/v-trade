# Kalshi public REST captures

This directory is reserved for exact captures produced by
`scripts/probe_kalshi_public_rest.py` from the intended French production host.

The probe writes the raw UTF-8 response bytes under `responses/` and a separate
`manifest.json` containing request identity, status, timing, secret-free headers,
cutoff data, and SHA-256 values. Response JSON must never be hand-written,
rounded, normalized, or replaced with a fake provider.

The repository currently has no owner-supplied French-host capture in this
checkout. Until that probe is run and reviewed, fixture-dependent deployment
evidence remains `owner_pending`; offline tests must use explicitly labelled
test doubles rather than claiming a real-host result.
