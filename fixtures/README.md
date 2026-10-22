# Contract fixtures

Example request and response bodies for every integration point, generated from the synthetic adapters by
`scripts/fixtures.py`. They are **synthetic reference contracts, not the Alsense API**. Compare them with the
real endpoints when the owning teams publish their contracts, then change the contract models in
`src/alsense_chat/adapters/contracts.py` deliberately and regenerate:

```bash
uv run python scripts/dev.py fixtures
```

`tests/test_contracts.py` fails if a committed fixture drifts from the synthetic adapters, if a fixture stops
matching its contract model, or if a fixture has no producing case.

| Fixture | Interface method | Shows |
|---|---|---|
| `identity/users-me.json` | `IdentityProvider.users_me` | Signed-in user with tenant and permitted sites |
| `sites/permitted.json` | `SiteDirectory.permitted_sites` | The signed-in user's permitted sites, with stable IDs |
| `sites/permitted-similar-names.json` | `SiteDirectory.permitted_sites` | Similar names, so the assistant asks which one |
| `sites/unavailable.json` | `SiteDirectory.permitted_sites` | Site directory down (503) |
| `sites/alarms-active.json` | `SiteDirectory.active_alarms` | Active alarms for one site |
| `sites/forbidden.json` | backend permission check | A site outside the user's permissions (403) |
| `health/unreachable.json` | `HealthCheckService.site_health` | An open gateway-offline check |
| `health/reachable.json` | `HealthCheckService.site_health` | No open checks |
| `health/unavailable.json` | `HealthCheckService.site_health` | Service down (503) |
| `health/timeout.json` | `HealthCheckService.site_health` | Service too slow (504) |
| `knowledge/search-results.json` | `KnowledgeSearch.search` | Approved excerpts with approved links |
| `knowledge/no-approved-answer.json` | `KnowledgeSearch.search` | Nothing approved matches |
| `reports/report-types.json` | `ReportService.report_types` | Report types, formats and which can be scheduled |
| `reports/request.json` | `ReportService.submit_report` (input) | A validated report request |
| `reports/submitted.json` | `ReportService.submit_report` | Job accepted |
| `reports/processing.json` | `ReportService.report_status` | Job still running |
| `reports/completed.json` | `ReportService.report_status` | Job finished, with a download link and its expiry time |
| `reports/failed.json` | `ReportService.report_status` | Job failed |
| `reports/schedule-request.json` | `ReportService.schedule_report` (input) | A validated schedule: frequency, recipients, start and end date |
| `reports/scheduled.json` | `ReportService.schedule_report` | Schedule accepted, with the first run date |
| `support/request.json` | `SupportService.submit_request` (input) | A support request prefilled by a guided check |
| `support/received.json` | `SupportService.submit_request` | Request received |
| `support/unavailable.json` | `SupportService.submit_request` | Service down (503): prepared but not submitted |
| `schemas/*.schema.json` | all | JSON Schema for each contract model |

Matching a friendly site name happens in the backend (`src/alsense_chat/domains/sites.py`), not in the
adapter: `permitted_sites` returns the user's sites and the resolver picks one, asks which one, or reports that
the site wasn't found. A site the user can't access is reported the same way as a site that doesn't exist.

Error fixtures describe the **categorized error an adapter raises**, not an upstream response body: map the
real API's status codes and error bodies onto `service`, `category` and `http_status`, and never pass upstream
bodies, tokens or internal identifiers to the user or the model. See
[`docs/integration-map.md`](../docs/integration-map.md) for what Alsense must provide for each interface.
