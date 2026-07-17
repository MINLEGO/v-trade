# V-Trade additional findings

Date checked: 2026-07-13

These findings influence budgeting, interpretation, and future experiment design but are not implementation phases.

## Public endpoint audit changes the evidence plan

The Python audit in [predictionarena-polymarket-endpoints.md](predictionarena-polymarket-endpoints.md) confirms that `/cycles` is the useful trace endpoint: a 50-cycle page was about 4 MB decoded and included the rendered prompt, reasoning, before/after portfolio snapshots, settlements, and tool calls. The account-value history was about 3.3 MB for 19,878 points but contained no decision context, while `/actions` and `/markets` were much poorer presentation views.

The cycle prompt exposes a five-step protocol (strategy choice, research, side verification, expected P&L, sizing/execution) and persistent knowledge tools. It does not expose system/user message roles or complete JSON schemas, so these observations narrow the behavioral reproduction but do not prove a source-level clone. Preserve exact prompt text, tool arguments/results, cycle status, and a data cutoff as content-addressed fixtures; do not paste multi-megabyte endpoint responses into logs or model context.

The audit also found 32 completed and 18 failed cycles in one 50-cycle sample, while all 849 recorded tool calls were marked successful. V-Trade should therefore track tool-call success separately from model-loop/cycle success and retain failure reasons even when the public read model does not provide them. The checked snapshot was temporally mixed: cycles ended on June 24, 2026, whereas the external S&P 500 series extended to July 13, 2026. Current-looking aggregate values must not be assumed to share one cutoff.

## Search capacity is likely the first hard limit

With two agents and one cycle per hour, a 30-day month contains about 1,440 agent-cycles:

```text
2 agents x 24 cycles/day x 30 days = 1,440 agent-cycles/month
```

Tavily currently gives 1,000 free credits per month. Basic search costs one credit and advanced search costs two, so the free tier supports an average of only 0.69 basic searches per agent-cycle. A 4,000-credit allowance supports 2.78 basic searches per agent-cycle. This is lower than an unconstrained research agent may naturally request.

Exa is the selected baseline provider. At the owner's planning assumption of approximately 13 external web searches per agent-cycle, expected usage is about 18,720 searches per 30-day month. This is close enough to Exa's currently advertised 20,000-request free allowance that bursts and retries still matter. The proposed $1,000 education grant is valuable but should not be treated as available until the account is approved. Headline limits and eligibility can change, so the application must read limits from configuration and enforce its own counters rather than assume a provider plan.

The endpoint audit does **not** establish 13 web searches per cycle. Its 50-cycle sample reports a median of 10.5 total tool calls and 763 discovery calls overall; discovery includes internal market tools as well as `web_search`. Thirteen is therefore a capacity/budget assumption to measure during the shadow run. A much higher per-cycle safety ceiling permits exceptional research bursts, while separate monthly request/credit and dollar caps prevent runaway use.

Do not let a search outage or exhausted allowance silently switch providers. Different indexes and ranking systems change the information an agent sees, which makes the provider part of the experimental treatment even if the tool schema is identical.

Sources: [Tavily credits](https://docs.tavily.com/documentation/api-credits), [Exa pricing](https://exa.ai/pricing).

## Search caching is both an optimization and a confounder

A short-lived provider-response cache can save money, but it changes evidence freshness and may make later agents benefit from earlier agents' queries. Log the original provider timestamp, cache hit, cached age, query normalization, and content hash. Set the cache policy in the immutable experiment definition.

For the fairest baseline, each model should have the same logical-search limit. Provider cache hits can cost less, but should still count as logical tool usage in behavioral metrics.

## Hourly cycles do not guarantee exactly half the total cost

Halving cycle frequency roughly halves scheduled model invocations, but monthly cost also depends on prompt growth, output/reasoning tokens, search behavior, retries, provider caching, and whether agents take more tool turns when more time has elapsed. Measure cost per successful agent-cycle and per day during the shadow run before projecting a multi-month budget. V-Trade v1 has a $40 hard all-in external API ceiling per calendar month, with automatic stopping rather than an alert-only policy.

At current advertised OpenRouter list prices, DeepSeek V4 Flash is substantially cheaper than MiMo V2.5 Pro, but prices and routes are mutable. Store the provider-reported cost on every call instead of calculating historical bills from today's price table. Track free-credit consumption and nominal provider value alongside billed dollars, because a zero-dollar invoice does not mean unbounded capacity.

Sources: [DeepSeek V4 Flash on OpenRouter](https://openrouter.ai/deepseek/deepseek-v4-flash), [MiMo V2.5 Pro on OpenRouter](https://openrouter.ai/xiaomi/mimo-v2.5-pro).

## Paper results have an execution advantage

PredictionArena states that its paper cohort's trades execute without requiring a real counterparty, while live orders can be rejected. Paper profitability therefore measures forecasting, selection, and sizing under simplified execution; it is not an estimate of realizable live return.

V-Trade should display the execution policy beside every leaderboard and export. A liquidity-aware shadow score can later estimate the gap, but it must not rewrite baseline fills.

Source: [Prediction Arena paper, cohort design](https://arxiv.org/html/2604.07355v1).

## Profit alone is noisy over short horizons

PredictionArena's paper cohort covered only three days and its authors explicitly treat those results as directional. Prediction markets also settle unevenly, so short windows can overrepresent unresolved mark-to-market gains, a few large bets, or particular event categories.

Keep account value as the primary success criterion, but retain Brier score, calibration, drawdown, edge at entry, settled-position accuracy, and category/horizon breakdowns. These diagnose whether PnL reflects repeatable forecasting skill or a small number of concentrated outcomes.

## Model comparisons require synchronized cohorts

Adding a model later gives it a different opportunity set and a later start date; this is an accepted baseline property. Agents remain isolated, and adding or removing one never changes existing schedules or state. The UI must display each agent's start date and avoid implying synchronized exposure windows.

## Reproducibility requires preserving what the model saw

Market metadata is not enough. Exact prompts, search results, order books, rules, tool errors, provider settings, and timing affect decisions. Raw artifacts should be content-addressed and retained so a failed or surprising cycle can be reconstructed without fetching newer data.

## Provider-neutral does not mean behavior-neutral

Stable interfaces make Exa/Tavily or Polymarket/Kalshi easy to replace in code. They do not make experimental results comparable automatically. Exa is the baseline treatment; switching to Tavily, or changing a venue, fill model, model route, prompt, threshold, or cycle length, must produce a new immutable experiment version and a visible cohort label.

## The VPS is adequate if ingestion remains selective

The orchestration workload is small for 4 vCPUs and 8 GB RAM because most time is spent waiting for external APIs. Storage is the larger risk. Full-universe, high-frequency order-book history, verbose reasoning traces, and PredictionArena's rich multi-megabyte cycle JSON could grow indefinitely. Persist metadata deltas broadly, fetch order books on demand, preserve imported endpoint payloads compressed with hashes, retain normalized indexes separately, and alert on both database and bucket growth.

## Live trading is a separate product and risk phase

A live Polymarket adapter would add jurisdiction/availability review, wallet/key custody, signing, balances, approvals, nonce/idempotency behavior, partial fills, cancellations, reconciliation, and emergency controls. It should not be enabled merely by changing `paper=false`. Require a separately deployed broker, explicit operator approval, tiny capital limits, and a completed liquidity-aware shadow period. The same principle applies to a future Kalshi integration.
