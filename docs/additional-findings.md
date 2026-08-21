# V-Trade additional findings

These findings are operational evidence and constraints for the active Kalshi paper
release. They do not replace the frozen experiment contract or external deployment
gates.

## Evidence and cutoff discipline

Exact prompts, research responses, market metadata, canonical books, fee policies,
resolution observations, validation decisions, fills, and timing are persisted as
content-addressed audit evidence. Every cycle has one immutable `data_cutoff`; newer
source data cannot enter an earlier freeze.

## Search and model capacity

Exa is the configured research provider. Provider failures do not silently switch
the experiment to another provider. Monthly request, credit, billed-cost, prompt,
and result ceilings are enforced from the versioned configuration and are recorded
per agent and cycle.

## Paper execution interpretation

Paper results measure forecasting, selection, sizing, and the configured liquidity
haircut. They are not evidence of realizable live return. The active system never
submits a venue order, and a future real adapter would require a separate version,
eligibility review, authentication, signing, reconciliation, and risk approval.

## Storage and operations

Raw evidence is retained in private content-addressed storage with audit metadata.
The image, database, and object store are deployment resources rather than local
test substitutes. A missing resource or failed external gate blocks readiness.

