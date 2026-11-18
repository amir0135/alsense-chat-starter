# Latency investigation

## Result and limits

The measured, low-risk optimization is **reuse of report-catalog metadata within draft preparation**. Preparation
now makes one catalog read instead of two. Confirmation always fetches fresh metadata, including a second read
inside the submission transaction after any lock wait. Both confirmation validations, permission checks,
explicit confirmation, idempotency, link checks, reply guards and categorized failures remain in place.

**These are not Azure/OpenAI latency measurements.** No Foundry endpoint or model deployment was configured in
this workspace. The benchmark runs the real FastAPI endpoints, Foundry runner, tool loop, guards, SQLite store
and existing synthetic adapters with an explicitly substituted, deterministic Responses provider. It does not
test natural-language interpretation quality, token generation, Azure authentication, remote networking,
browser rendering or iframe/dashboard integration.

The starter **cannot open or filter dashboards**. Its `alarms` and `frontend_actions` agent modules are future
placeholders, not executable tools. Adding dashboard actions, or claiming they succeeded, would change business
behavior and was not done.

## Reproduce the measurements

```bash
uv sync --frozen
# Local overhead: no artificial waits.
uv run --no-sync python scripts/benchmark_latency.py --runs 20 --output benchmarks/latency-after-local.json
# Network-sensitivity experiment ONLY: fixed sleeps, not measured provider/API latency.
uv run --no-sync python scripts/benchmark_latency.py --runs 20 --model-ms 40 --api-ms 15 \
  --output benchmarks/latency-after-delayed.json
```

Recorded results: 20 measured repetitions **of each scenario** (the CLI defaults to five), one excluded warm-up
per scenario, a fresh Alice
session per repetition, one temporary SQLite database and reused application/provider clients. Sign-in is
outside the measured interval; the lifecycle includes prepare, explicit confirmation and one completed-status
read. The synthetic report service completes immediately. There is no polling sleep in this HTTP benchmark.
All scenarios run in the listed order. Fixed sleeps may overshoot because of OS scheduling.

Initial five-run baselines were captured before production changes at commit
`f9597e209369d1761ce9e7d08d5bcc9c5ee18268`, with only the new benchmark harness added. To reduce sample noise,
the final committed results expand both states to 20 runs: the before runs import that commit's immutable
`src` archive; the after runs import the changed source. Inputs, provider, delays and warm-up policy are identical.
Medians use the arithmetic mean of the middle two samples for even-sized runs; p95 uses nearest rank.
Both artifacts' `base_commit` identifies the common parent, **not** a distinct after revision; filenames identify
the measured states. Reproduce the baseline without modifying the current checkout:

```bash
mkdir -p .venv/latency-baseline
git archive f9597e209369d1761ce9e7d08d5bcc9c5ee18268 src | tar -x -C .venv/latency-baseline
PYTHONPATH="$PWD/.venv/latency-baseline/src" uv run --no-sync python scripts/benchmark_latency.py \
  --runs 20 --output benchmarks/latency-before-local.json
PYTHONPATH="$PWD/.venv/latency-baseline/src" uv run --no-sync python scripts/benchmark_latency.py \
  --runs 20 --model-ms 40 --api-ms 15 --output benchmarks/latency-before-delayed.json
```

Recorded on Darwin arm64, Python 3.12.10. The JSON files retain every sample, model/tool/adapter counts,
per-operation adapter counts, stage timings, correlation IDs and serialized SDK request-argument byte counts.
Those byte counts are not token counts or exact HTTP body sizes. No session tokens, credentials or prompts
are saved in the result files. Failed or unexpected scenarios fail the benchmark rather than producing a
success-shaped measurement.

### Local overhead, no artificial delays

Before/after are medians, milliseconds. **Small-sample differences are not established speedups**: most of this
starter's in-memory work takes microseconds, and even unchanged paths vary with scheduling and warm caches.

| Scenario | Before | After | Improvement | Main bottleneck |
|----------|--------|-------|-------------|-----------------|
| Simple question | 3.70 ms | 4.15 ms | None: +0.45 ms overhead/variation | HTTP/thread scheduling, SQLite |
| Site resolution | 3.65 ms | 3.87 ms | None: +0.22 ms overhead/variation | HTTP/thread scheduling, SQLite |
| Dashboard/navigation request, unsupported | 3.93 ms | 4.39 ms | None: +0.46 ms overhead/variation | Persistence/scheduling; no dashboard work |
| Report preparation | 5.54 ms | 5.65 ms | None: +0.11 ms; one fewer in-memory call | Tool validation/draft persistence |
| Report lifecycle: prepare, confirm, status | 9.23 ms | 10.07 ms | None: +0.84 ms; one fewer in-memory call | Three HTTP requests and SQLite |

Instrumentation adds small local overhead. Removing an effectively free in-memory call is not a speedup for
the default synthetic service; the benefit is eliminating a network boundary when a real adapter is connected.

### Fixed-delay sensitivity experiment

Each replay model call waits **40 ms**, each synthetic adapter call **15 ms**. These numbers were chosen to
expose sequential call costs; they are not estimates of real Alsense or Foundry services. The key reliable
finding is the reduction in adapter calls; 20 synthetic samples still do not establish production latency.

| Scenario | Before | After | Improvement | Main bottleneck |
|----------|--------|-------|-------------|-----------------|
| Simple question | 123.94 ms | 117.14 ms | 5.5% observed; noise, no changed calls | Two sequential replay model rounds |
| Site resolution | 134.17 ms | 135.27 ms | -0.8%; noise, no changed calls | Two replay rounds plus two adapter calls |
| Dashboard/navigation request, unsupported | 51.77 ms | 50.80 ms | 1.9% observed; noise, no changed calls | One replay response, not dashboard execution |
| Report preparation | 225.44 ms | 194.29 ms | 31.16 ms / 13.8% observed | Three replay rounds plus three adapter calls |
| Report lifecycle: prepare, confirm, status | 307.17 ms | 275.02 ms | 32.15 ms / 10.5% observed | Three replay rounds plus seven adapter calls |

For the delayed lifecycle, accumulated model durations stayed approximately 130 ms; tool durations fell from
approximately 149 to 129 ms. Removing one adapter wait removes **15 ms of configured wait budget**. Observed
median differences are larger because other scheduling/transport variation remains: do not attribute the
entire 32 ms to the cache, or extrapolate these percentages to production.

### Tail observations

With 20 samples, nearest-rank p95 is the 19th ordered observation, a coarse indication only. At the default five
runs it is just the maximum. Collect substantially more real observations before setting a production SLO.

| Scenario | Local p95, before -> after | Delayed p95, before -> after |
|----------|----------------------------|------------------------------|
| Simple question | 4.35 -> 4.54 ms | 130.85 -> 124.31 ms |
| Site resolution | 3.73 -> 4.23 ms | 142.65 -> 142.10 ms |
| Dashboard/navigation request, unsupported | 4.01 -> 5.39 ms | 53.77 -> 53.29 ms |
| Report preparation | 5.94 -> 6.78 ms | 231.35 -> 217.16 ms |
| Report lifecycle | 9.57 -> 11.56 ms | 317.08 -> 291.33 ms |

### Calls per measured repetition

Counts are identical across both profiles and all 20 repetitions. Adapter calls are in-process interface
invocations here, **not actual HTTP calls**. They mark where real integration calls would occur.

| Scenario | Model rounds, before -> after | Tool/action calls, before -> after | Adapter calls, before -> after |
|----------|-------------------------------|-----------------------------------|-------------------------------|
| Simple question | 2 -> 2 | 1 -> 1 | 1 -> 1 |
| Site resolution | 2 -> 2 | 1 -> 1 | 2 -> 2 |
| Dashboard/navigation request, unsupported | 1 -> 1 | 0 -> 0 | 0 -> 0 |
| Report preparation | 3 -> 3 | 3 -> 3 | 4 -> 3 |
| Report lifecycle | 3 -> 3 | 5 -> 5 | 8 -> 7 |

Artifacts: [before local](benchmarks/latency-before-local.json), [after local](benchmarks/latency-after-local.json),
[before delayed](benchmarks/latency-before-delayed.json), [after delayed](benchmarks/latency-after-delayed.json).

## Complete request path and sequential boundaries

1. **Bootstrap/sign-in, separate from a turn.** `widget.js` starts `/api/health`, `/api/demo/personas` and
   `/api/scenarios` independently, not serially. Starting a session posts `/api/demo/session`; the backend calls
   `identity.users_me`, saves the session/user snapshot to SQLite and returns the demo token. Identity/account
   metadata is not fetched from an upstream service on every message. The token stays in the widget; the
   model never receives it.
2. **User sends a message.** `send()` creates a correlation ID, renders the local pending state, then posts
   `/api/chat` with the bearer token and `X-Correlation-ID`. Middleware validates/echoes that ID; the synchronous
   dependency authenticates against SQLite and applies internal-user, feature and session fault checks.
3. **Backend routing/history.** Slash commands always use `demo_run`, even in Foundry mode. Otherwise the
   Foundry runner reads up to 20 history turns, routes locally using the modular agent, builds only the selected
   domains' instructions/tool schemas and appends the current question. Routing also reads history for previous
   domains: a duplicate local SQLite read, not another network call. The default demo runner uses scripted
   interpretation and makes **zero** model calls.
4. **Model round 1, if Foundry is enabled.** `FoundryRunner.create()` synchronously calls
   `client.responses.create(store=False)` with configured deployment, reasoning effort and output limit.
   Authentication/token acquisition, connection establishment, service queueing and inference are within this
   boundary when live. The SDK/client/credential are created once at application startup and closed at shutdown,
   not recreated per turn. The client sends `X-Client-Request-Id` for correlation with provider diagnostics.
5. **Tool selection and execution.** A completed model response may contain multiple proposed calls. The backend
   executes them **sequentially in response order**, checking caller/allowed tools, schema, feature flags, the
   session's authorized sites and current-turn verified-site requirements. It streams progress `step` frames;
   those are not model tokens and not permission to execute a frontend action. Each handler then makes its
   adapter calls sequentially (detailed below). All synthetic adapter calls are local functions.
6. **Follow-up model rounds.** Function-call outputs and encrypted reasoning are appended to the request.
   Another blocking Responses call selects more tools or composes an answer. For the report replay, round 1
   selects site/catalog tools, round 2 selects preparation, round 3 answers. Definitions/instructions are sent
   on each round because the service stores no conversation. A structural/evidence guard can request **one**
   repair answer; the overall tool loop is bounded at 12 rounds. No round or guard was removed.
7. **Retry boundaries.** Each model round can have up to two retries on the existing retryable conditions.
   Retry-After/backoff sleeps block the worker, separately measured as `model_throttled` or `model_retry`;
   failed attempts are `model_failed`. These can dominate live latency; the benchmark does not inject retries.
8. **Response/persistence/render.** Only a guarded answer is saved, given an answer ID and streamed as a `reply`.
   The widget renders that full reply as soon as the frame arrives; it does not wait for stream closure to
   render. It waits for closure only before enabling feedback/reporting its timings, so the audit exists.
   There is no streaming of model text or tokens. The final request audit write is another local SQLite boundary.
9. **Deterministic actions.** Preparing a report creates a draft, not a submission. Explicit Confirm posts
   `/api/actions/{id}/submit`: authenticate, load the session-owned action, validate site/catalog/payload,
   acquire the SQLite submission transaction, fetch the catalog afresh and validate again in `submit_action`,
   call the report service with
   the idempotency key and persist the result. No LLM is involved. A repeat submit still revalidates, then returns
   the existing result without resubmitting.
10. **Status/download.** After report confirmation, the widget immediately gets `/api/actions/{id}/status`.
    Backend authentication, session ownership/site checks and report-job lookup precede approved-link checks.
    No LLM is involved. Processing jobs are polled at 1 s, growing by 1.5 to at most 5 s, with a two-minute
    active polling budget, one loop per card, page-visibility waits and session/card cancellation. Errors stop
    polling and remain visible; only a completed service result supplies a download link/expiry. A browser
    suspended while hidden may resume after the nominal budget; this was not changed. Manual `/status` is
    also deterministic. Opening a report builder may separately fetch `/api/report-types`; the widget already
    coalesces and caches that catalog per session.

Adapter sequences:

| Operation | Sequential adapter calls |
|-----------|--------------------------|
| `search_docs` | `knowledge.search` |
| `get_permitted_sites` | `sites.permitted_sites` -> authorized filtering/name resolution -> `sites.active_alarms` for each returned site |
| `check_site_health` | Permission check -> `health.site_health` -> site/freshness checks |
| Scripted report preparation | Site tool (directory + alarms) -> `reports.report_types` -> prepare/validate using the same request's catalog |
| Confirm report/schedule | Fresh `reports.report_types` -> acquire transaction -> fresh `reports.report_types` -> `reports.submit_report` or `reports.schedule_report` |
| Prepare/confirm support | Site checks -> `sites.active_alarms` validation; confirmation validates again -> `support.submit_request` |
| Report status | Action ownership/site checks -> `reports.report_status`; an unconfirmed draft or schedule needs no job API call |

### Exact dashboard example

For **"Open the alarms dashboard and filter to critical and high alarms"**, a fresh scripted session runs:
`POST /api/chat` -> authentication/local routing -> `search_docs(query=message)` -> `knowledge.search` ->
guarded guidance reply -> SSE -> render evidence/text. It opens no dashboard and applies no filter.

The reproducible model-mode replay runs:
`POST /api/chat` -> authentication/history/routing -> **one replay model response** (`out_of_scope`) ->
guard/persist -> SSE -> render. **Zero tools, zero adapter calls, zero dashboard/frontend actions.**
The live model's exact choice is not knowable without running it; it may choose permitted guidance or site tools,
adding model/tool rounds. An invented `open_alarms_dashboard` call is refused as `unknown_tool`, and a claim
that a dashboard was opened/filtered is refused by the guard (and may cost the existing repair round).

Natural-language interpretation in Foundry mode belongs to the LLM. Authorized-site filtering/name matching,
input validation, draft preparation, confirmation, submission, polling and rendering are deterministic.
In the real iframe implementation, applying an already interpreted and validated navigation/filter command
would be deterministic too, but this repository has **no bridge/acknowledgment contract to measure or optimize**.

## Instrumentation and safe changes

| Area | Measurements and correlation |
|------|------------------------------|
| Whole HTTP request | New structured `http_timing` log: `correlation_id`, status and `http_total_ms`, including stream sending and final audit write, including rejected requests |
| Existing request stages | `session`, `history`, `route`, `guard`, `persist`, tool durations/counts, reply/progress offsets and audit `total` retained |
| Individual integration calls | New `alsense.api` spans and structured `latency_stage` logs with fixed operation names and durations; `api.<operation>` / `api_calls` aggregate milliseconds and counts in audited requests |
| Model | Existing selection/answer/failure/retry durations retained; new `first_model_response` offset, `model_followup` nested duration/count, numeric round/tool counts on spans, correlation header sent to provider |
| Widget chat | Existing headers/first-event/reply/paint offsets posted to `/api/timings` in the same authenticated session; browser User Timing entries now carry the correlation ID; `chat_render` measures reply DOM update through next paint opportunity |
| Actions/reports | New browser User Timing entries `action_submit`, `report_status`, `report_render` (synchronous DOM work), `report_poll` (whole loop, including waits), with correlation ID, poll count and fixed outcome categories |

The chat's correlation ID follows its newly rendered action cards through confirmation and automatic status
requests. A card restored without an active chat trace gets a new ID. Status polls deliberately share the
interaction ID; it is a correlation ID, not a globally unique identifier for each poll. Manual status checks
start a new chat interaction. `/api/timings` still rejects ambiguous reused **chat** IDs and cross-session reports.
No authorization/cache key depends on a correlation ID.

`first_model_response` is **time to a complete non-streaming provider response**, not TTFT. True first-token
latency is unavailable until model streaming is deliberately introduced. `first_event` may be a progress step;
`headers` measures neither first model output nor rendered content. `api.*` is nested inside tools, and
`model_followup` inside model categories: do not sum overlapping durations.

Action/poll durations are local User Timing records, not newly uploaded customer telemetry. Inspect them in
browser developer tools or with `performance.getEntriesByType("measure")`; their `detail.correlation_id` joins
backend logs/spans. The HTTP benchmark does not execute widget code. The Node regression executes the actual
widget functions with a fake clock/transport, checking correlation, two-poll completion, errors/cancellation and
numeric chat telemetry. A browser-canvas verification was attempted, but its webview was unavailable/timed out;
**no real browser/paint results are claimed**.

Report catalogs alone are reused during preparation, only after a successful read and only on a request-local
`Context`. Confirmation explicitly invalidates the snapshot before transactional revalidation. There is
no process-wide, session-wide, cross-user or authorization cache. Empty catalogs remain authoritative and
validation still runs. Failures are not cached. Regression coverage changes catalog availability both between
prepare/confirmation and between outer/transactional confirmation checks, checks other users, failed loads and
idempotent resubmission.

New logs/spans contain IDs, allowlisted stage/operation/result categories and numbers, never tool arguments,
tool results, message/prompt text, URLs, credentials or bearer tokens. Unexpected chat exceptions now log only
their class and correlation ID, not their potentially sensitive exception text/traceback; the explicit failure
reply and runtime-error audit remain. This is diagnostic redaction, not swallowed failure.

## Considered but not changed

| Candidate | Decision |
|-----------|----------|
| Parallel tools from one model response | Not safe generically: site verification must precede site-bound tools, and context evidence/actions/store are mutable and ordered. No generic concurrent tool executor was added. |
| Parallel directory/catalog lookups or per-site alarms | Independent reads are potential savings, but the service protocols promise no thread safety; synthetic calls are already negligible. Measure real adapter fan-out and use a supported batch/concurrent client contract first. |
| Remove alarms from site resolution | Directory -> one alarm call per resolved site is an N+1 shape. Alarms are existing evidence/UI behavior; omitting them would change the contract. |
| Cross-turn site/account caching | Rejected: stale permissions or fault state could leak authorization changes. Identity is already session-scoped; site tools still query/filter authorized sites each turn. |
| Trim tool/context payload or cache definitions | Tools are already domain/feature scoped; YAML is cached. Removing dependencies, definitions on stateless follow-up rounds, history or encrypted reasoning risks correctness. Real token/round measurements are needed. |
| Remove final/repair model round or lower reasoning | Would change natural-language synthesis/guard behavior or model quality; not justified by replay. Keep the LLM where interpretation is needed. Default reasoning remains `high`, output limit 8000. |
| Share metadata across outer/transactional confirmation validation | Rejected: the transaction can wait for a lock. Both checks and their fresh catalog reads remain. |
| Shorter/more aggressive polling | Existing immediate first check, backoff, visibility and stop/error behavior are appropriate. Changing them has no measured benefit for immediately completing synthetic reports. |
| Duplicate history read, schema generation, YAML/SQLite tuning | Duplicate history is local and sub-millisecond in these samples. No evidence of material impact versus network/model latency; left unchanged. |
| Async rewrite, worker/thread changes, client pooling | SDK clients already live for the application lifespan. Sync FastAPI handlers/stream iterators run via worker threads, not blocking calls in an async route. Load/thread-pool exhaustion and SQLite write locks still need concurrency testing. |
| Early deterministic dashboard action | No implementation or trusted command/ack bridge exists. Do not execute anything from unguarded model output, speculative tool progress or free text. |

## Largest contributors and highest-value next fixes

**Three contributors supported by this investigation:**

1. Sequential model selection/synthesis rounds: three for report preparation in the replay, approximately
   130 ms of accumulated model-stage time in the fixed-delay lifecycle. This is a structural finding, not a
   measurement of Azure. Reasoning, queueing, throttling and repairs can add substantially more live time.
2. Sequential adapter round trips: eight before/seven after for the lifecycle, including catalog validation and
   the directory/alarms fan-out. The delayed lifecycle's tool stages fell about 20 ms. More authorized sites
   would add alarm calls; no many-site scalability result is claimed.
3. Local HTTP/thread scheduling, persistence and audit work: these dominate the zero-delay replay (roughly
   4-9 ms medians before changes). Their real impact under load is unmeasured; the iframe's navigation,
   network and report-generation delays cannot be ranked from this starter.

**Three highest-value fixes, with implementation status:**

1. **Implemented:** remove the duplicate report-catalog read during draft preparation, without caching
   permission decisions or crossing confirmation/transaction boundaries.
2. **Implemented diagnostic prerequisite:** correlated HTTP/model/adapter/widget/poll timings, separating
   first complete model response, follow-up rounds, retries and actual render/poll work instead of guessing
   that every delay is the LLM.
3. **Conditional on real measurements:** batch independent authorized-site/alarm metadata reads using a
   concurrency-safe real adapter; retain verification/order for dependent tools. For actual dashboard latency,
   separately measure the iframe bridge's validated command dispatch -> host acknowledgment -> dashboard data
   load -> filter application -> paint before designing an early-action protocol.

### Real Alsense / Azure follow-up

- Capture enough interactions per scenario, tenant/site-count bucket, cold/warm connection and history length
  for meaningful p50/p95 distributions; include concurrent users, timeouts, 429s and confirmation failures.
- Correlate actual provider request IDs/diagnostics with `X-Client-Request-Id`; distinguish token acquisition,
  DNS/TLS, provider queue/inference, reasoning tokens, tool-selection rounds and retry waits. Compare models or
  reasoning settings only with the existing safety/behavior evaluations and live quality checks.
- Measure tool/context bytes and actual input/cached/reasoning/output tokens by round. The existing
  `scripts/dev.py measure`, `latency` and `usage` commands support configured **live, billed** Foundry runs;
  the offline benchmark never does. Avoid sending customer material to a measurement provider.
- Instrument real adapters internally for their own sequential HTTP calls and propagate the correlation ID
  through approved request-scoped headers. The interface-level spans here cannot expose hidden HTTP calls in
  an adapter that is not implemented.
- Capture report service queue/generation time separately from status-call RTT and browser polling wait,
  including background-tab resume. Do not equate an accepted submission with a completed report.
- Run real browser/iframe traces for postMessage handshakes, origin validation, host authorization, navigation,
  dashboard fetching/filtering and paint. Preserve confirmation requirements and host acknowledgment/error
  semantics. Investigate proxy/SSE buffering separately from model output time.

Validation: 473 tests passed, including authorization/site isolation, confirmation/idempotency, report failures,
dashboard boundaries, telemetry privacy, the benchmark and widget timing regressions. Ruff, mypy, JavaScript
syntax and protected-file checks passed. No live Azure/Alsense or real browser performance validation was possible.
