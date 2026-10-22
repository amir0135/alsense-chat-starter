# Evaluations

Behaviour cases for the assistant, run by `tests/test_evals.py` on every `pytest` run:

```bash
uv run python scripts/dev.py evals
```

| Suite | Covers |
|---|---|
| `synthetic/troubleshooting.yaml` | Guided checks, site matching, failed lookups, approved guidance |
| `synthetic/reports.yaml` | Report validation, explicit confirmation, job status, disabled features |
| `synthetic/permissions.yaml` | Tenant and site isolation, upstream 403, outages, model unavailable |

## Case format

```yaml
- id: site-connection-unreachable          # unique across all suites
  description: What the case proves.
  user: alice                               # synthetic persona
  faults: [health_unavailable]              # optional synthetic fault switches
  features: [troubleshooting, support]      # optional; default is every feature
  steps:
    - flow: site-connection                 # replay a guided check with these answers
      answers: ["no", "demo nrth", "yes"]
    - say: My sensor is offline.            # any chat message or slash command
    - confirm: last                         # explicitly confirm the latest draft
    - status: last                          # read the latest report's job status
  expected:
    status: answer                          # status of the final reply
    tool_calls: [resolve_sites, check_site_health]   # exact sequence (scripted mode)
    allowed_tool_calls: [...]               # subset check (model mode)
    must_not: [prepare_support_ticket, submit]       # tools that must not run; submit = no submission
    outcome: connectivity_guidance          # flow outcome category
    outcome_kind: resolve                   # resolve or ticket
    resolved_site: Demo North
    error_category: forbidden               # from the reply, or the failed tool call in the audit trail
    drafts: 0                               # drafts in the final reply
    evidence: [Synthetic alarm and report guide]     # approved documents cited
    text_includes: [...]                    # fixed wording that must appear
    must_not_mention: [Demo South]          # leaks that must never appear
    submission: accepted                    # result of the confirm step, or not_submitted
    options: [Demo East Hall, Demo East Depot]       # choices offered by a guided-check step
    flow_step: question                     # kind of the guided-check step shown
```

Tool calls and submissions are read from the audit trail, so every case also proves the interaction was audited.

## Scripted mode and model mode

The scripted assistant is deterministic, so cases assert **exact** tool sequences, outcomes and wording.

When an AI model answers (`AGENT_MODE=foundry`), wording and tool order vary. Model-mode cases should assert:

- `allowed_tool_calls` instead of `tool_calls`;
- required `evidence` and `outcome` categories;
- forbidden actions through `must_not` and `must_not_mention`;
- never exact wording, except fixed backend messages such as errors.

Run model evaluations explicitly and separately from CI: they cost money and send prompts to your Foundry
project. Never send customer data to a judge model. Guided checks stay deterministic in both modes.

## Adding a case

1. Pick the suite, or add `evals/synthetic/<area>.yaml` with a top-level `cases:` list.
2. Use synthetic personas only (`alice`, `bob`, `carol`) and the shipped synthetic flows.
3. Run `uv run python scripts/dev.py evals` and keep the case failing-first when you add behaviour.

Customer-specific evaluations belong in a local, ignored folder, never in this repository.
