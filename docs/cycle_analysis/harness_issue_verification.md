# Harness issue verification and tool-call bloat reduction

Date: 2026-07-19  
Scope: 24 non-empty cycle logs in `docs/cycle_analysis/cycle_log`, covering the 02:00-13:00 run, plus the current production harness code.

## Executive conclusion

The discovery failure is real, but it has two interacting causes:

1. Discovery is restricted to the small frozen market universe. `_market_rows()` queries only `market_snapshot_ids`, and the freeze currently selects roughly 10-11 market snapshots.
2. The default result ceiling is 4,000 bytes, while a single verbose market row averages about 3,464 bytes in the observed logs. The ceiling therefore reduces many discovery results to one visible market, or to an empty list with `truncated: true`.

The report's pagination diagnosis is only partly correct: discovery tools have no agent-facing pagination, but the observed `truncated` flag is primarily the generic output-bounding mechanism, not proof that the SQL `LIMIT 100` was reached.

## Verification results

| Finding | Status | Evidence and interpretation |
|---|---|---|
| Discovery repeatedly returns Argentina/Jesus/Chelsea rather than a broad market set | **Confirmed** | Across the logs, only six distinct market questions appear in discovery outputs. Argentina appears 192 times, Jesus 25, Chelsea 20, Spain 7, Oprah 3, and Andrew Yang once. The code filters the frozen snapshot IDs before applying discovery filters (`src/vtrade/production_tools.py:270-326`). |
| `search_tags` is empty or nearly useless | **Confirmed, with two causes** | It searches only `_market_rows(100)`, so it cannot see markets outside the frozen set. It also returns the same verbose market object and is subject to the 4,000-byte output bound. |
| `discover_events`, `list_top_events`, and `get_newest_events` fail to discover | **Confirmed in this sample** | `discover_events` was called 160 times and returned no events 141 times; `list_top_events` returned no events in all 24 calls; `get_newest_events` returned one event in all 23 calls. The event tools also use `_market_rows(100)`, so they cannot expand the universe. |
| Lack of pagination | **Confirmed as an API limitation, but not the only cause** | Discovery schemas expose no cursor. However, `truncated` is also added by `_bounded_output()` (`src/vtrade/production_tools.py:828-898`) whenever the serialized result exceeds the ceiling. A compact result should be introduced before adding more pages. |
| Full tag metadata is bloated | **Confirmed** | 738 tag objects were observed. Full tag objects averaged about 1,260 bytes per market; label-only arrays averaged about 95 bytes, a measured reduction of approximately 13.3x. |
| Full order books are bloated | **Confirmed** | 62 order-book responses averaged about 4,285 bytes in the logs. Keeping the first five levels per side plus best bid/ask reduced the same observed payloads to about 610 bytes on average, approximately 86% smaller. The raw snapshot must remain archived internally. |
| Market discovery cards contain unnecessary metadata | **Confirmed** | A market row includes repeated internal IDs, full resolution text, complete tag objects, and duplicated outcome identifiers. The observed market row averages 3,464 bytes. A compact candidate card containing the question, timing, liquidity/volume, prices, status, a stable market reference, and tag names averages about 585 bytes in a replay calculation. |
| `get_market_details` failures | **Confirmed** | 15 failures occurred. The current tool accepts only an exact frozen slug (`src/vtrade/production_tools.py:193-201`). Agents frequently reconstructed slugs with or without numeric suffixes, or passed an internal UUID. Example: `mimo_19-07-2026_3-38.json:162`. |
| `get_orderbook` ID confusion | **Confirmed as an interface problem** | The portfolio exposes an internal `outcome_id`, while `get_orderbook` queries by `venue_token_id` (`src/vtrade/production_tools.py:336-374`). One failure passed `fe140eeb-...`, visible in `mimo_19-07-2026_3-38.json:91,187,215`. |
| Fee outcome is catastrophic | **Confirmed as an observed execution/accounting result; unit root cause still needs raw fee-artifact verification** | The log records $1,000 gross and $59 fee at price 0.41 (`mimo_19-07-2026_13-37.json:91`). The code converts stored basis points using `bps / 10_000` (`src/vtrade/market_data.py:265-283`) and applies `shares * rate * price * (1-price)` (`src/vtrade/broker.py:97-108`). At 1,000 bps this produces a 5.9% fee on notional. Do not silently replace it with a guessed rate; verify the archived provider response and owner-approved fee policy first. |
| Belief probability mismatch | **Confirmed as a schema/implementation ambiguity** | `create_general_belief` accepts `confidence`, then writes it into `BeliefRecord.probability` (`src/vtrade/production_tools.py:476-491`). The database revision writes `probability` and explicitly stores `confidence` as NULL (`src/vtrade/harness_repository.py:529-543`). The log then shows the text estimating 44% while the stored field is 0.55 (`mimo_19-07-2026_2-38.json:202`). |
| Repeated failed searches | **Confirmed** | There were 424 discovery calls in total. At least 246 returned empty results across `discover_events`, `search_tags`, `discover_hot_markets`, and `list_top_events`. Exact repeated examples include `search_tags({query: politics})` 14 times, `search_tags({query: World Cup})` 8 times, and `discover_events({keyword: Chelsea Clinton})` 6 times. |
| Belief/plan write overhead | **Confirmed** | Across the 24 logs: 29 belief creations, 15 belief deletions, 24 next-cycle plan creations, and 3 long-term plan creations. One rendered context contained 12 active beliefs and 10 plans, reaching about 28.6 KB before tool calls. Plans are inserted as active and `read_plans()` does not filter by status (`src/vtrade/harness_repository.py:575-647`). |
| Concentration risk is a harness defect | **Not confirmed** | The observed $1,000 order is 10% of a $10,000 account and remains below the configured 15% per-market cost-basis limit (`src/vtrade/broker.py:312,539`). It is a strategy/policy concern because it was the only position, but it is not evidence that the risk guard failed. |

## Recommended response, in order

### 1. Make discovery results compact before expanding the universe

Use a dedicated discovery-card representation rather than `_market_row()` for every search result. A suitable default is:

```json
{
  "market_ref": "558938",
  "question": "Will Argentina win the 2026 FIFA World Cup?",
  "closes_at": "2026-07-20T00:00:00Z",
  "volume_24h_micros": 2517353301182,
  "liquidity_micros": 7834524223400,
  "competitive": 0.9919,
  "status": "open",
  "tradeable": true,
  "tag_names": ["Sports", "Soccer", "FIFA World Cup"],
  "outcomes": [
    {"name": "Yes", "indicative_price": "0.4095"},
    {"name": "No", "indicative_price": "0.5905"}
  ]
}
```

The user's proposed comma-separated tag names are sufficient information-wise. A JSON array of names is preferable for machine use; a compact comma-separated string is also acceptable if the schema must be minimal. Keep full tag objects, resolution rules, condition IDs, question IDs, internal IDs, and duplicate outcome IDs only in `get_market_details` or an internal archive.

Do not remove every actionable identifier. Keep one stable `market_ref` in discovery, and keep `venue_token_id` either in the selected market-details response or in a clearly named outcome reference. The current duplicated `id`/`venue_market_id`/`event_id`/outcome `id` combination is what should be removed.

### 2. Separate candidate discovery from full inspection

Use two payload tiers:

- Discovery: compact cards, tag names, indicative prices, volume/liquidity, timing, and one stable market reference.
- Details: canonical slug, full resolution rules, outcome names, `venue_token_id`, tick/minimum order constraints, and any metadata needed before ordering.

Change `get_market_details` to accept `market_ref` or `market_id` in addition to exact slug, and return the canonical slug. Change `get_orderbook` to accept an explicitly typed `outcome_id` or `venue_token_id`, or make the portfolio expose both fields. This removes model-side ID reconstruction and prevents 15 repeated detail failures plus the observed order-book failure.

### 3. Add bounded pagination after compaction

Add `cursor` and `next_cursor` to discovery tools, using a deterministic offset over the frozen market snapshot set. Distinguish:

- `has_more`: more frozen candidates exist;
- `payload_truncated`: the response was shortened by the output ceiling.

Do not use one `truncated` field for both cases. Keep the generic result ceiling as a safety net, but compacting the result should let several candidates fit in the first response. If the goal is broader market access, increase the freeze shortlist separately; pagination cannot reveal markets that were never frozen.

### 4. Remove raw snapshot IDs from the model prompt

The rendered cycle context currently exposes `market_snapshot_ids` and `order_book_snapshot_ids`, although the tools already hold those IDs server-side. The rendered context observed in these logs did not include `fee_rate_snapshot_ids`, so the report's claim of three UUID arrays is not accurate for this run. Retain `data_cutoff`, counts, and perhaps one opaque context hash; omit the raw UUID arrays from the agent-facing prompt.

### 5. Stop repeated discovery loops

Add a per-cycle discovery cache keyed by tool name, normalized arguments, and cutoff. Return a small `cached: true` marker for repeats. The model policy should also stop after one empty result for a tool/keyword combination unless the parameters or cutoff changed. A compact server-side `discover_markets` aggregator could combine filters, but this would be an explicit harness deviation from the current 29-name baseline.

The 131 web-search calls deserve a second-stage optimization rather than an aggressive reduction: cache identical queries at the same cutoff, deduplicate URLs across queries, and return compact title/URL/date/snippet cards. Preserve enough independent sources for a thesis; do not reuse stale results across a changed cutoff without marking their age.

### 6. Fix memory semantics without losing thesis diversity

Do not auto-deactivate all beliefs in the same broad category: the logs contain distinct Sports and Market Analysis theses. Prefer a `thesis_key`/`market_ref` and a revise/upsert operation that creates a revision on the same logical belief. The rendered prompt should include only the latest active revision per thesis.

For plans, `next_cycle` is naturally singleton. When a new next-cycle plan is created, mark the prior one superseded and make `read_plans()` return active plans only. Keep historical revisions in the database for audit. This removes repeated delete/create choreography while preserving the six-month retained history.

### 7. Split belief probability from confidence

Add distinct `probability` and `confidence` inputs. Keep backward compatibility only as an explicitly labeled legacy path; do not interpret `confidence` as probability. Before enabling trades again, verify that the rendered prompt and stored revision show the same probability used by the agent's EV calculation.

### 8. Investigate fees before changing them

The $59 fee is a hard stop for the current strategy, but the source-unit diagnosis must use the retained raw fee-rate artifact. Verify:

1. the exact provider response for the token;
2. whether `base_fee` is basis points or another unit for that endpoint/version;
3. the intended fee policy and formula;
4. a regression test covering the observed value and a normal low-fee value.

The existing test fixture expects an official `base_fee` of 30 bps, which supports the current normalization convention, but it does not prove why this run produced 1,000 bps.

## Expected first-pass savings

The highest-confidence first pass is:

1. discovery-card serializer with tag names only;
2. top-5 order-book depth by default;
3. typed market/outcome references;
4. prompt removal of raw snapshot UUID arrays;
5. same-cutoff discovery and web-search caching;
6. singleton active next-cycle plan rendering.

On the observed logs, tag compaction alone saves roughly 13x on tag bytes; the compact market-card replay is about 83% smaller than the current market row; and top-five order books reduce observed order-book payloads by about 86%. These are payload observations, not guarantees of future token-cost savings until rerun with the actual model tokenizer.

No production code was changed while preparing this verification. The recommendations above include intentional interface changes and should be implemented as a versioned harness change after owner approval, especially the expanded freeze universe, fee policy, and belief schema.
