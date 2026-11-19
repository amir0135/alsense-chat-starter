# Dashboard navigation latency: exact-scenario follow-up

> **Scenario:** “Open the alarms page and show only critical and high priority alarms.”

**No executable navigation/filter command exists in this starter.** The exact wording defaults to the `sites`
and `troubleshooting` domains. It never reaches a dashboard, priority-filter schema, action catalog or iframe
bridge. There is therefore **no finite number of current steps after which the UI can execute this action**:
every measured path emits zero commands. Time to a reply is measurable; time to a dashboard action is
**not present**, not zero milliseconds.

No production optimization was implemented in this follow-up. Forcing a decline, removing tools, skipping
guards or inventing a navigation command would change the existing behavior/evaluation boundary. Changes are
limited to the benchmark harness, an isolated headless-browser harness, regression coverage and this report.
The earlier report-catalog optimization does not apply to this request.

## What was measured

- **Real application paths, synthetic provider:** the existing FastAPI endpoints, session authentication,
  routing, Foundry runner/tool loop, tools, adapters, guard, persistence and SSE handling execute unchanged.
  The only provider is the explicitly injected deterministic Responses replay; **no Azure inference or model
  quality measurement is claimed**. The scripted path makes no model call at all.
- **Six paths:** scripted guidance; direct model decline; site lookup then evidence-based answer; guidance
  lookup then evidence-based answer; an unsupported “opened/filtered” claim repaired directly; the same rejected
  claim after a guidance tool. The replay chooses these outputs; this does not measure their live likelihood.
  No navigation tool, host schema, action bridge or test-only navigation prototype was created.
- **Two profiles:** zero artificial delay; fixed **40 ms per replay provider call / 15 ms per adapter call**.
  These waits are a sensitivity experiment, not estimates of Azure or Alsense latency. OS scheduling makes
  actual observed waits longer.
- **HTTP controls:** 20 repetitions per path per profile, one excluded warm-up, a fresh synthetic Alice session
  each time, sign-in outside timing, in-process ASGI. Before and after use **identical production source**:
  Git `src` tree `21a74f5ee730d1da89e7b6f8a8a5a91584047a3c`, `production_source_dirty=false`.
  Their variation is a no-change control, **not an optimization gain**.
- **Real frontend:** five repetitions per path/profile, one excluded warm-up, real `widget.js` in isolated,
  headless Chrome 157.0.8103.79 against loopback HTTP. This measures actual fetch/SSE/DOM behavior and a
  `requestAnimationFrame`-then-task paint opportunity, **not a real Alsense dashboard, iframe, physical display
  presentation or WAN**. No existing browser profile or credentials were used.

All samples, counts, request-argument byte sizes and backend per-round spans are retained. The HTTP samples
have span start offsets, duration and guard failure categories; the browser samples have User Timing entries,
resource timings and correlated backend records. No bearer tokens, prompts or raw tool outputs are saved.

## Ordered steps through each real path

Fresh-session routing is `default`; offered tools, in runtime order, are `get_permitted_sites`, `search_docs`,
`prepare_support_ticket`. The `alarms` and `frontend_actions` modules have `status: future`, with no tools.
The pinned [frontend-action evals](evals/synthetic/frontend_actions.yaml) allow site/guidance lookups, not
dashboard actions or support preparation for this intent. The exact combined wording is now also covered by
the [boundary regression tests](tests/test_dashboard_boundary.py).

For these sequences:

- **F:** widget sends one authenticated `POST /api/chat`.
- **B:** backend validates the session token, expiry, internal-user status and feature/fault context.
- **R:** local routing; Foundry also reads history and builds the scoped request. A fresh scripted request
  reads history once for previous domains; Foundry reads it twice (history + previous domains).
- **M1/M2/M3:** first/second/third complete, non-streaming provider calls.
- **T:** caller/allowed-tool/schema/feature/site validation, then the named handler and its adapter calls.
- **G:** final-text structural/evidence/interface-claim guard; **G!** is a rejected claim.
- **P:** answer ID and turn persistence.
- **S:** SSE reply received and rendered. Stream completion/audit follows; feedback waits for it.

| Path | Ordered macrosteps before/through a reply, not an action | Macrosteps | Model / tool / adapter calls | SSE frames |
|------|---------------------------------------------------------|------------|------------------------------|------------|
| Scripted | F → B → R → T(search_docs) → G → P → S | 7 | 0 / 1 / 1 | step started, step done, reply |
| Direct decline | F → B → R → M1(decline) → G → P → S | 7 | 1 / 0 / 0 | reply |
| Sites then answer | F → B → R → M1(select) → T(get_permitted_sites) → M2(answer) → G → P → S | 9 | 2 / 1 / 2 | step started, step done, reply |
| Guidance then answer | F → B → R → M1(select) → T(search_docs) → M2(answer) → G → P → S | 9 | 2 / 1 / 1 | step started, step done, reply |
| Direct claim repair | F → B → R → M1(claim) → G! → M2(repair) → G → P → S | 9 | 2 / 0 / 0 | reply |
| Claim repair after tool | F → B → R → M1(select) → T(search_docs) → M2(claim) → G! → M3(repair) → G → P → S | 11 | 3 / 1 / 1 | step started, step done, reply |

These are explicitly defined macrosteps, not a count of all internal function calls or packets. Each path
makes **one chat HTTP request**, plus asynchronous numeric client-timing telemetry after completion.
Every row has **zero UI commands, zero host calls and zero dashboard/filter executions**.

`search_docs` calls `knowledge.search` once. `get_permitted_sites({})` calls `sites.permitted_sites`, filters to
the session's authorized sites, resolves the result, then calls `sites.active_alarms` for Alice's one site.
The latter two adapter calls are sequential. The answer remains a denial of screen control even when it also
describes authorized sites or approved guidance. An unverified claim is never streamed or persisted.

Live Foundry can choose a different permitted sequence; the current loop permits up to 12 rounds and one guard
repair. Retryable model failures can add attempts/backoff. Those bounds remain unchanged; this benchmark
exercises the listed successful/repair paths, not retries, every possible live output or a history-heavy session.

## The requested seven timing stages

**Do not sum stages 2 and 3:** a model's tool selection is part of that same inference response. Similarly,
send-to-reply/render entries are offsets, not additional durations.

| Requested stage | What exists and what the measurement means |
|-----------------|--------------------------------------------|
| 1. Frontend → backend | Pure one-way transit is not separately identifiable here. Real-browser send → response-headers is **1.2–1.4 ms local / 1.3–2.4 ms delayed** across path medians, including loopback, authentication and response startup. Headers do not wait for inference. |
| 2. Model inference | Complete replay-provider round M1, measured below. Not actual inference, TTFT, provider queueing or token generation. Scripted mode: not present. |
| 3. Tool selection | For tool paths this is **inside M1**, not a second selection service. Local R also assembles tool schemas/instructions. M1 can instead return a direct decline. No dashboard tool is available to select. |
| 4. Backend/tool processing | Authentication, history/routing/request assembly, tool validation/adapter work, guard and persistence; measured separately below. No navigation-command validation/catalog lookup exists. |
| 5. Second model inference, if any | M2 generates the evidence-based answer or repair. The post-tool-claim path also needs M3 repair. No second call in scripted/direct-decline paths. |
| 6. Frontend receives action | **Not present** on all paths. Reply/progress receipt is measured separately; it must not be relabeled as action receipt. |
| 7. Dashboard navigation/filter execution | **Not present** on all paths. No route change, filter application, host validation/completion or dashboard data request occurs. |

### Provider and backend stage medians

Values below are **zero-delay / fixed-delay milliseconds**, from the after-control's 20 HTTP samples.
“Pre” sums session authentication, history and local routing/schema assembly. “Tool” includes its adapter
work and validation. Guards are summed if one fails and another succeeds. Spans include small instrumentation
overhead; independent medians do not add to a request median.

| Path | Pre | M1 | Tool | M2 | M3 | Guard(s) | Persist |
|------|-----|----|------|----|----|----------|---------|
| Scripted | 0.50 / 0.57 | — | 0.09 / 18.44 | — | — | 0.21 / 0.27 | 0.65 / 1.07 |
| Direct decline | 1.17 / 2.00 | 0.06 / 43.44 | — | — | — | 0.09 / 0.34 | 0.69 / 1.71 |
| Sites then answer | 1.14 / 2.90 | 0.06 / 44.82 | 0.12 / 35.46 | 0.06 / 44.90 | — | 0.12 / 0.90 | 0.67 / 3.76 |
| Guidance then answer | 1.11 / 2.70 | 0.05 / 45.14 | 0.09 / 18.95 | 0.06 / 44.47 | — | 0.12 / 0.76 | 0.64 / 3.03 |
| Direct claim repair | 1.11 / 2.67 | 0.05 / 44.68 | — | 0.05 / 44.47 | — | 0.15 / 1.33 | 0.66 / 2.32 |
| Claim repair after tool | 1.07 / 2.87 | 0.05 / 44.70 | 0.09 / 19.21 | 0.06 / 45.05 | 0.05 / 45.15 | 0.19 / 1.85 | 0.64 / 3.00 |

### Actual frontend receipt/render medians

Five **real Chrome** runs per path/profile; entries are **zero-delay / fixed-delay milliseconds**.
The first frame on a tool path is progress, not token output or an executable command. “Paint opportunity”
is send → reply's frame/task mark; “render interval” starts just before reply DOM work and ends at that mark,
so it includes refresh scheduling. Headless measurements are not production UX claims.
The widget's timing origin is immediately before `api("/api/chat")`, after its pending-message DOM is built;
the browser harness's separate send → stream-end clock starts just before calling `send()`.

| Path | Send → first frame | Send → reply | Send → paint opportunity | Reply render interval | Send → stream end |
|------|--------------------|--------------|--------------------------|-----------------------|-------------------|
| Scripted | 2.1 / 1.8 | 3.7 / 22.5 | 13.9 / 33.3 | 10.2 / 10.9 | 4.9 / 24.0 |
| Direct decline | 4.8 / 51.4 | 4.8 / 51.4 | 15.8 / 65.0 | 10.9 / 14.8 | 6.1 / 53.3 |
| Sites then answer | 2.6 / 49.4 | 4.2 / 133.4 | 9.1 / 146.1 | 4.7 / 14.2 | 5.8 / 135.5 |
| Guidance then answer | 2.9 / 48.5 | 4.7 / 114.9 | 15.9 / 121.8 | 11.5 / 6.4 | 5.9 / 116.7 |
| Direct claim repair | 4.5 / 96.1 | 4.5 / 96.1 | 9.5 / 108.3 | 5.8 / 12.8 | 6.6 / 98.5 |
| Claim repair after tool | 2.6 / 47.4 | 5.2 / 163.6 | 10.2 / 176.2 | 5.2 / 12.1 | 6.7 / 167.8 |

With five browser runs, nearest-rank p95 is only the maximum. Observed send → reply p95/max for the six paths
in table order was **3.9/23.0, 5.1/54.0, 4.8/137.8, 5.5/116.2, 5.4/102.8, 6.5/169.1 ms** (local/delayed).
There is no meaningful action-execution p95 because no action is executed.

## Answers to the six questions

**Are we making more than one model call?** Sometimes: zero scripted, one direct decline, two with a site or
guidance tool, two for direct repair and three for tool → rejected claim → repair. A tool-selection round and
its synthesis round are separate provider calls; selection itself is not a separate call from M1 inference.

**Is a second call necessary before the UI action can execute?** No such action exists today, so no current
round can make it executable. The second call is necessary **under today's selected tool path** to produce the
guarded natural-language reply, not to perform a backend lookup. For the documented future host flow, a second
LLM call is not inherently needed to *execute an already interpreted and independently validated command*.
It can narrate the actual result after host completion. That is a design projection, not permission to bypass
the current guard/loop or a claim that the required host protocol already exists.

**Are site/user details fetched unnecessarily?** Authentication reads the session's stored user/permission
snapshot on every request; it is mandatory, local SQLite, not a new identity API call. `users_me` runs at
sign-in, outside these timings. Direct-decline, guidance and their repair paths fetch **no site metadata**.
The site-choice path fetches directory + active alarms once each. Those are not needed simply to explain that
navigation is unsupported, but they are the existing permitted site-evidence path; a real site-bound command
would still need authorized-site validation. Alarm enrichment is coupled to the current site-tool contract,
so removing it would change returned evidence. No repeated identical metadata API calls occur in these paths.
Foundry's duplicate *local history* read remains; in zero-delay controls one history read is ~0.15–0.17 ms,
and route plus scoped schema assembly ~0.68–0.78 ms. Neither justifies a dashboard-latency production change.

**Are tool definitions/context unnecessarily large?** There are **three** scoped tool definitions, **1,901
serialized bytes**, plus **3,099 UTF-8 bytes** of core/domain instructions. The fresh question's serialized
input is 102 bytes. This is measured, not an estimate of tokens. Complete SDK request-argument sizes are:

| Model path | Request-argument bytes per round, in-process harness |
|------------|---------------------------------------------------|
| Direct decline | 5,406 |
| Sites then answer | 5,406 → 5,861 |
| Guidance then answer | 5,406 → 6,306 |
| Direct claim repair | 5,406 → 5,830 |
| Claim repair after tool | 5,406 → 6,306 → 6,730 |

The browser uses a 36-character correlation ID rather than the HTTP harness's 16-character ID, so those SDK
argument sizes are 20 bytes larger per round. Byte counts include Python SDK arguments/headers, **not exact
wire-body sizes or token counts**. Instructions and definitions repeat in stateless `store=False` rounds;
tool output/repair instructions enlarge subsequent input. The model client and loaded YAML are already reused.
This is not evidence of a large-context bottleneck; no tokenizer, prompt-cache or provider processing
measurement was made. The offered support-draft tool is not an allowed action for this intent in the pinned
eval, but removing it or narrowing routing would change that declared boundary. Long history (up to 20 turns)
and a real host capability catalog still need measurement.

**Is anything sequential that could run concurrently?** On the site path, the directory call precedes the
alarm call because returned/authorized sites determine its target. With more sites, alarms for *already
authorized* distinct sites could potentially be batched/concurrent; Alice has one site, and that would require
a thread-safe/batch adapter contract. No such fan-out is measurable here. Model selection → chosen tool →
answer/guard is dependent. A repair depends on a failed guard. There are no independent dashboard catalog,
navigation or filter calls to parallelize today. Frontend bootstrap already launches health/personas/scenarios
independently.

**Is the frontend waiting for generated text before executing an already validated command?** **Not today:
there is no command.** The widget handles `step` as progress only; it receives evidence/actions with the final
`reply` and renders it immediately. Only feedback waits for stream closure. Under the existing
`ctx.actions`/reply-envelope pattern, a future UI command would indeed be held until the follow-up answer,
its guard and persistence; copying that pattern into the host integration could add avoidable latency.
Emitting an existing `step` early is not a safe replacement: it contains a tool name/state, not validated
command arguments, host approval or completion.

## Intended host flow and projected saving — not an implementation

The existing [integration map](docs/integration-map.md#frontend-actions-future-integration) reserves the capability
manifest and host contracts for the frontend team:
**current-screen/user action catalog → host validation → host completion → assistant confirmation**.

An eventual implementation must retain token/user authorization, allowed-tool and schema checks, catalog/current
screen and filter validation, current site authorization where applicable, required explicit confirmations,
and host validation before an executable command is accepted/dispatched for execution. If host validation
requires a separate acknowledgment/preflight, that additional round trip must be specified and measured, not
assumed away. The model chooses only catalog-supported actions. The host must enforce/refuse permissions and
filters and report what actually happened; the assistant cannot claim success beforehand.

If built by simply appending an action to today's final envelope, the order would be:
**M1 interprets → backend tool validation/preparation → M2 writes text → text guard → persist → reply/action
release → frontend/host validation and execution → completion**. This creates a narration wait and risks
confirming too early unless text explicitly describes only preparation.

A separately specified, independently validated command release could instead let deterministic execution
proceed without waiting for narration; only **after host completion** would any model-produced success text
be generated and pass the existing text guard before release. A failure/refusal must remain explicit.
This requires a new trusted command/acknowledgment contract and command validator; **today's guard cannot
simply be bypassed or moved behind an unvalidated action**.

Measured after-control values support these **conditional projections only**:

- One otherwise-blocking follow-up provider round accounts for **~44.5–44.9 ms** in the fixed-delay tool paths;
  that is how much model wait might be moved off the *navigation critical path*, not eliminated from total
  assistant work. The configured wait is only 40 ms; observed values include scheduling/harness overhead.
  In the zero-delay replay the round is only ~0.06 ms, so it demonstrates no meaningful real inference saving.
- Tool-span end → persistence-span end is **51.00 ms for sites / 49.30 ms for guidance** delayed, versus
  **1.06 / 1.02 ms** local. These same-clock gaps include the follow-up round, guard, persistence and scheduling.
  They illustrate the current *post-lookup reply gate*, **not a validated UI command's latency or saving**.
- Post-tool bad-claim repair makes that gap **~100.24 ms** delayed. The extra provider round is ~45.15 ms,
  plus guard work. Better grounded future output could avoid repairs; removing the guard is not a fix.
- Host catalog acquisition, required host validation/confirmation/acknowledgment, dashboard data loading and
  paint costs are unknown and would offset or dominate these projections. No production percentage saving
  can be inferred from fixed sleeps.

## Before/after control: no production change, no claimed improvement

The “after” control follows the assessment and regression additions, **not a dashboard optimization**. Both
states use exactly the same `src` tree and the same benchmark code/settings. Twenty samples yield a coarse
nearest-rank p95 (19th ordered value), not a reliable production tail. Instrumented ASGI and Chrome runs were
separate; do not combine their individual medians into a single end-to-end decomposition.

| Path | Zero-delay median before → after | Zero-delay p95 before → after | Fixed-delay median before → after | Fixed-delay p95 before → after |
|------|---------------------------------|-------------------------------|----------------------------------|-------------------------------|
| Scripted | 3.56 → 3.43 ms | 4.13 → 3.89 ms | 23.90 → 22.84 ms | 26.70 → 24.13 ms |
| Direct decline | 5.36 → 4.62 ms | 8.04 → 5.25 ms | 54.29 → 53.59 ms | 56.24 → 58.17 ms |
| Sites then answer | 4.17 → 4.30 ms | 4.93 → 4.51 ms | 140.79 → 139.40 ms | 144.62 → 145.87 ms |
| Guidance then answer | 4.16 → 4.15 ms | 4.91 → 4.83 ms | 123.27 → 120.88 ms | 130.31 → 131.22 ms |
| Direct claim repair | 5.60 → 4.40 ms | 6.72 → 5.25 ms | 100.46 → 104.05 ms | 108.09 → 113.24 ms |
| Claim repair after tool | 6.19 → 4.78 ms | 10.47 → 5.04 ms | 166.21 → 172.86 ms | 173.26 → 176.94 ms |

Variation in both directions with identical source demonstrates why small millisecond deltas must not be
presented as improvements. These are response latencies, **not navigation latencies**.

## Reproduce and inspect the evidence

```bash
# Original general-purpose suite is unchanged by default; this selects only the exact dashboard scenario.
uv run --no-sync python scripts/benchmark_latency.py --suite dashboard --runs 20 \
  --output benchmarks/dashboard-before-local.json
uv run --no-sync python scripts/benchmark_latency.py --suite dashboard --runs 20 --model-ms 40 --api-ms 15 \
  --output benchmarks/dashboard-before-delayed.json
# Repeat with --output dashboard-after-*.json for the unchanged-source control.

# Real installed Chrome, isolated temporary profile, ephemeral loopback server; no Selenium/Playwright needed.
uv run --no-sync python scripts/benchmark_dashboard_browser.py --output benchmarks/dashboard-browser-local.json
uv run --no-sync python scripts/benchmark_dashboard_browser.py --model-ms 40 --api-ms 15 \
  --output benchmarks/dashboard-browser-delayed.json
# On another OS use --chrome /absolute/path/to/chrome.
```

The installed ChromeDriver was too old (126 versus browser 157); the browser harness uses Chrome's direct
DevTools protocol instead. Temporary profiles/server/processes are closed after each run. The harness never
changes production routing or introduces browser-facing test endpoints.

Evidence: [HTTP before local](benchmarks/dashboard-before-local.json),
[HTTP after local](benchmarks/dashboard-after-local.json),
[HTTP before delayed](benchmarks/dashboard-before-delayed.json),
[HTTP after delayed](benchmarks/dashboard-after-delayed.json),
[Chrome local](benchmarks/dashboard-browser-local.json),
[Chrome delayed](benchmarks/dashboard-browser-delayed.json).

Regression checks cover the exact wording, absent navigation tools/actions, unchanged permitted tools,
authentication before model access, invalid tool fields refused before adapter execution, per-path call counts,
and unreleased bad claims followed by a guarded repair. Authorization/site-isolation and confirmation tests
remain in the existing suite.
