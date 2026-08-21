# vtrade-kalshi-v1 conformance checklist

- [ ] The four active contract files are canonical UTF-8/LF and their SHA-256 values
      are recorded in `config/experiments/vtrade-kalshi-v1.json`.
- [ ] The active configuration is exactly `vtrade-kalshi-v1`, paper-only, and admits
      only ordinary binary markets with `YES` and `NO`.
- [ ] The prompt distinguishes immutable `data_cutoff` evidence from the refreshed
      execution context and never treats later order feedback as new event evidence.
- [ ] The prompt requires market rules, both binary outcomes, canonical book levels,
      fee policy, exact risk capacity, net expected value, and disconfirming evidence
      before a trade.
- [ ] Every active tool uses `market_ref`, `YES`/`NO`, exact microdollars, exact
      contract units, fee data, settlement data, reconciliation state, and audit refs.
- [ ] All 27 tools have unique names, strict input/output schemas, and no unknown
      properties or compatibility aliases.
- [ ] The order tool requires agent-scoped idempotency, `IOC`/`FOK`, exact amount
      semantics, and returns normalized lifecycle and reconciliation state.
- [ ] Beliefs remain queryable but are not injected into the initial prompt; plans are
      complete replacements bounded at 4,000 characters.
- [ ] The Kalshi fixture manifest is required, content-addressed, and rejected when a
      raw response is missing, modified, incomplete, or newer than its cutoff.
- [ ] Missing external capture evidence fails closed; no fake provider or fabricated
      fixture can make the active surface runnable.
- [ ] No active artifact contains provider credentials, signing material, or
      real-execution instructions.
- [ ] A registered experiment definition is immutable; any contract change requires a
      new experiment version and new hashes.
