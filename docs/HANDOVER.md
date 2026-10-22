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

Open **Demo scenarios** in the rail (or "Run a demo scenario" on the welcome view). Each scenario shows what it
demonstrates, the starting user and the expected result. **Start scenario** signs in as the right user with any
synthetic faults it needs; the coach above the composer then shows each step's talking point and a **Run step**
button. **Reset scenario** starts it again from a clean conversation. Scenarios live in
`src/alsense_chat/scenarios.yaml`.

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
```

CI runs the same checks, plus the Docker smoke test, the Dev Container build and secret scans.

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
