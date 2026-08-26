# vtrade-kalshi-v1 validation matrix

The release is not complete by passing offline tests alone. Each gate records its
scope and the resource it proves.

| Gate | Scope | Command/evidence | What it proves |
|---|---|---|---|
| Pytest | Offline | `$env:UV_CACHE_DIR='.uv-cache'; uv run --extra dev python -m pytest` | Unit and contract behavior without provider calls. |
| Ruff | Offline | `uv run --extra dev python -m ruff check src tests` | Active Python lint and import hygiene. |
| mypy | Offline | `uv run --extra dev python -m mypy src/vtrade` | Strict type safety for the active package. |
| Frozen artifacts | Offline | `uv run --extra dev python -m vtrade.frozen_artifacts` | Canonical UTF-8/LF bytes, SHA-256 values, schema shape, and fixture reference. |
| Release sweep | Offline | `uv run --extra dev python -m vtrade.release_verification` | Exact seven migrations, archive boundary, absent legacy paths, and zero active legacy venue references. |
| Cutover evidence | Recorded evidence | `uv run --extra dev python scripts/verify_kalshi_cutover_evidence.py --require-ready` | Six named gates, both pre-cutover snapshots, active artifact/migration hashes, paper-only reachability, and infrastructure-only rollback. |
| Compose shape | Offline-shape | `docker compose -f compose.coolify.yaml config --quiet` | Rendered migrate/API/worker dependency graph; not resource readiness. |
| Image | Built-image | `docker build --pull -t vtrade:kalshi-cutover .` | Frozen artifact verification and imports in the built image. |
| PostgreSQL bootstrap | Real disposable PostgreSQL | `$env:VTRADE_RUN_POSTGRES_INTEGRATION='1'; uv run --extra dev python -m pytest tests/test_postgres_*.py` | Fresh 0001-0007 apply/rerun, checksum rejection, latest migration, and rollback-only isolation. |
| Migration/readiness | Real PostgreSQL and private storage | `uv run --extra dev python -m vtrade.migrate` plus authenticated readiness | Real schema, latest migration, private object store, and active configuration. |
| French-host REST probe | Intended French production host | `uv run --extra dev python scripts/probe_kalshi_public_rest.py --output <redacted-output>` | Public catalogue traversal, historical cutoff, ordinary binary book, bounded concurrency, raw bytes, and hashes. |

The PostgreSQL, image, private-storage, provider-egress, and French-host results must
be recorded in `docs/evidence/` from real resources. A local fixture, fake provider,
proxy, VPN, or old archive cannot satisfy a same-cutover gate. A missing or failed
gate stops the cutover. The committed 2026-08-27 observation is intentionally
`blocked` until the missing external records are added.
