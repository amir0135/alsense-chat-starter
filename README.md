# Alsense-style chatbot starter

**Reference implementation with synthetic APIs, not a working Alsense integration.** To connect it, the owning
teams must provide API contracts, token audiences/scopes, `users/me` and site permissions, report flow, ticket
endpoint, and Search index access. No real customer data, credentials, or confidential workshop files are included
in the standalone export.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/amir0135/alsense-chat-starter.git
cd alsense-chat-starter
cp .env.example .env
uv run --env-file .env uvicorn alsense_chat.app:create_app --factory --port 8001
```

Open `http://127.0.0.1:8001`. Select a synthetic user and start a session. Alice can see Demo North; Bob can see
Demo South. Nothing is sent to Alsense, email, or any customer API.

The default `AGENT_MODE=demo` is a deterministic harness, **not an AI model**. It exercises the actual backend
tool handlers without Azure. Use the buttons, ask about an offline sensor, or use these commands:

```text
/sites *
/reports
/docs offline sensor
/report {"site_id":"site-a","report_type":"alarm-history","format":"pdf","start":"2026-01-01","end":"2026-01-31"}
/ticket {"site_id":"site-a","alarm_id":"alarm-site-a","summary":"Offline sensor","steps_tried":"Checked power"}
/status <prepared-report-action-id>
```

The widget presents prepared report/ticket payloads for review. Only its separate Submit button calls the
submission endpoint. Results and links are clearly synthetic. To correct a draft, prepare a replacement; the
submission endpoint does not accept model- or client-supplied edits to an existing draft.

## What's implemented

| Component | Behavior |
|---|---|
| Curated tools | `search_docs`, `list_report_types`, `resolve_sites`, `prepare_report`, `get_report_status`, `prepare_support_ticket` |
| API boundary | `AlsenseSource` protocol and `MockAlsenseSource` with entirely synthetic users, sites, docs, alarms and submissions |
| Local auth | Expiring opaque demo sessions; token hashes persisted, raw tokens never given to the runner |
| Permission checks | Internal-user backend gate; tenant and allowed-site checks on draft preparation, status and submission |
| Session persistence | SQLite stores user context, turns, drafts, submission results and unanswered categories across restarts |
| Widget | Same-origin, iframe-embeddable page; text, sourced evidence, approved deep links, forms and explicit submission |
| Run loop | Backend-owned tool execution; optional Foundry Responses client; no hosted MCP or direct model API access |
| Reply checks | Strict reply shape, known evidence references, numeric grounding, approved structured links, draft wording |
| Fallback backlog | Categories for unsupported questions, failed inputs, prepared support drafts and runtime failures |
| Tests | Cross-user/site isolation, expiry, persistence, confirmation, duplicate submission, schemas and model tool flow |

These reply checks **do not prove that every sentence is factually entailed by a source**. Source-id and numeric
checks are only part of grounding. Add the product's answer-quality evaluations before trusting model answers.
The support/report forms do not calculate numbers or render charts in phase 1.

## Optional Foundry model

For a hosted demo in your own subscription, follow [`docs/azure-demo.md`](docs/azure-demo.md):

```bash
az login
bash infra/deploy.sh --subscription YOUR_SUBSCRIPTION_ID --what-if
bash infra/deploy.sh --subscription YOUR_SUBSCRIPTION_ID
```

Bicep creates an isolated registry, Container App, Foundry/model, identity, Key Vault and monitoring.
The Azure demo requires a generated access code. It uses synthetic tools only, with replica-local sessions.

Set `AGENT_MODE=foundry`, `FOUNDRY_PROJECT_ENDPOINT`, and `MODEL_DEPLOYMENT` in `.env`, then sign in with
`az login`. The deployment must support the Responses API and function calling. The service sends the
instructions/tool schemas on each request and executes every requested tool itself.

This mode uses a model deployment in your Foundry project, not a published hosted-agent version. Tools remain
synthetic. Your messages and selected synthetic tool results go to **your configured Azure project**; session
credentials do not. You must configure appropriate regional, privacy and content-filter policies there.
No live Foundry request is necessary to run the tests.

## Where to add your APIs

Read [`docs/integration.md`](docs/integration.md) and [`docs/architecture.md`](docs/architecture.md).
Implement a request-scoped `AlsenseApiSource` matching the `AlsenseSource` protocol. Replace demo login with
trusted user-token validation and a separate downstream delegated-token flow; **do not forward a backend-audience
token to another API unless that API accepts its audience**. An OAuth on-behalf-of exchange may be required.
Keep all credentials in the authentication/adapter context, not tool arguments, history, or prompts.

Real identity validation, delegated-token exchange, Alsense HTTP/MCP clients, Azure AI Search, APIM,
explicit Content Safety integration and shared cloud persistence are **not implemented**.
Key Vault, managed identity, request telemetry and Bicep hosting are implemented for the protected synthetic demo.
There are no invented production endpoint paths or fallback service credentials. Startup deliberately refuses
`DEMO_LOGIN=false` until you wire a real authentication provider.

SQLite is a persistent **single-instance local POC**, not Cosmos DB/Redis and not multi-replica cloud storage.
Mount its directory to keep sessions across container replacement. Existing sessions expire; the browser keeps
its demo token only in memory, so refreshing the page starts a new browser session. Question text is stored in the
local SQLite history; unanswered telemetry records categories only. Set a retention and access-control policy
before handling real users.

## Tests and clean sharing

```bash
uv run pytest
uv run ruff check src tests infra export.py
python export.py
```

The export command produces `dist/alsense-chat-starter.zip` from an explicit file allowlist. It excludes `.env`,
SQLite databases, caches, parent repository files and **all Git history**. It refuses to overwrite an existing
archive. Share this clean export or initialize a **new** repository from it; sharing a branch from the parent
repository does not remove confidential files from that repository's history.

For a local container:

```bash
docker build -t alsense-chat-starter .
docker volume create alsense-starter-data
docker run --rm -p 127.0.0.1:8001:8000 -v alsense-starter-data:/data alsense-chat-starter
```

The demo has no production sign-in. Never expose its login endpoint publicly. Backend auth, tenant checks,
Search permissions, shared persistence and deployment policy must be reviewed before external access.
