# Agent modules

The assistant's instructions and tools are split by domain. Each request is routed to the domains it needs, and the
model receives only those domains' instructions and tools. A report request gets the site and reporting tools, and
nothing from troubleshooting, alarms or energy.

```text
agent/
├── core/instructions.yaml        sent with every request: scope, tone, output format, domain order
├── sites/                        friendly site names → the user's permitted sites (get_permitted_sites)
├── reporting/                    on-demand and scheduled reports, status and confirmation
├── troubleshooting/              approved guidance, support drafts, backend health checks
├── alarms/                       future: description only
├── energy/                       future: description only
└── frontend_actions/             future: waits for the frontend capability manifest
```

## A domain folder

`instructions.yaml`:

| Key | Meaning |
|---|---|
| `domain`, `title` | The folder name and its display name (shown in "Why this result?" and on `/system`). |
| `status` | `active`, or `future` for a placeholder with a description only. |
| `requires` | Domains that are always offered with this one. Reporting requires sites. |
| `routing` | Word lists. A message is routed here when it contains every word of one list, matched at word starts. |
| `commands` | Slash commands that belong to this domain. |
| `instructions` | What the model must know to use this domain's tools. |

`tools.yaml` declares each tool once:

| Key | Meaning |
|---|---|
| `caller` | `model` (offered to the model), `user` (runs only from an explicit user action, such as Confirm) or `backend` (code only, such as guided-check lookups). |
| `feature` | The `ENABLE_*` switch the tool needs, if any. |
| `description`, `parameters` | What the model reads. Parameter descriptions are merged into the schema the backend generates from the handler. |

## Routing

1. A slash command routes to the domain that lists it.
2. Otherwise every domain whose routing words appear in the message.
3. Otherwise the previous turn's domains, so a follow-up such as "make it Excel" stays in reporting.
4. Otherwise `core.default_domains`.

Required domains are added, and tools whose feature is switched off are removed. Each chat turn's domains and
offered tools are written to the audit trail.

## Adding a domain or a tool

1. Write the handler: a Pydantic schema and a function, in `src/alsense_chat/domains/<domain>.py`, and add it to
   `HANDLERS` in `src/alsense_chat/tools.py`.
2. Declare the tool in the domain's `tools.yaml` with the same parameter names and required fields. Startup fails
   (`check_registry()`) when a declaration and its handler disagree, or when a handler isn't declared.
3. To activate a future domain, set `status: active` and add `routing`, `instructions` and a `tools.yaml`.
4. Add tests and evaluation cases (`evals/`), including `must_not: [offer_unrelated_tools]` for its requests.
