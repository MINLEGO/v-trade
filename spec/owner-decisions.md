# Owner decisions

Recorded: 2026-07-18.

## Resolved

- DeepSeek model slug: `deepseek/deepseek-v4-flash`.
- MiMo model slug: `xiaomi/mimo-v2.5-pro`.
- DeepSeek quantization: `fp8` only.
- MiMo quantizations: `fp8` and `unknown` (its only published route is accepted even
  when OpenRouter reports no more precise quantization label).
- Reasoning effort: explicitly request the owner-fixed maximum effort for both models.
- Provider routing: allow every compatible provider, sort by price, and permit
  OpenRouter to fall back between those providers.
- Cross-model fallback: forbidden.
- Prompt, transcript and reasoning retention: six months.
- Prompt, transcript and reasoning visibility: operator-only.
- Audit redaction: always redact secrets, tokens and authorization headers.
- Paper fill: buy at best ask, sell at best bid, do not require a counterparty, and
  reject when the required quote is absent.
- No-bid valuation: use the latest archived executable bid when it is no more than
  300 seconds old at the valuation cutoff; otherwise block the account snapshot and
  scoring. Never substitute zero.
- Exa: strict maximum of 50 searches per agent-cycle.
- Research result volume: strict maximum of 10 results per search.
- Research planning expectations supplied by the owner from PredictionArena data:
  8 searches on average conditional on a cycle using search, and 3.5 searches on
  average across all cycles. These are owner-provided empirical expectations and were
  not independently verified by this workstream.
- OpenRouter request bounds: reserve at most $0.014 (14,000 micro-dollars) for
  `deepseek/deepseek-v4-flash`, using provider maximum prices of $0.12 prompt and $0.24
  completion per million tokens with no request fee; reserve at most $0.050 (50,000
  micro-dollars) for `xiaomi/mimo-v2.5-pro`, using $0.44 prompt and $0.88 completion
  per million tokens with no request fee. These request reservations round the exact
  88,000-input/12,000-output upper bounds of $0.01344 and $0.04928 upward.
- Research request bounds: reserve at most $0.020 (20,000 micro-dollars) for every
  Exa search and $0.008 (8,000 micro-dollars) for every Tavily basic search before the
  provider call.
- Model context: 100,000 tokens total, with 12,000 tokens reserved for model output
  and at most 88,000 tokens of assembled input.
- Tool payloads: at most 4,000 tokens of arguments per tool call and 4,000 tokens
  per tool result by default. `get_portfolio` may return up to 24,000 tokens and
  must paginate beyond that limit.
- `get_portfolio` pagination: optional opaque cursor and optional `limit` from 1 to
  200 (default 100), response fields `items`, `next_cursor`, and `has_more`, with a
  stable total order by ascending `position_id`. The first page materializes one
  immutable snapshot bound to the calling agent and agent cycle; later pages read
  only that snapshot. Invalid or foreign cursors are rejected.
- Scheduling: each model/agent has its own start date, hourly schedule and immutable
  per-cycle data cutoff. Simultaneous start is not required. Adding/removing an agent
  does not change any existing agent.

The optional `cohort_cycles` relation may group deliberately synchronized comparison
cycles, but it is not required for normal scheduling and does not control membership.

The pagination contract is resolved. Market data uses the actual completed freeze time
as the cycle cutoff over a bounded pre-freeze market universe; the owner accepts the
resulting few minutes of schedule drift. The Tavily credential is supplied, but the
owner explicitly keeps Tavily disabled and future-only; credential presence must never
enable it or trigger a live call.
- Fee source: archive and persist `base_fee` from the official public Polymarket CLOB
  `GET /fee-rate/{token_id}` endpoint during the cycle freeze; only snapshot IDs from
  that same cycle may determine paper-broker fees, and no default rate is allowed.
- Exa monthly capacity: atomically cap both requests and credits at 18,000 per calendar
  month. Free-plan Exa is excluded from the billed-dollar breaker, while nominal cost
  remains auditable; any positive billed amount halts and alerts Exa.

## Pending

No owner decision remains pending. Tavily remains outside the active baseline by owner
decision and has no live-call validation requirement for this version.
Provider fallback is strictly a same-model provider-route fallback; it never authorizes
substituting one configured model for another.
