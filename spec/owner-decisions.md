# Owner decisions

Recorded: 2026-07-16.

## Resolved

- DeepSeek model slug: `deepseek/deepseek-v4-flash`.
- MiMo model slug: `xiaomi/mimo-v2.5-pro`.
- DeepSeek quantization: `fp8` only.
- MiMo quantizations: `fp8` and `unknown` (its only published route is accepted even
  when OpenRouter reports no more precise quantization label).
- Reasoning effort: omit an explicit effort and use the provider default.
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
- OpenRouter request bounds: at most $0.011 (11,000 micro-dollars) for
  `deepseek/deepseek-v4-flash`, using provider maximum prices of $0.09 prompt and $0.18
  completion per million tokens with no request fee; at most $0.040 (40,000
  micro-dollars) for `xiaomi/mimo-v2.5-pro`, using $0.348 prompt and $0.696 completion
  per million tokens with no request fee.
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
resulting few minutes of schedule drift. Tavily remains disabled until its real
credential is supplied.

## Pending

- Authorize the official Polymarket CLOB fee-rate endpoint as the runtime fee-policy
  source, or provide another exact source.
- Define strict monthly Exa request and credit caps. No default is inferred.
Provider fallback is strictly a same-model provider-route fallback; it never authorizes
substituting one configured model for another.
