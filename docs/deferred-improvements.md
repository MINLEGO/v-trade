# Deferred improvements

These items are intentionally deferred so they do not delay the seven-day shadow run.
They are useful production or analysis improvements, but they are not required for the
accuracy, isolation, auditability, or safety of the current frozen baseline.

## Advanced probabilistic metrics

- Compute Brier score, log loss, calibration curves, calibration error, and reliability
  bins once enough resolved forecasts exist.
- Separate forecast quality from trading performance and preserve the exact forecast,
  market snapshot, cutoff, and resolution used by every score.
- Add cohort comparisons by model, category, horizon, liquidity, and market age without
  changing the frozen baseline ranking retroactively.

## Complete immutable reports

- Generate content-addressed weekly and monthly reports containing the full portfolio,
  realized and unrealized PnL, fees, drawdown, turnover, rejection reasons, research
  usage, provider usage, and probabilistic metrics.
- Persist each report's query cutoff, schema version, input fingerprints, and artifact
  hash so a published report can be reproduced and cannot silently change.
- Add explicit corrections as new report versions instead of overwriting an existing
  report.

## Periodic isolated restoration drill

- Restore a real backup into an isolated disposable PostgreSQL/Supabase environment on
  a schedule.
- Verify migrations, row counts, foreign keys, ledger balance, artifact references,
  hashes, retention metadata, and a sample portfolio replay before declaring the drill
  successful.
- Record measured recovery point and recovery time, retain the drill evidence, alert on
  failure, and delete the disposable environment after verification.

This drill requires an operator-provided isolated restore target and backup access. It
must not be simulated with mocks when it is scheduled.

## Other non-blocking improvements

- Add exact per-turn harness resume. The seven-day baseline currently fails closed when
  a crash occurs before the completed harness run is durable; it never repeats paid
  provider calls or trading tool mutations automatically, but that cycle requires
  operator review instead of resuming at the last model turn.
- Add an orphan-artifact sweeper for the narrow crash window between a successful
  provider upload and durable artifact-inventory registration. During recovery of an
  already completed harness run, also recover exact stored byte lengths instead of the
  conservative zero-byte placeholder used only for storage projections.
- Run the `liquidity_aware` paper policy as a separately labelled experiment; never mix
  it into the `predictionarena_unconditional` baseline ranking.
- Replace the conservative provider-neutral byte bound with verified provider-native
  token accounting if stable tokenizer contracts become available.
- Optimize large immutable snapshot reads, pagination, and reporting projections after
  shadow measurements identify actual bottlenecks.
- Add richer operator visualizations, downloadable audit bundles, and longer-horizon
  capacity projections.
- Evaluate additional research providers only as explicitly versioned experiments; the
  baseline remains Exa and provider additions must not alter other agents.
