# vtrade-kalshi-v1 conformance checklist

- [ ] The four active contract files are canonical UTF-8/LF and their SHA-256 values
      are recorded in `config/experiments/vtrade-kalshi-v1.json`.
- [ ] The active configuration is exactly `vtrade-kalshi-v1`, paper-only, and admits
      only ordinary binary markets with YES and NO.
- [ ] The prompt separates immutable `data_cutoff` evidence from refreshed execution
      context and rejects look-ahead data.
- [ ] The prompt requires market rules, both outcomes, canonical book levels, fee
      policy, exact capacity, net expected value, and disconfirming evidence.
- [ ] Every active tool uses `market_ref`, YES/NO, exact microdollars, contract
      units, fee data, settlement data, reconciliation state, and audit references.
- [ ] All 27 tools have unique names, strict input/output schemas, and no unknown
      properties or compatibility aliases.
- [ ] The order tool requires agent-scoped idempotency, IOC/FOK, exact amount
      semantics, and normalized lifecycle/reconciliation results.
- [ ] The Kalshi fixture manifest is content-addressed and fails closed on missing,
      modified, incomplete, or newer raw evidence.
- [ ] Missing external capture evidence fails closed; no fake provider can make the
      active surface runnable.
- [ ] No active artifact contains credentials, signing material, or real-execution
      instructions.
- [ ] A registered experiment definition is immutable; contract changes create a new
      experiment version and new hashes.
- [ ] The release verifier passes the active-venue sweep and archive boundary.

