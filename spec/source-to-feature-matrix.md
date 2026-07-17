# Source-to-feature matrix — predictionarena-polymarket-v1

Evidence cutoff: 2026-07-13. `documented` means stated by a primary PredictionArena
source; `inferred` fills a public gap; `vtrade_deviation` is intentional. A row never
upgrades observed behavior into a claim about unpublished internals.

| Feature or rule | Classification | Evidence / rationale |
|---|---|---|
| Isolated $10,000 virtual balance per agent | documented | PredictionArena paper cohort design |
| Shared harness and tools across agents | documented | PredictionArena paper methodology; schedules remain independent |
| Private positions, beliefs and plans | documented | Paper and public agent profile |
| One cycle per agent every 60 minutes | vtrade_deviation | Owner baseline; public cadence is about 15–45 minutes |
| No simultaneous-start requirement | vtrade_deviation | Owner decision; every agent starts on its own date |
| Adding/removing an agent does not alter existing schedules | vtrade_deviation | Owner decision; agents are operationally isolated |
| Skip rather than burst-replay missed cycles | inferred | Operational fairness rule; unpublished |
| Synchronize, settle, freeze, run, validate, execute, value, persist order | inferred | Required deterministic orchestration |
| Tool-only agent capabilities | documented | Paper methodology and public tool traces |
| Fundamental outcome strategy | documented | Public paper and rendered prompt trace |
| Pre-settlement price strategy | documented | Public paper and rendered prompt trace |
| Research before trade | documented | Rendered prompt protocol |
| State YES and NO winning conditions | documented | Rendered prompt protocol |
| Estimate probability/target and expected P&L | documented | Rendered prompt protocol |
| Include fees/gas, sizing, exit plan and portfolio review | documented | Rendered prompt protocol |
| Maximum 15% account value cost basis per market | documented | Public PredictionArena methodology |
| No leverage, transfer, negative cash or cross-agent netting | inferred | Conservative paper-accounting constraints |
| Value positions at executable bid | inferred | Required conservative valuation; public current-price convention unknown |
| Weighted-average cost basis | inferred | Accounting convention needed for additions |
| Append-only audit inputs and outputs | inferred | Reproducibility requirement |
| Profit/account value primary score | documented | PredictionArena benchmark objective |
| Forecast and operational diagnostics separate | inferred | Avoid conflating forecast quality and profit |
| Python 3.12 modular monolith | vtrade_deviation | Owner-approved implementation shape |
| PostgreSQL/Supabase persistence | vtrade_deviation | Owner infrastructure choice |
| Content-addressed compressed object artifacts | inferred | Storage and replay design |
| Provider-neutral canonical ports | vtrade_deviation | Extensibility design |
| OpenRouter model gateway | vtrade_deviation | Owner baseline provider |
| DeepSeek V4 Flash and MiMo V2.5 Pro | vtrade_deviation | Owner baseline models; DeepSeek `fp8`, MiMo `fp8`/`unknown`, all compatible providers sorted by price |
| Exa research | vtrade_deviation | Owner baseline provider |
| Same-model provider fallback | vtrade_deviation | All compatible OpenRouter providers allowed and sorted by price; no cross-model fallback |
| Polymarket live read data | documented | Same venue as target paper cohort |
| Paper rather than real-money execution | vtrade_deviation | Owner safety/cost choice |
| Gamma metadata and CLOB executable prices | inferred | Official APIs serve distinct data roles |
| Frozen cutoff per agent-cycle | vtrade_deviation | Owner selected independent scheduling; cutoff is still immutable per cycle |
| 29 AI-facing tool names | inferred | Observed public trace inventory, schemas unpublished |
| Versioned discovery policy | inferred | Thresholds unpublished |
| Bounded multi-turn tool loop | documented | Ordered tool-call traces demonstrate a loop |
| Track cycle and tool statuses separately | inferred | Failed cycles contain successful calls |
| About 13 Exa searches/cycle planning assumption | inferred | Capacity assumption, not trace-established behavior |
| $40 monthly billed external-API circuit breaker, excluding free-plan Exa | vtrade_deviation | Frozen owner budget; positive billed Exa cost halts Exa |
| 18,000 monthly Exa requests and 18,000 monthly Exa credits | vtrade_deviation | Frozen owner caps, atomically reserved in PostgreSQL |
| Deterministic critical learning without another LLM | inferred | Unpublished mechanism and cost control |
| Paper fill at best quote, absent quote rejected | inferred | Owner-confirmed approximation; no counterparty required |
| Counterparty-independent valid paper execution | documented | PredictionArena paper describes paper advantage |
| Separate liquidity-aware comparison policy | inferred | Future experimental comparison only |
| Double-entry ledger and idempotent settlement | inferred | Reproducible accounting requirement |
| Independent hourly runs per model/agent | vtrade_deviation | Frequency and scheduling differ from PredictionArena |
| Private authenticated admin only | vtrade_deviation | Owner UI decision |
| New version for provider/prompt/model/policy changes | inferred | Prevents silent mutation of baseline |

Primary evidence pointers: `docs/predictionarena-polymarket-endpoints.md`,
`docs/predictionarena-cycle-analysis.json`, `sources-predictionarena-polymarket.md`, and
the references listed in `docs/implementation-plan.md`.
