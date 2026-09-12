# Live-first cycle orchestration and crash-recovery contract

Status: approved by the owner on 2026-09-12. This specifies `vtrade-kalshi-live-v1`;
it does not change the running `vtrade-kalshi-v1` implementation.

Decision ticket: [Define cycle orchestration and crash recovery without market freezes](https://github.com/MINLEGO/v-trade/issues/34).

Canonical resolution: [approved contract](https://github.com/MINLEGO/v-trade/issues/34#issuecomment-5647476239).
This file is the local working copy; the issue resolution is authoritative.

## Lifecycle and scheduling

- Each scheduled occurrence has one operational record. It becomes a running
  decision cycle only after its start gates pass and a successfully loaded cycle
  catalogue is selected.
- A start gate that does not pass produces `skipped`, with an explicit reason. Gates
  include agent eligibility and pause state, agent-scoped financial reconciliation,
  and successful complete catalogue preload.
- A running cycle ends as `completed`, `failed`, or `abandoned`. `failed` means the
  current owner recorded a clean terminal failure. `abandoned` means exclusive
  ownership was lost before a clean terminal result could be recorded.
- Every terminal status is final. A failed or abandoned cycle is never resumed, and
  its provider or tool calls are never replayed as recovery.
- No automatic replacement is created for a failed, abandoned, skipped, or missed
  occurrence. The next scheduled occurrence advances normally. An operator `run
  once` creates a new occurrence and identity.
- Several start attempts may await and share the same complete catalogue build while
  it is in progress. After publication, each member of that group selects the newly
  published generation. An attempt arriving later must trigger or join the next
  build; an older published generation cannot satisfy its start gate.
- At most one decision cycle is active per agent. Different agents may run
  concurrently subject to the separately decided provider and runtime budgets.

## Ownership and fencing

- A cycle lease grants temporary exclusive authority and carries a fencing
  generation. Losing that authority prevents the old worker from starting another
  provider call, tool call, order attempt, or cycle-trace mutation.
- Fencing is checked before every new effect and again after every network return.
  A late response cannot influence the cycle after ownership is lost, though its
  cost and independently committed effects remain accountable.
- A timeout does not claim to cancel I/O that has already left the process.
- Financial operations and other durable mutations use their own identities and
  transaction boundaries. Their validity does not depend on the cycle lease after
  they commit.

## Durable boundaries, not resumable stages

The live-first runtime keeps six diagnostic boundaries:

1. start-gate result;
2. cycle-catalogue selection and cycle activation;
3. assembled initial prompt context;
4. model and tool invocation events in the cycle trace;
5. references to independently durable effects;
6. terminal cycle result.

These boundaries support diagnosis only. They cannot resume execution. The legacy
`market_freeze`, `pre_settlement`, `prompt`, `harness`, `broker`, and
`settlement_valuation` stage checkpoints do not belong to this contract.

An abandoned-cycle sweeper writes only the terminal time, last known durable
boundary, lost fencing generation, and a redacted reason. It does not reconstruct a
summary, complete a transcript, or alter an independently durable effect.

## Maintenance and financial effects

- Reconciliation, tracked-market refresh, position tracking, settlement, and their
  financial projections run independently of decision cycles. Catalogue or agent
  failures cannot stop this maintenance.
- Before starting a cycle, unresolved agent-scoped financial state in `REQUIRED` or
  `CONFLICT` blocks that agent. Other agents and maintenance continue.
- A paper-order intention is durable before execution. Its result, fills, ledger
  entries, fees, position changes, and portfolio version commit atomically under an
  independent idempotency key.
- A committed financial effect remains authoritative even if its originating cycle
  is later abandoned. An uncertain commit is queried or reconciled with the same
  identity and never recreated as a second fill.
- This contract does not specify real Kalshi order submission.

## Provider, tool, state, and artifact effects

- Every paid provider invocation has a durable identity and the semantic states
  `reserved`, `sent`, `completed`, and `unknown`. A `sent` or `unknown` invocation is
  not replayed within the cycle.
- A reservation left unresolved by abandonment becomes `unknown`; it is never
  silently released or accounted as zero. It continues to consume budget
  conservatively until evidence or an audited operator decision resolves it.
- Ordinary read tools append lightweight `started`, `completed`, or `failed` events
  to the cycle trace. They do not create resumable checkpoints.
- Before a mutating tool executes, V-Trade persists a server-issued
  `tool_invocation_id`, tool name, cycle identity, and exact argument fingerprint.
  Reusing that identity with the same fingerprint returns its recorded result;
  divergent reuse fails closed. A started mutation without a terminal result is
  explicitly `unknown`.
- A committed plan or belief survives abandonment without compensation. Its
  provenance identifies the originating cycle and tool invocation, and consumers
  can observe that cycle's terminal status.
- Immutable content-addressed artifacts may be uploaded before registration. A
  later cleanup removes unreferenced objects only after the retention grace period
  chosen by the persistence contract. An orphan does not prove its producing
  activity succeeded.

## Prompt warning, pauses, and failures

- If the previous started decision cycle ended `failed` or `abandoned`, the next
  initial prompt context includes only the warning: `Warning: the previous cycle did
  not terminate correctly.` Details remain in operational records. A `skipped`
  occurrence does not trigger this warning because no decision cycle started.
- A normal pause prevents new claims and lets an active cycle finish. A critical
  financial alert immediately fences new order attempts without force-killing the
  process. Graceful shutdown stops new effects; forced shutdown ends through lease
  expiry and abandonment.
- Durable alerts and automatic pauses survive abandonment. Resumption remains an
  authenticated, idempotent, audited operator action; it is never automatic.
- A failed complete preload skips the occurrence. A selected-data refresh may use
  the approved dated read fallback. A terminal model failure fails the cycle without
  replay. A recoverable tool error may be returned to the agent within its budget.
  A business order rejection is a normal tool result. Persistence or invariant
  failures fail cleanly when the owner can record them and otherwise lead to
  abandonment.

## Required implementation evidence

Offline checks are insufficient on their own. PostgreSQL-backed integration evidence
must cover:

- two competing workers and fencing of the loser;
- crash boundaries before and after provider calls;
- crash boundaries before and after financial commits;
- lease expiry and terminal abandonment;
- absence of same-cycle recovery or replay;
- stable idempotence for mutating tools;
- conservative unresolved provider reservations;
- persistence and provenance of plans and beliefs from an abandoned cycle;
- independent reconciliation and settlement while catalogue discovery is down;
- the minimal next-prompt warning after a failed or abandoned cycle.

## Decisions left to downstream tickets

- The durable-state ticket owns physical tables, indexes, retention periods, artifact
  cleanup timing, and the exact minimal stored fields.
- The execution ticket owns numerical freshness limits and detailed order, fee,
  reconciliation, risk, and settlement evidence.
- The provider-budget ticket owns timeouts, retry counts, concurrency, cost ceilings,
  and the operator policy for provider usage that cannot be proven exactly.
- The tool-surface ticket owns agent-visible schemas and any tool removal,
  modification, merge, or addition requiring owner approval.

No product code, migration, deployment, or tool-surface change is approved by this
contract.
