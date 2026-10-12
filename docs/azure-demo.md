# Deploy your own synthetic demo

This deploys billable resources to **the subscription you explicitly pass**, never whichever subscription happens
to be the global CLI default. It does not deploy real Alsense connections or any confidential data.

## Requirements

- Azure CLI and Bicep: `az bicep install` if the compiler is missing.
- Python 3.12+; no local Docker daemon required (ACR builds the image remotely).
- Login: `az login`. For a particular directory use `az login --tenant YOUR_TENANT_ID`.
- Resource deployment and scoped RBAC permissions: Owner, or Contributor plus Role Based Access Control
  Administrator/User Access Administrator at the deployment scope.
- Available GPT-5-mini DataZoneStandard quota in the model region. Defaults are Sweden Central for the model
  and North Europe for the app; choose supported alternatives with flags.

## Preview and deploy

From the standalone starter directory:

```bash
az login
bash infra/deploy.sh --subscription YOUR_SUBSCRIPTION_ID --what-if
bash infra/deploy.sh --subscription YOUR_SUBSCRIPTION_ID
```

The deployment prints the target subscription and asks you to type `deploy` before creating resources.
For an explicitly approved automated run use `--yes`. `--name my-demo` creates a separate `rg-my-demo` stack.
If that group already exists but is not tagged as this starter, the script refuses to touch it.

The script provisions infrastructure, builds **only the clean allowlisted export** in your ACR, then deploys
the application image. The parent repository, `.env`, databases, session tokens and Git history are never sent
to the image build.

It generates a high-entropy access code, provisions it as a protected Container Apps secret, and stores
the same value in Key Vault and locally in
`.azure/alsense-demo/demo-access-code` with owner-only permissions. The code is not emitted in deployment
outputs or printed to the terminal. Read that local file to obtain it and paste it into the widget's
**Demo access code** field before starting a synthetic session. Only share the code with intended demo users.

Keep `.azure/` private and preserve it between runs: it contains the code used by your existing demo. It is
excluded from Git, Docker and clean exports. Different users should use a different `--name`; deploying to the
same named stack is an update, not a new environment.

Key Vault's public access stays disabled. The app does not need a public vault endpoint or private-link
networking: ARM provisions its secret from a secure parameter. After correcting an infrastructure failure,
`--image-tag <existing-tag>` can reuse the already built image rather than rebuild it.

## Resources

| Resource | Purpose |
|---|---|
| Resource group | Owns an isolated, tagged demo |
| Basic Container Registry | Private image repository; remote build; admin login disabled |
| Consumption Container Apps environment/app | HTTPS widget and backend, one replica |
| User-assigned managed identity | ACR pull, Foundry/model access and Key Vault secret retrieval |
| Foundry AI Services account/project | Backend model client |
| GPT-5-mini model deployment | DataZoneStandard, pinned version 2025-08-07, configurable capacity |
| Key Vault | Demo access code, RBAC and purge protection |
| Log Analytics + Application Insights | App/container logs and Python request telemetry |

The Foundry deployment uses its Azure default content-filter configuration; an explicit Content Safety
integration and APIM policies are not implemented. No Search, Cosmos DB or APIM is created merely as a placeholder.

Costs include the registry, container replica, model usage, monitoring and Key Vault operations. The logging
workspace has a daily ingestion cap, but it is **not a subscription spending cap**. Idle resources still cost
money. Inspect Cost Management and remove your demo when finished. Key Vault purge protection retains a deleted
vault for its retention interval; you cannot immediately reuse its name after deletion.

## Demo walkthrough

1. Open the URL printed by the deployment. Enter your access code and choose synthetic Alice.
2. Ask “Which sites can I access?” The model invokes the backend tool and sees only Demo North.
3. Ask “My sensor is offline. What should I check?” The model retrieves synthetic troubleshooting evidence.
4. Prepare an alarm-history report with a date window. Review the form; nothing is sent until you press Submit.
5. Prepare a ticket for `alarm-site-a`, providing the summary and steps tried. Submission remains synthetic.
6. Start a new session as Bob: his only site is Demo South. Asking for Alice's site cannot access it.

The form buttons use deterministic backend tools even in Foundry mode; ordinary messages use the real model.
Every screen labels the data as synthetic. There are no real emails, tickets or reports sent to Alsense.

## Limits and recovery

This is a protected **demo**, not corporate authentication: anyone holding the code can choose a synthetic
persona. Replace it with verified Entra/Alsense identity before handling real users or customer data.
Hosted SQLite is on replica-local disk; sessions/drafts disappear after replacement or redeployment.
Do not increase maxReplicas until a shared store is implemented.

The script stops on failures and leaves provisioned resources available for inspection; it does not silently
delete the stack or change region/model. Role assignments may take time to propagate. Inspect the deployment
operations and Container Apps logs, then rerun the same command with the same `.azure/` state.

For a lost access-code file, rotate the code intentionally rather than reading it into model context. For a
policy/quota/RBAC failure, fix permissions or choose supported explicit flags and preview again.

The CLI does not change your global default subscription. It uses `--subscription` on every Azure command.
