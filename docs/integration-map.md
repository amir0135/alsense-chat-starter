# Integration map: synthetic components and what Alsense must provide

Every external capability sits behind one interface in
[`src/alsense_chat/adapters/interfaces.py`](../src/alsense_chat/adapters/interfaces.py). The synthetic
implementation runs by default; the matching class in
[`src/alsense_chat/adapters/alsense/`](../src/alsense_chat/adapters/alsense/__init__.py) is a placeholder that
raises `NotConfiguredError`. Conversation, guided-check and UI code never change when an adapter is replaced.

Nothing here describes the actual Alsense API. The contracts and [`fixtures/`](../fixtures/README.md) are synthetic
reference shapes to compare with the real endpoints once the owning teams publish them.

## How to replace a synthetic service

1. Implement the class in `src/alsense_chat/adapters/alsense/__init__.py` against the interface below. Return the
   contract models from `adapters/contracts.py`; map the real API onto them rather than leaking its shapes.
2. Receive the user's delegated credential in the adapter's constructor, **per request**. Never put credentials in
   method arguments, tool arguments, prompts, history or logs.
3. Use fixed, configured API origins and paths, explicit timeouts, bounded responses and typed validation.
4. Map upstream failures to the errors in `adapters/errors.py` (table below). Never return upstream bodies.
5. Add contract tests next to `tests/test_contracts.py` with recorded responses that contain **no customer data**.
6. Set `IMPLEMENTED = True` and run with `DATA_SOURCE=alsense`. `/system` then shows "Alsense API" for each
   capability, and synthetic fault switches are refused.

To integrate one service at a time, change `build_services()` in `adapters/__init__.py` to combine real and
synthetic services, and label the mix on `/system`. Never fall back to synthetic data when a real call fails.

## Error mapping

| Upstream result | Raise | Category | User sees |
|---|---|---|---|
| 401, expired or rejected delegated token | `Unauthenticated(service)` | `unauthenticated` | Sign-in no longer valid; start a new session |
| 403 for a site or resource | `Forbidden(service)` | `forbidden` | "You do not have access to this site." |
| 404 for a site or job | `NotFound(service)` | `not_found` | The service couldn't find it |
| 5xx, connection refused | `ServiceUnavailable(service)` | `unavailable` | Temporarily unavailable; try again |
| Timeout | `ServiceTimeout(service)` | `timeout` | Didn't respond in time |
| Data older than the freshness limit | `StaleData(service)` | `stale` | Status out of date, not relied on |
| Adapter not wired | `NotConfiguredError(service)` | `not_configured` | Not connected in this environment |

Guided checks stop with the explicit reason; a failed lookup is never treated as a "no". Failed confirmations
say "Your request was prepared but not submitted." The synthetic `SYNTHETIC_FAULTS` switches demonstrate every row.

## Components

### Identity and site permissions

- **Interface:** `IdentityProvider.users_me(identity) -> User`
- **Synthetic:** `SyntheticIdentity` with Alice (Demo North), Bob (Demo South) and Carol (Demo East Hall,
  Demo East Depot); `/api/demo/session` signs in without a password.
- **Fixtures:** `identity/users-me.json`
- **Alsense must provide:**
  - Token issuer, JWKS, backend audience, scopes and internal-user eligibility, so the backend can validate sign-in
    and replace the demo login in `app.py`.
  - `users/me` (or equivalent) with a stable user id, tenant id and the permitted sites with display names.
  - Revocation semantics: how long a permission snapshot may be cached (sessions store it; submissions re-check).
  - The downstream audience and delegated scopes, and whether an on-behalf-of exchange is required.

### Site directory

- **Interface:** `SiteDirectory.search_sites(user, query) -> list[Site]`, `active_alarms(user, site_id)`
- **Synthetic:** `SyntheticSiteDirectory` filters the user's sites by name; every site has one active alarm.
- **Fixtures:** `sites/search-success.json`, `sites/search-ambiguous.json`, `sites/search-empty.json`,
  `sites/alarms-active.json`, `sites/forbidden.json`
- **Alsense must provide:**
  - Search over the **user's permitted sites only**, filtered server-side by the user context.
  - A stable site id and a display name for each result.
  - Correct handling of zero, one and several matches; the backend asks the user to choose when several match.
  - Active alarms per site with ids, labels and status, for support requests.

### Gateway health and other operational checks

- **Interface:** `HealthCheckService.site_health(user, site_id) -> HealthReport`
- **Synthetic:** `SyntheticHealth`: Demo North has an open primary-gateway check, Demo East Hall a secondary one.
- **Fixtures:** `health/unreachable.json`, `health/reachable.json`, `health/unavailable.json`, `health/timeout.json`
- **Alsense must provide:**
  - Open health checks per site (or unit), each with a stable type code that flows can match (`any_open`).
  - The observation time, so stale status is reported instead of trusted (limit: `HEALTH_MAX_AGE` in `tools.py`).
  - Permission enforcement for the site; the backend also checks before calling.
  - Further check types that guided checks need (for example, whether sensor or HACCP logging data is arriving)
    become new methods on this interface plus a new entry in `CHECK_TOOLS` in `flows.py`.

### Guidance search

- **Interface:** `KnowledgeSearch.search(user, query) -> list[Document]`
- **Synthetic:** `SyntheticKnowledge` with two synthetic documents.
- **Fixtures:** `knowledge/search-results.json`, `knowledge/no-approved-answer.json`
- **Alsense must provide:**
  - Access to the approved Azure AI Search index, field mapping and document-level permissions.
  - Excerpts and **approved** links only; the backend rejects unapproved links. Configure approved origins in
    `safe_link()` when real links arrive.
  - An empty result when nothing approved matches; the assistant then says so instead of inventing advice.

### Report metadata

- **Interface:** `ReportService.report_types(user) -> list[ReportType]`
- **Synthetic:** alarm history and site status, each as PDF or CSV.
- **Fixtures:** `reports/report-types.json`
- **Alsense must provide:** report types, permitted formats, required fields, date semantics and limits. The
  backend validates drafts against them; the report builder shows what the service offers.

### Report submission, job status and retrieval

- **Interface:** `ReportService.submit_report(user, request, idempotency_key) -> Submission`,
  `report_status(user, job_id) -> ReportJob`
- **Synthetic:** jobs are accepted and complete immediately; `report_processing` and `report_failed` faults show
  the other states.
- **Fixtures:** `reports/request.json`, `reports/submitted.json`, `reports/processing.json`,
  `reports/completed.json`, `reports/failed.json`
- **Alsense must provide:**
  - An idempotent submission endpoint honoring the draft id as idempotency key, or a reconciliation mechanism.
  - Job status (processing, completed, failed with a user-safe reason).
  - Retrieval: an approved link or download endpoint for the finished report.

Preparing a report is local: the backend validates and stores a draft. Only the explicit confirmation endpoint
calls `submit_report`, after re-checking permissions.

### Support request preparation and submission

- **Interface:** `SupportService.submit_request(user, request, idempotency_key) -> Submission`
- **Synthetic:** requests are received immediately; `support_unavailable` shows an outage.
- **Fixtures:** `support/request.json`, `support/received.json`, `support/unavailable.json`
- **Alsense must provide:**
  - The approved ticket workflow (API or service desk), its mandatory fields and the destination, configured
    server-side. The model never chooses a destination.
  - Idempotency, because a ticket must not be created twice when a confirmation is retried.

Preparation is local: guided checks and the support form produce a draft that the user reviews and confirms.

### Troubleshooting flows

- **Not an adapter:** flows are versioned YAML that operations teams own (`FLOWS_FILE`).
- **Alsense must provide:** the decision trees, their owner and approval status, and the check types they need.
  Customer trees stay in a local `*.local.yaml` file; see the README section on adding a flow.

### AI model

- **Not an adapter:** `FoundryRunner` in `runner.py` calls your Azure AI Foundry deployment with backend-owned tools.
- **Alsense must provide:** the gateway (APIM) route, model deployment, content-safety policy and data-handling
  approval. When the model is unreachable the user is told; the scripted assistant is never substituted.

### Persistence and audit

- **Not an adapter:** `Store` in `store.py` (SQLite, single instance, schema migrations).
- **Alsense must provide:** the shared store (for example Cosmos DB or Redis), retention rules and access control
  for sessions, drafts and the audit trail. Keep user and tenant ownership, expiry, idempotent submission and the
  allowlisted audit fields (no tokens, no free text).
