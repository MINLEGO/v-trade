# PredictionArena fixture corpus

The repository currently contains compact structural reports, not the byte-exact raw
cycle payload required by the phase-0 gate. Do not synthesize it from those reports.

When an owner-approved public API capture is available, ingest the untouched JSON with:

```powershell
vtrade-fixture-ingest docs/prediction_arena_cycles.json `
  --endpoint "https://www.predictionarena.ai/api/polymarket/cycles?offset=0&limit=200" `
  --artifact-root artifacts/predictionarena `
  --manifest spec/fixtures/manifest.json
```

The owner-approved capture has 200 unique cycle IDs and SHA-256
`2362521d0597263e882c397ab8ef456f64af2cb373ed1888319d157d3b18f2f2`.
The ingestor hashes and gzip-archives raw bytes in bounded chunks, deduplicates by
endpoint/content hash, extracts a source cutoff, rejects cycles without stable IDs,
and deliberately ignores `count` and `hasMore` as pagination truth. It never prints
raw prompts, reasoning or tool results.
