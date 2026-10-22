# Connecting a real Alsense environment

Nothing in this document is an assertion about the actual Alsense API contract. The contracts in
`src/alsense_chat/adapters/contracts.py` and the examples in `fixtures/` are reference shapes to replace or map once
the owners supply their contracts. [`integration-map.md`](integration-map.md) lists, per synthetic component, the
interface it implements and what Alsense must provide.

## Inputs needed from the owning teams

| Owner input | Implementation point |
|---|---|
| Login issuer, JWKS, backend audience, tenant allowlist, scopes and internal-user eligibility | Replace `/api/demo/session` and `context()` in `app.py`; fail closed on missing claims |
| Downstream API audience and delegated scopes; OBO client configuration if required | Request-scoped credential handling in your adapter; never use model-provided auth |
| `users/me` response and site/alarm authorization model | Map to `User` and `Site`; cache permissions only for an approved TTL and handle revocation |
| Report types, formats, required fields, date semantics and submit/status flow | Adapt `Report`, `report_types`, `submit`, and status handling; do not invent defaults |
| Support ticket endpoint/mail workflow and mandatory fields | Adapt `Ticket`, confirmation and `submit`; configure destination server-side |
| Existing Azure AI Search index, field mapping and document-access rules | Replace mock `search_docs`; return restricted excerpts and approved source links |
| Operations decision trees, their content owner, and an API listing open health checks per site/unit | Load the trees with `FLOWS_FILE`; implement `HealthCheckService.site_health` in `adapters/alsense` |
| Shared gateway URL, model deployment, auth and content-safety policy | Configure model client and gateway routing; do not assume the SDK can route through an arbitrary gateway unchanged |
| Cosmos DB/Redis choice and retention requirements | Replace `Store` while preserving user/tenant ownership, expiry, draft state and idempotency |

## Adapter checklist

Implement the placeholder services in `src/alsense_chat/adapters/alsense/`, one class per interface in
`adapters/interfaces.py`, then select them with `DATA_SOURCE=alsense`. Instantiate them with a credential
**per request**, not as singletons that change tokens between users. Never store a user token in model conversation state or serialize
it as part of the tool context. Apply explicit timeouts, bounded responses, TLS, fixed configured API origins and
paths, typed response validation, and permission filtering at the adapter boundary.

The starter intentionally does not accept arbitrary API URLs, headers or HTTP methods from the model. Do not add
a generic HTTP tool. Each new capability needs a typed schema, one curated handler, authorization tests, and a
matching entry in `src/alsense_chat/alsense.semantic.yaml`. `model_definition()` checks field/required-field parity;
Pydantic enforces the actual runtime constraints.

Map upstream failures to the categorized errors in `adapters/errors.py` (forbidden, not found, unavailable,
timeout, stale, unauthenticated) without exposing credentials or raw upstream bodies; the UI shows a distinct
message for each. An empty
data response is not proof that there are no alarms. Never substitute a service credential after a delegated
request fails. Revalidate permissions at action submission, including retries.

The mock submission result is cached transactionally. A real API must honor an idempotency key or provide a
reconciliation/status mechanism; a local database transaction cannot undo a network side effect if a process
dies after the API succeeds. Do not automatically retry a non-idempotent ticket creation.

## Guided troubleshooting flows

Operations decision trees run as deterministic flows, not model answers. Each flow is versioned YAML owned by
the content owner (`version`, `status`, `owner`, `approved_for_demo`) and validated at startup: duplicate keys,
unknown or unreachable nodes, loops, unsupported keys or checks, and outcomes without an explicit kind fail fast,
all reported at once. Only flows approved for demos are offered. Node types are `ask` (yes/no), `identify_site`
(match the user's description to a permitted site, offer "Do you mean …?", ask which one when several match,
and stop after `attempts`), `check` (a backend lookup) and `outcome` (`resolve` or `ticket`, with a `category`
for audits and evaluations). A `ticket` outcome prefills a support request with the answered steps; nothing is
sent until the user confirms it. When a lookup fails, the flow stops with the reason (or follows the check's
optional `unavailable` branch) instead of taking the "no" branch. See `src/alsense_chat/troubleshooting.yaml`.

Where a tree says the bot "queries the database", the starter calls a backend-only adapter method with the
user's permissions (`HealthCheckService.site_health`, synthetic here). The model cannot call it, and there is no direct
database access. Each request replays the flow with the answers so far, so no flow state is stored and every
lookup is re-checked against the current session.

Customer-provided trees and wording belong to the customer. Keep them in a local `*.local.yaml` file loaded
through `FLOWS_FILE`; Git, Docker images and the export all exclude it. Ticket outcomes are recorded as
`flow_ticket:<flow>:<node>` categories in the fallback backlog to show which branches still need people.

## Production deployment work not included

The starter's `infra/` deploys a protected synthetic demo with Container Apps, Foundry, Key Vault, managed
identity and monitoring. See `azure-demo.md`. Production still needs your approved gateway/network path,
real sign-in, Search access and shared persistence. Do not reuse another environment's resource IDs or
customer data.

The model's configured content filters are not a complete application Content Safety integration. Define the
organization's input/output handling and test blocked responses before enabling real traffic. Add tracing spans
without token or message capture and add user-approved data retention policies.

The current internal-user flag is a mock property. In production use verified directory/tenant/group eligibility
and a backend-controlled rollout flag. It is not sufficient to hide the widget in the UI.

## Evaluation gate

`tests/` and the cases in `evals/` are an offline behavioral gate for the synthetic implementation. They are not
the missing Alsense business question bank; see `evals/README.md` for the case format, including model mode. Get that set from the team and add expected clarification, refusal, source
grounding, report preparation, ticket confirmation and cross-customer cases.

Run `uv run python scripts/dev.py test` on every change. Cross-user/site authorization failures must fail the build; never mark them
as optional live tests. Real model evaluation should be explicit and separate, with no automatic exposure of
customer data to a judge.
