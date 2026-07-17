# Runtime and private administration

Checked: 2026-07-16.

## Deployment order

The container image includes `config/`, `migrations/`, and the prompt/tool contracts in
`spec/`. It never includes `.env`. Coolify starts a one-shot migration service first;
the API and worker start only after that service exits successfully.

For a manual deployment, export the real environment resources and run:

```powershell
python -m vtrade.migrate
python -m uvicorn vtrade.api:create_app --factory --host 0.0.0.0 --port 8000
python -m vtrade.worker
```

## Explicit experiment and agent registration

Registration never starts the baseline implicitly. First register the immutable
definition, prompt, two model configurations, and a `ready` run. The command checks
the prompt/config/model fingerprints and fails if an existing record differs:

```powershell
vtrade-bootstrap register --config config/experiments/predictionarena-polymarket-v1.json `
  --prompt spec/prompt/predictionarena-polymarket-v1.md --code-version <commit-sha> `
  --run-label shadow-2026-07 --starts-at 2026-07-20T00:00:00Z
```

Adding an agent creates it paused with its independent hourly schedule disabled.
Starting or soft-removing one agent addresses only that agent and preserves all
history. Use a real configured model for the shadow run; no stub is authorized.

```powershell
vtrade-bootstrap add-agent --run-label shadow-2026-07 `
  --model-label "DeepSeek V4 Flash" --name deepseek-shadow
vtrade-bootstrap start-agent --run-label shadow-2026-07 `
  --name deepseek-shadow --starts-at 2026-07-20T00:00:00Z
vtrade-bootstrap remove-agent --run-label shadow-2026-07 --name deepseek-shadow
```

The database URL is read from `VTRADE_DATABASE_URL` by default; `--database-url-env` may name
another environment variable without putting the secret on the command line.

The migration runner uses a PostgreSQL advisory transaction lock and refuses a changed
checksum for an already applied migration. The worker checks the versioned experiment
configuration before acquiring leases or mutating external state. All required owner
decisions are now resolved; no incomplete tool handler or fake runtime is substituted.

## Scheduling and recovery

Each agent owns an independent hourly cursor in PostgreSQL. One scheduler transaction
holds an advisory lock, locks due cursors with `SKIP LOCKED`, records missed instants as
`skipped`, advances beyond them, and claims at most the current eligible instant. It
never backfills model decisions. Global and per-agent pause state is checked in the
claim query.

Cycles use expiring leases and immutable data cutoffs. The five persisted checkpoints
are market freeze, prompt, harness, broker, and settlement/valuation. A replacement
worker recovers expired work and reuses completed checkpoints; downstream financial
operations must retain their existing idempotency keys, so a crash after a side effect
cannot authorize a duplicate trade or settlement.

Raw artifacts are registered with at least six calendar months of retention. Cleanup
leases expired inventory rows, purges expired prompt/transcript/reasoning payloads,
strictly validates the content-addressed Supabase URI, deletes the private object, and
retains audit metadata. Storage and billed/nominal cost projections are persisted; this
does not claim that the seven-day observation window has elapsed.

Exa is governed by a separate monthly PostgreSQL circuit: 18,000 requests and 18,000
credits. Each search atomically reserves one of each before network I/O. Exa's nominal
20,000-micro-dollar search value remains auditable but does not consume the $40 billed
API breaker while the route is free. A positive `costDollars.total` response records a
critical alert, halts Exa, and raises instead of silently entering the dollar ledger.

## Private administration

Every registered route, including `/`, `/health/live`, and `/health/ready`, requires
the admin secret. Use either `Authorization: Bearer <secret>` or HTTP Basic with the
secret as the password. Never put the secret in a URL. API documentation and OpenAPI
routes are disabled, responses are non-cacheable, and the HTML dashboard has a
restrictive content-security policy.

Readiness probes the real PostgreSQL schema, private Supabase bucket, and runnable
configuration. It returns `503` while an owner decision or required resource is
missing. The dashboard/API expose leaderboard and PnL, drawdown, positions with the
strict 300-second bid status, trades, settlements, rejections, cycles, provider usage
and cost, freshness, alerts, and decision versions. Global and per-agent pause/resume
are the only control mutations; each requires an operator identity and idempotency key
and is committed with an append-only `operator_actions` audit record.

## Validation boundaries

Offline unit/recovery tests do not contact model or research providers. PostgreSQL
integration tests are opt-in and rollback-only:

```powershell
$env:VTRADE_RUN_POSTGRES_INTEGRATION='1'
python -m pytest tests/test_postgres_phase5_integration.py tests/test_postgres_phase6_integration.py
python -m pytest tests/test_postgres_phase9_integration.py
```

Both verifiers pass against the configured database after migrations `0005` and
`0006`; rollback checks confirm that their fixture rows are absent afterward.

The seven-day shadow observation, a scored baseline, and the 30-consecutive-day
operational gate remain time-based work. They cannot be claimed by code completion.
