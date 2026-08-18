# Runtime and private administration

Checked: 2026-08-18.

## Deployment order

The container image includes `config/`, `migrations/`, and the prompt/tool contracts in
`spec/`. It never includes `.env`. Coolify starts a one-shot migration service first;
the API starts only after that service exits successfully, and the worker starts only
after the API's authenticated `/health/ready` check reports healthy. A migration
failure therefore prevents both runtime services from starting.

The image uses the pinned Python `3.12.11-slim-bookworm` base and `uv 0.11.2` installer.
The Dockerfile copies `pyproject.toml` and `uv.lock` before installing the application,
fails at `uv lock --check` when project metadata is stale, and installs with
`uv sync --frozen --no-dev --no-editable`. The resulting `/app/.venv` is first on
`PATH`, so the Compose migration, API, and worker commands all use the locked runtime
environment without installing development dependencies.

For a manual deployment, export the real environment resources and run:

```powershell
python -m vtrade.migrate
python -m uvicorn vtrade.api:create_app --factory --host 0.0.0.0 --port 8000
python -m vtrade.worker
```

## Liveness, readiness, and operator preflight

Both health endpoints remain private and require the same Bearer or Basic
authentication as the rest of the API:

- `/health/live` is the cheap process/liveness probe. It confirms that the HTTP process
  is serving requests and does not contact PostgreSQL, Supabase, or a provider. Use it
  for restart diagnostics.
- `/health/ready` is the dependency/readiness probe. It checks the PostgreSQL schema,
  the private Supabase artifact bucket, and that the selected experiment configuration
  is runnable. It returns `200` only when every check passes and `503` otherwise.

The Compose API healthcheck calls `/health/ready` with
`VTRADE_ADMIN_AUTH_SECRET`. Python's HTTP client returns a failed healthcheck for a
`503`, so the worker dependency cannot become healthy while any readiness component
is unavailable. Readiness never calls OpenRouter, Exa, or Tavily.

Before starting a manual deployment, validate the rendered Compose configuration with
the real environment resources and then check both endpoints without putting the
secret in a URL:

```powershell
docker compose -f compose.coolify.yaml config --quiet
$headers = @{ Authorization = "Bearer $env:VTRADE_ADMIN_AUTH_SECRET" }
Invoke-WebRequest http://127.0.0.1:8000/health/live -Headers $headers -UseBasicParsing
try {
  $ready = Invoke-WebRequest http://127.0.0.1:8000/health/ready -Headers $headers -UseBasicParsing
} catch {
  throw "V-Trade is not ready: $($_.Exception.Message)"
}
$ready.Content
```

If migrations fail, or if the database, private bucket, or experiment configuration
fails its readiness check, keep the API/worker deployment stopped and resolve the
reported owner or infrastructure resource before retrying. No billed provider call
belongs in this preflight.

## Explicit experiment and agent registration

Registration never starts the active experiment implicitly. First register the immutable
definition, prompt, two model configurations, and a `ready` run. The command checks
the prompt/config/model fingerprints and fails if an existing record differs:

```powershell
vtrade-bootstrap register --config config/experiments/predictionarena-polymarket-v1-liquidity-aware.json `
  --prompt spec/prompt/predictionarena-polymarket-v1.md --code-version <commit-sha> `
  --run-label shadow-2026-07 --starts-at 2026-07-20T00:00:00Z
```

Adding an agent creates it paused with its independent hourly schedule disabled.
Starting or soft-removing one agent addresses only that agent and preserves all
history. Use a real configured model for the shadow run; no stub is authorized.

```powershell
vtrade-bootstrap add-agent --experiment-version predictionarena-polymarket-v1-liquidity-aware `
  --run-label shadow-2026-07 `
  --model-label "DeepSeek V4 Flash" --name deepseek-shadow
vtrade-bootstrap start-agent --experiment-version predictionarena-polymarket-v1-liquidity-aware `
  --run-label shadow-2026-07 `
  --name deepseek-shadow --starts-at 2026-07-20T00:00:00Z
vtrade-bootstrap remove-agent --experiment-version predictionarena-polymarket-v1-liquidity-aware `
  --run-label shadow-2026-07 --name deepseek-shadow
```

The database URL is read from `VTRADE_DATABASE_URL` by default; `--database-url-env` may name
another environment variable without putting the secret on the command line.

The migration runner uses a PostgreSQL advisory transaction lock and refuses a changed
checksum for an already applied migration. The worker checks the versioned experiment
configuration before acquiring leases or mutating external state. All required owner
decisions are now resolved; no incomplete tool handler or fake runtime is substituted.

The active liquidity-aware worker applies the configured `best-level-haircut-v1` only to
its private simulator capacity: it observes six raw levels to execute at most five positive
effective levels, while the agent-facing order-book response remains unchanged. The rule,
ignored shares, effective shares, and consumption are persisted per agent, cycle, token,
side, price level, and immutable snapshot for audit and idempotent retries. Changing the
haircut requires a new versioned active configuration; it must not rewrite snapshots or
historical baseline results.

## Scheduling and recovery

Each agent owns an independent hourly cursor in PostgreSQL. One scheduler transaction
holds an advisory lock, locks due cursors with `SKIP LOCKED`, records missed instants as
`skipped`, advances beyond them, and claims at most the current eligible instant. It
never backfills model decisions. Global and per-agent pause state is checked in the
claim query.

Cycles use expiring leases and immutable data cutoffs. The six persisted checkpoints
are market freeze, pre-prompt settlement, prompt, harness, broker, and final
settlement/valuation. A replacement
worker recovers expired work and reuses completed checkpoints; downstream financial
operations must retain their existing idempotency keys, so a crash after a side effect
cannot authorize a duplicate trade or settlement.

OpenRouter retries only explicit pre-inference 429 and 503 responses, at most three
total attempts, while respecting numeric `Retry-After` delays up to 60 seconds. Other
HTTP and transport failures remain fail-closed because their billing/side-effect state
can be ambiguous. If accumulated tool dialogue reaches the 88,000-token input ceiling,
the harness preserves the full transcript and ends normally as `assembled_input_limit`;
it does not compact evidence or issue an over-limit request.

The production worker claims one cycle per batch. Harness recovery reuses only a fully
persisted harness run and its inventoried artifacts; otherwise it fails closed and does
not recall OpenRouter or Exa. Exact resume within an unfinished model turn is deferred,
so an operator must review that failed cycle instead of the runtime guessing whether an
external side effect occurred.

Raw artifacts are registered with at least six calendar months of retention. Cleanup
leases expired inventory rows, purges expired prompt/transcript/reasoning payloads,
strictly validates the content-addressed Supabase URI, deletes the private object, and
retains audit metadata. Storage and billed/nominal cost projections are persisted; this
does not claim that the seven-day observation window has elapsed.

Exa is governed by a separate monthly PostgreSQL circuit: 18,000 requests and 18,000
credits. Each search atomically reserves one request and ten credits (the strict
ten-result maximum) before network I/O. Exa's nominal 20,000-micro-dollar search value
remains auditable but does not consume the $40 billed API breaker while the route is
free. Exa documents `costDollars.total` as an estimated nominal endpoint cost rather
than actual billing; it remains outside the billed-dollar ledger. Reconciliation records
actual credits and releases unused pending capacity. Credit use above the reservation
still records a critical alert, halts Exa, and raises.

The Tavily credential is present, but Tavily is intentionally disabled and future-only
for this experiment version. Credential presence does not enable the adapter, and no
live Tavily validation call belongs to the baseline procedure.

## Private administration

Every registered route, including `/`, `/health/live`, and `/health/ready`, requires
the admin secret. Use either `Authorization: Bearer <secret>` or HTTP Basic with the
secret as the password. Never put the secret in a URL. API documentation and OpenAPI
routes are disabled, responses are non-cacheable, and the HTML dashboard has a
restrictive content-security policy.

Readiness probes the real PostgreSQL schema, private Supabase bucket, and runnable
configuration. It returns `503` while an owner decision or required resource is
missing. The dashboard/API expose leaderboard and PnL, drawdown, positions with the
active 1,800-second archived-bid status (historical definitions retain 300 seconds),
live order-context freshness, trades, settlements, rejections, cycles, provider usage
and cost, alerts, and decision versions. Global and per-agent pause/resume
are the only control mutations; each requires an operator identity and idempotency key
and is committed with an append-only `operator_actions` audit record.

Freshness and valuation intentionally use different thresholds. The 300-second value
comes from `execution.maximum_order_book_age_seconds` and governs current order-book
and venue-sync freshness. Position valuation uses the persisted experiment definition's
`owner_decisions.no_bid_valuation.maximum_age_seconds` (falling back to
`limits.maximum_archived_bid_age_seconds`): it is 1,800 seconds for the active
`predictionarena-polymarket-v1-liquidity-aware` definition and 300 seconds for the
historical baseline. The dashboard and compatibility positions view read these values
from the stored definition, so a stale or missing bid is never displayed as a valid
liquidation value and net unrealized P&L remains
`liquidation_value_micros - cost_basis_micros - entry_fees_micros`.

The dashboard UI is a separate, read-only presentation module mounted by the private
API. Its 30-day default window can be changed to 24 hours, seven days, or the complete
run. The cycle explorer joins retained model reasoning, tool calls, research sources,
provider usage, belief and plan revisions, order execution, and runtime checkpoints
without importing worker or broker logic. Detailed payloads are marked unavailable
after retention cleanup; surviving audit metadata is never presented as if the raw
reasoning were still available.

## Validation boundaries

Offline unit/recovery tests do not contact model or research providers. PostgreSQL
integration tests are opt-in and rollback-only:

```powershell
$env:VTRADE_RUN_POSTGRES_INTEGRATION='1'
python -m pytest tests/test_postgres_phase5_integration.py tests/test_postgres_phase6_integration.py
python -m pytest tests/test_postgres_phase9_integration.py
python -m pytest tests/test_postgres_phase10_integration.py `
  tests/test_postgres_phase11_integration.py tests/test_postgres_bootstrap_integration.py
```

The phase 5, 6, 9, 10, 11, and bootstrap verifiers pass against the configured database;
rollback checks confirm that their fixture rows are absent afterward.

The seven-day shadow observation, a scored baseline, and the 30-consecutive-day
operational gate remain time-based work. They cannot be claimed by code completion.
