discover events / get newest events doesn't seem to work properly
search tags also doesn't seem to work
same for list_top_events, discover_hot_markets

everything seems to return fifa world cup and jesus christ for some reason.
This may be caused by the lack of pagination use. (if pagination is even usable by the models).

when listing markets, we can aggressively strip tags as well as token / ids if they are not used in other tools


## Analysis of 3 Cycle Logs: Issues & Efficiency Losses

### Confirmed Issues from `found_issues.md` (verified with source code)

**1. Market Discovery returns only 1-2 markets (Argentina WC + Jesus Christ)**

- **Root cause**: `_market_rows()` at [`src/vtrade/production_tools.py:311-316`](src/vtrade/production_tools.py:311) queries only `self._context.market_snapshot_ids` — the **frozen snapshot set** from the cycle. The cycle freezes only 10-11 market IDs, and of those, only 1-2 are `status='open' AND tradeable=true`. All discovery tools (`discover_events`, `discover_hot_markets`, `list_top_events`, `search_tags`, `browse_markets_by_volume`, `discover_by_time_remaining`, `discover_by_price_volatility`, `get_newest_events`, `get_all_active_markets`) all call `_market_rows(100)` then filter — so they all return the same tiny subset.

- **Evidence**: Cycle 1 `discover_events("Argentina Spain World Cup final")` → empty. `discover_events("FIFA World Cup")` → empty, `truncated: true`. `list_top_events` → empty, `truncated: true`. `discover_hot_markets` → empty. Only `discover_events("Argentina")` finds the market. This pattern repeats identically across all 3 cycles.

**2. `search_tags` returns empty**

- **Root cause**: At [`production_tools.py:216-219`](src/vtrade/production_tools.py:216), `search_tags` filters rows from `_market_rows(100)` by checking if the query string is in `str(row[12]).casefold()` (the metadata JSON). Since `_market_rows` only returns 1-2 markets, the tag filter has almost nothing to work with.

- **Evidence**: `search_tags("politics")` → empty, `truncated: true`. `search_tags("crypto")` → empty. `search_tags("economy")` → empty (all 3 cycles).

---

### New Issues Found

**3. Fee rate is catastrophic: ~10% rate → 5.9% effective fee on trades**

- **Root cause**: The Polymarket CLOB fee-rate API returns `base_fee` in basis points. The code at [`market_data.py:280-283`](src/vtrade/market_data.py:280) converts `bps → rate = Decimal(bps)/10000`. The stored fee rate is **1000 bps = 10%**. The formula at [`broker.py:104-107`](src/vtrade/broker.py:104) is `fee = shares × rate × price × (1-price)`, which for a $0.41 market produces effective fee = 5.9% of notional.

- **Evidence** (Cycle 1): `gross_micros=1,000,000,000 ($1,000)`, `fee_micros=59,000,000 ($59)` → $59/$1,000 = **5.9% fee**. On a ~3% edge trade, the fee consumes almost **double** the expected edge. Real Polymarket fees are ~0.1%.

- **Impact**: This destroys the agent's edge entirely. The agent estimated a ~3% edge on Argentina YES, but the fee alone is 5.9% of notional. The trade had **negative expected value after fees**.

**4. `get_market_details` + `get_orderbook` fail with ToolContextUnavailable**

- **Root cause A** (`get_market_details`): Requires exact slug match against frozen snapshots at [`production_tools.py:193-200`](src/vtrade/production_tools.py:193). The agent tried slug "will-argentina-win-the-2026-fifa-world-cup-245" (with trailing number) and "will-argentina-win-the-2026-fifa-world-cup" (without). Neither matched the frozen slug, so the tool errored every time from Cycle 2 onward.

- **Root cause B** (`get_orderbook`): At [`production_tools.py:336-351`](src/vtrade/production_tools.py:336), the query joins `outcomes ON o.id = obs.outcome_id WHERE o.venue_token_id = %s`. In Cycle 2, the agent passed the `outcome_id` from the portfolio (`fe140eeb-7a82-5499-ac23-6b2122951164`) instead of the `venue_token_id`. These are different IDs — the portfolio returns `outcome_id`, but the tool expects `venue_token_id`.

- **Evidence**: Cycle 2 line 160-163: `get_market_details({"slug": "will-argentina-win-the-2026-fifa-world-cup"})` → error. Cycle 2 line 186-189: `get_orderbook({"token_id": "fe140eeb-7a82-5499-ac23-6b2122951164"})` → error. The agent then cannot verify its position's current price or order book depth.

**5. Belief management creates overhead each cycle**

- **Root cause**: The agent creates a new belief (and deletes the old one) every cycle, even for tiny probability changes (44% → 42% → 41%). Each operation is a separate database write.

- **Evidence**: Cycle 1: creates belief (44%). Cycle 2: deletes old belief, creates new one (42%). Cycle 2: creates next_cycle_plan. Cycle 3: creates new belief (41%), deletes 42% belief, creates another next_cycle_plan. Total: **3 beliefs created, 2 deleted, 3 plans created** across 3 cycles for essentially the same analysis.

**6. Stored probability field doesn't match belief text**

- **Root cause**: The `create_general_belief` tool stores a `probability` field separate from the `content` text. The agent writes "~44%" in the text but stores `probability: 0.55` (55%). This is a 25% relative error.

- **Evidence**: Cycle 1 belief: content says "~44% probability" but `probability: "0.550000000"`. The agent doesn't notice this discrepancy and uses the wrong value in its analysis.

**7. Agent repeats identical failed searches across cycles**

- **Root cause**: The agent doesn't cache or learn from previous discovery failures. Each cycle, it runs the same searches (`discover_events`, `discover_hot_markets`, `list_top_events`, `search_tags`) that all return empty, wasting tool calls and reasoning tokens.

- **Evidence**: Cycle 1: 8 discovery calls (most empty). Cycle 2: 10 discovery calls (most empty). Cycle 3: 9 discovery calls (most empty). The agent spends ~40% of each cycle on fruitless market discovery.

**8. No pagination mechanism despite `truncated: true`**

- **Root cause**: `_market_rows()` at [`production_tools.py:311-316`](src/vtrade/production_tools.py:311) has a hard LIMIT 100 and no cursor/pagination support. The `truncated: true` flag in responses signals truncated data but the agent has no way to request the next page.

- **Evidence**: Multiple responses show `"truncated": true` (e.g., `discover_events`, `list_top_events`, `search_tags`). The agent never paginates because no pagination tool exists.

**9. Concentration risk: single binary position = 100% of trading exposure**

- **Root cause**: The agent deploys $1,000 (10% of cash) into a single binary outcome. If Argentina loses, the entire position is wiped out. The protocol's risk limit guidance is not enforced.

- **Evidence**: Cycle 1: $1,000 → 2,439 YES shares. Cycle 2-3: position unchanged. If Argentina loses, loss = $1,000 (100% of deployed capital, 10% of total account).

---

### Summary of 5-7 Possible Root Causes (Distilled to 2 Most Likely)

| # | Root Cause | Evidence | Impact |
|---|-----------|----------|--------|
| 1 | **Frozen snapshot scope is too narrow** — cycle freezes only 10-11 market snapshots, of which 1-2 are open+tradeable | `_market_rows()` queries only `market_snapshot_ids`; discovery tools consistently return 1-2 markets | Agent can't find opportunities; wastes ~40% of cycles on fruitless discovery |
| 2 | **Fee rate is 1000 bps (10%)** — 100x higher than real Polymarket fees | Fee calculation: $59 on $1,000 = 5.9% effective fee; formula `shares × rate × p × (1-p)` with rate=0.10 | Every trade starts with negative expected value; edge-destroying |

**Secondary contributors:**
- No pagination mechanism for truncated results
- No ID type validation (confusing outcome_id vs venue_token_id vs slug vs market_id)
- No cross-cycle memory of what was already searched
- Belief probability field is disconnected from belief text content

### Recommended Fixes (Highest Priority)

1. **Fix fee rate**: Verify the Polymarket fee-rate API returns `base_fee` in the expected unit. If the test environment uses an artificially high rate, configure a realistic fee rate (10-30 bps). If the formula is wrong, correct it.

2. **Expand frozen snapshot scope**: Increase the number of market snapshots frozen per cycle (currently 10-11). The `freeze()` method at [`market_data.py:391-484`](src/vtrade/market_data.py:391) uses `maximum_additional_outcomes=20` and `venue_batch_size=20` — these could be increased.

3. **Add pagination to market queries**: Implement cursor-based pagination in `_market_rows()` and expose a `cursor`/`next_cursor` parameter in discovery tools.

4. **Add ID type validation/documentation**: The `get_orderbook` tool should accept both `outcome_id` and `venue_token_id`, or clearly document which ID is expected. Same for `get_market_details` accepting market_id or slug.

5. **Add cross-cycle memory**: The agent should remember which discovery searches returned empty and skip them in future cycles, or the system should provide a "recently seen markets" view.



## Data Bloat Analysis in Tool Call Results

You're right to flag this. I analyzed the actual response payloads from the cycle logs and found **3 major sources of unnecessary data**:

### 1. Tag metadata: ~5x bloat (10 fields → 2 needed)

Each tag in the `metadata` section returns **10 fields** when only **2** are ever used by the agent:

```
Returned (per tag):
{
  "createdAt":      "2023-10-24T22:37:50.31Z",   // NEVER used
  "forceHide":      true,                          // NEVER used (UI flag)
  "forceShow":      false,                         // NEVER used (UI flag)
  "id":             "1",                           // NEVER used (internal DB id)
  "label":          "Sports",                      // ✓ USED for categorization
  "publishedAt":    "2023-10-24 22:37:50.296+00",  // NEVER used
  "requiresTranslation": false,                    // NEVER used
  "slug":           "sports",                      // ✓ USED for categorization
  "updatedAt":      "2026-04-17T20:47:16.461062Z", // NEVER used
  "updatedBy":      15                             // NEVER used
}
```

The Argentina market has ~8 tags. The tag section alone wastes **~80 useless fields per market returned**. This multiplies if more markets become discoverable.

**Fix**: In `_market_row()` at [`production_tools.py:705`](src/vtrade/production_tools.py:705), transform `metadata.tags` to keep only `{"label": ..., "slug": ...}`.

### 2. Order book levels: ~80% of response bytes are never used

The `get_orderbook` response at [`production_tools.py:366-374`](src/vtrade/production_tools.py:366) returns the **full bids/asks arrays** (40+ levels in the truncated view, likely thousands in full). The agent **only uses `best_bid` and `best_ask`** — confirmed across all 3 cycles.

From Cycle 3 (4-38), the order book returns ~40 ask levels. The agent's reasoning: *"The order book shows best bid $0.409 and best ask $0.41 — a tight spread"*. It never analyzes the depth distribution, levels below top 1, or any other detail. The vast majority of the response payload is ignored.

**Fix**: In `_get_orderbook()` at [`production_tools.py:366`](src/vtrade/production_tools.py:366), trim bids/asks to top 5 levels per side, or add a `depth` parameter defaulting to 3.

### 3. Metadata fields on discovery markets: ~6 unnecessary fields

Each `_market_row` returns these metadata fields at [`production_tools.py:690-707`](src/vtrade/production_tools.py:690):

| Field | Used by agent? | Notes |
|-------|---------------|-------|
| `slug` | ✓ | For get_market_details |
| `question` | ✓ | To understand the market |
| `closes_at` | ✓ | For timing analysis |
| `volume_micros` | ✓ | For liquidity assessment |
| `liquidity_micros` | ✓ | For liquidity assessment |
| `status` | ✓ | To check if open |
| `tradeable` | ✓ | To check if tradeable |
| `resolution_rules` | Sometimes | Long text, rarely read |
| `opens_at` | ✗ | Never referenced |
| `venue_market_id` | ✗ | Never used directly |
| `event_id` | ✗ | Never used directly |
| `id` | ✗ | Internal ID, agent confuses it with slug |
| `outcomes[].venue_token_id` | ✗ | 77-char hex, needed for order placement but not discovery |
| `metadata.condition_id` | ✗ | 66-char hex, never used |
| `metadata.question_id` | ✗ | 66-char hex, never used |
| `metadata.one_day_price_change` | ✗ | Never referenced |
| `metadata.one_hour_price_change` | ✗ | Never referenced |
| `metadata.negative_risk` | ✗ | Never referenced |
| `metadata.created_at` | ✗ | Never referenced |
| `metadata.enable_order_book` | ✗ | Never referenced |

Additionally, the cycle context in each user message contains `market_snapshot_ids`, `order_book_snapshot_ids`, `fee_rate_snapshot_ids` — arrays of full UUIDs that the agent never uses.

### 4. Agent-generated bloat (cross-cycle)

The agent creates a new belief and plan each cycle, with the old ones remaining in the database. By Cycle 3 there are **3 active plans** and **3 beliefs** (1 active, 2 inactive). This accumulates:

- Cycle 1: 1 belief + 1 plan created
- Cycle 2: 1 belief deleted, 1 new belief created, 1 plan created
- Cycle 3: 1 belief created, 1 belief deleted, 1 plan created

That's **6 belief operations + 3 plan operations** for essentially the same analysis. Each cycle's user message includes **all** beliefs and plans in the context.

### Summary of bloat reduction opportunities

| Area | Current | Stripped | Reduction |
|------|---------|----------|-----------|
| Tags per market | 10 fields × ~8 tags = **80 fields** | 2 fields × ~8 tags = **16 fields** | **5x** |
| Order book levels | ~**40+ levels** (full: thousands) | 5 levels per side | **8x**+ |
| Market ID fields | **~18 field values** per market | ~10 field values | **~45%** |
| Cycle context IDs | **3 UUID arrays** (~50 UUIDs total) | omit entirely | **~3KB** |
| Belief/plan retention | Accumulates every cycle, never pruned | Auto-deactivate when superseded | Linear |

### Recommended Fixes (in priority order)

1. **Strip tag fields**: In `_market_row()`, map `metadata.tags` to `[{label, slug}]` only
2. **Trim order book**: Return only top 5 bids/asks, or return only `best_bid`/`best_ask` with an optional `depth` parameter
3. **Strip stale metadata fields**: Remove `condition_id`, `question_id`, `created_at`, `one_hour_price_change`, `enable_order_book` from discovery responses; keep only what the agent demonstrably uses
4. **Auto-prune beliefs**: When a new belief is created in the same category, auto-deactivate the previous active one (don't make the agent do it manually)
5. **Remove hex IDs from discovery**: `venue_token_id`, `venue_market_id`, `event_id` are not needed in discovery responses — only in order placement contexts