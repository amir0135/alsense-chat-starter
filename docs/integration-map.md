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

### Site directory and authorized-site resolution

- **Interface:** `SiteDirectory.permitted_sites(user) -> list[Site]`, `active_alarms(user, site_id)`
- **Synthetic:** `SyntheticSiteDirectory` returns the user's sites; every site has one active alarm.
- **Fixtures:** `sites/permitted.json`, `sites/permitted-similar-names.json`, `sites/unavailable.json`,
  `sites/alarms-active.json`, `sites/forbidden.json`
- **Connect the real operation in:** `AlsenseSiteDirectory.permitted_sites` in `adapters/alsense/__init__.py`.

Matching a friendly name is backend code ([`domains/sites.py`](../src/alsense_chat/domains/sites.py)), so it works
the same whatever the real operation returns:

```text
User mentions a friendly site name
        ↓
get_permitted_sites → SiteDirectory.permitted_sites(user)    the sites the signed-in user may access
        ↓
resolve(name, permitted sites)                               match against those sites only
        ↓
one match       → use its stable Alsense site id (a near match is confirmed with the user first)
several matches → ask the user to choose
no match        → "I couldn't find that site among the sites you can access", plus the sites they can choose
service down    → "The site directory is temporarily unavailable"; nothing is guessed
```

A near match is a misspelling or a name with extra words: "Demo North Annex" gets "Do you mean Demo North?" instead
of a Demo North report. The model never invents or guesses a site id. `Context.execute` refuses a model-supplied `site_id` unless
`get_permitted_sites` resolved exactly that site in the same turn (`unverified_site`). The backend also drops any site
the directory returns outside the session's permissions. A site the user can't access gets the same reply as a
site that doesn't exist, so nothing reveals that it exists. The cases are tested in `tests/test_sites.py` and
evaluated in `evals/synthetic/sites.yaml`: one match, several similar sites, no match, a site without access, and
the site service unavailable.

- **Alsense must provide:**
  - The sites the signed-in user may access, filtered server-side by the delegated user context, each with a stable
    site id and a display name. A server-side name search may narrow the list; the backend still resolves the name.
  - Error semantics: access revoked (403), unknown user (401) and outages (5xx, timeouts), mapped as in the table
    above. An empty list means the user has no sites, never "the service failed".
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
- **Synthetic:** temperature log (PDF, CSV or Excel; can be scheduled daily, weekly or monthly), alarm history (PDF,
  CSV or Excel; weekly or monthly) and site status (PDF or CSV; on demand only).
- **Fixtures:** `reports/report-types.json`
- **Alsense must provide:** report types, permitted formats, required fields, date semantics and limits, and for
  each type whether it can be scheduled: its frequencies, whether a schedule can have an end date, and the
  maximum number of recipients. The backend validates every draft against this catalog, so the assistant can only
  offer a report type the service returned; the report builder shows the same options.

### On-demand reports: submission, job status and download link

- **Interface:** `ReportService.submit_report(user, request, idempotency_key) -> Submission`,
  `report_status(user, job_id) -> ReportJob`
- **Synthetic:** jobs are accepted and complete immediately. The download link expires 24 hours after the job
  completed (`LINK_TTL` in `adapters/synthetic/services.py`). The `report_processing` and `report_failed` faults
  show the other states.
- **Fixtures:** `reports/request.json`, `reports/submitted.json`, `reports/processing.json`,
  `reports/completed.json`, `reports/failed.json`
- **Connect the real operation in:** `AlsenseReports.submit_report` and `AlsenseReports.report_status`.

```text
prepare_report            → validated draft, stored locally; nothing is sent
user presses Confirm      → POST /api/actions/{id}/submit → submit_report (permissions re-checked)
widget reads the status   → GET /api/actions/{id}/status → report_status
completed                 → clickable download link, with "Expires 24 hours after the report was ready"
```

- **Alsense must provide:**
  - An idempotent submission endpoint honoring the draft id as idempotency key, or a reconciliation mechanism.
  - Job status (processing, completed, failed with a user-safe reason).
  - The download link for the finished report and its expiry time (`completed_at`, `expires_at`). The widget shows
    the times the service returns, so a different link lifetime shows correctly. Approve the link's origin in
    `safe_link()`.

### Scheduled reports

- **Interface:** `ReportService.schedule_report(user, request, idempotency_key) -> Submission` (status
  `scheduled`, with `next_run`)
- **Contract:** `ScheduledReportRequest`: site, report type, format, frequency, recipients, start date and an
  optional end date when the report type supports one.
- **Synthetic:** schedules are accepted immediately and the first report goes out on the start date.
- **Fixtures:** `reports/schedule-request.json`, `reports/scheduled.json`
- **Connect the real operation in:** `AlsenseReports.schedule_report`.
- **Alsense must provide:**
  - An idempotent endpoint that creates a schedule, honoring the draft id as idempotency key.
  - Recipient rules: allowed domains or internal users only, limits, and whether recipients must be confirmed.
  - The schedule's status and next run. Listing, changing and cancelling schedules are not implemented yet.

Preparing a report or a schedule is local: the backend validates and stores a draft. Only the explicit
confirmation endpoint calls `submit_report` or `schedule_report`, after re-checking permissions. The assistant
says a report was created or scheduled only when a status from the service confirms it; the reply guard rejects
any other claim. Recipient email addresses are never written to the audit trail, only their count.

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

### Frontend actions (future integration)

- **Not implemented.** The assistant does not operate the host application's screens yet. The capability manifest
  and the host contracts will come from the frontend team; until then the empty `frontend_actions` agent module
  (`status: future`) only reserves the domain, and no schema is defined.
- **Intended flow:**

```text
Frontend action catalog → the actions available on the current screen for the current user
        ↓
Host validation         → the host validates the action, the user's permissions and the filters
        ↓
Host completion         → the host returns the filters it actually applied and the actual result
        ↓
Assistant               → confirms only after host completion, describing what the host applied
```

- **Needed before implementing:** how the catalog is delivered per screen and user, the action and filter
  vocabulary, how the host reports a refused action, how completion and the applied filters come back, and how the
  chat and the host correlate one request. The assistant will offer only catalog actions as tools, and treat a
  host refusal like any other categorized failure.

### Persistence and audit

- **Not an adapter:** `Store` in `store.py` (SQLite, single instance, schema migrations).
- **Alsense must provide:** the shared store (for example Cosmos DB or Redis), retention rules and access control
  for sessions, drafts and the audit trail. Keep user and tenant ownership, expiry, idempotent submission and the
  allowlisted audit fields (no tokens, no free text).
