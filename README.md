# V-Trade

V-Trade is an auditable, paper-only prediction-market experiment. The active release
is `vtrade-kalshi-v1`: it discovers ordinary binary Kalshi markets, exposes YES/NO
outcomes, and simulates IOC/FOK orders against real public market data without
submitting orders to a venue.

The active release has one experiment, one clean five-migration database chain, and
no compatibility loader, dual venue, dual write, legacy upgrade, or real-execution
fallback. Historical research is retained only under
[`docs/archive/predictionarena/`](docs/archive/predictionarena/); that archive is
read-only evidence and is never imported or loaded by the application.

## Local validation

Python 3.12 and the locked development environment are required:

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv sync --extra dev
uv run --extra dev python -m pytest
uv run --extra dev python -m ruff check src tests
uv run --extra dev python -m mypy src/vtrade
uv run --extra dev python -m vtrade.release_verification
docker compose -f compose.coolify.yaml config --quiet
```

The release verifier checks frozen artifact hashes, the exact migration chain, the
archive boundary, the fixture manifest, and the zero-active-legacy-venue sweep. It
prints PostgreSQL, image, storage, and French-host probe gates separately; local
tests do not claim those external checks.

## Deployment boundary

Deploy a disposable empty PostgreSQL target with the real private object store. The
sequence is `migrate -> authenticated API readiness -> worker`. `/health/live` is
cheap and provider-independent. `/health/ready` checks the database, latest
migration, private configuration, storage, and active artifact contract; it does not
call Kalshi, model, or research providers.

The image verifies active artifacts before dropping privileges. Missing private
resources, reviewed external fixture evidence, or migrations fail closed. Real
Kalshi authentication, signing, WebSockets, and order submission are not part of
this release.

See [`docs/runtime-operations.md`](docs/runtime-operations.md) for the operator
runbook and [`docs/adr/0002-kalshi-only-paper-cutover.md`](docs/adr/0002-kalshi-only-paper-cutover.md)
for the irreversible fresh-database cutover decision.
