# Cutover evidence

`kalshi-cutover-2026-08-27.json` is the machine-readable record for the current
staging/cutover observation. It intentionally contains no credentials, private URLs,
host identifiers, raw provider payloads, or database connection details.

Validate its binding to the active checkout with:

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv run --extra dev python scripts/verify_kalshi_cutover_evidence.py
```

The record may be `blocked` while a real gate is missing. Before starting a worker on
a fresh target, run the same command with `--require-ready`; it fails closed unless
all six gates, both pre-cutover snapshots, the image digest, the paper-only check, and
the infrastructure-only rollback record are present and passed.

Evidence references must point to redacted logs or immutable operator records. Never
put secrets, bearer tokens, connection strings, private object-store URLs, or raw
provider headers in this directory.
