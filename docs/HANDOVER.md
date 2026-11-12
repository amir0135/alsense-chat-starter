# Handover: running and extending the starter

A practical guide for whoever takes the demo over. Everything below runs on synthetic data and never contacts
Alsense, customers, email or support systems.

## What to run first

Pick one route. Docker is the fastest; it needs nothing but Docker Desktop (Compose 2.24 or newer).

```bash
git clone https://github.com/amir0135/alsense-chat-starter.git
cd alsense-chat-starter
cp .env.example .env            # optional for Docker, recommended for local runs
docker compose up --build       # then open http://127.0.0.1:8001
```

Without Docker you need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 itself):

```bash
uv run python scripts/dev.py setup    # pinned dependencies, .env, data folder, then the setup doctor
uv run python scripts/dev.py start    # http://127.0.0.1:8001
```

In VS Code, **Reopen in Container** does the setup for you; then run the **Start demo** task.

## What you should see

- An amber banner across the top: *Local reference demo. Uses synthetic data and does not connect to Alsense.*
- A left rail with quick actions, **Demo scenarios**, and a **Demo session** panel at the bottom.
- The welcome view: "How can I help with your operations?" with four suggestions.
- `http://127.0.0.1:8001/system` lists where every capability's data comes from and should say **Demo ready**.

If anything else appears, run the doctor (below).

## Selecting a user

Choose a synthetic user in the **Demo session** panel and press **Start demo session**:

| User | Sites | Useful for |
|---|---|---|
| Alice | Demo North | Gateway offline: guided check ends with connectivity guidance |
| Bob | Demo South | No open checks: guided check ends with a prefilled support request |
| Carol | Demo East Hall, Demo East Depot | Several sites: ambiguous names and the site picker |

**Switch user** returns to the panel. **New conversation** keeps the user and starts a clean conversation.

## Running a scripted demo

Open **Demo scenarios** in the rail (or "Run a demo scenario" on the welcome view). Both appear only with
synthetic data, when at least one scenario can run. Each scenario shows what it demonstrates, the starting user
and the expected result. **Start scenario** signs in as the right user with any synthetic faults it needs; the
coach above the composer then shows each step's talking point and a **Run step** button. **Reset scenario**
starts it again from a clean conversation. Scenarios live in `src/alsense_chat/scenarios.yaml`.

## Resetting the demo

| Situation | Command |
|---|---|
| Local run, clean sessions, drafts and audit trail | Stop the demo, then `uv run python scripts/dev.py reset` |
| Docker, keep the image, delete demo data | `docker compose down -v` |
| A clean demo on every start | `RESET_DEMO_DATA_ON_START=true` in `.env` |
| One scenario only | **Reset scenario** in the app |

## Switching modes

All settings are in `.env`; `.env.example` explains each one. Restart after changing it.

- **Scripted (default):** `AGENT_MODE=demo`. No AI model, no Azure, deterministic answers.
- **Azure AI Foundry:** `AGENT_MODE=foundry`, `FOUNDRY_PROJECT_ENDPOINT` and `MODEL_DEPLOYMENT`, plus `az login`
  (or a service principal in a container). Startup fails if a value is missing; it never falls back to scripted.
  Optional: `MODEL_REASONING_EFFORT` (default `high`) and `MODEL_MAX_OUTPUT_TOKENS` (default 8000).
- **Trim the demo:** `ENABLE_REPORTS`, `ENABLE_TROUBLESHOOTING`, `ENABLE_SUPPORT_REQUESTS`.
- **Rehearse failures:** `SYNTHETIC_FAULTS=api_timeout,report_failed` (see `.env.example` for the list).
- **Real data:** `DATA_SOURCE=alsense` is refused until the Alsense adapters are implemented.

## Where things live

| What | Where |
|---|---|
| Synthetic users, sites, alarms, documents, health checks, reports | `src/alsense_chat/adapters/synthetic/services.py` |
| Synthetic fault switches | `src/alsense_chat/adapters/synthetic/faults.py` |
| Example responses per integration call | `fixtures/` (regenerate: `uv run python scripts/dev.py fixtures`) |
| Troubleshooting flows (synthetic sample) | `src/alsense_chat/troubleshooting.yaml` |
| Customer flows | your own `flows.local.yaml`, loaded with `FLOWS_FILE` (never committed) |
| Demo scenarios | `src/alsense_chat/scenarios.yaml`, or `SCENARIOS_FILE` |
| Where real Alsense integrations belong | `src/alsense_chat/adapters/alsense/` and [`integration-map.md`](integration-map.md) |
| Model instructions and tool declarations, one folder per domain | `src/alsense_chat/agent/` ([guide](../src/alsense_chat/agent/README.md)) |
| Site resolution, report validation and the scripted report planner | `src/alsense_chat/domains/` |
| Evaluation cases and the harness that runs them | `evals/` ([format](../evals/README.md)), `src/alsense_chat/evaluation.py` |
| Evaluation cases written from customer examples | `evals/local/` (ignored by Git) |
| UI | `src/alsense_chat/static/` |

## Adding an environment variable

1. Add it to `ENVIRONMENT`, `Settings` and `load_settings()` in `src/alsense_chat/config.py`, with validation in
   `Settings.problems()` if a wrong value should stop startup.
2. Document it in `.env.example` under the right heading, with its marker (every mode, synthetic, azure, secret).
   `tests/test_config.py` fails until both lists match.
3. If Docker needs a container-specific value, set it under `environment:` in `compose.yaml`.
4. If it changes where data comes from, show it on `/system` (`src/alsense_chat/readiness.py`) without its value
   when it is secret.

## Running tests

```bash
uv run python scripts/dev.py test     # tests, contract fixtures and evaluations
uv run python scripts/dev.py lint     # formatting, lint, types, JavaScript, protected files
uv run python scripts/dev.py evals    # only the evaluation cases, with a pass/fail line per case
uv run python scripts/dev.py evals --mode model evals/local   # your own cases against the Foundry model
uv run python scripts/dev.py latency  # per-stage timings of the requests the local demo recorded
uv run python scripts/dev.py feedback # answer KPI and ratings with their context, never question or answer text
uv run python scripts/dev.py usage    # requests, errors, latency, tokens and estimated cost per user (no quota)
uv run python scripts/dev.py measure  # measured run of the sample flows, then the latency and usage reports
```

CI runs the same checks, plus the Docker smoke test, the Dev Container build and secret scans. `latency`,
`feedback` and `usage` read the demo database (`--db PATH`; `latency` and `feedback` take `--limit N`,
`feedback` `--rating up|down`, `usage` `--user ID` and `--price-input`, `--price-cached-input` and
`--price-output`, `feedback` and `usage` `--since DAYS`, and all three `--json`);
[`architecture.md`](architecture.md) describes what each one reports.

## Reading answer feedback

`uv run python scripts/dev.py feedback --since 30` starts with the initial KPI, **satisfied answers**: the answers
rated helpful divided by the answers given, for answers given in the last 30 days (all recorded answers without
`--since`).

- **Answers given:** chat replies and guided-check outcomes stored with an answer id that can be rated: answers,
  clarifications, refusals and error replies. Not the menu of checks, and not guided-check steps that still wait
  for an answer.
- **Rated helpful:** answers whose latest rating is Helpful. A rating counts with its answer, whenever it was given.
- **Unrated answers** count as not satisfied, so read the KPI with the **rated share** (rated ÷ answers given) and
  the **helpful share of rated answers**.
- **Not helpful** answers are split by the reason chosen: incorrect, incomplete, too slow, other or none.
- **Failed runs** ended without a stored answer, after an unexpected error. They are counted separately and are not
  part of the KPI.

Breakdowns by assistant (scripted, model, guided) and by routed domains follow, then the ratings themselves.
`--json` prints the same as one document. The review reads the audit trail and the ratings only, never the
conversation.

## Estimating model cost

The starter ships no prices, so the estimated cost shows as "not configured" until all three are set.

1. **Read the rates** from the Azure OpenAI pricing page, in US dollars per million tokens: input, cached input and
   output, for your deployment's model, deployment type and region. Data Zone, Global and Regional prices differ;
   the hosted demo deploys Data Zone Standard.
2. **Set them** as `MODEL_PRICE_INPUT`, `MODEL_PRICE_CACHED_INPUT` and `MODEL_PRICE_OUTPUT`, all three or none:
   - locally and with Docker Compose, in `.env`;
   - in the hosted demo, as deploy flags:
     `bash infra/deploy.sh --subscription ID --price-input INPUT --price-cached-input CACHED --price-output OUTPUT`
     ([azure-demo.md](azure-demo.md#model-prices));
   - on any other host, as the app's environment variables.

   Startup refuses a partial set, or a value that isn't a non-negative number.
3. **Check them** on `/system` (the Estimated cost row) or with `uv run python scripts/dev.py doctor`.
4. **Update them** when the price list or the deployment changes: edit `.env` and restart the app, or deploy again
   with the new flags. A deployment without the flags removes the prices.

`dev.py usage` then shows the estimated cost per user and in total. It re-estimates all recorded usage at the
current rates; `--price-input`, `--price-cached-input` and `--price-output` compare other rates without changing
the settings. Each chat request's log line keeps the estimate made at the time, as `estimated_cost_usd`.

> estimated cost = (input − cached input) × input price + cached input × cached input price + output × output price

Reasoning tokens are part of the output tokens and are not counted again. Calls that reported no usage are left
out and counted, so the estimate is a lower bound when there are any. The bill can differ: it depends on the
agreement, discounts and how the service rounds, so treat this as a guide, not an invoice.

## Measuring model latency

`uv run python scripts/dev.py measure --runs 5` signs in a synthetic user and, in each run, asks "Which sites can
I access?", asks for a temperature report for Demo North for the last seven days, confirms the draft and reads its
status. It then prints the `latency` and `usage` reports for that run (a fresh temporary database unless you pass
`--db PATH`). It uses the assistant set by `AGENT_MODE` in `.env`: scripted by default, which only measures the
backend. With `AGENT_MODE=foundry`, `FOUNDRY_PROJECT_ENDPOINT`, `MODEL_DEPLOYMENT` and an Azure sign-in
(`az login`), every question makes real, billed calls to that deployment. Data is always synthetic;
`DATA_SOURCE=alsense` is refused.

How to read the model stages in the `latency` report (milliseconds; `runs` is the average number per request):

| Stage | What it measures |
|---|---|
| `model_tool_selection` | Model calls that answered with tool calls: the model choosing lookups |
| `model_answer` | The model call that wrote the final reply (a second one means the reply checks asked for a repair) |
| `model_failed` | Model calls that failed: an error status, a timeout or a lost connection, until the failure |
| `model_throttled` | Waiting after the service answered 429 (too many requests or tokens), before trying again |
| `model_retry` | Waiting after any other retryable failure (408, 409, 5xx, timeout, connection) |
| `tools`, `guard`, `persist` | Backend lookups, reply checks and storing the turn |
| `total` | From the request's arrival to the end of its response |

Failed calls are retried at most twice, as the OpenAI SDK would: the wait follows the service's `Retry-After`
(up to two minutes) or backs off from half a second. Each retry is logged as a warning with the correlation id,
status code and wait, so it also shows in Application Insights. A failure after the last retry is reported to the
user as "the model is unavailable". The `usage` report adds the tokens per user, including reasoning tokens.

Then compare with the model deployment's own metrics in the Azure portal: requests split by status code (any
429s), time to response, and tokens per minute against the deployment's tokens-per-minute limit. Each call counts
its prompt plus its maximum output tokens against that limit when it starts, so a few calls in a row can exceed a
small deployment's limit. Many `model_throttled` waits mean the deployment's capacity, not the model, sets the pace.

Reasoning effort, the output limit and capacity interact. A higher `MODEL_REASONING_EFFORT` means more reasoning
tokens: longer `model_tool_selection` and `model_answer` stages, and more output tokens in the `usage` report.
Reasoning tokens count toward `MODEL_MAX_OUTPUT_TOKENS`, and a call that reaches the limit ends incomplete: the run
fails, and a warning names the reason (`max_output_tokens`) with the call's output and reasoning tokens. Raise the
limit or lower the effort. A higher limit makes every call reserve more of the deployment's tokens per minute, so
fewer calls fit in a minute and `model_throttled` waits start sooner; raise the capacity to match
([`azure-demo.md`](azure-demo.md#model-and-capacity) has the sizing rule).

## Diagnosing startup problems

Start with the doctor; it names each problem and the fix:

```bash
uv run python scripts/doctor.py
docker compose exec app python scripts/doctor.py     # inside the running container
```

| Symptom | Likely cause and fix |
|---|---|
| `Port 8001 is used by another program` | Another demo or app is running. Stop it or set `APP_PORT` in `.env`. |
| `Foundry mode requires FOUNDRY_PROJECT_ENDPOINT and MODEL_DEPLOYMENT` | Set both, or `AGENT_MODE=demo`. |
| `flows.local.yaml has N problem(s)` | Run `uv run python scripts/dev.py validate-flows flows.local.yaml` and fix each line. |
| `schema vN, newer than this app supports` | The database came from a newer version: `uv run python scripts/dev.py reset`. |
| Dependency downloads fail on a corporate network | Set `UV_DEFAULT_INDEX` to your package mirror, then `uv run --no-sync python scripts/dev.py setup` (or `docker compose build` with the variable set). Versions and hashes stay pinned. |
| The app shows "Service unavailable" | The backend isn't running or crashed: check the terminal or `docker compose logs`. |
| Chat says the session expired | Sessions last `SESSION_TTL_SECONDS`; start a new session. |
| Every model question says the assistant is unavailable; the log shows `Model call failed (BadRequestError)` | The deployment refused a request setting. Use a reasoning model, or a `MODEL_REASONING_EFFORT` value it supports. |
| Model answers fail; the log shows `not completed (incomplete, max_output_tokens)` | Reasoning used up the output limit. Raise `MODEL_MAX_OUTPUT_TOKENS` or lower `MODEL_REASONING_EFFORT`, then check the capacity. |

## Files that must never be committed

Git, Docker and the export already ignore these; `scripts/check_protected_files.py` and CI fail if one is tracked.

- `.env` and any `.env.*` other than `.env.example`
- customer material: `*.local.yaml` (flows, scenarios), PDFs, Office documents, mail exports
- `compose.override.yaml`
- demo data: `data/`, `*.sqlite3`
- Azure deployment state: `.azure/` (contains the generated demo access code)

Before sharing a branch, run `python3 scripts/check_protected_files.py --history`. A `.gitignore` rule does not
remove a file that was committed earlier; if one ever was, rewrite the history or share a fresh export
(`uv run python scripts/dev.py export`) instead of the branch.
