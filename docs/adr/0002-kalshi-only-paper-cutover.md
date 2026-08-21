# ADR 0002: irreversible Kalshi-only paper cutover

## Status

Accepted for `vtrade-kalshi-v1`.

## Decision

V-Trade is replaced by a Kalshi-only, paper-only release on a fresh empty PostgreSQL
database. The active process admits ordinary binary markets only, reads public Kalshi
REST data without credentials, and exposes a semantic YES/NO order contract. The
worker can simulate paper fills but cannot authenticate, sign, or submit a venue
order.

The active repository contains exactly four dependency-ordered migrations:

1. foundation, agent state, raw evidence, and balanced ledger;
2. Kalshi catalogue, dynamic grids, freezes, and canonical books;
3. semantic orders, fees, portfolio accounting, risk, and settlement;
4. runtime checkpoints, retention, admin controls, and readiness projections.

Historical research is retained only below `docs/archive/predictionarena/` with an
explicit read-only marker. It is not imported or loaded by runtime, tests, image
builds, migrations, or fixture discovery and does not establish venue, schema,
execution, or performance equivalence.

## Deployment sequence

The release gates are ordered:

```text
zero-active-venue sweep -> offline checks -> disposable PostgreSQL bootstrap
-> image build/import checks -> private storage readiness
-> French-host public REST probe -> migrate -> API readiness -> worker
```

Offline checks are evidence about repository-local behavior only. PostgreSQL,
built-image, private-storage, provider-egress, and French-host results must be
recorded from the real resources named by the deployment contract. Missing resources
fail closed.

## Rollback boundary

Rollback is infrastructure-only. Before the new database is initialized, deployment
may return to the prior image and snapshot. After any active Kalshi data is written,
the worker is stopped or paused and the matching pre-cutover snapshot/image is
restored, or a new empty target is rebuilt. There is no application downgrade,
dual-read, data conversion, or schema mixing path.

Because this release cannot submit real orders, rollback has no external-order
reconciliation step. Future real execution requires a separate architecture review,
experiment version, eligibility decision, authentication/signing boundary, and
reconciliation contract.

## Consequences

The cutover is intentionally irreversible at the application layer. Operators must
capture database, object-storage, image, configuration, and artifact digests before
migration. A failed gate stops the sequence; it is not replaced by a local mock,
fake provider, old fixture, or historical result.

