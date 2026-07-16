# PredictionArena fixture corpus

The repository currently contains compact structural reports, not the byte-exact raw
cycle payload required by the phase-0 gate. Do not synthesize it from those reports.

When an owner-approved public API capture is available, ingest the untouched JSON with:

```powershell
vtrade-fixture-ingest cycles.json `
  --endpoint "https://www.predictionarena.ai/api/polymarket/cycles?offset=0&limit=50" `
  --artifact-root artifacts/predictionarena `
  --manifest spec/fixtures/manifest.json
```

The ingestor hashes and gzip-archives raw bytes, deduplicates by endpoint/content hash,
extracts a source cutoff, rejects cycles without stable IDs, and deliberately ignores
`count` and `hasMore` as pagination truth.

