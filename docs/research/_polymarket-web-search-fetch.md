# Historical Polymarket web search and webpage fetching

## Scope and source revision

This note covers only the historical Polymarket-side implementation in
[MINLEGO/v-trade-polymarket](https://github.com/MINLEGO/v-trade-polymarket), inspected at
commit [cb808f43bd305f4ebac65bc52ab7919ca9bdf346](https://github.com/MINLEGO/v-trade-polymarket/commit/cb808f43bd305f4ebac65bc52ab7919ca9bdf346).
The repository was read from that commit; no application code was changed in the
workspace.

Relevant history:

- ca4b49bef7cabbc7885f281214091a871abb624f (improved web_search, 2026-07-31)
  added the later search contract and provider behavior
  ([commit](https://github.com/MINLEGO/v-trade-polymarket/commit/ca4b49bef7cabbc7885f281214091a871abb624f)).
- 442681137ff39953bcf344fb01e2b63a67950a54 (added the fetch_webpage tool,
  2026-08-01) added webpage fetching through Exa /contents
  ([commit](https://github.com/MINLEGO/v-trade-polymarket/commit/442681137ff39953bcf344fb01e2b63a67950a54)).
- c2f8f12b35a9fb371a04d4602912e42bed7c03e3 made result_type optional
  ([commit](https://github.com/MINLEGO/v-trade-polymarket/commit/c2f8f12b35a9fb371a04d4602912e42bed7c03e3));
  e4726d1fd87cb52d254acf470383a15412aabf42 added input/output validation
  ([commit](https://github.com/MINLEGO/v-trade-polymarket/commit/e4726d1fd87cb52d254acf470383a15412aabf42)).

## End-to-end path

The active path is:

spec/tool-schemas-v1.json -> BoundedToolHarness validation/limits ->
ProductionToolRegistry._web_search or _fetch_webpage ->
ExaResearchProvider.search or .fetch -> Exa HTTP API.

The registry loads the frozen schema artifact and requires its handler set to
match exactly; the two research handlers are registered at
[src/vtrade/production_tools.py:126-233](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/production_tools.py#L126-L233).
The handlers forward all arguments except the primary query/url key and return
ToolExecution(output, telemetry) at
[src/vtrade/production_tools.py:475-488](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/production_tools.py#L475-L488).

## Agent-facing contracts

### web_search

The frozen agent-facing contract is at
[spec/tool-schemas-v1.json:418-510](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/spec/tool-schemas-v1.json#L418-L510):

- Required input: non-empty query.
- num_results: integer 1-10, default 10.
- max_highlight_length: integer 1-15,000, default 1,500. The product
  num_results * max_highlight_length must not exceed 15,000.
- start_published_date and end_published_date accept a non-negative days-back
  integer or YYYY-MM-DD; defaults are 30 and 0 respectively.
- The active frozen schema has additionalProperties: false and exposes no
  include_domains/exclude_domains fields.
- Output is {query, results}. Each result contains only title, url, nullable
  published_at, and string content.

The lower-level provider schema constant in
[src/vtrade/providers.py:33-91](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L33-L91)
also describes include_domains and exclude_domains (up to 50 each), and the
provider implementation accepts them. They are not part of the active frozen
tool artifact, so they are not public agent inputs in this revision.

### fetch_webpage

The frozen contract is at
[spec/tool-schemas-v1.json:513-581](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/spec/tool-schemas-v1.json#L513-L581):

- Required input: non-empty url.
- result_type is optional and defaults to highlights; allowed values are
  full_text and highlights.
- highlight_query is a string or null, default null; it is invalid with
  result_type: full_text.
- max_length is 1-12,000 characters, default 4,000.
- Output is a strict oneOf: metadata (title, url, published_at, author) plus
  either full_text or a string-array highlights.

The schema-level tests assert these exact bounds and output branches in
[tests/test_spec.py:119-159](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/tests/test_spec.py#L119-L159).

## Provider and HTTP behavior

ExaResearchProvider is the active implementation. Endpoint constants, limits,
and httpx client setup are at
[src/vtrade/providers.py:17-31](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L17-L31)
and the class is at
[src/vtrade/providers.py:443-588](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L443-L588).

### Search request

ExaResearchProvider.search() normalizes options, then POSTs to
https://api.exa.ai/search with the x-api-key header and JSON equivalent to:

    {
      "query": "...",
      "type": "auto",
      "numResults": 10,
      "contents": {"highlights": {"maxCharacters": 1500}},
      "startPublishedDate": "YYYY-MM-DD",
      "endPublishedDate": "YYYY-MM-DD"
    }

Non-empty domain filters are added as Exa includeDomains and excludeDomains. The
exact request construction is at
[src/vtrade/providers.py:465-487](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L465-L487).
Days-back dates are converted using the supplied timezone-aware now reference;
date validation and the ordered-range check are in
[src/vtrade/providers.py:698-761](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L698-L761).

The response normalizer requires a results list and a non-empty URL per row.
For Exa, result highlights are joined with newlines into provider-neutral
content; title and publishedDate become title and published_at. Extra provider
fields are discarded. See
[src/vtrade/providers.py:802-835](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L802-L835).

### Webpage request

ExaResearchProvider.fetch() validates and trims the URL, requires an absolute
HTTP(S) URL, then POSTs one URL to https://api.exa.ai/contents. It uses a
Bearer header rather than the search endpoint's x-api-key header. For full_text,
the body is {"urls": [url], "text": {"maxCharacters": N}}; for highlights it is
{"urls": [url], "highlights": {"maxCharacters": N, "query": "..."}}, with query
omitted when no highlight_query is given. This is implemented at
[src/vtrade/providers.py:489-504](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L489-L504).

normalize_contents_output() requires a non-empty statuses list and rejects any
status other than success, including the provider error tag in
ProviderPayloadError. It then requires a non-empty results list, selects the row
whose url or id equals the requested URL, and returns only title, URL, optional
publication date/author, and the selected text or highlights. It does not parse
HTML locally; repository dependencies contain httpx but no HTML readability or
extraction library. See
[src/vtrade/providers.py:764-891](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L764-L891)
and locked httpx 0.28.1 at
[uv.lock:292-304](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/uv.lock#L292-L304)
(pyproject.toml allows httpx>=0.28,<1 at
[pyproject.toml:11-20](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/pyproject.toml#L11-L20)).

### Timeout, retry, and errors

- The default Exa client is httpx.Client(timeout=httpx.Timeout(30.0,
  connect=10.0)) ([providers.py:450-463](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/providers.py#L450-L463)).
- Exa requests make one client.post() call and immediately call raise_for_status();
  there is no Exa retry loop, backoff, or exception translation in _request_exa()
  ([providers.py:535-588](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/providers.py#L535-L588)).
  Consequently HTTP status errors and httpx transport/timeouts propagate.
- Invalid local options fail before network I/O with ProviderConfigurationError:
  bad ranges/dates, result counts over 10, bad URL schemes, unknown options,
  invalid result modes, invalid highlight queries, and lengths over 12,000.
- Malformed or semantically failed Exa payloads raise ProviderPayloadError. A
  budget reservation is created before the network call; reconciliation occurs
  only after successful parsing, artifact persistence, and telemetry assembly.
- At the harness boundary, ValueError subclasses such as
  ProviderConfigurationError are recorded as failed, agent-visible tool calls,
  while ProviderPayloadError, HTTPStatusError, transport errors and timeouts are
  not in the caught expected-error set and therefore propagate as system
  failures. See
  [harness.py:289-341](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/harness.py#L289-L341)
  and the corresponding test at
  [tests/test_harness.py:504-548](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/tests/test_harness.py#L504-L548).
- OpenRouter has a separate three-attempt retry path in the same module; that
  behavior does not apply to either web research tool.

The provider tests cover successful HTTP shapes and validation, but there is no
Exa-specific timeout/retry test. Relevant cases are
[tests/test_providers.py:272-551](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/tests/test_providers.py#L272-L551)
and
[tests/test_providers.py:553-665](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/tests/test_providers.py#L553-L665).

## Limits and budget accounting

Both web_search and fetch_webpage belong to EXA_RESEARCH_TOOL_NAMES
([providers.py:130](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/providers.py#L130)).
The harness counts both against the strict per-cycle web-search limit before
executing any calls ([harness.py:165-253](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/harness.py#L165-L253)).
The experiment config sets that limit to 50, with 10 results per search and
4,000 default result tokens
([predictionarena-polymarket-v1.json:111-120](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/config/experiments/predictionarena-polymarket-v1.json#L111-L120)).
The research configuration selects Exa, has no fallback, and disables Tavily
([predictionarena-polymarket-v1.json:34-53](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/config/experiments/predictionarena-polymarket-v1.json#L34-L53));
the liquidity-aware config repeats the same policy
([predictionarena-polymarket-v1-liquidity-aware.json:41-57](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/config/experiments/predictionarena-polymarket-v1-liquidity-aware.json#L41-L57)).

Each Exa request reserves one request and ten worst-case credits before the POST,
then reconciles actual requestCredits; the database guard enforces the 18,000
monthly request and credit caps before network I/O
([harness_repository.py:214-264](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/harness_repository.py#L214-L264),
[0010_exa_monthly_quota.sql:4-49](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/migrations/0010_exa_monthly_quota.sql#L4-L49),
and the 1-10 reservation constraint in
[0011_private_runtime_and_strict_exa.sql:22-30](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/migrations/0011_private_runtime_and_strict_exa.sql#L22-L30)).
The Exa nominal estimate is 20,000 microdollars per request, but the provider
records billed cost as zero because costDollars.total is treated as nominal
provider-estimated telemetry; positive actual billing or credit overruns halt
the Exa circuit and raise after recording usage. See
[providers.py:544-586](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/providers.py#L544-L586)
and
[harness_repository.py:266-406](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/harness_repository.py#L266-L406).

The registry also applies a general non-paginated tool-result safety net to both
tools. If the result exceeds the configured token ceiling, strings longer than
512 characters are first clipped, then list elements are removed or strings
shrunk until the result fits; it sets payload_truncated: true when it had to
shorten the result. This is a second layer after Exa's character limits and is
implemented at
[production_tools.py:187-209](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/production_tools.py#L187-L209)
and
[production_tools.py:1667-1733](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/production_tools.py#L1667-L1733).
The strict frozen output schemas do not declare payload_truncated for these two
research outputs. Because the harness validates the handler output after the
registry wrapper runs, a web-search/fetch result that actually needs this
fallback can subsequently fail output validation with ToolOutputContractError;
the payload_truncated field is not a documented research-output field. The
post-handler validation order is visible at
[harness.py:289-341](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/harness.py#L289-L341).

## Persistence and caching

There is no response cache or cache lookup in ExaResearchProvider: every
successful call performs the provider POST. The cache_hit database column
belongs to the generic usage schema and is not populated by Exa telemetry.

After parsing, _request_exa() canonicalizes the full provider JSON after
recursive secret/Bearer redaction, stores those bytes through ArtifactStore, and
records ProviderTelemetry with provider, request/credit counts, nominal/billed
cost, latency, artifact URI, SHA-256 and byte length
([providers.py:556-588](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L556-L588)).
The production worker wires Exa to the required SupabaseArtifactStore using
VTRADE_EXA_API_KEY and Supabase credentials
([worker.py:2370-2394](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/worker.py#L2370-L2394)).

The store is content-addressed by SHA-256 and gzip-compresses canonical raw
bytes. Supabase uploads use a private bucket and x-upsert: true; the artifact
store has no local fallback in production
([artifacts.py:30-51](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/artifacts.py#L30-L51),
[artifacts.py:95-179](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/artifacts.py#L95-L179)).
The artifact tests verify deterministic content addressing, gzip round-tripping,
private-bucket validation, and exact raw-byte upload
([tests/test_artifacts.py:18-63](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/tests/test_artifacts.py#L18-L63)).

Successful research calls are also linked to normalized research_documents and
research_artifacts rows. Document identity is (canonical_url, content_sha256);
the artifact links the tool call, provider, query or highlight query, source
cutoff, raw artifact URI and raw SHA. This persistence happens in
ProductionHarnessPort._persist_detailed_audit()/_persist_research() only for
successful research records at
[worker.py:952-1129](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/worker.py#L952-L1129),
backed by the tables at
[migrations/0001_foundation.sql:144-185](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/migrations/0001_foundation.sql#L144-L185).
Provider usage is persisted separately with raw artifact URI, SHA and retention
metadata at
[harness_repository.py:771-804](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/harness_repository.py#L771-L804).

This is durable audit/archive behavior, not a replay cache: recorded model
replay exists separately, while runtime recovery explicitly does not recall Exa
or OpenRouter for an unfinished call.

## Test coverage summary

- tests/test_providers.py:272-339 verifies the Exa search URL, headers, request
  JSON, normalized output, latency and nominal/billed telemetry.
- tests/test_providers.py:340-397 verifies highlight/date bounds and that bad
  options fail before the provider request.
- tests/test_providers.py:398-509 verifies full-text /contents, metadata
  filtering, highlight mode, guiding query and 12,000-character forwarding.
- tests/test_providers.py:510-597 verifies invalid webpage modes, lengths, failed
  content statuses, invalid dates and result counts.
- tests/test_production_tools.py:270-333 verifies registry forwarding for both
  tools.
- tests/test_harness.py:451-502 verifies nullable highlight_query handling;
  tests/test_harness.py:597-650 verifies the strict combined 50-call research
  ceiling before handlers execute.
- tests/test_harness_repository.py:190-205 verifies that fetch_webpage counts as
  an Exa search in persisted harness totals.
- tests/test_config.py:122-165 and tests/test_migration_0011.py:8-15 verify the
  frozen Exa limits and reservation constraint; migration semantics are tested
  in tests/test_migration_0014.py:4-15.

The tests strongly cover request construction, normalization, validation, limits,
artifact mechanics and persistence accounting. They do not demonstrate live Exa
availability, do not exercise the research-specific payload_truncated edge, and
do not establish retries for Exa; the source shows that no Exa retry behavior was
implemented.

## Comparison with the current Kalshi checkout

The current checkout was inspected at HEAD
`f3f3c37c1c7241cc872a97c2507b5ec3c30366cc` (2026-08-31). The short conclusion is:
the Exa provider implementation was carried forward; the meaningful changes are
around the active schema, registry output bounding, Kalshi composition, and
quota-schema organization.

### What did not change

The current `ExaResearchProvider` search/fetch class and its option/normalization
helpers compare line-for-line with the Polymarket source for these paths. Current
code is at [providers.py:466-613](../../src/vtrade/providers.py#L466-L613) and
[providers.py:721-923](../../src/vtrade/providers.py#L721-L923); the historical
class/helpers are at [providers.py:443-588](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/providers.py#L443-L588)
and [providers.py:698-891](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac52ab7919ca9bdf346/src/vtrade/providers.py#L698-L891).
Consequently both revisions still:

- POST search requests to Exa `/search` and page requests to Exa `/contents`;
- use the same headers, 30-second client timeout, 10-second connect timeout, and
  no Exa retry/backoff;
- expose the same effective bounds and output shapes;
- join Exa search highlights into provider-neutral `content`, and return either
  `full_text` or `highlights` for a fetched page;
- archive redacted raw responses and return the same provider telemetry; and
- count both tool names against the same strict 50-call research ceiling.

The current registry handlers also still forward every argument except `query` or
`url` to the same provider methods ([production_tools.py:470-485](../../src/vtrade/production_tools.py#L470-L485);
historical [production_tools.py:475-488](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/production_tools.py#L475-L488)).
The only web-adjacent provider-code wording change is the disabled Tavily error,
which names the active Kalshi experiment instead of the Polymarket experiment;
Tavily remains disabled in both.

### Actual behavioral difference: oversized result handling (fixed)

Both tools are non-paginated, so the registry applies the general result wrapper
to them ([current production_tools.py:198-215](../../src/vtrade/production_tools.py#L198-L215)).
The historical wrapper recursively clipped nested strings, removed list elements,
and shrank strings until the conservative 4,000-token ceiling was met
([historical production_tools.py:1667-1733](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/production_tools.py#L1667-L1733)).
Before this fix, the current `_bounded_output` only clipped three top-level keys
(`message`, `question`, `content`) to 128 characters, then raised if the result
was still too large. It therefore did not compact
`web_search.results[*].content`, `fetch_webpage.full_text`, or
`fetch_webpage.highlights`.

The current implementation now restores the historical recursive strategy at
[production_tools.py:1434-1500](../../src/vtrade/production_tools.py#L1434-L1500):
it clips nested strings to 512 characters, removes the largest nested list entries
when needed, and progressively shrinks remaining strings until the conservative
token ceiling is met. It deliberately does not add `payload_truncated` because
these non-paginated research output schemas are strict and do not declare that
field; the returned ellipsis is the schema-compatible truncation signal.

The original regression used ten 1,500-character non-ASCII search highlights: the
result was 30,857 UTF-8 bytes and the pre-fix implementation raised
`ToolContextUnavailable`; the new regression passes and validates the compacted
output against the active schema. This is a registry-level fix, not an Exa
request change. The historical fallback added `payload_truncated`, but the strict
research output schemas did not declare that field, so the current fix keeps the
research output contract unchanged
([current web schemas:1232-1418](../../spec/tool-schemas-vtrade-kalshi-v1.json#L1232-L1418)).

### Agent-facing contract and composition changes

The functional input/output contract is almost unchanged: query/url, the same
result modes, defaults, numeric limits, and strict output branches remain. The
current active schema is Kalshi-specific and is the runtime source loaded by the
registry ([production_tools.py:924-960](../../src/vtrade/production_tools.py#L924-L960)).
Compared with the historical schema ([web_search:418-510](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/spec/tool-schemas-v1.json#L418-L510),
[fetch_webpage:513-581](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/spec/tool-schemas-v1.json#L513-L581)),
the current artifact:

- shortens both tool descriptions substantially, removing the old explicit
  publication-cutoff, source-quality, disconfirmation, and authoritative-rules
  guidance;
- removes the historical `classification` and `observed_name` metadata;
- removes schema-level descriptions for date fields, `highlight_query`, and
  `max_length`; and
- removes the old `format: date` annotations for the two published-date fields.

Runtime validation still performs the date and URL checks in `providers.py`, so
the last item is a schema-metadata difference rather than permission to send
invalid dates. Neither active schema exposes `include_domains` or
`exclude_domains`, even though the lower-level provider accepts those options.

The surrounding venue composition changed from Polymarket to Kalshi: the active
experiment/configuration and schema are `vtrade-kalshi-v1`, the registry is tied
to the Kalshi tool artifact and reviewed fixture gate, and the other tools now
read Kalshi opaque market references. None of that changes the Exa HTTP path.

### Persistence and quota differences

Research-result persistence is unchanged in substance: the current worker still
links successful results to `research_documents`/`research_artifacts`, stores the
provider raw-artifact URI and hash, and writes provider telemetry. The current
body is [worker.py:955-1132](../../src/vtrade/worker.py#L955-L1132) and matches the
historical body at [worker.py:952-1129](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/worker.py#L952-L1129).

The Exa policy is also effectively unchanged: Exa is selected, Tavily is disabled,
there is no fallback, each call reserves one request and ten worst-case credits,
and the monthly caps remain 18,000 requests/credits ([current config:54-72](../../config/experiments/vtrade-kalshi-v1.json#L54-L72);
[historical config:34-53](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/config/experiments/predictionarena-polymarket-v1.json#L34-L53)).
The persistence representation was reorganized for the Kalshi nine-migration
chain. In particular, the current `exa_quota_reservations` table has a unique
`request_key` ([0004:282-290](../../migrations/0004_runtime_audit_and_admin.sql#L282-L290))
and the guard populates it as `exa:<reservation-id>` ([harness_repository.py:251-263](../../src/vtrade/harness_repository.py#L251-L263));
the historical reservation insert had no such column
([historical harness_repository.py:214-264](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/src/vtrade/harness_repository.py#L214-L264)).
This is a database/audit difference, not a different search or fetch request.

### Regression coverage

The current provider tests still cover Exa request construction, normalization,
validation, and telemetry ([test_providers.py:328-707](../../tests/test_providers.py#L328-L707)).
However, the Polymarket suite had explicit registry-forwarding tests for both web
tools ([historical test_production_tools.py:270-333](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/tests/test_production_tools.py#L270-L333))
and explicit web-schema assertions ([historical test_spec.py:119-159](https://github.com/MINLEGO/v-trade-polymarket/blob/cb808f43bd305f4ebac65bc52ab7919ca9bdf346/tests/test_spec.py#L119-L159)).
The current production-tool tests now add registry-level nested-content
regressions for both tools ([test_production_tools.py:30-110](../../tests/test_production_tools.py#L30-L110)); the schema tests still cover general
27-tool parity and schema compilation rather than dedicated web fields. Neither
revision has a dedicated Exa timeout/retry test.
