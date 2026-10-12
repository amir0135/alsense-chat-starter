# Architecture: backend-owned tools

The key token-flow choice is to execute every tool in the backend. Foundry receives tool schemas and selected
results, never the user's credential. It does not connect directly to a hosted MCP server or an Alsense API.

```mermaid
flowchart LR
    U[Internal user] --> W[Embedded chat widget]
    W -->|backend access token| B[Chat backend<br/>auth + sessions + conversation]
    B <--> G[APIM / AI gateway<br/>future integration]
    G <--> F[Foundry Responses model<br/>optional]
    F -->|function request only| B
    B --> T[Curated backend tools]
    T --> P[AlsenseSource adapter]
    P --> M[Synthetic API<br/>implemented]
    P -. delegated token / OBO .-> A[Real Alsense APIs or MCP<br/>future]
    T -. service identity .-> K[Azure AI Search<br/>future]
    W -->|explicit Submit| C[Confirmation endpoint]
    C --> P
    B --> D[(SQLite<br/>local persistence)]
    B -. future .-> O[App Insights + evaluations]
```

## Local implementation

The widget receives an opaque demo token; the backend resolves its hash to an expiring session. The mock
`users_me` is called once at session creation. The session stores only synthetic user identity and allowed sites,
never the raw token. All authenticated endpoints enforce the backend feature flag and internal-user gate.
The widget can request permitted report types and start a fresh conversation under the same authenticated
synthetic identity; both endpoints require the existing bearer session.

Tool arguments are validated against typed schemas. Sites are filtered by both tenant and allowed-site list.
Support drafts also validate the alarm/site pair. Prepared actions belong to the session that created them.
The model cannot submit an action: submission is a different HTTP endpoint requiring explicit confirmation and
repeat authorization. Duplicate confirmations reuse the stored result.

The default runner is deterministic. Optional Foundry mode uses the Responses API with stateless model calls
(`store=false`) and a bounded local history. The backend retains the conversation, runs function calls, and
validates the final structured reply. It makes one repair attempt and then abstains.

SQLite supports one local instance and survives process restarts. It is not a shared cloud session store. A real
distributed deployment needs a store with tenant/user ownership, expiry, concurrency control, and idempotent
submission supported by the downstream API.

## Target production boundaries

The synthetic demo Bicep creates Foundry, Container Apps, Key Vault, identity and monitoring.
APIM, Search, explicit Content Safety and shared cloud storage remain integration points. Put the selected
gateway on the **backend-to-model path** without assuming it can
validate a user identity token that is deliberately not forwarded to the model. Validate user auth at the
backend's authenticated ingress or in the backend. Use a separate service identity for Foundry.

For delegated API access, validate the incoming audience and exchange the token via OBO when the downstream
audience differs. Keep the delegated credential in a request-local adapter context. API authorization remains
mandatory even after backend site filtering. Knowledge retrieval must enforce document permissions before
returning excerpts, and retrieved text must be treated as untrusted data.

Ticket creation must use the existing approved workflow only after user confirmation. A support mailbox is
not a destination the model is allowed to choose. Telemetry must not capture raw authorization headers, prompts
or results by default; the local fallback backlog stores categories.
