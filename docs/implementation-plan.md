# V-Trade implementation plan

Status: approved baseline plan
Date: 2026-07-16

## 1. Objective

Build a long-running, auditable prediction-market experiment that reproduces the publicly documented behavior of PredictionArena as closely as practical, with four intentional initial differences:

1. one cycle every 60 minutes instead of roughly every 15-45 minutes;
2. a provider-neutral web-search tool, initially backed by Exa;
3. models accessed through OpenRouter, initially DeepSeek V4 Flash and MiMo V2.5 Pro;
4. paper trading on live Polymarket data instead of real-money execution.

The first goal is not to improve PredictionArena. It is to establish a stable baseline whose deviations are explicit, versioned, measurable, and reversible. Later changes must run as new experiment versions rather than mutating the baseline.

## 2. What “faithful copy” means

PredictionArena's public paper documents the architecture and behavior, but not its source code, complete prompts, JSON schemas, discovery thresholds, sampling parameters, exact paper-fill algorithm, or per-cycle spending cap. V-Trade can therefore be a behavioral reproduction, not a bit-for-bit clone.

The public site's dynamic JSON adds substantially richer observable evidence than the paper alone. In particular, `/cycles` exposes rendered prompts, reasoning, research data, ordered tool calls, settlements, and before/after account, order, and position snapshots. These dated responses are reproduction fixtures, not an operational dependency or proof of hidden implementation details. Their audit is documented in [predictionarena-polymarket-endpoints.md](predictionarena-polymarket-endpoints.md).

Every baseline feature must be marked as one of:

- `documented`: stated in a primary PredictionArena source;
- `inferred`: a conservative implementation choice needed to fill a published gap;
- `vtrade_deviation`: one of the intentional cost or venue changes above.

Store this classification alongside configuration and prompt versions. Do not call an inferred value “PredictionArena's value.”

## 3. Baseline invariants

These rules define experiment version `predictionarena-polymarket-v1`:

- Each agent starts with an isolated virtual balance of $10,000.
- Agents share the same harness, prompt template, tool schemas, market snapshot cutoff, risk rules, and search-provider class of service.
- Agents have private portfolios, beliefs, plans, notes, and previous-cycle reasoning.
- Each agent has an independent 60-minute schedule and immutable per-cycle data cutoff. A slow or failed cycle is recorded and skipped; missed cycles are not replayed in a burst.
- A cycle follows: synchronize markets -> process settlements -> freeze context -> run agent -> validate actions -> execute paper trades -> value portfolio -> persist metrics.
- Agents can only use explicitly exposed research, discovery, account, trading, belief, and planning tools. They receive no shell, database, filesystem, wallet, or arbitrary HTTP access.
- The prompt teaches the two documented strategies: fundamental outcome trading and pre-settlement price trading.
- The prompt requires research, explicit YES and NO winning-condition statements, probability or price-target estimation, side/rule verification, expected-value analysis after fees/gas, sizing, an exit plan, portfolio review, and then execution.
- Maximum cost basis per market is 15% of the agent's current account value.
- No leverage, transfers, negative cash, or cross-agent netting is allowed.
- Open positions are valued at executable bid/liquidation value, not entry price or midpoint.
- Successive buys use weighted-average cost basis.
- All raw inputs, model outputs, tool calls, validation decisions, fills, settlements, and valuations are timestamped and auditable.
- Profit/account value is the primary score. Forecast-quality and operational metrics remain separate diagnostic scores.

## 4. Recommended technical shape

Use the owner-approved modular-monolith architecture in Python 3.12 rather than microservices. It fits the 4-vCPU/8-GB VPS, minimizes operational overhead, and has strong data, HTTP, and evaluation libraries. Split it into independently testable packages and only separate processes where reliability requires it.

Recommended runtime components:

| Component | Responsibility | Deployment |
|---|---|---|
| API/admin | authenticated private health, run controls, experiment views, manual pause/resume | one small container |
| Scheduler/worker | hourly cohort orchestration and agent tool loops | one container; PostgreSQL lock prevents duplicate cycles |
| PostgreSQL | durable state, append-only financial ledger, indexed metadata | existing Supabase |
| Object storage | compressed raw provider payloads, prompts, transcripts, snapshot artifacts | existing Supabase bucket |
| Dashboard | private authenticated operator UI; no public routes in v1 | phase 6; can share API deployment |

Suggested libraries are FastAPI, Pydantic, SQLAlchemy, Alembic, `httpx`, and a small in-process scheduler. Avoid Redis/Celery initially. PostgreSQL advisory locks plus idempotency keys are enough for one worker and make restarts safe.

## 5. Architecture boundaries

The core domain must not import Tavily, Exa, OpenRouter, Polymarket, or Kalshi SDK types. Provider adapters translate external payloads into these canonical interfaces:

```text
ModelGateway
  complete(messages, tools, model_config) -> ModelTurn

ResearchProvider
  search(query, options) -> SearchResultSet
  fetch(url, options) -> Document

MarketVenue
  sync_markets(cursor) -> MarketDelta
  get_order_book(outcome_ids) -> OrderBookSnapshot
  get_resolutions(market_ids) -> ResolutionSet

Broker
  place(order, portfolio, snapshot) -> ExecutionResult

Clock
  now() -> timestamp
```

Initial adapters:

- `OpenRouterModelGateway`
- `ExaResearchProvider` as the only enabled baseline research adapter
- optional contract-tested `TavilyResearchProvider` for a future experiment version
- `PolymarketVenue`
- `PredictionArenaPaperBroker`

Future adapters should be additive:

- `KalshiVenue`
- `LiquidityAwarePaperBroker`
- `PolymarketLiveBroker`
- direct model-provider gateways if needed

The AI-facing tool names and response envelopes must stay stable when an adapter changes. Provider name, latency, cost, cache status, and raw-response hash belong in telemetry, not in agent-visible content unless the provider itself returns it as evidence.

## 6. Canonical domain model

Keep venue-specific identifiers but normalize the concepts the harness consumes:

- `Event`: topic containing one or more markets.
- `Market`: question, full resolution rules, open/close times, status, category, volume, liquidity, and venue metadata.
- `Outcome`: YES/NO or named outcome, venue token ID, best bid/ask, tick size, and minimum order size.
- `OrderIntent`: agent, market, outcome, buy/sell, amount or shares, strategy, thesis, estimated probability, expected value, and timestamp.
- `Order`, `Fill`, `Position`, `Settlement`, and `LedgerEntry`: deterministic execution/accounting records.
- `Belief`: structured confidence/thesis with evidence, constrained category, agent ownership, and revision history.
- `Plan`: an agent-owned `long_term` or date-bound `next_cycle` plan, with status, due time, and revision history.
- `CycleSnapshot`: the immutable set of market/account/history data visible to an agent.

Use integer micro-dollars or fixed-precision decimals for money and shares. Never use binary floating point in accounting.

## 7. Data and audit design

Minimum PostgreSQL tables:

- `experiment_definitions`, `experiment_runs`, `agents`, `model_configs`;
- `cohort_cycles`, `agent_cycles`, `cycle_contexts`, `prompt_versions`;
- `model_turns`, `tool_calls`, `provider_usage`, `research_documents`, `research_artifacts`;
- `events`, `markets`, `outcomes`, `market_snapshots`, `order_book_snapshots`, `resolutions`;
- `order_intents`, `orders`, `fills`, `positions`, `settlements`;
- `ledger_entries`, `performance_snapshots`;
- `beliefs`, `belief_revisions`, `plans`, `plan_revisions`;
- `alerts`, `operator_actions`.

Financial events and experiment definitions are append-only. Corrections use reversing entries or superseding versions, never destructive edits.

For each agent-cycle, preserve:

- scheduled time, actual start/end, data cutoff, and status;
- V-Trade's exact role-separated messages and tool schemas, plus the byte-exact rendered cycle prompt;
- model slug, provider route if known, parameters, and response identifiers;
- every tool request/result in order, including call ID, category, display name, arguments, raw output, success, and timestamp;
- prompt, completion, reasoning, and cached token counts when available;
- all costs and search credits/requests;
- pre/post portfolio state and the exact market snapshot IDs used;
- validation/fill/rejection reasons;
- code version, database migration, experiment version, and config hash.

Store large raw JSON and transcripts compressed in object storage, with content hash and pointer in PostgreSQL. Retain normalized searchable fields in PostgreSQL. Do not snapshot every order book on Polymarket: store market metadata deltas broadly, and order books for shortlisted, viewed, held, or traded markets. This protects the VPS's 75-GB disk and the database from unnecessary growth.

Imported PredictionArena fixtures must also record endpoint, `checked_at`, source cutoff, raw content hash, and completeness status. The fixture ingestor must deduplicate by stable record ID and must not interpret `/cycles.count` as a global total or trust `hasMore` alone. Preserve raw external position projections separately from the canonical V-Trade position/ledger model, including `negativeRisk`, opposite outcome/asset, outcome index, redeemable, and mergeable fields.

The public PredictionArena read API confirms that `/cycles` is the richest observable artifact: each cycle includes a rendered `prompt`, `reasoning`, before/after portfolio snapshots, `tool_calls`, and cycle status. The endpoint does not separate system and user messages or expose complete tool schemas, so store the field as an exact `rendered_cycle_prompt` and classify the missing separation/schemas as `inferred`. `/actions` and `/markets` are presentation-oriented views and must not be treated as the canonical financial ledger or complete market-data source. See [the endpoint audit](predictionarena-polymarket-endpoints.md) and the compact Python reports referenced there.

## 8. Market discovery and data flow

The Polymarket adapter should use the appropriate public APIs separately:

- Gamma for events, markets, rules, tags, dates, volume, and liquidity;
- CLOB for executable prices and order books;
- resolution/on-chain or documented resolution sources for settlement verification;
- Data API only where account/trade data is relevant to a future live broker.

Expose provider-neutral discovery tools matching the documented PredictionArena capabilities:

- search markets by keywords;
- browse categories/tags;
- filter by volume, liquidity, volatility, price range, status, and time to expiry;
- list trending or recently moving markets;
- inspect complete rules and outcome prices;
- inspect one or more order books.

For baseline compatibility, phase 0 must map every observed PredictionArena name to a canonical implementation while preserving the AI-facing name. The dated trace currently contains 29 names across research/discovery, account/history, memory/plans, and trading, including `web_search`, `discover_events`, `discover_hot_markets`, `discover_by_time_remaining`, `get_market_details`, `get_orderbook`, `get_balance`, `get_portfolio`, `get_general_beliefs`, `create_general_belief`, `create_long_term_plan`, `create_next_cycle_plan`, and `place_market_order`. Observed argument forms are evidence; unobserved optional fields, enums, schemas, and authorization limits remain `inferred`.

Quality thresholds are unpublished, so put them in a named, versioned `discovery_policy`. Initial values must be labelled inferred and tested against live market distributions before freezing the baseline.

All agents in an hourly cohort receive the same frozen market-data cutoff. Discovery calls may reveal different subsets, but they query the same versioned cache/snapshot. This avoids model order creating a hidden advantage.

## 9. Agent harness

Implement a bounded model/tool loop, not a single completion:

1. Build the agent's immutable cycle context.
2. Call the configured model with the common system prompt and provider-neutral tool schemas.
3. Validate each tool call against its schema and authorization.
4. Execute tools, append results, and continue until the model ends or a limit is reached.
5. Parse trade intents, run deterministic validation, and pass accepted orders to the broker.
6. Persist the final reasoning/summary and update beliefs/plans only through tools.

Store cycle status, model termination status, and individual tool-call status independently. Public traces included failed cycles even when all recorded tool calls were successful, so tool success must never imply cycle success.

The observed public traces show 29 distinct tool names across discovery, account/history, knowledge/plans, and trading, including `place_market_order`, `get_market_details`, `get_orderbook`, `web_search`, and structured belief/plan tools. Reproduce these as versioned provider-neutral schemas, but do not infer a complete contract from observed arguments alone. Record both tool-call success and cycle-level status: the public sample contains failed cycles whose individual tool calls are still marked successful.

Limits must be configuration, recorded per run:

- maximum model turns, total tool calls, and wall-clock time per cycle;
- maximum prompt/context and output tokens;
- owner-provided empirical planning expectations of 8 web searches on average conditional on a cycle using search and 3.5 across all cycles, not independently verified by this workstream, tracked separately from market-discovery calls;
- a strict ceiling of 50 Exa searches per agent-cycle, plus a separate total-tool-call ceiling above the observed trace maximum of 92;
- independent strict monthly Exa limits of 18,000 requests and 18,000 credits, plus a
  billed-dollar limit for providers other than free-plan Exa;
- maximum discovery calls and markets returned;
- maximum trade intents and notional spend per cycle;
- maximum active beliefs per agent/model, configurable per experiment;
- a hard $40 billed external API budget per calendar month, excluding Exa while it is
  on the free plan, with alerts at $20, $32, and $40 and an emergency stop before a
  request can knowingly exceed the remainder. Exa still records nominal value; any
  positive billed Exa cost halts and alerts Exa immediately.

The baseline prompt should be reconstructed from the public methodology and dated dynamic cycle traces, then frozen as a versioned artifact. Preserve imported rendered prompts byte-for-byte before normalization: observed traces contain an unresolved `{trading_tool_ref}` placeholder and possible repeated/concatenated protocol blocks. V-Trade rendering tests should fail on unintended placeholders or duplicated protocol blocks. Changes require a new experiment version. Do not tailor the prompt to an individual model unless running an explicitly separate ablation.

The “critical learning” section is unpublished. Version 1 should generate it deterministically from recent settled winners/losers, early exits, rejected trades, drawdown, and concentration events. It must not use another LLM, both to control cost and to avoid adding an undocumented model-dependent component.

## 10. Paper execution and accounting

Support two paper policies from the start, but run only one in the baseline:

### `predictionarena_unconditional` (baseline)

Represents the documented PredictionArena paper-trading advantage: an otherwise valid order does not fail because a counterparty is absent. The exact unpublished price rule is owner-confirmed, documented as inferred, and frozen:

- buy at the current best ask and sell at current best bid;
- if the required side has no quote, reject rather than invent a price;
- record exchange fees using the market's current fee parameters;
- use the same tick/minimum-size validation as a live order.

### `liquidity_aware` (future comparison)

Walk timestamped order-book levels, permit partial fills, and reject unfilled remainder according to FAK/FOK semantics. Never combine results from this mode with the baseline leaderboard without a visible cohort label.

Accounting requirements:

- double-entry ledger for cash, position cost, proceeds, fees, realized PnL, and settlement payout;
- weighted-average cost for additions;
- sells cannot exceed owned shares;
- concentration is calculated from cost basis and capped at 15% of current account value;
- solvency includes fees and pending accepted orders;
- account value equals cash plus positions valued at the latest archived executable bid;
  the bid may be at most 300 seconds old at the immutable valuation cutoff, and a
  missing or older bid blocks that account snapshot and its scoring rather than using
  zero or an unbounded stale price;
- settlements are idempotent and independently reconcilable;
- resolution and settlement ingestion always applies the cycle's as-of cutoff and stores source creation and settlement timestamps to prevent look-ahead leakage;
- every derived balance must be reproducible from ledger entries alone.

## 11. Metrics

Primary:

- account value;
- realized, unrealized, and total PnL;
- return on initial $10,000.

Secondary:

- settled-position win rate, with a separate early-close win rate;
- maximum drawdown;
- turnover, exposure, concentration, and cash utilization;
- number of trades, holds, exits, rejections, and partial fills.

Forecast diagnostics:

- Brier score and calibration bins for beliefs recorded before resolution;
- edge at entry and closing-line movement;
- performance by category, horizon, strategy, and confidence band.

Efficiency and reliability:

- tokens and model cost per cycle/trade/dollar of PnL;
- research calls/credits, unique domains, cache hits, and research cost;
- tool errors, invalid calls, cycle latency, skipped/failed cycles, data freshness;
- discovery-to-inspection and inspection-to-trade conversion.

Never use win rate alone as the headline measure, and do not compare paper and live returns as if their execution conditions were equal.

## 12. Deployment and operations

Deploy through Coolify as one repository with API and worker services. Use the existing Supabase PostgreSQL and bucket. Keep model/search/live-trading secrets only in Coolify environment secrets; never expose the Supabase service role or trading credentials to the browser or model. All v1 dashboard and API routes are private and authenticated.

Required operational controls:

- global pause and per-agent pause;
- database-backed lease/advisory lock for each scheduled cohort;
- idempotency keys on cycles, orders, fills, and settlements;
- startup recovery for interrupted cycles without re-executing trades;
- health/readiness endpoints and last-success timestamps;
- alerts for stale market data, consecutive failures, budget thresholds, ledger mismatch, and abnormal drawdown;
- daily database backup verification and periodic restore drill;
- structured logs with secret and reasoning redaction controls;
- retention policy and storage-growth alerts;
- automatic halt at the $40 monthly billed API budget, not merely a notification.
  Free-plan Exa is excluded from that dollar circuit but has strict atomic 18,000
  request and 18,000 credit monthly caps. Promotional/student credits retain request
  counters and nominal usage value even when billed cost is zero.

Run agent model calls sequentially or with very low concurrency to fit the VPS. Freeze data first so execution order does not affect fairness. The external APIs, not CPU, are expected to dominate cycle time.

## 13. Implementation phases and acceptance gates

### Project execution workflow

Code implementation and heavy research should be delegated to sub-agents and reviewed by the primary agent before acceptance. Route the hardest work to the `Luna xhigh` workstream first and medium-difficulty work to `Terra medium`. The primary agent remains responsible for reconciling conclusions with sources, reviewing changes, and running the relevant tests. These labels express the owner's routing preference; where the execution environment cannot select or verify an underlying model, it must say so rather than imply that it did.

### Phase 0 — Freeze the reproduction specification

Deliver:

- a source-to-feature matrix classifying every rule as documented, inferred, or V-Trade deviation;
- a dated, hashed PredictionArena cycle-fixture corpus and defensive fixture ingestor;
- a name-by-name compatibility matrix for all 29 currently observed tools, with observed argument evidence separated from inferred schemas;
- canonical tool schemas, trace-derived initial prompt, and prompt-conformance checklist;
- experiment definition schema and config file;
- explicit answers to the open decisions in section 15.

Gate: a reviewer can identify every known deviation from PredictionArena and every inferred value without reading code.

### Phase 1 — Repository and persistence foundation

Deliver:

- Python 3.12 project, lint/type/test setup, migrations, configuration validation;
- core domain types and provider interfaces;
- append-only ledger skeleton, object-artifact store, health endpoints;
- reproducible local environment and Coolify service definitions.

Gate: migrations apply cleanly to a disposable database; config hashes are stable; duplicate idempotency keys cannot create duplicate financial events.

### Phase 2 — Polymarket read-only adapter

Deliver:

- market/event sync with pagination and rate limiting;
- normalized outcomes and rules;
- discovery tools and on-demand CLOB snapshots;
- resolution ingestion with strict as-of cutoff and raw-payload archiving.

Gate: sampled normalized markets reconcile with official API values; stale/closed/ambiguous markets cannot be presented as tradeable; identical cutoff queries reproduce identical context.

### Phase 3 — Paper broker and accounting

Deliver:

- deterministic validation and both paper policies;
- positions, weighted cost, fees, sells, settlements, and bid valuation;
- concentration/solvency enforcement and full rejection taxonomy;
- property and scenario tests for accounting invariants.

Gate: ledger replay exactly reconstructs all balances; duplicate execution/settlement is harmless; no generated test sequence creates negative cash, overselling, or concentration above the configured cap.

### Phase 4 — Model and research harness

Deliver:

- OpenRouter gateway with DeepSeek and MiMo configs;
- Exa as the enabled baseline adapter and Tavily as a contract-tested alternate behind the same tool schema;
- bounded tool loop, prompt builder, beliefs/plans, deterministic critical-learning summary;
- separate web-search versus market-discovery telemetry, token/search/cost accounting, the owner-provided 8 conditional / 3.5 all-cycle search expectations, burst ceilings, monthly request/credit limits, and the $40 circuit breaker.

Gate: a recorded model response can be replayed without external calls; swapping search adapters does not change AI-facing schemas; one agent cannot read another's memory; malformed tool calls cannot mutate financial state; the hard call ceiling is compatible with the observed 92-call trace maximum.

### Phase 5 — End-to-end shadow run

Run at least 7 days with one inexpensive model or a stub before the scored cohort.

Verify:

- hourly scheduling across restarts;
- settlement and valuation correctness;
- actual storage growth and a projected all-in API spend no greater than $40/month;
- Exa searches per cycle and burst distribution against the owner-provided 8 conditional / 3.5 all-cycle empirical expectations, plus search-credit consumption and model tool-call compatibility;
- prompt/context sizes, including comparison to the observed 9,012-27,904-character rendered-prompt range, latency, data freshness, and alerts;
- no manual database corrections are needed.

Gate: zero unexplained ledger mismatches, zero duplicate cycles/trades, successful recovery tests, and projected spend within the agreed monthly cap.

### Phase 6 — Private admin dashboard and production hardening

Deliver operator views for:

- agent account value/PnL/drawdown and cohort leaderboard;
- positions, trades, settlements, and rejection reasons;
- cycle status, model/search usage, costs, data freshness, and alerts;
- prompt/config/code version for every decision;
- pause/resume controls with audit log.

Gate: all experiment state is observable without direct database access; every route requires authentication; controls use least privilege and every operator action is audited. There is no public leaderboard or unauthenticated route in v1.

### Phase 7 — Start the frozen baseline

Create each configured agent with its own start date, initial capital, immutable per-cycle cutoff and the same frozen experiment config version. Simultaneous start is not required, and adding or removing an agent does not alter existing agents. Do not backfill missed decisions. Generate and store weekly immutable operator summaries and a monthly cost/reliability report.

Gate for calling the baseline operational: 30 consecutive days without accounting discrepancies or silent configuration changes.

### Phase 8 — Controlled improvements

Only after the baseline is operational, evaluate changes as parallel or replayed experiment versions:

- different models or reasoning budgets;
- Tavily versus Exa;
- 30- versus 60-minute cycles;
- critical-learning variants, search budgets, or discovery thresholds;
- liquidity-aware paper execution;
- Kalshi adapter;
- real-money broker after legal/availability review and explicit deployment approval.

## 14. Test strategy

Use three layers:

1. Unit/property tests for money math, positions, risk, parsing, config, and metrics.
2. Contract tests against recorded provider fixtures for OpenRouter, each search API, and each market venue.
3. Replay/end-to-end tests using frozen timestamps and raw artifacts, with network access disabled during replay.

Critical scenarios include no bid/ask, crossed or stale books, partial liquidity, fee changes, minimum size/tick rounding, cancelled/ambiguous/resolved markets, 50/50 resolution, repeated settlement, model timeout, malformed/duplicate tool calls, provider outage, budget exhaustion, worker restart after intent but before fill, and database outage after fill persistence.

Record fixtures with timestamps and source hashes. Never allow a replay to fetch newer web or market data, because that introduces look-ahead leakage.

## 15. Owner decisions and remaining questions

Frozen owner decisions:

1. **Monthly billed API budget:** hard ceiling of $40 per calendar month, excluding
   free-plan Exa; a positive billed Exa cost halts and alerts Exa.
2. **Research provider:** Exa is the enabled baseline provider; Tavily is only an alternate for a future version.
3. **Language:** Python 3.12.
4. **Dashboard:** private, authenticated, and admin-focused; no public v1 interface.
5. **Research capacity:** the owner reports empirical expectations of 8 searches on average conditional on a cycle using search and 3.5 across all cycles. This workstream has not independently verified those figures. The strict safety ceilings are 50 searches per agent-cycle, 10 results per search, 18,000 Exa requests/month, and 18,000 Exa credits/month.
6. **Delivery workflow:** delegate code and heavy research by difficulty to Luna xhigh and Terra medium workstreams, with primary-agent review.

Additional frozen owner decisions:

1. **Paper fill rule:** `best ask/bid, no counterparty requirement, reject absent quote` is the initial inferred PredictionArena approximation.
2. **Model routing:** DeepSeek permits only `fp8`; MiMo permits `fp8` and `unknown`. Both explicitly request the owner-fixed maximum reasoning effort. Every compatible provider is allowed and sorted by price; provider fallback is enabled, but cross-model fallback is forbidden.
3. **Prompt/transcript visibility:** prompts, transcripts and reasoning are operator-only, retained for six months, with secrets, tokens and authorization headers always redacted.
4. **Experiment comparison:** agents are isolated and have independent start dates and hourly schedules. Adding/removing one has no effect on the others.

### 15.1 Evidence from the public Polymarket read API

The endpoint audit found a rendered protocol with five mandatory stages: strategy selection, efficient research, YES/NO side verification, expected-P&L calculation, and sizing/execution. It also exposed persistent general beliefs, long-term plans, and next-cycle plans. This narrows the reconstruction target for phase 0, but does not make the prompt or tool schemas public in full: system/user role separation, sampling parameters, hidden limits, retries, and the exact critical-learning generator remain open decisions. Keep the endpoint-derived prompt and tool inventory as dated fixtures, not as claims of source-code equivalence.

## 16. Definition of done for version 1

V-Trade v1 is complete when two configured OpenRouter models can run hourly for 30 days against immutable live Polymarket cutoffs on their independent schedules, use Exa through a provider-neutral research tool and private persistent knowledge, place validated paper trades through the frozen baseline fill policy, settle and value positions reproducibly, expose a complete audit trail and private authenticated admin dashboard, remain at or below the $40 monthly billed API cap and the 18,000/18,000 Exa request/credit caps plus storage limits, and recover from restarts without duplicate or missing financial events.

## Primary references

- [Prediction Arena paper](https://arxiv.org/html/2604.07355v1)
- [Polymarket API introduction](https://docs.polymarket.com/api-reference/introduction)
- [Polymarket order book concepts](https://docs.polymarket.com/concepts/prices-orderbook)
- [Polymarket order creation](https://docs.polymarket.com/trading/orders/create)
- [Tavily credits and pricing](https://docs.tavily.com/documentation/api-credits)
- [Exa pricing](https://exa.ai/pricing)
- [OpenRouter DeepSeek V4 Flash](https://openrouter.ai/deepseek/deepseek-v4-flash)
- [OpenRouter MiMo V2.5 Pro](https://openrouter.ai/xiaomi/mimo-v2.5-pro)
