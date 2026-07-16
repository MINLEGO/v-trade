# Implementation status

Checked: 2026-07-13.

## Stable local lot

Phase 0 currently includes the source/feature matrix, the name-by-name matrix and
canonical inferred schemas for all 29 observed tools, a trace-derived prompt and
conformance checklist, a versioned experiment definition, and a defensive raw-fixture
ingestor. Phase 1 includes the Python 3.12 project, canonical domain ports/types,
configuration hashing and run gate, append-only domain ledger, content-addressed gzip
artifact store, health API, PostgreSQL foundation migration, and Coolify service shape.

Local standard-library validation passes without network access. PostgreSQL/provider
contract tests are intentionally not claimed as passing.

## Required owner/external inputs

The experiment definition blocks execution while these are unresolved:

1. paper fill rule;
2. OpenRouter reasoning effort and provider allowlist;
3. prompt/transcript/reasoning visibility and redaction;
4. conflict between Exa burst ceilings 50 (§9) and 100 (§15).

The owner has resolved these formerly open points:

- model slugs are `deepseek/deepseek-v4-flash` and `xiaomi/mimo-v2.5-pro`;
- quantization is capped at 8 bits and cross-model fallback is forbidden;
- prompts, transcripts and reasoning are retained for six months;
- agents have independent start dates, hourly schedules and data cutoffs; adding or
  removing one does not alter the others, and simultaneous start is not required.

The following resources are also required; no mock or fallback has been created:

- a disposable PostgreSQL/Supabase database URL to apply and test the migration;
- Supabase project URL, service-role credential and bucket name for the durable artifact adapter;
- an owner-approved byte-exact PredictionArena `/cycles` capture for the fixture corpus;
- OpenRouter and Exa API credentials for later provider contract tests;
- the private admin authentication secret before non-health admin routes exist.

The existing repository contains compact endpoint reports only. They must not be
reverse-engineered into a fake raw fixture corpus.
