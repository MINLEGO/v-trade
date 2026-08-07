# Accounting glossary

| Term | Meaning |
|---|---|
| `cost_basis_micros` | Gross purchase notional assigned to the currently held shares. It excludes trading fees and is unchanged by the entry-fee correction. |
| `average_cost` | Gross purchase notional per currently held share. It remains a gross metric and does not include trading fees. |
| `entry_fees_micros` | Buy fees still attributable to the currently held shares. It is reduced proportionally by partial sells and fully consumed by a full sell or settlement. |
| `total_fees_micros` | All fees in a closed SELL lifecycle, including both BUY and SELL fees. |
| Net realized P&L | For a closed SELL lifecycle: `exit_proceeds_micros - entry_cost_micros - total_fees_micros`. For settlement: `payout_micros - cost_basis_micros - entry_fees_micros`. |
| Net unrealized P&L | Current liquidation value minus gross `cost_basis_micros` minus remaining `entry_fees_micros`. |
