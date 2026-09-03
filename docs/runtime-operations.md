# Runtime and private administration

## Deployment sequence

The target is a fresh empty PostgreSQL database. The image contains only the active
configuration, ten migrations, Kalshi fee schedule, Kalshi fixture manifest, and frozen tool/prompt
contracts. Coolify starts one migration job, then the API, then the worker after the
API's authenticated readiness check is healthy:

```text
migrate -> /health/ready -> worker
```

Before `migrate`, record the database and object-storage snapshots plus the source,
image, configuration, artifact, and migration digests. Do not start the worker until
the cutover evidence record passes `--require-ready`.

A migration failure stops the API and worker. After active data is written there is
no application rollback or schema downgrade path; use the matching infrastructure
snapshot or rebuild an empty target.

## Preflight commands

From the repository root:

```powershell
$env:UV_CACHE_DIR='.uv-cache'
uv run --extra dev python -m vtrade.release_verification
uv run --extra dev python -m vtrade.frozen_artifacts config/experiments/vtrade-kalshi-v1.json
uv run --extra dev python scripts/verify_kalshi_cutover_evidence.py
docker compose -f compose.coolify.yaml config --quiet
docker build --pull -t vtrade:kalshi-cutover .
```

These commands prove repository and image shape only. A real disposable PostgreSQL
run, private object-storage readiness, built-image startup, and the French-host
public REST and market-candlestick probes remain separate evidence gates. The evidence record must be checked
with `--require-ready` before starting the worker on a fresh target.

## Health boundaries

- `/health/live` is a cheap authenticated process probe. It does not contact the
  database, storage, Kalshi, model, or research providers.
- `/health/ready` checks the real PostgreSQL schema and latest migration, private
  storage configuration, and the runnable active artifact contract. It returns
  `503` when any required resource or reviewed fixture is missing. It never calls a
  provider.

Use the secret only in an authorization header:

```powershell
$headers = @{ Authorization = "Bearer $env:VTRADE_ADMIN_AUTH_SECRET" }
Invoke-WebRequest http://127.0.0.1:8000/health/live -Headers $headers -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8000/health/ready -Headers $headers -UseBasicParsing
```

## Active experiment and agents

Only `vtrade-kalshi-v1` may be registered. It is paper-only, ordinary-binary, and
uses the frozen prompt, tool schema, compatibility statement, fee policy, dynamic
price grids, canonical books, and `best-level-haircut-v1`. Missing owner decisions
or external capture evidence fail closed; local or fake resources are not substitutes.

Agents have independent hourly schedules and isolated balances. Register the frozen
experiment and a ready run before adding a paused agent. Starting, pausing, and
removing an agent are authenticated, idempotent, and append-only audited actions.
The worker does not submit venue orders.

The private `/admin` dashboard exposes the same pause/resume actions without a
terminal: use `Pause run` or `Resume run` beside the run status for a global
control, or select an agent and use its `Pause agent`/`Resume agent` button. Each
action asks for confirmation, uses a fresh idempotency key, and leaves active
cycles running to completion.

## Runtime safety

Every cycle persists an immutable cutoff and checkpoint sequence: market freeze,
pre-settlement, prompt, harness, broker, and final settlement/valuation. Leases and
scheduler locks prevent duplicate claims. Recovered work reuses completed evidence;
it never replays an external model or research call when the prior result is ambiguous.

Order results use semantic market references, YES/NO, exact prices and contract
units, fee data, lifecycle state, reconciliation state, and audit references. A
pending reconciliation blocks new orders for that agent without reserving cash or
positions. Settlement pays only FINALIZED binary evidence with `settlement_ts`.
An unavailable or contradictory global fee schedule aborts the freeze and opens
the critical `fee_policy_global_failure` system alert; a market-local unsupported
or invalid policy remains visible with its explicit closed reason.

## External gates

With a real staging database and private storage:

```powershell
$env:VTRADE_RUN_POSTGRES_INTEGRATION='1'
uv run --extra dev python -m pytest tests/test_postgres_*.py
uv run --extra dev python -m vtrade.migrate
uv run --extra dev python scripts/probe_kalshi_public_rest.py --output <redacted-output>
uv run --extra dev python scripts/verify_kalshi_cutover_evidence.py --require-ready
```

The probe is read-only and credential-free. It captures public catalogue pages with
complete opaque-cursor traversal, event/series metadata, historical cutoff, an
ordinary binary order book, market candlesticks, bounded concurrency observations,
raw bytes, redacted headers, and SHA-256 evidence. The local JSON fee-schedule
artifact is validated by the frozen release checks; the probe never downloads its
PDF provenance. Geographic or payload failures block cutover and
must not be replaced by a proxy, VPN, old capture, or local mock.

### Evidence record

Record redacted command output and immutable references in
`docs/evidence/kalshi-cutover-YYYY-MM-DD.json`. The record is blocked until it
contains the six gates (`offline`, `postgresql`, `built_image`, `private_resources`,
`provider_egress`, and `french_host`), database/object-storage snapshots, the image
digest, the active artifact, fee-schedule, and ten-migration hashes, the paper-only reachability
check, and the infrastructure-only rollback record. Never place credentials,
connection strings, private URLs, or raw authorization headers in the record.

## Archive boundary

Historical evidence lives under `docs/archive/predictionarena/`. It is read-only,
is not imported or loaded by runtime, tests, image builds, migrations, or fixture
discovery, and does not establish venue, schema, execution, or performance
equivalence. The release verifier fails when an active path crosses this boundary.
