# Issue 24 merge record

This record preserves the rollback references for the focused dashboard merge.

- Work branch: `codex/issue-24-python-dashboard-canonical`
- `main` before the merge: `4eddc552eec7d17530a91d2153d0777340fd8cad`
- `admin_interface` before the merge: `082b9f9b02c84452a873be19a06fc8dc9435f28c`
- Merge-base: `507ee6e0674f0c1427d535d1565c8a78dc0106ef`

The Python dashboard under `src/vtrade/dashboard/` is canonical. The React/Vite
dashboard and its obsolete plan were removed during the no-fast-forward merge.
The merge preserves the liquidity-aware experiment as the active default and reads
position valuation age from each persisted experiment definition.

Rollback review should use the first parent (`main` above) and retain the second
parent (`admin_interface` above) as the provenance of the dashboard implementation.
