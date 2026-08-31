# vtrade-kalshi-v1 tool compatibility matrix

The active AI-facing boundary contains 27 strict tools. Every market-facing tool
uses opaque `market_ref`, YES/NO, exact prices, contract units, fee/settlement data,
reconciliation state, and audit references. No compatibility alias accepts a legacy
shape.

| Capability | Active contract | Evidence boundary |
|---|---|---|
| Market discovery | frozen Kalshi catalogue, exact series tags, and freeze-scoped metrics | cycle cutoff |
| Market details | opaque `market_ref`, binary rules, dynamic grid, indicative midpoint, and metric context | cycle cutoff |
| Order book | reciprocal YES/NO bids and asks with exact levels | content-addressed snapshot |
| Portfolio | market_ref, market_question, contract units, gross basis, entry fees, realized P&L | calling agent only |
| Orders | CASH/CONTRACTS, BUY/SELL, optional limit, IOC/FOK | frozen plus refreshed context |
| Order result | lifecycle, fills, fees, reconciliation, risk, audit | idempotent operation |
| Settlements | FINALIZED result, market_question, settlement timestamp, payout, audit | cutoff and terminal gate |
| Research | configured provider with bounded calls and redacted evidence | provider budget |
| Plans and beliefs | agent-scoped, paginated, length-bounded state | append-only audit |

The active schema is `spec/tool-schemas-vtrade-kalshi-v1.json`. The current v1
revision adds the metric and tag fields and refreshes the bound hashes. Later
incompatible changes require a new experiment version. The historical archive is
not a tool-schema source.
