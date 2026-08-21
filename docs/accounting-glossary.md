# Accounting glossary

| Term | Meaning |
|---|---|
| `contract_units` | Exact hundredths of one binary contract; no floating-point financial quantity is persisted. |
| `gross_cost_basis_micros` | Gross purchase notional assigned to the currently held contract units. |
| `entry_fees_micros` | Buy fees still attributable to currently held contract units; reduced proportionally on a partial sale or final settlement. |
| `total_fees_micros` | All authoritative fees in a completed order lifecycle. |
| Net realized P&L | Exit proceeds or payout minus released gross basis, released entry fees, and exit fees. |
| Net unrealized P&L | Current executable bid value minus gross cost basis and remaining entry fees. |
| Balanced ledger event | An append-only event whose monetary postings sum to zero and whose contract-unit dimensions reconcile with the portfolio projection. |

