# Prompt conformance checklist

- [ ] Artifact is versioned and its SHA-256 is stored in the experiment definition.
- [ ] Strategy selection allows fundamental outcome and pre-settlement price trading.
- [ ] Research stage names discovery, details/rules, order book and web search.
- [ ] YES and NO winning conditions are mandatory before a trade.
- [ ] Probability/target, edge, post-fee expected P&L and disconfirmation are mandatory.
- [ ] Sizing, concentration, portfolio review and exit plan are mandatory.
- [ ] Frozen decision data is distinguished from live execution refresh.
- [ ] Post-cutoff execution feedback updates the affected order only, not unrelated
      frozen-event evidence.
- [ ] A missing fee policy or unverifiable net expected value blocks an order.
- [ ] Beliefs remain tool-accessible but are not injected into the initial prompt.
- [ ] The user context uses named `long_term_plan`, `next_cycle_plan` and bounded recent activity.
- [ ] Paper-only internal liquidity controls are absent from agent-facing text.
- [ ] Only canonical tool names are referenced.
- [ ] No unresolved `{...}` template placeholder remains after rendering.
- [ ] The cycle protocol occurs exactly once after rendering.
- [ ] System/user role separation is labeled `inferred`.
- [ ] Exact rendered messages and tool schemas are persisted before the model call.
- [ ] A registered experiment definition remains immutable; material changes to a registered
      version require a new experiment version.
- [ ] An unregistered active candidate may evolve in place until it is registered.

