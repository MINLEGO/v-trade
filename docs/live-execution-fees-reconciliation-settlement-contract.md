# Live-first execution, fees, reconciliation, and settlement contract

Status: approved by the owner on 2026-09-14. This specifies
`vtrade-kalshi-live-v1`; it does not change the running `vtrade-kalshi-v1`
implementation.

Decision ticket: [Define live execution fees reconciliation and settlement](https://github.com/MINLEGO/v-trade/issues/36).

Canonical resolution: [approved contract](https://github.com/MINLEGO/v-trade/issues/36#issuecomment-5669536163).
The issue resolution is authoritative. This file is its local working copy.

## Scope

- The experience is Kalshi-only and paper-only. No venue order is submitted.
- `preview_market_order` and `place_market_order` use live venue observations, exact
  financial units, the same paper fill algorithm, and the same risk semantics.
- A preview creates no order, reservation, reusable context, or execution entitlement.
  Placement independently rebuilds all evidence.
- Missing, stale, future-dated, contradictory, invalid, or unverifiable financial
  evidence fails closed. It never becomes a permissive or zero-valued default.
- Financial records are authoritative and append-only. Current portfolio state is a
  transactionally maintained, rebuildable projection.

## Fresh execution context

Each evidence class has its own freshness rule. Age is measured from its
`observed_at` when the execution context is assembled and again before financial
commit.

| Evidence | Maximum age or validity rule |
| --- | --- |
| Selected market state and price grid | 15 seconds |
| Execution book | 5 seconds at risk check and financial commit |
| Dynamic fee inputs | 5 minutes, shortened to the next scheduled change, waiver expiration, or market close |
| Verified static fee schedule | No arbitrary TTL; its authenticated version and effective interval must cover execution |
| Portfolio and positions | Current locked PostgreSQL portfolio version; no wall-clock TTL |
| Risk policy | Immutable effective version covering the check |

The complete placement assembly has a ten-second deadline. A source time more than
five seconds in the future invalidates the context. Every selected observation must
be causally valid for the assembly time. A close, fee-policy boundary, portfolio
version change, lease-fencing change, or freshness expiry invalidates the whole
context rather than only the affected field.

The implementation orders work so that slow structural reads precede the execution
book fetch. Risk evaluation and commit follow the book fetch. If the context becomes
stale after risk approval but before commit, the order attempt completes as an
explicit `STALE_EXECUTION_CONTEXT` rejection with no fill or financial mutation.
It is not an unknown outcome.

Provider retries, network timeout values, cost ceilings, and quota behavior remain
owned by [Define provider budgets and degraded operation](https://github.com/MINLEGO/v-trade/issues/38).

## Agent-visible execution book

The canonical order book is immutable venue evidence. The agent-facing execution
book is the only liquidity view exposed by `get_orderbook`, `preview_market_order`,
and `place_market_order`.

For this paper experience, `capped-best-level-haircut-v2` transforms each requested
outcome/action side independently:

1. Select at most the six best canonical levels available on the required side.
2. Let `raw_total` be their exact quantity in hundredths of a contract.
3. Let `maximum_ignored = floor(raw_total / 2)`.
4. Remove `min(best_level_quantity, maximum_ignored)` from the best level only.
5. Retain any surplus at that same best price, followed by every other captured
   level unchanged.

At least `ceil(raw_total / 2)` units remain. The rule also applies when fewer than
six levels exist. With one level it retains the upper half. For example,
`[60, 10, 10, 10, 5, 5]` becomes `[10, 10, 10, 10, 5, 5]`.

The agent behaves as though removed units never existed:

- no raw level, removed quantity, retained fraction, haircut name, haircut version,
  or haircut-specific error is agent-visible;
- `INSUFFICIENT_LIQUIDITY` describes insufficient transformed depth;
- `PRICE_LIMIT` describes incompatible transformed prices;
- IOC fills compatible transformed depth and cancels the remainder;
- FOK fills the full request or produces no fill;
- CASH requests convert with exact arithmetic and round quantity down so the cash
  ceiling, including authoritative fees, is never exceeded.

Internally, the execution context retains the canonical book observation, the rule
version, raw and effective totals, removed and retained best-level quantities, and a
fingerprint of the transformed view. Fills reference exact effective levels and
consumed quantities. A dedicated liquidity-haircut audit table is unnecessary.

The haircut definition is immutable for the experience and included in its
configuration fingerprint. Changing it creates a new paper-experience version.
Each agent applies the rule independently and may simulate against the same external
book as another agent; paper fills never deplete shared synthetic liquidity.

A future live experience uses the same tool shapes but does not apply a haircut. Its
adapter exposes actual venue-authorized liquidity. This contract does not authorize
or specify real order submission.

## Paper fill authority

The immutable order request, execution-context fingerprint, transformed execution
book, paper algorithm version, exact consumed levels, fill quantities and prices,
IOC/FOK result, and fee calculations together make a paper fill authoritative.
No midpoint, indicative catalogue price, missing counterparty, or depth beyond the
execution book may create a fill.

`preview_market_order` uses the same rules but only returns a simulation. Placement
uses a newly observed execution book and may legitimately differ. Neither tool
reveals the pre-haircut canonical levels to the agent.

## Fee policy and calculation

An executable fee-policy observation must establish:

- verified official schedule version, authenticated artifact fingerprint, formula,
  rounding rule, effective interval, and covered product class;
- exact series fee type and rational multiplier;
- exact event override or explicit evidence that it is cleared or absent;
- waiver state and expiration when applicable;
- TAKER participant role;
- observation/source times and role-labelled evidence references.

IOC/FOK paper orders are TAKER operations because they never rest on a venue book.
MAKER semantics are unsupported in this experience. An unsupported fee type or
participant role, incomplete pagination, source conflict, expired evidence, missing
coverage, or unverifiable schedule rejects execution.

The fee engine uses exact integer/rational arithmetic. It calculates each consumed
level in price order and carries one sub-cent alignment accumulator across the
entire order so splitting one order across levels cannot change its total fee. Each
fill stores gross cash, raw trade fee, rounded trade fee, rounding adjustment,
rebate, net authoritative fee, accumulator transition, and fee-policy fingerprint.
In paper trading this computed and committed amount is authoritative.

## Financial serialization and risk

Only one financial mutation may run for an agent at a time. Placement, settlement,
and financial correction use the same agent-scoped lock and portfolio-version guard.
A `started` order attempt prevents another mutation until resolved. An `unknown`
attempt opens a reconciliation case and blocks new cycles and orders for that agent.
Maintenance and other agents continue independently.

The established maximum market concentration remains exactly `15/100`. BUY risk
uses the deterministic ledger book value:

`account_value = current_cash + gross_cost_basis_of_all_open_positions`

`existing_market_gross_basis + proposed_fill_gross_basis <= account_value * 15/100`

Equality is accepted. Entry fees already reduce cash and are not added back. Cash
sufficiency includes exact proposed fees. SELL does not apply the BUY concentration
test but must prove the owned quantity and fee-adjusted cash effect.

Every immutable risk check retains:

- execution-context and risk-policy fingerprints;
- checked time and locked portfolio version;
- cash and account value;
- existing market gross basis;
- proposed gross basis and exact fees;
- `15/100`, the computed limit, and projected exposure;
- projected cash and position quantities;
- blocking attempt/reconciliation state;
- approval or structured rejection reason.

Risk approval and financial commit share the guarded transaction boundary. A
concurrent version change requires a new execution context and risk check.

## Atomic order accounting

An order attempt and its mutating tool invocation are durable before any financial
effect. Reusing the same agent-scoped idempotency key and request fingerprint returns
the recorded outcome; divergent reuse fails closed.

Finalization commits the completed result, fills, fee components, balanced ledger
entries/postings, positions, cash, and new portfolio version in one PostgreSQL
transaction. A business rejection is completed. A provider-read failure before a
financial transaction is a rejection or unfilled result, never a financial unknown.

Buy entry fees remain allocated to the open position. A partial sale releases the
proportional gross basis and entry fees. Net realized P&L includes released entry
fees and sale fees, consistent with ADR 0001.

## Reconciliation

Because the experience is paper-only, order reconciliation never searches Kalshi
for a venue order. It resolves uncertainty around the local durable transaction by
re-reading the original agent/idempotency identity under the financial lock:

- a complete result with fills and balanced ledger facts returns the committed
  result;
- demonstrated absence of all financial facts completes the attempt as rejected or
  unfilled according to its last durable boundary;
- PostgreSQL unavailability preserves `unknown`;
- partial facts, missing ledger links, divergent fingerprints, or imbalance create
  `CONFLICT`.

An unresolved case blocks only its agent. `CONFLICT` requires an evidenced and
audited operator decision. Original financial facts remain immutable.

## Settlement observation and polling

Settlement maintenance is independent of decision cycles and catalogue discovery.
It follows every market with a non-zero position until a financial settlement is
committed or an operator resolves a conflict.

A payable resolution observation must establish exact market identity,
`FINALIZED`, binary `YES|NO`, `settlement_ts`, `observed_at`, optional source update
time, provider/endpoint identity, a raw content-addressed payload or verifiable
fingerprint, and successful binary, temporal, and conflict validation. `DETERMINED`,
`DISPUTED`, `AMENDED`, empty results, and missing settlement times remain evidence
only.

`settlement_timer_seconds` is a scheduling hint, not payment authority. Before close,
maintenance wakes at the next known boundary. At or after the announced timer it
checks immediately. If the result is not payable, it retries after 1, 5, 15, then
60 minutes and caps routine polling at once per hour. A missing or invalid timer
starts checks at the known close. A valid FINALIZED result pays immediately even if
the timer suggests a later time.

The worker acquires the same agent financial lock as order execution, then rereads
the current position and resolution. A refreshed order seeing FINALIZED is rejected.
Order and settlement cannot consume the same contracts concurrently.

## Settlement accounting and fees

Each non-zero `(agent, market, outcome)` position settles separately:

- winning payout is `10_000 microdollars * contract_units`;
- losing payout is zero;
- a settlement fee is accepted as zero only when an applicable verified policy
  explicitly says zero;
- gross basis and all remaining entry fees are released;
- net realized P&L is payout minus gross basis, entry fees, and settlement fee.

The applicable settlement-fee policy is the verified version whose effective
interval covers `settlement_ts`. Missing or contradictory fee evidence leaves the
settlement pending. Settlement, ledger, cash, position closure, realized P&L, and
portfolio version commit atomically. Duplicate identical finalization is a no-op;
conflicting terminal evidence opens `CONFLICT` and pays nothing further.

## Financial corrections

If later evidence proves a committed fill, fee, or settlement incorrect, the system
does not update or delete it. It opens a `CONFLICT`, blocks the affected agent's new
cycles and orders, retains both evidence bundles, and requires an audited operator
decision.

An approved correction creates an append-only `financial_corrections` fact with the
reconciliation case, original financial record, sequence, operator action, old and
new evidence, structured reason, signed cash/contract/entry-fee/realized-P&L deltas,
idempotency key, fingerprint, effective time, and recorded time. The correction,
balanced ledger source, affected projections, and portfolio version commit
atomically. `ledger_entries` therefore admits `financial_correction_id` as another
exclusive authoritative source. This is a required narrow extension to the earlier
clean-database baseline, not a funding event.

An authoritative correction is never truncated to preserve non-negative cash. A
negative corrected balance is recorded exactly, financially pauses the agent, and
blocks purchases. Settlement, reconciliation, and further correction continue. The
pause clears only after authoritative facts restore non-negative cash.

Agent visibility remains minimal:

- `get_balance` and `get_portfolio` naturally expose corrected projections without
  additional correction payloads;
- affected `get_closed_trades` and `get_settlements` rows add only
  `correction_status=none|under_review|corrected`,
  `net_financial_effect_micros`, and nullable `latest_correction_at`;
- raw evidence, operator identity, and detailed justification stay operator-only.

## Evidence and retention matrix

| Decision or effect | Fresh authority | Durable minimum |
| --- | --- | --- |
| Market executability | Market observation no older than 15 seconds | Observation identity, lifecycle fields, price grid, times, fingerprint |
| Agent-visible liquidity | Execution book no older than 5 seconds | Canonical book evidence, transformed fingerprint, private haircut inputs/result |
| Paper fill | Effective levels plus immutable algorithm version | Request, context, consumed levels, quantities, prices, IOC/FOK result |
| Fees | Dynamic evidence no older than 5 minutes and valid static schedule | Exact inputs, effective bounds, formula, accumulator, components, policy/evidence fingerprints |
| BUY risk | Locked current portfolio and effective policy | Exact inputs/outputs, portfolio version, `15/100`, decision and reason |
| Order commit | Guarded PostgreSQL transaction | Attempt, fills, fees, ledger, position/cash deltas, portfolio version |
| Unknown order | Original idempotent identity and database state | Reconciliation case/events and evidence of effect, absence, or conflict |
| Settlement | Validated FINALIZED binary result and settlement-time fee policy | Resolution evidence, quantity, payout, fees, ledger, closed position, portfolio version |
| Correction | Audited operator decision with conflicting evidence | Immutable correction, evidence bundles, ledger and projection deltas |

Financial facts, their idempotency identities, used policies, corrections, and all
referenced observations/evidence live for the experiment. Non-referenced observations
retain the shorter cleanup policy established by the durable-state contract.

## Implementation boundaries

This contract requires a narrow update to the logical baseline approved by
[Define minimal durable state and the clean database baseline](https://github.com/MINLEGO/v-trade/issues/35):

- add `financial_corrections`;
- allow `ledger_entries.financial_correction_id` as an XOR source;
- allow audited corrections to produce negative agent cash and a financial pause;
- retain private haircut derivation fields within execution evidence without
  restoring `liquidity_haircut_audits`.

No migration, product code, deployment, tool-schema edit, or real-order capability is
authorized by this planning decision.
