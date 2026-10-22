# Evaluations

Behaviour cases for the assistant. Each case states a question, the user and their sites, and what must happen:
the tools, the prepared parameters, any clarification, the result, whether the user must confirm, and what must
never happen. `tests/test_evals.py` runs every case here on each `pytest` run. To run them on their own, with a
pass/fail line per case:

```bash
uv run python scripts/dev.py evals                              # synthetic cases, scripted assistant
uv run python scripts/dev.py evals evals/local                  # your own cases
uv run python scripts/dev.py evals --mode model evals/local     # your own cases against the Foundry model
uv run python scripts/dev.py evals -k reporting --json          # one suite, machine-readable
```

| Suite | Covers |
|---|---|
| `synthetic/reporting.yaml` | The report path end to end, on-demand and scheduled reports, validation, job states |
| `synthetic/sites.yaml` | Site resolution: one match, similar sites, no match, no access, service unavailable |
| `synthetic/permissions.yaml` | Tenant and site isolation, upstream 403, outages, assistant unavailable |
| `synthetic/troubleshooting.yaml` | Guided checks, failed lookups, approved guidance |

## Writing a case

```yaml
cases:
  - name: create_on_demand_report
    description: What the case proves.                  # optional
    question: >
      Create a temperature report for Store North
      for the last seven days.
    context:
      user: alice                                       # synthetic identity: alice, bob or carol
      sites: [Store North, Store South]                 # optional: this user's permitted sites for the case
    expected:
      tools:
        - get_permitted_sites
        - list_report_types
        - prepare_report
      parameters:
        site: Store North
        report_type: temperature-log
        period_days: 7
      clarification_required: false
      confirmation_required: true
      result: draft_prepared
      must_not:
        - invent_report_type
        - submit_before_confirmation
        - use_unauthorized_site
```

A case has a `name` (lowercase, unique across all suites) and a `question`, `steps`, or both: the question is
sent first, then the steps. Unknown keys fail the run, so a typo can't silently skip a check.

### Question, steps and context

| Key | Meaning |
|---|---|
| `question` | The user's message. Folded YAML (`>`) is joined into one line. |
| `steps` | Further turns, each one of: `say: <message>`, `choose: <label>` (an answer the assistant offered), `confirm: last` (press Confirm on the latest draft), `status: last` (ask for the latest draft's status), or `flow: <id>` with `answers: [...]` (replay a guided check). |
| `context.user` | Synthetic identity: `alice`, `bob` or `carol`. |
| `context.sites` | Optional. Replaces the user's permitted sites for this case, as names or `{name, id}`; ids default to `site-<name>`. |
| `context.inaccessible_sites` | Optional. Names that exist for other users; they must never appear in a reply unless the user typed them. |
| `context.faults` | Optional synthetic fault switches, for example `[sites_unavailable]`. See `.env.example`. |
| `context.features` | Optional. The capabilities switched on; the default is all of them. |
| `modes` | Optional. `[scripted]`, `[model]` or both (the default). |

### What a case can check

| Key | Passes when |
|---|---|
| `tools` | The assistant called exactly these tools, in this order. In model mode they must appear in this order; other calls are allowed. |
| `allowed_tools` | Every call was one of these. |
| `parameters` | The latest draft has these values. `site` is a site name; `period_days` counts the days of an on-demand period; dates accept `today`, `tomorrow`, `yesterday` and `today-6`; `recipients` is a count or a list. Any other key is compared with the draft field of that name. |
| `clarification_required` | `true`: the assistant asked at least one question. `false`: it asked none. |
| `clarification` | The first question asked: `asks_for` (`site`, `report_type`, `period`, `format`, `frequency`, `recipients`, `start_date`), `options` (the answer buttons offered, scripted mode only) and `text_includes`. |
| `confirmation_required` | `true`: a draft was prepared for the user to confirm. `false`: none was. |
| `result` | The outcome, or a list of acceptable outcomes (see below). |
| `must_not` | None of these happened (see below). Plain tool names mean the tool was never called. |
| `domains` | The final request was routed to exactly these agent domains. |
| `offered_tools` | The final request was offered exactly these tools. |
| `download_link` | The final reply has an approved download link; `expires_after_hours: 24` checks its expiry. |
| `status`, `error_category`, `drafts`, `evidence`, `text_includes`, `must_not_mention`, `submission` | Details of the final reply: its status, error category, number of drafts, cited document titles, fixed wording, leaks, and the result of the last confirm step. |
| `flow` | A guided check's `outcome` (`resolve` or `ticket`), `resolved_site` and `step`. |

### Results

| Result | Meaning |
|---|---|
| `draft_prepared` | The final reply holds a draft waiting for the user's confirmation. |
| `clarification` | The assistant asked a question. |
| `answer`, `not_found`, `out_of_scope` | An answer, nothing found in the permitted data, or outside the assistant's scope. |
| `report_completed`, `report_processing`, `report_failed`, `report_scheduled`, `report_not_confirmed` | The final reply read a report's or schedule's status. |
| `submitted:<status>`, `not_submitted` | The last step was a confirm: the service's answer (`accepted`, `scheduled`, `received`) or a refusal. |
| `error:<category>` | A categorized failure, for example `error:unavailable` or `error:forbidden`. |
| `flow:<category>` | A guided check ended with this outcome category. |

### Things that must not happen

| `must_not` | Fails when |
|---|---|
| `invent_report_type` | A report type was used that the report service doesn't offer, or before `list_report_types` ran. |
| `submit_before_confirmation` | Something was submitted without a confirm step, or the assistant called a user-only action. |
| `use_unauthorized_site` | A tool, draft or reply used or showed a site outside the user's permitted sites. |
| `guess_site_id` | A site id reached a tool before `get_permitted_sites` resolved it in the same turn. |
| `claim_without_confirmation` | A reply said something was created, scheduled, submitted or sent without the service confirming it. |
| `offer_unrelated_tools` | A request was offered tools outside the domains of the expected `tools` (or `domains`). |
| `submit` | Anything was submitted, even after a confirm step. |

Tool calls are read from the audit trail, so a passing case also proves the interaction was audited.

## Scripted mode and model mode

The scripted assistant is deterministic: cases assert exact tool sequences, wording and answer buttons. With an AI
model (`--mode model`, which needs `AGENT_MODE=foundry` and the Foundry settings in `.env`), wording and tool order
vary, so the harness relaxes these checks:

- `tools` must appear in order, with other calls allowed;
- `text_includes` and clarification `options` apply only to fixed backend wording (errors and guided checks);
- `must_not`, `parameters`, `result`, `confirmation_required` and `clarification_required` still apply in full.

Run model evaluations explicitly and separately from CI: they cost money and send the questions to your Foundry
project. Guided checks, slash commands and confirmations are deterministic in both modes.

## Cases from customer examples

Keep cases written from customer examples in `evals/local/` (ignored by Git and refused by
`scripts/check_protected_files.py`), even when anonymized. Start by copying a synthetic case, then replace the
question, the `context` sites and the expectations with the anonymized example. Use `context.sites` and
`context.inaccessible_sites` to recreate a user's sites without changing the synthetic data.
