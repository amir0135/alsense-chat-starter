# Deploy your own synthetic demo

This deploys billable resources to **the subscription you explicitly pass**, never whichever subscription happens
to be the global CLI default. It does not deploy real Alsense connections or any confidential data.

## Requirements

- Azure CLI and Bicep: `az bicep install` if the compiler is missing.
- Python 3.12+; no local Docker daemon required (ACR builds the image remotely).
- Login: `az login`. For a particular directory use `az login --tenant YOUR_TENANT_ID`.
- Resource deployment and scoped RBAC permissions: Owner, or Contributor plus Role Based Access Control
  Administrator/User Access Administrator at the deployment scope.
- Data Zone Standard quota for **gpt-6.1-terra** (version 2026-10-26) in the model region, at least the capacity
  you deploy: 250K tokens per minute by default. Some subscription types must request quota for this model family
  before they can deploy it. Defaults are Sweden Central for the model and North Europe for the app; choose
  supported alternatives with flags.

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
| GPT-6.1 Terra model deployment | DataZoneStandard, pinned version 2026-10-26, 250K tokens per minute by default (`--model-capacity`) |
| Key Vault | Demo access code, RBAC and purge protection |
| Log Analytics + Application Insights | App/container logs and Python request telemetry |

The Foundry deployment uses its Azure default content-filter configuration; an explicit Content Safety
integration and APIM policies are not implemented. No Search, Cosmos DB or APIM is created merely as a placeholder.

Costs include the registry, container replica, model usage (input and output tokens, reasoning included),
monitoring and Key Vault operations. `dev.py usage` estimates the model cost per user once you pass the model
prices ([Model prices](#model-prices)). The logging
workspace has a daily ingestion cap, but it is **not a subscription spending cap**. Idle resources still cost
money. Inspect Cost Management and remove your demo when finished. Key Vault purge protection retains a deleted
vault for its retention interval; you cannot immediately reuse its name after deletion.

## Model and capacity

The assistant uses **gpt-6.1-terra** at reasoning effort `high`, with up to 8,000 output tokens per model call.
`MODEL_REASONING_EFFORT` and `MODEL_MAX_OUTPUT_TOKENS` change them; the hosted app uses these defaults until you
add the variables to the container app. Choose another model with `--model` and `--model-version`: it must be a
reasoning model with the Responses API and function calling.

Size the deployment's capacity in tokens per minute (TPM). Azure reserves each call's prompt plus its maximum
output tokens when the call starts, so:

> TPM ≥ questions per minute × model calls per question × (prompt tokens + `MODEL_MAX_OUTPUT_TOKENS`)

Assuming about three model calls per question with prompts of about 5,000 tokens, a question reserves about
3 × (5,000 + 8,000) ≈ 40,000 tokens, so the default capacity of 250 (250K TPM) allows about six questions a minute.
Capacity costs nothing by itself on pay-as-you-go, where you pay per token, but it needs that much quota. Check the
real numbers with `dev.py usage` and the deployment's metrics, then adjust `--model-capacity`. Redeploying an
existing stack with another model adds that deployment and points the app at it; the previous one stays until you
delete it.

**Why this model (October 2026).** gpt-6.1-terra was chosen for strong reasoning at a moderate price. In
independent tests it scores about 94% of the next larger model, GPT-6.1 Sol (51.0 against 54.5 on the Artificial
Analysis intelligence index, read 28 October 2026), at half the per-token price, and it gives the fewest wrong
answers in its family. Effort `high` rather than `max`, because maximum effort is much slower and not reliably
better. The model was released on 26 October 2026 and independent results are still few: run your own evaluation
cases (`dev.py evals --mode model`) and `dev.py measure` before relying on it. If it falls short, GPT-6.1 Sol is
the next step up, at twice the price per token.

## Model prices

The app estimates the model cost per user once it has the model's prices; none are shipped. Read them from the
Azure OpenAI pricing page for the model, Data Zone Standard and the model region, in US dollars per million tokens,
and pass all three:

```bash
bash infra/deploy.sh --subscription YOUR_SUBSCRIPTION_ID \
  --price-input INPUT --price-cached-input CACHED --price-output OUTPUT
```

The script checks them before it deploys anything and sets them as the container app's `MODEL_PRICE_INPUT`,
`MODEL_PRICE_CACHED_INPUT` and `MODEL_PRICE_OUTPUT`. `/system` then shows the rates in its Estimated cost row.
Every deployment sets these variables from its flags, so pass the prices again when you redeploy; add
`--image-tag <existing-tag>` to change only the prices. Without the flags the cost shows as not configured.
[HANDOVER](HANDOVER.md#estimating-model-cost) explains the estimate.

## Demo walkthrough

1. Open the URL printed by the deployment. Enter your access code and choose synthetic Alice.
2. Ask “Which sites can I access?” The model invokes the backend tool and sees only Demo North.
3. Ask “My sensor is offline. What should I check?” The model retrieves synthetic troubleshooting evidence.
4. Prepare an alarm-history report with a date window. Review the draft; nothing is sent until you press Confirm.
5. Prepare a ticket for `alarm-site-a`, providing the summary and steps tried. Submission remains synthetic.
6. Start a new session as Bob: his only site is Demo South. Asking for Alice's site cannot access it.
7. Open **Demo scenarios** for scripted walkthroughs, including failure cases, and `/system` to see which
   capabilities are synthetic. Scenarios started from an existing session don't need the access code again.

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
