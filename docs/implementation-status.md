# Implementation status

Checked: 2026-07-18.

## Stable local lot

Phase 0 includes the source/feature matrix, the name-by-name matrix and canonical
inferred schemas for all 29 observed tools, a trace-derived prompt and conformance
checklist, a runnable versioned experiment definition, and a defensive raw-fixture
ingestor. Phase 1 includes the Python 3.12 project, canonical domain ports/types,
configuration hashing and run gate, append-only domain ledger, content-addressed gzip
artifact store, health API, PostgreSQL foundation migration, and Coolify service shape.

Local validation passes without network access. The PostgreSQL migration has also been
applied successfully to the configured database and verified to create 34 tables.
OpenRouter and Exa credential checks pass; this does not yet claim the later full
provider contract-test suite.

## Frozen owner decisions

The portfolio pagination contract, fee source, worst-case provider request estimates,
and Exa quotas are frozen. No owner decision remains `owner_pending`:

- model slugs are `deepseek/deepseek-v4-flash` and `xiaomi/mimo-v2.5-pro`;
- DeepSeek uses `fp8`; MiMo permits `fp8` and `unknown`;
- both models explicitly request the owner-fixed maximum reasoning effort;
- all compatible OpenRouter providers are allowed and sorted by price, with provider
  fallback enabled but cross-model fallback forbidden;
- the paper broker buys at best ask, sells at best bid, does not require a counterparty,
  and rejects an order when its required quote is absent;
- held positions use the latest archived executable bid for valuation with a strict
  maximum age of 300 seconds; a missing or older bid blocks the snapshot and scoring;
- prompts, transcripts and reasoning are operator-only, retained for six months, with
  secrets, tokens and authorization headers always redacted;
- Exa has a strict ceiling of 50 searches per agent-cycle;
- every web search has a strict maximum of 10 returned results;
- Exa has separate strict PostgreSQL monthly caps of 18,000 requests and 18,000
  credits; reservations are atomic and Exa is excluded from the $40 billed-dollar
  breaker while it is on the free plan;
- the owner-provided empirical expectations are 8 searches on average among cycles
  that use search and 3.5 searches on average across all cycles; this workstream has
  not independently verified those two planning figures;
- OpenRouter reserves 14,000 micro-dollars per DeepSeek request and 50,000
  micro-dollars per MiMo request, rounding upward from the exact full-context bounds
  under the owner-approved $0.12/$0.24 and $0.44/$0.88 price ceilings;
- Exa records a conservative nominal 20,000 micro-dollars per search without consuming
  the dollar breaker; `costDollars` is provider-estimated nominal cost, not evidence of
  actual billing. The strict 18,000-request cap remains below the free allowance;
  Tavily's future basic route retains an 8,000-micro-dollar bound, but the current
  adapter is intentionally disabled and never contacts the provider;
- every configured model has a 100,000-token context, reserves 12,000 tokens for
  output, and therefore accepts at most 88,000 tokens of assembled input;
- tool-call arguments and ordinary tool results are capped at 4,000 tokens;
  `get_portfolio` may return up to 24,000 tokens and must paginate beyond that limit;
- `get_portfolio` uses an optional opaque cursor and a 1-to-200 item limit (default
  100), returns `items`, `next_cursor`, and `has_more`, and orders an immutable
  per-agent-cycle snapshot by ascending `position_id`; invalid or foreign cursors fail;
- agents have independent start dates, hourly schedules and data cutoffs; adding or
  removing one does not alter the others, and simultaneous start is not required.

The owner-approved 20,313,102-byte `/cycles?offset=0&limit=200` capture contains 200
unique cycles, 20 for each of 10 observed models. It is ingested as a byte-exact,
content-addressed gzip artifact. The manifest records its hash, byte length, cycle
count, source cutoff, endpoint and ingestion time; raw prompts and reasoning are not
printed into logs or documentation.

## External validation

All currently required owner resources are valid:

- PostgreSQL migrations `0001` through `0011` applied to the configured database;
- Supabase service-role format accepted and the private bucket validated end-to-end by
  metadata read, byte-exact authenticated upload/read and delete;
- OpenRouter and Exa credentials accepted; the Tavily credential is present but remains
  intentionally unused under the owner-frozen future-only policy;
- admin authentication secret supplied.

The authenticated readiness probe was rerun on 2026-07-18 against the real services:
an unauthenticated request returned `401`, while the authenticated request returned
`200` with PostgreSQL at migration `0011`, private Supabase storage healthy, and the
experiment configuration runnable. The final offline suite has 159 passing tests and
8 explicitly skipped opt-in integrations; Ruff, mypy, and `git diff --check` pass.

Phase 2 adds the real read-only Gamma/CLOB adapter, a mandatory
`SupabaseArtifactStore`, bounded live public contract probes and byte-exact offline
replay fixtures. Migration `0002` adds append-only venue sync pages plus tradeability and
indicative-price fields. Full provider behavior tests and private admin routes remain
work for their respective implementation phases, not missing owner resources.

Phase 3 adds both deterministic paper policies, fee-aware solvency, the 15% cost-basis
cap, weighted-average positions, oversell protection, cutoff-safe idempotent settlements,
posting-only portfolio replay, and the transactional PostgreSQL broker repository.
Migration `0003` adds replay dimensions, fee-parameter audit fields, idempotency
fingerprints and optimistic portfolio versions. It has been applied successfully to
the configured PostgreSQL database.

Phase 4 adds strict OpenRouter routing for the two frozen model slugs, an Exa search
adapter, a deliberately disabled future-only Tavily adapter, recursive
tool-schema validation, the bounded 32-turn/100-tool/50-search harness, private
per-agent beliefs and plans, deterministic critical learning, offline model replay,
redacted raw-response artifacts, and monthly budget reservations with separate billed
and nominal cost accounting. Migration `0004` persists harness runs, tool records,
provider telemetry, replay records, agent memory fingerprints and the transactional
monthly circuit breaker. Migration `0004` has been applied successfully to the
configured PostgreSQL database. A rollback-only, opt-in real-PostgreSQL verifier covers
its budget, memory and harness repositories without leaving test rows. That verifier
passes against the configured database and confirms the rollback removed its fixture
rows; the default offline suite skips it unless explicitly enabled. No paid provider
request is part of local validation.

Pre-request payload enforcement uses a strict, provider-neutral UTF-8 byte-count upper
bound. It is safe and intentionally conservative; it is not claimed to equal either
provider's native tokenizer count.

Migration `0007` persists immutable per-agent-cycle portfolio query snapshots and
server-side opaque page cursors. Pages never re-query mutable live positions, so
portfolio mutations between calls cannot create overlap or gaps. Page assembly trims
before the strict provider-neutral 24,000-token upper bound. The pagination owner
decision is resolved. The Tavily key is supplied, but owner policy keeps the adapter
disabled and future-only; no live Tavily call is made or claimed.

The fee-policy source is resolved to the official public Polymarket CLOB
`GET /fee-rate/{token_id}` endpoint. Fee payloads are archived before normalization,
persisted as immutable per-token basis-point observations, and accepted by the broker
only when their IDs belong to the current cycle freeze and their timestamps are no newer
than the finalized cycle cutoff. No zero or 5% default is substituted. Migration `0009`
adds the immutable fee snapshots. No required owner decision remains unresolved.

Migration `0010` adds the owner-frozen Exa quota ledger. Migration `0011` makes every
new search atomically reserve one request and ten credits (the strict maximum result
count), then reconciles actual credits and releases unused pending capacity. It caps
both monthly totals at 18,000, persists nominal
cost independently of the global billed-dollar breaker, and records a critical alert
before raising if Exa reports more credits than reserved.
Migration `0011` also adds `pre_settlement` to the durable stage checks and applies RLS
plus exact V-Trade-object revokes for `anon`/`authenticated`, without changing shared
schema/default privileges. Migrations `0009` through `0011` are applied. Their real
PostgreSQL verifiers pass: normalized freeze/frozen same-cycle fee lookup, pending Exa
quota enforcement/reconciliation, RLS/grants/stage constraints, and explicit isolated
bootstrap all complete without leaving fixture rows.

Migration `0012` permits an unchanged frozen configuration fingerprint to be registered
under a distinct code version. The executable definition is uniquely identified by the
pair `(config_sha256, code_version)`, so a bug-fix rerun remains auditable without
mutating or relabelling the owner-approved experiment configuration.

Migration `0013` applies the same definition scope to the immutable prompt fingerprint.
A byte-identical prompt may therefore be attached to a new code-versioned definition,
while remaining unique and immutable within that definition.

Artifact inventory registration now extends, but never shortens, the authoritative
expiry to six calendar months from the later database registration timestamp. This
removes a millisecond-scale timestamp race observed on both first v3 cycles while
preserving the original strict database constraint and preventing early cleanup.

The OpenRouter Chat Completions payload uses the provider-advertised `max_tokens`
parameter. Sending `max_completion_tokens` together with strict parameter support had
excluded every otherwise-compatible endpoint and produced a pre-inference 404 on both
v4 routes. A bounded live probe confirmed the corrected field routes successfully.

Migration `0014` corrects the Exa response-cost semantics discovered by the first v5
search. It resolves only the false `exa_unexpected_billed_cost` alert and clears its
derived halt/billed counters while preserving the completed request and credit usage.

Phase 5 runtime infrastructure now provides independent hourly PostgreSQL schedule
cursors, atomic skipped-slot recording without backfill, advisory leases, restart
recovery from typed stage checkpoints, idempotent orchestration boundaries, actual
post-sync cutoff finalization, transactional normalized Polymarket snapshot persistence,
fail-closed harness recovery from a fully persisted run without repeating provider calls,
six-month
Supabase retention cleanup, payload purging, storage/cost projections, and deduplicated
stale-data/failure/budget/ledger/drawdown alerts. Migration `0005` has been applied to
the configured PostgreSQL database. Its rollback-only real PostgreSQL verifier passes
without leaving fixture rows. The required
seven-day shadow observation has not been completed.

Exact per-model-turn continuation inside an interrupted, not-yet-persisted harness run
is deferred. Such a recovery fails the cycle closed; it never repeats OpenRouter or Exa
calls. A later scheduled cycle may proceed independently after operator review.

Phase 6 adds an authenticated private API and HTML dashboard, real PostgreSQL/Supabase/
config readiness probes, fixed bounded operator views, strict stale-bid display,
security/no-store headers, disabled API documentation, and audited idempotent global or
per-agent pause/resume controls. Migration `0006` has been applied to the configured
database; its rollback-only verifier executes every fixed view and audited control
successfully without leaving fixture rows. `operator_actions` remains protected by its
foundation append-only trigger. No public leaderboard or unauthenticated registered
route exists. See
`docs/runtime-operations.md` for deployment and verification boundaries.

Code completion does not satisfy the seven-day shadow gate or the 30-consecutive-day
baseline gate; both remain pending elapsed observation time.

This workstream does not read or display `.env`; live integration consumes it only
through the runtime configuration boundary.
